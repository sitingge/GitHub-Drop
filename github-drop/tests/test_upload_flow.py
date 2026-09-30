#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Drop 自测脚本
====================

不碰真实 GitHub：起一个假的 GitHub API 服务器，验证

1. 目录模板渲染 + 文件夹结构保留（build_repo_path）
2. Contents API 上传：路径、base64 内容、分支、提交信息是否正确
3. 409 冲突自动重试
4. autoRename 同名自动改名
5. 大文件走 Git Data API：blob -> tree(base_tree) -> commit -> ref
6. /api/state、/api/test 等接口

运行:
    python tests/test_upload_flow.py
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module():
    spec = importlib.util.spec_from_file_location("github_drop", os.path.join(ROOT, "github_drop.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


github_drop = load_module()

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


# --------------------------------------------------------------------------- #
# 假的 GitHub API
# --------------------------------------------------------------------------- #
class MockState:
    def __init__(self):
        self.files = {}                 # path -> {"content": bytes, "sha": str}
        self.puts = []                  # (path, content, sha_in_body, branch, message)
        self.deletes = []               # (path, sha, branch, message)
        self.blobs = []
        self.blob_index = {}            # blob sha -> content（让 tree 改动能在本地回放）
        self.trees = []
        self.commits = []
        self.ref_patches = []
        self.ref = "base-parent-commit"
        self.branches = ["main", "dev", "release/1.0"]
        # 仓库的默认分支可以是任意名字（真实例子里就有叫 save 的），这里必须可配
        self.default_branch = "main"
        self.truncated = False          # 让 /git/trees 假装被截断
        # 设成非 200 可以模拟「读不到仓库信息」（网络断了 / 私有仓库没 Token）
        self.repo_info_status = 200
        # None | "cut" | "error"：模拟「整棵树一次拉不下来」（真实网络里是读到一半被断开）
        self.recursive_mode = None
        # 目录列表（模拟仓库里已有的目录结构，给 /api/tree 用）
        self.tree = {
            "": [{"name": "myproj", "path": "myproj", "type": "dir"},
                 {"name": "pics", "path": "pics", "type": "dir"},
                 {"name": "readme.txt", "path": "readme.txt", "type": "file", "size": 5}],
            "pics": [{"name": "ai", "path": "pics/ai", "type": "dir"},
                    {"name": "index.html", "path": "pics/index.html", "type": "file", "size": 12}],
            "pics/ai": [{"name": "model.bin", "path": "pics/ai/model.bin", "type": "file", "size": 2048}],
            "myproj": [{"name": "img", "path": "myproj/img", "type": "dir"},
                       {"name": "index.html", "path": "myproj/index.html", "type": "file", "size": 6}],
        }
        self.fail_next_put_409 = False
        self.calls = []
        self.queries = []               # (path, query) 便于断言 ref=xxx 这类参数
        self.auth = []                  # 每次 GET 带的 Authorization 头（匿名应为 None）
        self.lock = threading.Lock()

    def all_files(self):
        """当前仓库快照：path -> 字节数。目录是推导出来的，不在文件表里。"""
        return {p: len(v["content"]) for p, v in self.files.items()}

    def git_tree_entries(self, base="", recursive=True):
        """拼一份和 GitHub /git/trees 一样的响应：path 相对 base，非递归只给直接子项。"""
        base = (base or "").strip("/")
        head = (base + "/") if base else ""
        files = {p: s for p, s in self.all_files().items() if not head or p.startswith(head)}

        entries, seen_dirs = [], {}

        def add_dir(rel):
            if rel not in seen_dirs:
                seen_dirs[rel] = True
                entries.append({"path": rel, "mode": "040000", "type": "tree",
                                "sha": "dir:" + (head + rel)})

        for path in sorted(files):
            rel = path[len(head):] if head else path
            if recursive:
                parts = rel.split("/")
                for i in range(1, len(parts)):
                    add_dir("/".join(parts[:i]))
                entries.append({"path": rel, "mode": "100644", "type": "blob",
                                "size": files[path], "sha": "blob:" + path})
            else:
                first = rel.split("/")[0]
                if "/" in rel:
                    add_dir(first)
                else:
                    entries.append({"path": first, "mode": "100644", "type": "blob",
                                    "size": files[path], "sha": "blob:" + path})
        return entries

    def apply_tree(self, entries):
        """把一次 tree 提交在本地回放：sha=None 视为删除，其余按 sha 找回内容。

        先给当前文件表拍个快照——改名是「先删旧名再写新名」，
        写新名时要能从快照里取到旧内容，否则一趟遍历下来内容就丢了。
        """
        snapshot = dict(self.files)
        for e in entries or []:
            path = e.get("path") or ""
            if not path:
                continue
            if e.get("sha") is None:
                self.files.pop(path, None)
                head = path + "/"
                for key in [k for k in self.files if k.startswith(head)]:
                    del self.files[key]
                continue
            src = e["sha"]
            if isinstance(src, str) and src.startswith("blob:"):
                origin = src[len("blob:"):]
                if origin in snapshot:
                    self.files[path] = dict(snapshot[origin])
            elif src in self.blob_index:
                content = self.blob_index[src]
                self.files[path] = {"content": content, "sha": sha1(content)}
            else:
                # 内容寻址：Contents API 给的就是文件内容 sha，按 sha 反查即可
                for origin, item in snapshot.items():
                    if item.get("sha") == src:
                        self.files[path] = dict(item)
                        break


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def repo_seed() -> dict:
    """一份「仓库里已经有东西」的快照，给文件管理用例用。"""
    raw = {
        "readme.txt": b"hello",
        "pics/index.html": b"<html>pics</html>",
        "pics/ai/model.bin": os.urandom(2048),
        "myproj/index.html": b"<html>",
    }
    return {p: {"content": c, "sha": sha1(c)} for p, c in raw.items()}


class MockHandler(BaseHTTPRequestHandler):
    server_version = "MockGitHub"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # 静音
        pass

    # ---------- 基础 ----------
    @property
    def m(self) -> MockState:
        return self.server.mock       # type: ignore[attr-defined]

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_cut(self, obj):
        """故意把响应体截短（Content-Length 照写完整长度）：
        复现真实网络里「大响应读到一半被断开」的故障。"""
        data = json.dumps(obj).encode("utf-8")
        cut = max(1, len(data) * 2 // 3)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data[:cut])
        self.wfile.flush()
        self.close_connection = True

    def _parts(self):
        return urllib.parse.urlparse(self.path)

    # ---------- GET ----------
    def do_GET(self):
        parsed = self._parts()
        path = parsed.path
        self.m.calls.append(("GET", path))
        self.m.queries.append((path, parsed.query))
        self.m.auth.append(self.headers.get("Authorization"))

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/branches/(.+)$", path)
        if mo:
            name = urllib.parse.unquote(mo.group(3))
            if name in self.m.branches:
                self._json({"name": name, "commit": {"sha": self.m.ref}})
            else:
                self._json({"message": "Branch not found"}, 404)
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/branches$", path)
        if mo:
            self._json([{"name": n} for n in self.m.branches])
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/contents(?:/(.*))?$", path)
        if mo:
            repo_path = urllib.parse.unquote(mo.group(3) or "")
            with self.m.lock:
                listing = self.m.tree.get(repo_path)
                item = self.m.files.get(repo_path)
            if listing is not None:
                self._json(listing)
            elif item:
                self._json({"path": repo_path, "sha": item["sha"], "size": len(item["content"])})
            else:
                self._json({"message": "Not Found"}, 404)
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/ref/heads/(.+)$", path)
        if mo:
            self._json({"ref": "refs/heads/" + mo.group(3), "object": {"sha": self.m.ref}})
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/commits/(.+)$", path)
        if mo:
            self._json({"sha": mo.group(3), "tree": {"sha": "base-tree-sha"}})
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/trees/([^/]+)$", path)
        if mo:
            sha = urllib.parse.unquote(mo.group(3))
            recursive = urllib.parse.parse_qs(parsed.query).get("recursive") == ["1"]
            if recursive and self.m.recursive_mode == "cut":
                # 复现真实故障：Content-Length 写全，但只发一半就断开
                self._json_cut({"sha": sha, "tree": self.m.git_tree_entries("", True),
                                "truncated": False})
                return
            if recursive and self.m.recursive_mode == "error":
                self._json({"message": "Server Error"}, 500)
                return
            if sha.startswith("dir:"):
                entries = self.m.git_tree_entries(sha[4:], recursive)
            elif sha == "base-tree-sha":
                entries = self.m.git_tree_entries("", recursive)
            else:
                entries = []
            self._json({"sha": sha, "tree": entries, "truncated": self.m.truncated})
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)$", path)
        if mo:
            if self.m.repo_info_status != 200:
                self._json({"message": "Server Error"}, self.m.repo_info_status)
                return
            self._json({"full_name": "%s/%s" % (mo.group(1), mo.group(2)), "private": True,
                        "default_branch": self.m.default_branch,
                        "permissions": {"push": True}})
            return

        self._json({"message": "Not Found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        parsed = self._parts()
        path = parsed.path
        self.m.calls.append(("POST", path))

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/blobs$", path)
        if mo:
            body = json.loads(self._body())
            content = base64.b64decode(body["content"]) if body.get("content") else b""
            sha = "blob-" + sha1(content)
            with self.m.lock:
                self.m.blobs.append({"content": content, "encoding": body.get("encoding"), "sha": sha})
                self.m.blob_index[sha] = content
            self._json({"sha": sha})
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/trees$", path)
        if mo:
            body = json.loads(self._body())
            with self.m.lock:
                self.m.trees.append(body)
                self.m.apply_tree(body.get("tree"))
            self._json({"sha": "new-tree-sha"})
            return

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/commits$", path)
        if mo:
            body = json.loads(self._body())
            with self.m.lock:
                self.m.commits.append(body)
            self._json({"sha": "new-commit-sha"})
            return

        self._json({"message": "Not Found"}, 404)

    # ---------- PUT ----------
    def do_PUT(self):
        parsed = self._parts()
        path = parsed.path
        self.m.calls.append(("PUT", path))

        mo = re.match(r"^/repos/([^/]+)/([^/]+)/contents/(.+)$", path)
        if not mo:
            self._json({"message": "Not Found"}, 404)
            return

        repo_path = urllib.parse.unquote(mo.group(3))
        body = json.loads(self._body())
        content = base64.b64decode(body["content"]) if body.get("content") else b""
        sha_in_body = body.get("sha")

        with self.m.lock:
            self.m.puts.append((repo_path, content, sha_in_body, body.get("branch"), body.get("message")))
            if self.m.fail_next_put_409:
                self.m.fail_next_put_409 = False
                self._json({"message": "sha does not match"}, 409)
                return

            existing = self.m.files.get(repo_path)
            if existing and sha_in_body != existing["sha"]:
                self._json({"message": "sha does not match existing file"}, 409)
                return

            sha = sha1(content)
            self.m.files[repo_path] = {"content": content, "sha": sha}
        self._json({"content": {"path": repo_path, "sha": sha},
                    "commit": {"sha": "commit-" + sha[:8]}})

    # ---------- PATCH ----------
    def do_PATCH(self):
        parsed = self._parts()
        path = parsed.path
        self.m.calls.append(("PATCH", path))
        mo = re.match(r"^/repos/([^/]+)/([^/]+)/git/refs/heads/(.+)$", path)
        if not mo:
            self._json({"message": "Not Found"}, 404)
            return
        body = json.loads(self._body())
        with self.m.lock:
            self.m.ref_patches.append(body)
            self.m.ref = body.get("sha", self.m.ref)
        self._json({"object": {"sha": self.m.ref}})

    # ---------- DELETE ----------
    def do_DELETE(self):
        parsed = self._parts()
        path = parsed.path
        self.m.calls.append(("DELETE", path))
        mo = re.match(r"^/repos/([^/]+)/([^/]+)/contents/(.+)$", path)
        if not mo:
            self._json({"message": "Not Found"}, 404)
            return
        repo_path = urllib.parse.unquote(mo.group(3))
        body = json.loads(self._body() or b"{}")
        with self.m.lock:
            self.m.deletes.append((repo_path, body.get("sha"), body.get("branch"), body.get("message")))
            item = self.m.files.get(repo_path)
            if item is None:
                if repo_path in self.m.tree:
                    self._json({"message": "is a directory"}, 422)
                else:
                    self._json({"message": "Not Found"}, 404)
                return
            if body.get("sha") != item["sha"]:
                self._json({"message": "sha does not match"}, 409)
                return
            del self.m.files[repo_path]
        self._json({"commit": {"sha": "delete-commit"}})


def start_mock(seed_files=None, default_branch="main", branches=None):
    mock = MockState()
    if seed_files:
        mock.files.update(seed_files)
    mock.default_branch = default_branch
    if branches is not None:
        mock.branches = list(branches)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    httpd.daemon_threads = True
    httpd.mock = mock        # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, mock, "http://127.0.0.1:%d" % httpd.server_address[1]


# --------------------------------------------------------------------------- #
# 启动被测服务
# --------------------------------------------------------------------------- #
def start_app(api_base, tmpdir, extra=None):
    cfg_path = os.path.join(tmpdir, "config.json")
    cfg = github_drop.default_config()
    cfg.update({
        "owner": "tester",
        "repo": "assets",
        "branch": "main",
        "token": "mock-token",
        "targetDir": "uploads/{date}",
        "apiBase": api_base,
        "commitMessage": "chore(upload): add {name}",
    })
    cfg.update(extra or {})
    github_drop.save_config(cfg, cfg_path)
    httpd, state = github_drop.create_server("127.0.0.1", 0, cfg_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, state, "http://127.0.0.1:%d" % httpd.server_address[1]


def upload(base, name, content, rel=None, target=None, keep=None, token="mock-token"):
    req = urllib.request.Request(base + "/api/upload", data=content, method="POST")
    req.add_header("X-File-Name", urllib.parse.quote(name))
    if rel:
        req.add_header("X-Rel-Path", urllib.parse.quote(rel))
    if target is not None:
        req.add_header("X-Target-Dir", urllib.parse.quote(target))
    if keep is not None:
        req.add_header("X-Keep-Rel", "1" if keep else "0")
    if token:
        req.add_header("X-GitHub-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8")), exc.code


def api_get(base, path, token="mock-token"):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("X-GitHub-Token", token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def manage(base, action, token="mock-token", **kw):
    """调一次 /api/manage，返回 (json, http_status)。"""
    body = json.dumps(dict(action=action, **kw)).encode("utf-8")
    req = urllib.request.Request(base + "/api/manage", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-GitHub-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8")), exc.code


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_path_rules():
    print("\n[1] 路径与目录模板规则")
    now = datetime(2026, 9, 30, 21, 47, 41)
    cfg = github_drop.default_config()

    p = github_drop.build_repo_path(cfg, "a.txt", "", now=now)
    check("默认模板 uploads/{date}", p == "uploads/2026-09-30/a.txt", p)

    p = github_drop.build_repo_path(cfg, "a.txt", "myproj/sub/a.txt", now=now)
    check("文件夹结构保留(含顶层目录名)", p == "uploads/2026-09-30/myproj/sub/a.txt", p)

    cfg2 = dict(cfg, targetDir="{yyyy}/{mm}/{dd}/{timestamp}")
    p = github_drop.build_repo_path(cfg2, "a.txt", "", now=now)
    check("多段模板", p == "2026/09/30/%d/a.txt" % int(now.timestamp()), p)

    cfg3 = dict(cfg, keepFolder=False)
    p = github_drop.build_repo_path(cfg3, "a.txt", "myproj/sub/a.txt", now=now)
    check("关闭保留结构后平铺", p == "uploads/2026-09-30/a.txt", p)

    p = github_drop.build_repo_path(cfg, "../../evil#1?.txt", "", now=now)
    check("路径穿越与特殊字符被清洗",
          ".." not in p and "#" not in p and "?" not in p and p == "uploads/2026-09-30/evil1.txt", p)

    p = github_drop.build_repo_path(cfg, "..", "", now=now)
    check("空/非法文件名有兜底", p.startswith("uploads/2026-09-30/file-"), p)

    p = github_drop.build_repo_path(cfg, "a.txt", "../../outside/a.txt", now=now)
    check("相对路径里的 .. 被剥离", p == "uploads/2026-09-30/outside/a.txt", p)

    p = github_drop.build_repo_path(cfg, ".gitkeep", "myproj/empty/.gitkeep", now=now)
    check("点开头文件不被误伤", p == "uploads/2026-09-30/myproj/empty/.gitkeep", p)

    cfg4 = dict(cfg, sanitizeNames=True)
    p = github_drop.build_repo_path(cfg4, "my report.pdf", "", now=now)
    check("空格转连字符", p.endswith("my-report.pdf"), p)

    p = github_drop.build_repo_path(cfg, "a.txt", "照片 2026/相册/a.txt", now=now)
    check("中文目录名保留", p == "uploads/2026-09-30/照片 2026/相册/a.txt", p)


def test_contents_api():
    print("\n[2] Contents API 上传")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp)
            today = datetime.now().strftime("%Y-%m-%d")
            payload = b"hello github drop\n"
            res, code = upload(base, "hello.txt", payload)
            check("HTTP 200", code == 200, str(code))
            check("返回 ok", res.get("ok") is True, json.dumps(res, ensure_ascii=False)[:200])
            check("使用 Contents API", res.get("mode") == "contents-api", str(res.get("mode")))
            check("仓库路径正确", res.get("path") == "uploads/%s/hello.txt" % today, str(res.get("path")))

            path, content, sha, branch, message = mock.puts[-1]
            check("远端收到原始字节", content == payload, repr(content))
            check("远端分支正确", branch == "main", str(branch))
            check("提交信息用了 {name}", message == "chore(upload): add hello.txt", str(message))
            check("首次上传不带 sha", sha is None, str(sha))
            check("直链格式", res["urls"]["raw"].endswith("/uploads/%s/hello.txt" % today), res["urls"]["raw"])
            check("CDN 链接", res["urls"]["cdn"].startswith("https://cdn.jsdelivr.net/gh/tester/assets@main/"), res["urls"]["cdn"])

            # 覆盖同名文件：应带上 sha
            res2, _ = upload(base, "hello.txt", b"v2")
            check("二次上传带上 sha（覆盖）", mock.puts[-1][2] is not None, str(mock.puts[-1][2]))
            check("内容已更新", mock.files["uploads/%s/hello.txt" % today]["content"] == b"v2", "")
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_folder_upload():
    print("\n[3] 文件夹上传（保留目录结构）")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"targetDir": "assets/{yyyy}{mm}{dd}"})
            today = datetime.now().strftime("%Y%m%d")

            files = [
                ("index.html", "myproj/index.html", b"<html>"),
                ("logo.png", "myproj/img/logo.png", b"\x89PNG\x00fake"),
                ("readme.txt", "myproj/docs/readme.txt", b"doc"),
                (".gitkeep", "myproj/empty/.gitkeep", b""),
            ]
            for name, rel, data in files:
                res, code = upload(base, name, data, rel=rel)
                check("上传 %s" % rel, code == 200 and res.get("ok"), json.dumps(res, ensure_ascii=False)[:160])

            got = sorted(mock.files.keys())
            expected = sorted("assets/%s/%s" % (today, rel) for _, rel, _ in files)
            check("仓库内完整复刻文件夹结构", got == expected,
                  "实际=%s" % got)
            check("空目录 .gitkeep 落库", "assets/%s/myproj/empty/.gitkeep" % today in mock.files, "")
            check("二进制文件未被破坏",
                  mock.files["assets/%s/myproj/img/logo.png" % today]["content"] == b"\x89PNG\x00fake", "")

            # 关闭保留结构
            res, _ = upload(base, "flat.txt", b"flat", rel="myproj/sub/flat.txt", keep=False)
            check("keep=0 时平铺到目标目录", res.get("path") == "assets/%s/flat.txt" % today, str(res.get("path")))

            # 自定义目标目录（含子目录）
            res, _ = upload(base, "x.txt", b"x", target="release/v1/cdn")
            check("自定义目标目录", res.get("path") == "release/v1/cdn/x.txt", str(res.get("path")))
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_conflict_retry():
    print("\n[4] 409 冲突自动重试")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp)
            mock.fail_next_put_409 = True
            before = len(mock.puts)
            res, code = upload(base, "race.txt", b"race")
            check("重试后成功", code == 200 and res.get("ok"), json.dumps(res, ensure_ascii=False)[:200])
            check("发生了 2 次 PUT", len(mock.puts) - before == 2, str(len(mock.puts) - before))
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_autorename():
    print("\n[5] 同名自动重命名")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"autoRename": True})
            today = datetime.now().strftime("%Y-%m-%d")
            r1, _ = upload(base, "note.md", b"one")
            r2, _ = upload(base, "note.md", b"two")
            r3, _ = upload(base, "note.md", b"three")
            check("第一个用原名", r1["path"].endswith("note.md"), r1["path"])
            check("第二个变 -1", r2["path"].endswith("note-1.md"), r2["path"])
            check("第三个变 -2", r3["path"].endswith("note-2.md"), r3["path"])
            check("三份内容都在",
                  mock.files["uploads/%s/note.md" % today]["content"] == b"one"
                  and mock.files["uploads/%s/note-1.md" % today]["content"] == b"two"
                  and mock.files["uploads/%s/note-2.md" % today]["content"] == b"three", "")
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_git_data_api():
    print("\n[6] 大文件通道（Git Data API）")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"largeFileThresholdMB": 0})
            payload = os.urandom(64 * 1024)
            res, code = upload(base, "big.bin", payload, rel="dataset/2026/big.bin")
            check("HTTP 200", code == 200, json.dumps(res, ensure_ascii=False)[:200])
            check("走 git-data-api", res.get("mode") == "git-data-api", str(res.get("mode")))
            check("blob 内容一致", mock.blobs and mock.blobs[-1]["content"] == payload, "")
            check("blob 用 base64 编码", mock.blobs[-1]["encoding"] == "base64", str(mock.blobs[-1]["encoding"]))
            tree = mock.trees[-1]
            check("tree 基于 base_tree", tree.get("base_tree") == "base-tree-sha", str(tree.get("base_tree")))
            check("tree 里路径正确",
                  tree["tree"][0]["path"].endswith("dataset/2026/big.bin"), json.dumps(tree)[:200])
            check("commit 的父提交正确", mock.commits[-1]["parents"] == ["base-parent-commit"], "")
            check("ref 已推进到新 commit", mock.ref == "new-commit-sha", mock.ref)
            check("ref 未强制覆盖", mock.ref_patches[-1].get("force") is False, "")
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_http_surface():
    print("\n[7] 页面与配置接口")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp)

            with urllib.request.urlopen(base + "/", timeout=20) as resp:
                page = resp.read().decode("utf-8")
            check("首页返回 HTML", "<title>GitHub Drop" in page, "")
            check("页面含拖拽逻辑", "webkitGetAsEntry" in page and "X-Rel-Path" in page, "")
            check("页面含目录选择", "webkitdirectory" in page, "")
            check("页面含仓库地址输入框", 'id="c_repoUrl"' in page, "")
            check("页面含分支下拉", 'list="branchOptions"' in page, "")
            check("页面含地址解析逻辑", "parseRepoUrl" in page and "/api/branches" in page, "")
            check("页面含目录浏览器", 'id="treeList"' in page and 'id="treeCrumbs"' in page, "")
            check("页面含目录浏览逻辑", "loadTree" in page and "/api/tree" in page, "")
            check("页面含选目录按钮",
                  'id="btnTreeAsDefault"' in page and "pickDir" in page and 'id="btnBrowseDir"' in page, "")
            check("页面含上传目标下拉（从仓库文件夹里挑）",
                  'id="o_dirPick"' in page and "fillDirPicker" in page and "onDirPickChange" in page, "")
            check("页面含仓库文件管理区",
                  'id="btnFilesLoad"' in page and 'id="filesList"' in page
                  and 'id="btnClearRepo"' in page and 'id="btnMkdir"' in page
                  and 'id="btnDelDir"' in page, "")
            check("管理区支持进文件夹 + 全量扫描开关",
                  'id="fileCrumbs"' in page and 'id="fileDeep"' in page
                  and "fileEnterDir" in page and "loadFiles" in page, "")
            check("页面含管理接口调用", "/api/manage" in page and "/api/files" in page, "")
            check("危险操作有二次确认弹窗",
                  'id="modalOk"' in page and 'id="modalBody"' in page and "CLEAR" in page, "")
            check("清空仓库要求手打 CLEAR",
                  "confirm: 'CLEAR'" in page or '"CLEAR"' in page, "")
            check("删文件夹要求重打一遍路径",
                  "confirmPath" in page and "路径打得不一致" in page, "")

            st = api_get(base, "/api/state")
            check("/api/state 不回传 token", "token" not in st["config"], json.dumps(st["config"])[:160])
            check("/api/state 标记 tokenSet", st["tokenSet"] is True, "")
            check("/api/state 带版本号", st["version"] == github_drop.VERSION, "")

            t = api_get(base, "/api/test")
            check("/api/test 连接成功", t.get("ok") and t.get("fullName") == "tester/assets", json.dumps(t)[:160])

            body = json.dumps({"config": {"targetDir": "cdn/{yyyy}/{mm}", "autoRename": True}}).encode()
            req = urllib.request.Request(base + "/api/config", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=20) as resp:
                saved = json.loads(resp.read().decode("utf-8"))
            check("配置保存成功", saved.get("ok") and saved["config"]["targetDir"] == "cdn/{yyyy}/{mm}", "")
            check("配置落盘", json.load(open(state.config_path, encoding="utf-8"))["autoRename"] is True, "")

            res, code = upload(base, "n.txt", b"n", token="")
            check("配置里有 token 时，空请求头可回落到服务端 token", code == 200, str(code))

            # 上传历史
            res, _ = upload(base, "hist.txt", b"h")
            hist = api_get(base, "/api/history")
            check("历史记录已写入", any(h["path"].endswith("hist.txt") for h in hist["history"]), "")
            check("历史文件存在", os.path.isfile(state.history_path), state.history_path)

            # 清空 token 后应该明确报 401
            body = json.dumps({"clearToken": True}).encode()
            req = urllib.request.Request(base + "/api/config", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=20) as resp:
                cleared = json.loads(resp.read().decode("utf-8"))
            check("token 可被清除", cleared.get("tokenSet") is False, json.dumps(cleared, ensure_ascii=False)[:160])
            res, code = upload(base, "n2.txt", b"n2", token="")
            check("完全没有 token 时返回 401", code == 401, "%s %s" % (code, json.dumps(res, ensure_ascii=False)[:120]))
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_repo_url():
    print("\n[8] 仓库地址解析")
    cases = [
        ("https://github.com/octocat/hello-world", "octocat", "hello-world", None, "https://api.github.com"),
        ("https://github.com/octocat/hello-world.git", "octocat", "hello-world", None, "https://api.github.com"),
        ("https://github.com/octocat/hello-world/", "octocat", "hello-world", None, "https://api.github.com"),
        ("   https://github.com/octocat/hello-world   ", "octocat", "hello-world", None, "https://api.github.com"),
        ("<https://github.com/octocat/hello-world>", "octocat", "hello-world", None, "https://api.github.com"),
        ("github.com/octocat/hello-world", "octocat", "hello-world", None, "https://api.github.com"),
        ("octocat/hello-world", "octocat", "hello-world", None, "https://api.github.com"),
        ("git@github.com:octocat/hello-world.git", "octocat", "hello-world", None, "https://api.github.com"),
        ("ssh://git@github.com/octocat/hello-world.git", "octocat", "hello-world", None, "https://api.github.com"),
        ("git clone https://github.com/octocat/hello-world.git", "octocat", "hello-world", None, "https://api.github.com"),
        ("https://github.com/octocat/hello-world/tree/dev", "octocat", "hello-world", "dev", "https://api.github.com"),
        ("https://github.com/octocat/hello-world/tree/feature/x/src/app.py",
         "octocat", "hello-world", "feature/x/src/app.py", "https://api.github.com"),
        ("https://github.com/octocat/hello-world/blob/main/README.md",
         "octocat", "hello-world", "main", "https://api.github.com"),
        ("https://github.com/octocat/hello-world/issues/12", "octocat", "hello-world", None, "https://api.github.com"),
        ("https://github.com/octocat/hello-world/stargazers", "octocat", "hello-world", None, "https://api.github.com"),
        ("octocat/hello-world/tree/dev", "octocat", "hello-world", "dev", "https://api.github.com"),
        ("https://ghe.example.com/team/asset-repo", "team", "asset-repo", None, "https://ghe.example.com/api/v3"),
        ("https://github.com/中文组织/素材库", "中文组织", "素材库", None, "https://api.github.com"),
    ]
    for raw, owner, repo, branch, api_base in cases:
        got = github_drop.parse_repo_url(raw)
        ok = bool(got) and got["owner"] == owner and got["repo"] == repo \
            and got["branch"] == branch and got["apiBase"] == api_base
        check("解析 %s" % raw.strip()[:46], ok, str(got))

    for bad in ["", "   ", "https://github.com", "https://github.com/octocat",
                "nothing here", "https://github.com/settings/profile",
                "https://github.com/orgs/myorg/repositories",
                "https://github.com/apps/some-app"]:
        got = github_drop.parse_repo_url(bad)
        check("拒绝非仓库地址 %r" % bad[:40], got is None, str(got))

    print("  -- 分支候选与最长前缀匹配 --")
    got = github_drop.parse_repo_url("https://github.com/octocat/hello-world/tree/feature/x/src/app.py")
    check("歧义链接的候选按最长优先",
          got["branchCandidates"] == ["feature/x/src/app.py", "feature/x/src", "feature/x", "feature"],
          str(got["branchCandidates"]))
    got = github_drop.parse_repo_url("https://github.com/octocat/hello-world/tree/dev")
    check("无歧义链接只有一个候选", got["branchCandidates"] == ["dev"], str(got["branchCandidates"]))
    got = github_drop.parse_repo_url("https://github.com/octocat/hello-world/blob/main/README.md")
    check("blob 链接把文件前的部分当分支", got["branchCandidates"] == ["main"], str(got["branchCandidates"]))
    got = github_drop.parse_repo_url("https://github.com/octocat/hello-world/commits/release/1.0")
    check("commits 链接整段当分支", got["branchCandidates"] == ["release/1.0"], str(got["branchCandidates"]))
    got = github_drop.parse_repo_url("https://github.com/octocat/hello-world")
    check("纯仓库地址没有分支候选", got["branchCandidates"] == [], str(got["branchCandidates"]))

    names = ["main", "dev", "feature/x", "release/1.0"]
    check("最长前缀匹配到 feature/x",
          github_drop.resolve_branch(["feature/x/src/app.py", "feature/x/src", "feature/x", "feature"],
                                     names) == "feature/x", "")
    check("候选顺序就是优先级",
          github_drop.resolve_branch(["dev", "main"], names) == "dev", "")
    check("匹配不上返回 None", github_drop.resolve_branch(["nope/a", "nope"], names) is None, "")
    check("空候选返回 None", github_drop.resolve_branch([], names) is None, "")
    check("分支列表为空返回 None", github_drop.resolve_branch(["main"], []) is None, "")

    print("  -- 链接里的目录（/tree/<分支>/<目录>）--")
    real = github_drop.parse_repo_url("https://github.com/octocat/my-assets/tree/main/pics")
    check("真机链接：owner/repo",
          real["owner"] == "octocat" and real["repo"] == "my-assets", str(real))
    check("真机链接：候选是 ['main/pics','main']",
          real["branchCandidates"] == ["main/pics", "main"], str(real["branchCandidates"]))
    check("真机链接：treePath 记下完整尾部", real["treePath"] == "main/pics", str(real.get("treePath")))
    check("纯 tree 链接 treePath 就是分支名",
          github_drop.parse_repo_url("https://github.com/o/r/tree/dev")["treePath"] == "dev", "")
    check("blob 链接没有 treePath",
          github_drop.parse_repo_url("https://github.com/o/r/blob/main/a.py")["treePath"] == "", "")
    check("普通仓库链接没有 treePath",
          github_drop.parse_repo_url("https://github.com/o/r")["treePath"] == "", "")

    branch, tree_path = github_drop.resolve_tree_path(["main/pics", "main"], ["main", "dev"])
    check("真机链接 -> 分支 main", branch == "main", str(branch))
    check("真机链接 -> 目录 pics", tree_path == "pics", repr(tree_path))

    branch, tree_path = github_drop.resolve_tree_path(["main", "main/x"], ["main"])
    check("分支本身在列表里时不留目录", branch == "main" and tree_path == "", "%s %r" % (branch, tree_path))

    branch, tree_path = github_drop.resolve_tree_path(
        ["feature/x/src", "feature/x", "feature"], ["feature/x"])
    check("带斜杠分支 -> 目录 src", branch == "feature/x" and tree_path == "src", "%s %r" % (branch, tree_path))

    branch, tree_path = github_drop.resolve_tree_path(["nope/nope"], ["main"])
    check("匹配不上时分支与目录都为空", branch is None and tree_path == "", "%s %r" % (branch, tree_path))

    branch, tree_path = github_drop.resolve_tree_path([], ["main"])
    check("没有候选时不猜目录", branch is None and tree_path == "", "%s %r" % (branch, tree_path))


def test_branches_api():
    print("\n[9] 分支列表 + 粘贴地址接口")
    httpd, mock, api = start_mock()
    app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"owner": "tester", "repo": "assets"})

            res = api_get(base, "/api/branches")
            check("/api/branches 返回成功", bool(res.get("ok")), json.dumps(res, ensure_ascii=False)[:200])
            check("默认分支排在第一", res["branches"][0] == "main", str(res["branches"]))
            check("分支列表完整",
                  {"main", "dev", "release/1.0"} <= set(res["branches"]), str(res["branches"]))
            check("带斜杠的分支名保留", "release/1.0" in res["branches"], str(res["branches"]))
            check("列表无重复", len(res["branches"]) == len(set(res["branches"])), str(res["branches"]))
            check("确实请求了 branches 接口",
                  any(p.endswith("/branches") for _, p in mock.calls), "")
            check("回传默认分支名", res.get("defaultBranch") == "main", str(res.get("defaultBranch")))

            mock.branches = ["dev", "main", "release/1.0"]
            res_dup = api_get(base, "/api/branches")
            check("默认分支提到首位且不重复",
                  res_dup["branches"] == ["main", "dev", "release/1.0"], str(res_dup["branches"]))

            res_ov = api_get(base, "/api/branches?owner=other&repo=repo2&apiBase=" + urllib.parse.quote(api, safe=""))
            check("query 可临时覆盖 owner/repo（还没保存也能拉）",
                  bool(res_ov.get("ok")), json.dumps(res_ov, ensure_ascii=False)[:200])
            check("没带候选时不返回 resolvedBranch", res_ov.get("resolvedBranch") is None,
                  str(res_ov.get("resolvedBranch")))

            q = "owner=tester&repo=assets&apiBase=" + urllib.parse.quote(api, safe="")
            for cand in ["release/1.0/app/x.py", "release/1.0/app", "release/1.0", "release"]:
                q += "&candidates=" + urllib.parse.quote(cand, safe="")
            res_rb = api_get(base, "/api/branches?" + q)
            check("带斜杠的分支能被最长前缀匹配出来",
                  res_rb.get("resolvedBranch") == "release/1.0", str(res_rb.get("resolvedBranch")))

            q2 = "owner=tester&repo=assets&candidates=" + urllib.parse.quote("nothing/at/all", safe="")
            res_rb2 = api_get(base, "/api/branches?" + q2)
            check("候选都匹配不上时 resolvedBranch 为空",
                  res_rb2.get("resolvedBranch") is None, str(res_rb2.get("resolvedBranch")))

            q3 = "owner=tester&repo=assets&candidates=" + urllib.parse.quote("main/pics", safe="") \
                + "&candidates=main"
            res_rb3 = api_get(base, "/api/branches?" + q3)
            check("像 octocat/…/tree/main/pics 这种链接能认出分支 main",
                  res_rb3.get("resolvedBranch") == "main", str(res_rb3.get("resolvedBranch")))
            check("同时把剩余部分当成目录 pics",
                  res_rb3.get("resolvedPath") == "pics", str(res_rb3.get("resolvedPath")))

            req = urllib.request.Request(base + "/api/test?owner=tester&repo=assets", data=b"", method="POST")
            req.add_header("X-GitHub-Token", "mock-token")
            with urllib.request.urlopen(req, timeout=20) as resp:
                t = json.loads(resp.read().decode("utf-8"))
            check("/api/test 也支持 query 覆盖",
                  bool(t.get("ok")) and t.get("defaultBranch") == "main",
                  json.dumps(t, ensure_ascii=False)[:200])

            def save(payload):
                body = json.dumps({"config": payload}).encode()
                req = urllib.request.Request(base + "/api/config", data=body, method="POST")
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        return json.loads(resp.read().decode("utf-8")), 200
                except urllib.error.HTTPError as exc:
                    return json.loads(exc.read().decode("utf-8")), exc.code

            res3, code = save({"repoUrl": "https://github.com/octocat/hello-world/tree/dev"})
            check("粘贴地址能保存", code == 200 and res3.get("ok"), json.dumps(res3, ensure_ascii=False)[:200])
            cfg = res3.get("config", {})
            check("owner 自动填入", cfg.get("owner") == "octocat", str(cfg.get("owner")))
            check("repo 自动填入", cfg.get("repo") == "hello-world", str(cfg.get("repo")))
            check("链接里的分支被识别", cfg.get("branch") == "dev", str(cfg.get("branch")))
            check("解析结果回传给页面", (res3.get("parsed") or {}).get("host") == "github.com", str(res3.get("parsed")))

            res4, code = save({"repoUrl": "git@github.com:me/my-assets.git"})
            check("SSH 地址也能识别",
                  code == 200 and res4["config"]["owner"] == "me" and res4["config"]["repo"] == "my-assets",
                  str(res4.get("config")))

            res5, code = save({"repoUrl": "https://ghe.example.com/team/asset-repo"})
            check("企业版地址自动换 apiBase",
                  code == 200 and res5["config"]["apiBase"] == "https://ghe.example.com/api/v3",
                  str(res5.get("config", {}).get("apiBase")))

            res6, code = save({"repoUrl": "这不是地址"})
            check("乱填地址被拒绝", code == 400 and not res6.get("ok"),
                  "%s %s" % (code, json.dumps(res6, ensure_ascii=False)[:120]))

            res7, code = save({"owner": "me2", "repo": "mine2", "branch": "main", "repoUrl": ""})
            check("不填地址、手填 owner/repo 仍然可用",
                  code == 200 and res7["config"]["owner"] == "me2", str(res7.get("config", {}).get("owner")))
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_tree_api():
    print("\n[10] 分支里的文件夹（目录浏览）")
    httpd, mock, api = start_mock()
    app = None
    empty_app = None
    anon_app = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"owner": "tester", "repo": "assets", "branch": "main"})

            res = api_get(base, "/api/tree")
            check("根目录加载成功", bool(res.get("ok")), json.dumps(res, ensure_ascii=False)[:200])
            names = [e["name"] for e in res["entries"]]
            check("仓库里的文件夹都列出来了", "pics" in names and "myproj" in names, str(names))
            check("文件夹排在文件前面",
                  [e["type"] for e in res["entries"]] == ["dir", "dir", "file"],
                  str([e["type"] for e in res["entries"]]))
            check("目录 size 为 0 / 文件带 size",
                  res["entries"][0]["size"] == 0 and res["entries"][-1]["size"] == 5,
                  str(res["entries"]))
            check("回传当前路径与分支", res["path"] == "" and res["branch"] == "main",
                  "%s / %s" % (res.get("path"), res.get("branch")))
            check("提示里有文件夹计数", "2 个文件夹" in (res.get("message") or ""), res.get("message"))

            res2 = api_get(base, "/api/tree?path=pics")
            check("能进子目录 pics",
                  res2["path"] == "pics" and [e["name"] for e in res2["entries"]] == ["ai", "index.html"],
                  json.dumps(res2.get("entries"), ensure_ascii=False)[:200])

            res3 = api_get(base, "/api/tree?path=pics/ai")
            check("两层目录也能进",
                  [e["name"] for e in res3["entries"]] == ["model.bin"], str(res3.get("entries")))

            res4 = api_get(base, "/api/tree?path=" + urllib.parse.quote("nope/nothere"))
            check("不存在的目录不报 500",
                  res4.get("ok") is True and res4["entries"] == [],
                  json.dumps(res4, ensure_ascii=False)[:160])
            check("不存在的目录有可读提示", bool(res4.get("message")), str(res4.get("message")))

            res5 = api_get(base, "/api/tree?path=" + urllib.parse.quote("../../etc"))
            check("路径穿越被清洗掉", res5.get("ok") is True and res5["path"] == "etc", str(res5.get("path")))

            mock.calls.clear()
            mock.queries.clear()
            api_get(base, "/api/tree?path=pics&branch=dev")
            check("分支参数传给了 GitHub",
                  any("/contents/pics" in p and "ref=dev" in q for p, q in mock.queries),
                  str(mock.queries))
            check("列目录用的是 Contents API",
                  any(p.endswith("/contents/pics") for p, _ in mock.queries), str(mock.queries))

            res6 = api_get(base, "/api/tree?owner=x&repo=y&apiBase=" + urllib.parse.quote(api, safe=""))
            check("query 覆盖 owner/repo 也能用", bool(res6.get("ok")),
                  json.dumps(res6, ensure_ascii=False)[:120])

            res7 = api_get(base, "/api/tree?owner=&repo=")
            check("query 传空值时回落到已保存的配置", res7.get("path") == "", str(res7.get("path")))

        # 没配置仓库时应该明确报 400，而不是抛异常
        with tempfile.TemporaryDirectory() as tmp2:
            empty_app, _st, base2 = start_app(api, tmp2, {"owner": "", "repo": ""})
            try:
                urllib.request.urlopen(base2 + "/api/tree", timeout=10)
                check("没配置仓库时 /api/tree 返回 400", False, "居然成功了")
            except urllib.error.HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                check("没配置仓库时 /api/tree 返回 400 + 中文提示",
                      exc.code == 400 and "owner" in body.get("error", ""), "%s %s" % (exc.code, body))

        # 不填 Token：公开仓库的读接口应该匿名可用，写接口仍然拒绝
        with tempfile.TemporaryDirectory() as tmp3:
            anon_app, _st3, base3 = start_app(api, tmp3, {"owner": "tester", "repo": "assets", "token": ""})
            mock.auth.clear()
            r_anon = api_get(base3, "/api/tree", token=None)
            check("没 Token 也能列目录（匿名 GET）", r_anon.get("ok") is True,
                  json.dumps(r_anon, ensure_ascii=False)[:120])
            check("匿名请求没有带 Authorization 头", all(a is None for a in mock.auth), str(mock.auth))
            mock.auth.clear()
            r_anon2 = api_get(base3, "/api/branches", token=None)
            check("没 Token 也能拉分支（匿名 GET）", r_anon2.get("ok") is True,
                  json.dumps(r_anon2, ensure_ascii=False)[:120])
            res_up, code = upload(base3, "anon.txt", b"x", token="")
            check("没 Token 上传仍然被拒（401）", code == 401, str(code))
    finally:
        for srv in (app, empty_app, anon_app):
            if srv:
                srv.shutdown(); srv.server_close()
        httpd.shutdown(); httpd.server_close()


def test_manage_api():
    print("\n[12] 仓库文件管理（列出 / 删除 / 重命名 / 新建 / 清空）")
    seed = repo_seed()
    httpd, mock, api = start_mock(seed)
    app = None
    anon_app = None
    saved_env = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GH_TOKEN")}
    for key in saved_env:
        os.environ.pop(key, None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"owner": "tester", "repo": "assets"})

            print("  -- 列出仓库文件：默认只列一层（快）--")
            shallow = api_get(base, "/api/files")
            check("/api/files 返回成功", bool(shallow.get("ok")),
                  json.dumps(shallow, ensure_ascii=False)[:200])
            check("默认不递归（deep=False）", shallow.get("deep") is False, str(shallow.get("deep")))
            check("浅列只给当前层的文件",
                  sorted(f["path"] for f in shallow["files"]) == ["readme.txt"],
                  str([f["path"] for f in shallow["files"]]))
            check("浅列给出直接子文件夹",
                  sorted(d["path"] for d in shallow["dirs"]) == ["myproj", "pics"],
                  str([d["path"] for d in shallow["dirs"]]))
            check("浅列的子文件夹不带数量（省得为这个再扫一遍）",
                  all(d.get("count") is None for d in shallow["dirs"]), str(shallow["dirs"]))
            check("浅列文件名与所在目录都给了",
                  shallow["files"][0]["name"] == "readme.txt" and shallow["files"][0]["dir"] == "", "")
            check("浅列也有可读提示", "根目录" in (shallow.get("message") or ""), str(shallow.get("message")))

            sub_shallow = api_get(base, "/api/files?path=pics")
            check("浅列可以指定子目录",
                  [f["path"] for f in sub_shallow["files"]] == ["pics/index.html"],
                  str([f["path"] for f in sub_shallow["files"]]))
            check("浅列子目录时给出下一层文件夹",
                  [d["path"] for d in sub_shallow["dirs"]] == ["pics/ai"],
                  str([d["path"] for d in sub_shallow["dirs"]]))

            print("  -- 递归列出仓库文件（deep=1）--")
            fl = api_get(base, "/api/files?deep=1")
            check("deep=1 时标记 deep", fl.get("deep") is True, str(fl.get("deep")))
            paths = [f["path"] for f in fl["files"]]
            check("递归列出了所有文件", sorted(paths) == sorted(seed.keys()), str(paths))
            check("每个文件都带 size",
                  all(isinstance(f["size"], int) and f["size"] > 0 for f in fl["files"]),
                  str(fl["files"][:1]))
            check("文件名与所在目录拆开给了前端",
                  next(f for f in fl["files"] if f["path"] == "pics/ai/model.bin")["name"] == "model.bin"
                  and next(f for f in fl["files"] if f["path"] == "pics/ai/model.bin")["dir"] == "pics/ai", "")
            check("目录去重后按路径排好",
                  [d["path"] for d in fl["dirs"]] == ["myproj", "pics", "pics/ai"],
                  str([d["path"] for d in fl["dirs"]]))
            check("目录统计了包含的文件数",
                  next(d for d in fl["dirs"] if d["path"] == "pics")["count"] == 2, str(fl["dirs"]))
            check("统计了总大小", fl["totalSize"] == sum(len(v["content"]) for v in seed.values()),
                  str(fl["totalSize"]))
            check("返回了当前 commit / tree", bool(fl.get("commit")) and bool(fl.get("tree")),
                  "%s %s" % (fl.get("commit"), fl.get("tree")))
            check("提示语可读", "4 个文件" in (fl.get("message") or ""), str(fl.get("message")))
            check("正常网络下走一次递归就够", fl.get("mode") == "recursive", str(fl.get("mode")))

            fl2 = api_get(base, "/api/files?branch=dev&deep=1")
            check("可以指定分支", fl2.get("branch") == "dev", str(fl2.get("branch")))

            fl3 = api_get(base, "/api/files?path=pics&deep=1")
            check("可以只看某个子目录",
                  sorted(f["path"] for f in fl3["files"]) == ["pics/ai/model.bin", "pics/index.html"],
                  str([f["path"] for f in fl3["files"]]))

            print("  -- 删除单个文件（Contents API DELETE）--")
            r, code = manage(base, "delete_file", path="readme.txt")
            check("删文件返回 200", code == 200 and r.get("ok"), json.dumps(r, ensure_ascii=False)[:160])
            check("走的是 Contents API", r.get("mode") == "contents-api", str(r.get("mode")))
            check("DELETE 请求参数正确",
                  bool(mock.deletes) and mock.deletes[-1][0] == "readme.txt"
                  and mock.deletes[-1][1] is not None and mock.deletes[-1][2] == "main"
                  and mock.deletes[-1][3].startswith("chore(delete)"), str(mock.deletes))
            check("仓库里真删掉了", "readme.txt" not in mock.files, str(sorted(mock.files)))

            r, code = manage(base, "delete_file", path="nope.txt")
            check("删不存在的文件报 404", code == 404 and not r.get("ok"),
                  "%s %s" % (code, r.get("error")))

            r, code = manage(base, "delete_file", path="")
            check("不给路径报 400", code == 400, "%s %s" % (code, r.get("error")))

            r, code = manage(base, "delete_file", path="../../secret.txt")
            check("管理接口也挡路径穿越",
                  ".." not in json.dumps(r, ensure_ascii=False)
                  and not any(k.startswith("..") for k in mock.files),
                  json.dumps(r, ensure_ascii=False)[:160])

            print("  -- 重命名文件（一次 commit 完成改名）--")
            r, code = manage(base, "rename", path="pics/index.html", newPath="pics/home.html", kind="file")
            check("重命名返回 200", code == 200 and r.get("ok"), json.dumps(r, ensure_ascii=False)[:160])
            check("走 git-data-api", r.get("mode") == "git-data-api", str(r.get("mode")))
            tr = mock.trees[-1]["tree"]
            check("同一棵树里既删旧名又加新名",
                  sorted(e["path"] for e in tr if e["sha"] is None) == ["pics/index.html"]
                  and sorted(e["path"] for e in tr if e["sha"] is not None) == ["pics/home.html"],
                  json.dumps(tr, ensure_ascii=False)[:260])
            check("删旧加新都挂在同一个 commit 上", len(tr) == 2, str(len(tr)))
            check("新名字在仓库里生效",
                  "pics/home.html" in mock.files and "pics/index.html" not in mock.files,
                  str(sorted(mock.files)))
            check("内容原样搬过去（不是重传新内容）",
                  mock.files["pics/home.html"]["content"] == seed["pics/index.html"]["content"], "")
            check("返回到父提交，没强推",
                  mock.commits[-1]["parents"] == ["base-parent-commit"]
                  and mock.ref_patches[-1].get("force") is False, str(mock.ref_patches[-1]))

            print("  -- 重命名文件夹（整棵子树一起搬）--")
            r, code = manage(base, "rename", path="pics/ai", newPath="pics/models", kind="dir")
            check("文件夹改名返回 200", code == 200 and r.get("ok"), json.dumps(r, ensure_ascii=False)[:160])
            tr = mock.trees[-1]["tree"]
            check("子目录下所有文件都被搬走",
                  sorted(e["path"] for e in tr if e["sha"] is None) == ["pics/ai/model.bin"]
                  and sorted(e["path"] for e in tr if e["sha"] is not None) == ["pics/models/model.bin"],
                  json.dumps(tr, ensure_ascii=False)[:260])
            check("旧目录已不存在于仓库",
                  "pics/models/model.bin" in mock.files and "pics/ai/model.bin" not in mock.files,
                  str(sorted(mock.files)))

            print("  -- 重命名的各种拒绝姿势 --")
            r, code = manage(base, "rename", path="pics/home.html", newPath="myproj/index.html", kind="file")
            check("目标已存在时拒绝（409）", code == 409 and not r.get("ok"),
                  "%s %s" % (code, r.get("error")))
            r, code = manage(base, "rename", path="pics", newPath="pics/pics", kind="dir")
            check("不能把文件夹塞进它自己里（400）", code == 400, "%s %s" % (code, r.get("error")))
            r, code = manage(base, "rename", path="pics/home.html", newPath="pics/home.html", kind="file")
            check("新旧名字一样报 400", code == 400, "%s %s" % (code, r.get("error")))
            r, code = manage(base, "rename", path="not/here.txt", newPath="x.txt", kind="file")
            check("源不存在报 404", code == 404, "%s %s" % (code, r.get("error")))

            print("  -- 新建文件夹 --")
            r, code = manage(base, "mkdir", path="assets/2026/秋")
            check("新建文件夹返回 200", code == 200 and r.get("ok"), json.dumps(r, ensure_ascii=False)[:160])
            check("空目录用 .gitkeep 占位",
                  "assets/2026/秋/.gitkeep" in mock.files, str(sorted(mock.files)))
            check(".gitkeep 是空文件（不是乱塞内容）",
                  mock.files["assets/2026/秋/.gitkeep"]["content"] == b"", "")
            check("中文目录名原样保留",
                  any(k.startswith("assets/2026/秋") for k in mock.files), str(sorted(mock.files)))

            r, code = manage(base, "mkdir", path="")
            check("新建文件夹不给路径报 400", code == 400, "%s %s" % (code, r.get("error")))

            print("  -- 删除整个文件夹 --")
            r, code = manage(base, "delete_dir", path="pics")
            check("删文件夹返回 200", code == 200 and r.get("ok"), json.dumps(r, ensure_ascii=False)[:200])
            check("走 git-data-api", r.get("mode") == "git-data-api", str(r.get("mode")))
            tr = mock.trees[-1]["tree"]
            check("目录下文件全部进了删除列表",
                  sorted(e["path"] for e in tr) == ["pics/home.html", "pics/models/model.bin"],
                  json.dumps(tr, ensure_ascii=False)[:260])
            check("全部标记为 sha=None（删除）", all(e["sha"] is None for e in tr), "")
            check("仓库里整个目录都没了",
                  not any(k.startswith("pics/") for k in mock.files), str(sorted(mock.files)))
            check("返回体里报告删掉的文件数", len(r.get("deleted") or []) == 2, str(r.get("deleted")))

            r, code = manage(base, "delete_dir", path="myproj/nothing")
            check("删空的/不存在的文件夹报 404", code == 404, "%s %s" % (code, r.get("error")))

            print("  -- 清空仓库（必须回填 CLEAR）--")
            r, code = manage(base, "clear")
            check("不带 confirm 的清空被拒（400）",
                  code == 400 and "CLEAR" in (r.get("error") or ""), "%s %s" % (code, r.get("error")))
            r, code = manage(base, "clear", confirm="nope")
            check("confirm 写错也被拒", code == 400, "%s %s" % (code, r.get("error")))

            r, code = manage(base, "clear", confirm="CLEAR")
            check("清空返回 200", code == 200 and r.get("ok"), json.dumps(r, ensure_ascii=False)[:200])
            tr = mock.trees[-1]["tree"]
            check("清空只针对顶层条目",
                  sorted(e["path"] for e in tr) == ["assets", "myproj"], json.dumps(tr, ensure_ascii=False)[:260])
            check("清空用一次 commit 完成", len(mock.commits) >= 1 and mock.ref == "new-commit-sha", mock.ref)
            check("仓库真的空了", mock.files == {}, str(sorted(mock.files)))
            check("返回体列出被清的顶层条目",
                  sorted(r.get("cleared") or []) == ["assets", "myproj"], str(r.get("cleared")))

            r, code = manage(base, "clear", confirm="CLEAR")
            check("已经空了再清会提示 400",
                  code == 400 and not r.get("ok"), "%s %s" % (code, r.get("error")))

            print("  -- 其它边界 --")
            r, code = manage(base, "drop_database")
            check("未知操作报 400", code == 400, "%s %s" % (code, r.get("error")))

            r, code = manage(base, "delete_file", path="anything.txt", branch="dev")
            check("可以指定分支操作（404 说明用的是 dev）", code == 404, str(code))

            hist = api_get(base, "/api/history")
            check("管理操作也写进历史",
                  any("delete_file" in (h.get("name") or "") for h in hist["history"]),
                  str([h.get("name") for h in hist["history"]][:5]))

        # 仓库没配置时应该明确报 400
        with tempfile.TemporaryDirectory() as tmp_bad:
            bad_app, _st, base_bad = start_app(api, tmp_bad, {"owner": "", "repo": ""})
            try:
                r, code = manage(base_bad, "delete_file", path="x.txt")
                check("没配置仓库时管理操作报 400", code == 400, "%s %s" % (code, r.get("error")))
            finally:
                bad_app.shutdown(); bad_app.server_close()

        # 没有 Token 时写操作必须被拦住
        with tempfile.TemporaryDirectory() as tmp_anon:
            anon_app, _st2, base_anon = start_app(api, tmp_anon,
                                                  {"owner": "tester", "repo": "assets", "token": ""})
            r, code = manage(base_anon, "delete_file", path="x.txt", token=None)
            check("没 Token 时管理操作报 401", code == 401, "%s %s" % (code, r.get("error")))
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for srv in (app, anon_app):
            if srv:
                srv.shutdown(); srv.server_close()
        httpd.shutdown(); httpd.server_close()


def test_files_walk_fallback():
    """整棵树一次拉不下来时（真实网络常见：读到一半被掐断），
    /api/files 必须能退回「逐目录扫」，各个管理动作也照样能跑。"""
    print("\n[13] 大仓库 / 断流兜底：改成逐目录扫描")
    seed = repo_seed()
    httpd, mock, api = start_mock(seed)
    app = None
    github_drop._RECURSIVE_COOLDOWN.clear()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"owner": "tester", "repo": "assets"})

            # ---- 正常情况：一次递归拿到，mode=recursive ----
            fl = api_get(base, "/api/files?deep=1")
            check("正常网络用一次递归 tree", fl.get("mode") == "recursive", str(fl.get("mode")))
            check("正常情况列出 4 个文件", len(fl["files"]) == 4, str(len(fl["files"])))
            check("正常情况不带截断提示", fl.get("truncated") is False, str(fl.get("truncated")))

            # ---- 断流：Content-Length 写全但只发一半 ----
            mock.recursive_mode = "cut"
            mock.queries.clear()
            # 浅列根本不用碰递归接口，被掐断也该秒回
            shallow_cut = api_get(base, "/api/files")
            check("断流时浅列照样正常（本来就不用大响应）",
                  shallow_cut.get("ok") is True and len(shallow_cut["files"]) == 1,
                  json.dumps(shallow_cut, ensure_ascii=False)[:160])

            mock.queries.clear()          # 下面只数「深度扫描」发了什么
            fl2 = api_get(base, "/api/files?deep=1")
            check("断流后仍然返回成功", fl2.get("ok") is True,
                  json.dumps(fl2, ensure_ascii=False)[:160])
            check("自动改用逐目录扫描", fl2.get("mode") == "walk", str(fl2.get("mode")))
            check("逐目录扫描也能列全 4 个文件",
                  sorted(f["path"] for f in fl2["files"]) == sorted(seed.keys()),
                  str(sorted(f["path"] for f in fl2["files"])))
            check("逐目录扫描的 size 也对",
                  next(f for f in fl2["files"] if f["path"] == "pics/ai/model.bin")["size"] == 2048, "")
            check("提示里说明了走的是逐目录扫描",
                  "逐目录" in (fl2.get("message") or ""), str(fl2.get("message")))
            tree_q = [(p, q) for p, q in mock.queries if "/git/trees/" in p]
            recursive_q = [q for _, q in tree_q if "recursive=1" in q]
            plain_p = [p for p, q in tree_q if q == ""]
            check("试一次递归就放弃（外面有兜底，不反复重试）",
                  len(recursive_q) == 1, str(recursive_q))
            check("放弃递归后，改成每个目录只发一个小请求（根 + 3 个子目录）",
                  len(plain_p) == 4, str(plain_p))
            check("小请求是按目录 sha 一个个走的",
                  all("dir%3A" in p or "base-tree-sha" in p for p in plain_p), str(plain_p))

            # 同一个仓库短时间内再问一次：应该直接走扫描，不再白等那次递归
            mock.queries.clear()
            fl2b = api_get(base, "/api/files?deep=1")
            check("冷却期内直接走逐目录扫描，不再试递归",
                  not any("recursive=1" in q for _, q in mock.queries)
                  and fl2b.get("mode") == "walk", str(mock.queries))
            check("冷却期内结果依旧完整", len(fl2b["files"]) == 4, str(len(fl2b["files"])))

            # 子目录也一样
            fl3 = api_get(base, "/api/files?path=pics&deep=1")
            check("断流时子目录也能列",
                  sorted(f["path"] for f in fl3["files"]) == ["pics/ai/model.bin", "pics/index.html"],
                  str([f["path"] for f in fl3["files"]]))

            # ---- 断流时各个管理动作仍然可用 ----
            r, code = manage(base, "rename", path="pics/ai", newPath="pics/models", kind="dir")
            check("断流时文件夹改名照样成功", code == 200 and r.get("ok"),
                  json.dumps(r, ensure_ascii=False)[:200])
            tr = mock.trees[-1]["tree"]
            check("改成名的旧新路径都对",
                  sorted(e["path"] for e in tr if e["sha"] is None) == ["pics/ai/model.bin"]
                  and sorted(e["path"] for e in tr if e["sha"] is not None) == ["pics/models/model.bin"],
                  json.dumps(tr, ensure_ascii=False)[:200])

            r, code = manage(base, "delete_dir", path="pics")
            check("断流时删文件夹照样成功", code == 200 and r.get("ok"),
                  json.dumps(r, ensure_ascii=False)[:200])
            check("断流时也把目录下文件删干净了",
                  not any(k.startswith("pics/") for k in mock.files), str(sorted(mock.files)))

            # ---- 服务器直接 500 时也要能兜底 ----
            mock.recursive_mode = "error"
            github_drop._RECURSIVE_COOLDOWN.clear()
            fl4 = api_get(base, "/api/files?deep=1")
            check("recursive 接口报错时也退回逐目录扫描",
                  fl4.get("ok") is True and fl4.get("mode") == "walk", str(fl4.get("mode")))
            check("服务器报错时仍列得出剩余文件",
                  "myproj/index.html" in [f["path"] for f in fl4["files"]],
                  str([f["path"] for f in fl4["files"]]))

            # ---- GitHub 真截断（truncated=true）时也要兜底 ----
            mock.recursive_mode = None
            mock.truncated = True
            github_drop._RECURSIVE_COOLDOWN.clear()
            fl5 = api_get(base, "/api/files?deep=1")
            check("GitHub 标记截断时改用逐目录扫描",
                  fl5.get("mode") == "walk", str(fl5.get("mode")))
            check("截断兜底后文件是齐的",
                  "myproj/index.html" in [f["path"] for f in fl5["files"]],
                  str([f["path"] for f in fl5["files"]]))

            # ---- 逐目录扫描也有请求上限，不能把页面拖死 ----
            mock.truncated = False
            mock.recursive_mode = "cut"
            github_drop._RECURSIVE_COOLDOWN.clear()
            original = github_drop.gh_walk_blobs
            try:
                github_drop.gh_walk_blobs = lambda *a, **kw: original(*a, **dict(kw, max_dirs=0))
                fl6 = api_get(base, "/api/files?deep=1")
                check("扫描超过上限时标记 truncated 而不是死等",
                      fl6.get("ok") is True and fl6.get("truncated") is True,
                      "%s %s" % (fl6.get("ok"), fl6.get("truncated")))
            finally:
                github_drop.gh_walk_blobs = original
    finally:
        github_drop._RECURSIVE_COOLDOWN.clear()
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_env_token():
    print("\n[11] Token 来源优先级（不把 Token 写进 config.json）")
    httpd, mock, api = start_mock()
    app = None
    saved_env = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GH_TOKEN")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_TOKEN"] = "env-token-123"
            os.environ.pop("GH_TOKEN", None)
            app, state, base = start_app(api, tmp, {"owner": "tester", "repo": "assets", "token": ""})

            mock.auth.clear()
            r = api_get(base, "/api/tree", token=None)
            check("没填 Token 时自动用环境变量", r.get("ok") is True,
                  json.dumps(r, ensure_ascii=False)[:120])
            check("环境变量的 Token 被带进请求",
                  any(a == "Bearer env-token-123" for a in mock.auth), str(mock.auth))

            st = api_get(base, "/api/state", token=None)
            check("/api/state 标记 tokenFromEnv", st.get("tokenFromEnv") is True, str(st.get("tokenFromEnv")))
            check("/api/state 的 tokenSet 也为 true", st.get("tokenSet") is True, str(st.get("tokenSet")))

            res_up, code = upload(base, "env.txt", b"e", token="")
            check("上传也能用环境变量里的 Token", code == 200, str(code))

            body = json.dumps({"config": {"targetDir": "x/{date}"}}).encode()
            req = urllib.request.Request(base + "/api/config", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=20) as resp:
                json.loads(resp.read().decode("utf-8"))
            disk = json.load(open(state.config_path, encoding="utf-8"))
            check("环境变量的 Token 不会被写进 config.json",
                  not (disk.get("token") or "").strip(), str(disk.get("token")))

            # 请求头优先级最高
            os.environ["GITHUB_TOKEN"] = "env-token-123"
            mock.auth.clear()
            api_get(base, "/api/tree", token="header-token")
            check("请求头 Token 优先于环境变量",
                  any(a == "Bearer header-token" for a in mock.auth), str(mock.auth))

            # 环境变量清掉后，写操作应该明确报缺 Token
            os.environ.pop("GITHUB_TOKEN", None)
            res_np, code_np = upload(base, "none.txt", b"n", token="")
            check("没有 Token 时上传返回 401", code_np == 401, str(code_np))
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_branch_default():
    print("\n[13] 默认分支不是 main 的仓库（绝不能假设 main）")
    seed = repo_seed()
    # 这个仓库的默认分支叫 save —— 真机上就有这种仓库
    httpd, mock, api = start_mock(seed, default_branch="save",
                                  branches=["save", "main", "dev"])
    app = None
    try:
        # ---- 1) 配置里没写分支：全部自动落到仓库默认分支 save
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"branch": ""})
            mock.queries.clear()

            st = api_get(base, "/api/state", token=None)
            check("新配置的分支默认为空（= 自动识别）",
                  (st.get("config") or {}).get("branch") == "",
                  repr((st.get("config") or {}).get("branch")))

            fl = api_get(base, "/api/files")
            check("没写分支时自动用仓库默认分支 save", fl.get("branch") == "save", str(fl.get("branch")))
            # 浅列走的是 Git Data（ref -> commit -> tree），不是 Contents API，
            # 所以这里查的是 ref 路径上的分支名。
            check("列表请求确实落在 ref/heads/save 上",
                  any(p.endswith("/git/ref/heads/save") for p, _ in mock.queries),
                  str(mock.queries[-3:]))
            check("没有偷偷去用 main",
                  not any("heads/main" in p or "ref=main" in q for p, q in mock.queries),
                  str([q for _, q in mock.queries if "ref=" in q][:5]))

            tr = api_get(base, "/api/tree?path=")
            check("目录浏览器也用 save", tr.get("branch") == "save", str(tr.get("branch")))

            up, code = upload(base, "auto.txt", b"auto-branch")
            check("上传也落在 save 上", code == 200 and mock.puts[-1][3] == "save",
                  "code=%s branch=%r" % (code, mock.puts[-1][3] if mock.puts else None))
            check("上传结果里带上了实际分支", up.get("branch") == "save", str(up.get("branch")))

            app.shutdown(); app.server_close(); app = None

        # ---- 2) 配置里存着一个「这个仓库没有」的分支：自动改回默认分支并说明
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"branch": "trunk"})
            mock.queries.clear()
            fl = api_get(base, "/api/files")
            check("配置里的分支不存在时改用默认分支 save",
                  fl.get("branch") == "save", str(fl.get("branch")))
            check("并且明确说明为什么换了分支",
                  "不存在" in (fl.get("message") or "") and "save" in (fl.get("message") or ""),
                  str(fl.get("message")))
            app.shutdown(); app.server_close(); app = None

        # ---- 3) 配置里写了一个真实存在的分支：仍然听用户的
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"branch": "dev"})
            mock.queries.clear()
            fl = api_get(base, "/api/files")
            check("存在的分支会被尊重，不会被改成默认分支",
                  fl.get("branch") == "dev", str(fl.get("branch")))
            check("确实走了 ref/heads/dev",
                  any(p.endswith("/git/ref/heads/dev") for p, _ in mock.queries), str(mock.queries[-3:]))
            app.shutdown(); app.server_close(); app = None

        # ---- 4) 测试连接要能告诉用户真正的默认分支
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"branch": ""})
            req = urllib.request.Request(base + "/api/test?owner=tester&repo=assets",
                                         data=b"", method="POST")
            req.add_header("X-GitHub-Token", "mock-token")
            with urllib.request.urlopen(req, timeout=20) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            check("/api/test 报出真实的默认分支 save",
                  res.get("defaultBranch") == "save", str(res.get("defaultBranch")))
            check("连接成功的提示里也写了默认分支",
                  "save" in (res.get("message") or ""), str(res.get("message")))
            app.shutdown(); app.server_close(); app = None

        # ---- 5) 前端把分支留空时，服务端自己认出分支（不再靠前端填 main）
        with tempfile.TemporaryDirectory() as tmp:
            app, state, base = start_app(api, tmp, {"branch": ""})
            mock.queries.clear()
            fl = api_get(base, "/api/files?branch=&path=")
            check("前端传空 branch 时服务端自动认成 save",
                  fl.get("branch") == "save", str(fl.get("branch")))
            app.shutdown(); app.server_close(); app = None

        # ---- 6) 连仓库信息都读不到时：宁可交给 GitHub，也绝不退回猜 main
        #     （老代码在这条路上会硬填 "main"，把 401/断网 伪装成"分支不存在"）
        saved_status = mock.repo_info_status
        mock.repo_info_status = 500
        mock.queries.clear()
        try:
            # 换一个没被缓存过的仓库名，免得撞上前面用例留下的分支缓存
            cfg = {"owner": "tester", "repo": "ghost-repo", "apiBase": api, "branch": ""}
            name, note = github_drop.pick_branch(cfg, "mock-token")
            check("读不到仓库信息时不猜 main", name == "", repr(name))
            check("并且明说是网络/权限问题", "权限" in note or "默认分支" in note, note)
            check("branch_of 也不会兜到 main",
                  github_drop.branch_of(cfg, "mock-token") == "",
                  repr(github_drop.branch_of(cfg, "mock-token")))
            urls = github_drop.build_urls(cfg, "pics/a.png")
            check("给不出分支时不给死链（不拼 /blob/main/）",
                  "/blob/main/" not in urls["html"] and urls["raw"] == "", str(urls))
            try:
                github_drop.gh_head_tree(cfg, "mock-token")
                check("拼不出 ref 时报错而不是硬用 main", False, "居然没报错")
            except github_drop.GitHubError as exc:
                check("拼不出 ref 时报错而不是硬用 main",
                      "分支" in exc.message and "main" not in exc.message, exc.message)
            check("过程中确实没往 heads/main 发过请求",
                  not any("heads/main" in p for p, _ in mock.queries),
                  str([p for p, _ in mock.queries][-4:]))
        finally:
            mock.repo_info_status = saved_status
    finally:
        if app:
            app.shutdown(); app.server_close()
        httpd.shutdown(); httpd.server_close()


def test_port_probe():
    print("\n[12] 端口占用探测（Windows 上不能靠 bind 判断）")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    httpd.daemon_threads = True
    httpd.mock = MockState()          # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    busy = httpd.server_address[1]
    try:
        check("有人监听时 port_is_serving 为 True",
              github_drop.port_is_serving("127.0.0.1", busy) is True, "port=%d" % busy)

        # 这一条就是原来会「静默抢端口」的根因：Windows 的 SO_REUSEADDR 让
        # bind 也能绑上已被监听的端口，所以 bind 试探会误报「空闲」。
        if os.name == "nt":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", busy))
                    dup_ok = True
                except OSError:
                    dup_ok = False
            check("Windows: SO_REUSEADDR+bind 能绑上已监听的端口（故不可用作探测）",
                  dup_ok is True, "dup_bind_succeeded=%s" % dup_ok)

        # 占 0 端口拿一个刚释放的端口：没人监听，应判为「空闲」
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        check("没人监听的端口 port_is_serving 为 False",
              github_drop.port_is_serving("127.0.0.1", free) is False, "port=%d" % free)

        chosen = github_drop.pick_port("127.0.0.1", busy)
        check("pick_port 会跳过正在服务的端口", chosen != busy, "chosen=%d busy=%d" % (chosen, busy))
        check("pick_port 选出的端口确实没人监听",
              github_drop.port_is_serving("127.0.0.1", chosen) is False, "chosen=%d" % chosen)

        check("pick_port(host, 0) 原样返回 0（交给系统分配）",
              github_drop.pick_port("127.0.0.1", 0) == 0, "n/a")

        raised = False
        try:
            github_drop.pick_port("127.0.0.1", busy, span=1)
        except SystemExit:
            raised = True
        check("整段端口都占用时 pick_port 明确报错而不是硬绑", raised is True, "n/a")
    finally:
        httpd.shutdown(); httpd.server_close()


def main():
    print("=" * 62)
    print(" GitHub Drop 自测（全部走本地 mock，不访问真实 GitHub）")
    print("=" * 62)
    test_path_rules()
    test_contents_api()
    test_folder_upload()
    test_conflict_retry()
    test_autorename()
    test_git_data_api()
    test_http_surface()
    test_repo_url()
    test_branches_api()
    test_tree_api()
    test_manage_api()
    test_files_walk_fallback()
    test_env_token()
    test_branch_default()
    test_port_probe()

    print("\n" + "=" * 62)
    print(" 通过 %d / %d" % (len(PASSED), len(PASSED) + len(FAILED)))
    if FAILED:
        print(" 失败用例:")
        for name, detail in FAILED:
            print("   - %s  %s" % (name, detail))
        return 1
    print(" 全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
