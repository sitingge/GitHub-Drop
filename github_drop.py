#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Drop —— 拖拽即上传（GitHub 版）
================================================

把文件（或整个文件夹）拖进浏览器页面，自动上传到你的 GitHub 仓库指定目录。

特点
----
* 只用 Python 标准库，不需要 pip 安装任何依赖
* 支持拖拽多文件 / 整个文件夹（保留目录结构）
* 目标目录支持模板：{date} {yyyy} {mm} {dd} {time} {timestamp} {name} {ext}
* 大文件自动走 Git Data API（blob/tree/commit/ref），避免 Contents API 的体积限制
* 同名文件默认覆盖；可开启「自动重命名」改为 file-1.txt
* 上传完成后给出 直链 / GitHub 页面 / jsDelivr CDN 三种链接，一键复制

用法
----
    python github_drop.py                  # 默认 http://127.0.0.1:8765
    python github_drop.py --port 9000
    python github_drop.py --no-browser     # 不自动打开浏览器
    python github_drop.py --config my.json

配置写在 config.json（首次运行自动生成模板），也可以在网页上改。

Token 权限：fine-grained PAT 需要 Contents: Read and write；经典 PAT 需要 repo 权限。
Token 也可以不落盘，直接在网页输入框里填。
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "GitHub Drop"
VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(HERE, "config.json")
MAX_BODY_BYTES = 300 * 1024 * 1024          # 单次请求体上限（本地保护，GitHub 单文件硬上限 100MB）
GITHUB_FILE_LIMIT = 100 * 1024 * 1024
RECURSIVE_COOLDOWN_SECONDS = 600            # 一次性拉整棵树失败后，这段时间内直接走逐目录扫描
BRANCH_CACHE_SECONDS = 300                  # 仓库默认分支 / 分支有效性的缓存时长（避免每个请求都去问一遍）
_BRANCH_CACHE = {}                          # (apiBase, owner, repo) -> (default_branch, 过期时间)
_BRANCH_OK = {}                             # (apiBase, owner, repo, branch) -> 过期时间（确认过存在）
_RECURSIVE_COOLDOWN = {}                    # (owner, repo, branch) -> 冷却到什么时候

DEFAULT_CONFIG = {
    "owner": "",                             # 仓库所有者，例如 octocat
    "repo": "",                              # 仓库名，例如 my-assets
    # 留空 = 自动使用仓库的真实默认分支（绝不能假设叫 main：
    # 默认分支可以是 save / master / develop…，`GET /repos/{o}/{r}` 里的 default_branch 才是答案）
    "branch": "",
    "token": "",                             # GitHub Token（可以不填，改用网页输入）
    "targetDir": "uploads/{date}",           # 仓库内的目标目录，支持模板
    "apiBase": "https://api.github.com",     # GitHub Enterprise / 代理可改这里
    "commitMessage": "chore(upload): add {name}",
    "sanitizeNames": False,                  # 把文件名里的空格换成连字符
    "autoRename": False,                     # 同名文件自动改名而不是覆盖
    "keepFolder": True,                      # 拖入文件夹时保留相对路径
    "largeFileThresholdMB": 20,              # 超过该体积改用 Git Data API
    "copyLink": True,
}


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def default_config() -> dict:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    cfg = default_config()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                for key, value in data.items():
                    cfg[key] = value
        except Exception as exc:                                  # noqa: BLE001
            print("[warn] 读取配置失败，使用默认值: %s" % exc)
    return cfg


def save_config(cfg: dict, path: str = DEFAULT_CONFIG_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def public_config(cfg: dict) -> dict:
    """给网页用的配置（不含 token）。"""
    out = dict(cfg)
    out.pop("token", None)
    return out


# --------------------------------------------------------------------------- #
# 路径处理
# --------------------------------------------------------------------------- #
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*#%\\]')


def clean_name(name: str, spaces_to_dash: bool = False) -> str:
    """清洗单个路径片段：去掉目录成分 / 控制字符 / URL 特殊字符，防止路径穿越。"""
    name = str(name).replace("\\", "/")
    if "/" in name:                      # 名称里混入路径时只取最后一段，杜绝 ../../ 穿越
        parts = [p for p in name.split("/") if p not in ("", ".", "..")]
        name = parts[-1] if parts else ""
    name = _BAD_CHARS.sub("", name).strip()
    if spaces_to_dash:
        name = re.sub(r"\s+", "-", name)
    if name in ("", ".", ".."):
        name = "file-%d" % int(time.time())
    return name


def render_template(tpl: str, filename: str, now: datetime) -> str:
    stem, ext = os.path.splitext(filename)
    mapping = {
        "date": now.strftime("%Y-%m-%d"),
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
        "time": now.strftime("%H%M%S"),
        "timestamp": str(int(now.timestamp())),
        "name": stem,
        "ext": ext.lstrip("."),
        "filename": filename,
    }
    out = str(tpl if tpl is not None else "")
    for key, value in mapping.items():
        out = out.replace("{" + key + "}", value)
    return out


def _split_segments(text: str) -> list:
    parts = str(text).replace("\\", "/").split("/")
    return [p for p in parts if p not in ("", ".", "..")]


def build_repo_path(cfg: dict, filename: str, rel_path: str = "",
                    target_dir: str = None, now: datetime = None) -> str:
    """把「文件名 + 可选相对路径 + 目标目录模板」拼成仓库内路径。"""
    now = now or datetime.now()
    sanitize = bool(cfg.get("sanitizeNames"))
    tpl = cfg.get("targetDir", "uploads/{date}") if target_dir is None else target_dir

    rel_parts = _split_segments(rel_path) if rel_path else []
    if rel_parts:
        filename = rel_parts[-1]
        rel_dirs = rel_parts[:-1]
    else:
        rel_dirs = []

    filename = clean_name(filename, sanitize)
    if not cfg.get("keepFolder", True):
        rel_dirs = []
    rel_dirs = [clean_name(d, sanitize) for d in rel_dirs]

    filled = render_template(tpl, filename, now)
    head = [clean_name(s, sanitize) for s in _split_segments(filled)]
    return "/".join(head + rel_dirs + [filename])


# --------------------------------------------------------------------------- #
# 仓库地址解析
# --------------------------------------------------------------------------- #
# GitHub 上被官方路由占用的顶级路径，出现这些说明粘贴的不是仓库地址
RESERVED_OWNERS = {
    "orgs", "users", "settings", "marketplace", "topics", "collections", "sponsors",
    "features", "about", "pricing", "explore", "notifications", "new", "login", "apps",
    "site", "security", "enterprise", "search", "trending", "codespaces", "dashboard",
    "pulls", "issues", "account", "organizations", "sessions", "logout",
}
# owner/repo 之后可能跟着这些段，紧跟的那一段才是分支名
BRANCH_PATH_HEADS = {"tree", "blob", "commits", "commit", "branches", "raw", "edit"}
TREE_LIKE = {"tree"}                       # /tree/<分支...>[/<路径...>]  分支可能带斜杠，有歧义
FILE_LIKE = {"blob", "raw", "edit"}        # /blob/<分支...>/<文件>       末尾一段是文件名
BRANCH_ONLY = {"commits", "commit"}        # /commits/<分支>              后面直接是分支名


def resolve_branch(candidates, branches):
    """分支名可以带斜杠，从 /tree/ 链接里猜只能靠「最长前缀匹配真实分支列表」。

    candidates 按最长优先排列，返回第一个真实存在的；都不匹配返回 None。
    """
    names = set(branches or [])
    for candidate in candidates or []:
        if candidate and candidate in names:
            return candidate
    return None


def resolve_tree_path(candidates, branches):
    """确定分支之后，把链接里剩下的部分当作目标目录。

    /tree/main/pics 的候选是 ['main/pics', 'main']：只有真实分支是 main 时，剩下的 pics 才是目录。
    返回 (branch, path)。
    """
    branch = resolve_branch(candidates, branches)
    if not branch or not candidates:
        return None, ""
    full = candidates[0] or ""
    if full == branch:
        return branch, ""
    if full.startswith(branch + "/"):
        return branch, full[len(branch) + 1:]
    return branch, ""


def parse_repo_url(text: str):
    """把各种写法的仓库地址解析成 owner / repo / branch / apiBase。

    支持：
        https://github.com/octocat/hello-world
        https://github.com/octocat/hello-world.git
        https://github.com/octocat/hello-world/tree/dev/src
        github.com/octocat/hello-world
        octocat/hello-world
        git@github.com:octocat/hello-world.git
        ssh://git@github.com/octocat/hello-world.git
        https://ghe.example.com/octocat/hello-world      -> apiBase 自动换成 /api/v3
        git clone https://github.com/octocat/hello-world.git   （前后有多余文字也认）
    """
    raw = str(text or "").strip().strip("\"'<>")
    if not raw:
        return None

    # 允许整段粘贴 `git clone ...` 之类的命令
    match = re.search(r"(?:https?|ssh|git)://[^\s]+", raw)
    if match:
        raw = match.group(0)
    else:
        match = re.search(r"[^\s/]+@[^\s:]+[:/][^\s]+", raw)
        if match:
            raw = match.group(0)

    if raw.startswith("git@") or raw.startswith("ssh://"):
        match = re.match(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+)$", raw)
        if not match:
            return None
        host, rest = match.group(1).lower(), match.group(2)
    elif "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.netloc.split("@")[-1].split(":")[0] or "").lower()
        rest = parsed.path
    else:
        segments = [s for s in raw.split("/") if s]
        if not segments:
            return None
        if "." in segments[0]:                 # github.com/o/r 或 ghe.corp.com/o/r
            host, rest = segments[0].lower(), "/".join(segments[1:])
        else:                                  # o/r
            host, rest = "github.com", "/".join(segments)

    segments = [urllib.parse.unquote(s) for s in rest.split("/") if s]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if owner.lower() in RESERVED_OWNERS:
        return None
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if repo in ("", "."):
        return None

    branch, candidates, tree_path = None, [], ""
    if len(segments) >= 3 and segments[2].lower() in BRANCH_PATH_HEADS:
        head, tail = segments[2].lower(), segments[3:]
        if head in TREE_LIKE and tail:
            # 尽量长的组合都当候选，稍后用真实分支列表校正
            candidates = ["/".join(tail[:i]) for i in range(len(tail), 0, -1)]
            tree_path = candidates[0]
        elif head in FILE_LIKE and len(tail) >= 2:
            candidates = ["/".join(tail[:-1])]
        elif head in BRANCH_ONLY and tail:
            candidates = ["/".join(tail)]
        branch = candidates[0] if candidates else None

    api_base = ("https://api.github.com" if host in ("github.com", "www.github.com")
                else "https://%s/api/v3" % host)
    return {"owner": owner, "repo": repo, "branch": branch, "branchCandidates": candidates,
            "treePath": tree_path, "host": host, "apiBase": api_base}


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
class GitHubError(Exception):
    def __init__(self, status: int, message: str, payload=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload or {}


def effective_token(cfg: dict, token: str = None) -> str:
    """Token 来源优先级：请求头 > config.json > 环境变量 GITHUB_TOKEN / GH_TOKEN。

    走环境变量就不用把 Token 明文写在 config.json 里了。
    """
    return (token or (cfg or {}).get("token") or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN") or "").strip()


def _read_response(resp) -> bytes:
    """读响应体。

    大响应（整个仓库的 git tree 动辄几百 KB ~ 几 MB）偶尔会被中途掐断，
    这时 http.client 抛 IncompleteRead，但已经收到的字节还是好的。
    宁可交给调用方判断内容是否完整，也不要把整个请求直接判死。
    """
    try:
        return resp.read()
    except http.client.IncompleteRead as exc:
        return getattr(exc, "partial", None) or b""


def gh_api(cfg: dict, method: str, api_path: str, body=None, token: str = None,
           timeout: int = 60, max_attempts: int = None):
    token = effective_token(cfg, token)
    # 读接口允许匿名（公开仓库不填 Token 也能浏览）；写接口必须有 Token
    if not token and method.upper() != "GET":
        raise GitHubError(401, "缺少 GitHub Token，请在页面上填写或写入 config.json")

    base = (cfg.get("apiBase") or "https://api.github.com").rstrip("/")
    url = base + api_path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    is_get = method.upper() == "GET"

    def build_request():
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        # 明确不压缩：Content-Length 才准，也不会因为解码出岔子把响应体读残
        req.add_header("Accept-Encoding", "identity")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "github-drop/%s" % VERSION)
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        return req

    # GET 是幂等的，被掐断了可以再来一次；写请求绝不能瞎重试
    attempts = max_attempts if max_attempts is not None else (3 if is_get else 1)
    attempts = max(1, int(attempts))
    last_problem = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(build_request(), timeout=timeout) as resp:
                raw = _read_response(resp)
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                # 读残了 -> JSON 也被截断，重试一次往往就好了
                last_problem = "响应不完整（收到 %d 字节）：%s" % (len(raw), exc)
                if attempt + 1 < attempts:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise GitHubError(0, "GitHub 返回的数据不完整，请重试（%s）" % last_problem)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:                                     # noqa: BLE001
                payload = {"message": raw.decode("utf-8", "replace")[:500]}
            msg = payload.get("message") or ("HTTP %s" % exc.code)
            if payload.get("errors"):
                try:
                    msg += " | " + "; ".join(str(e.get("message", e)) for e in payload["errors"])
                except Exception:                                 # noqa: BLE001
                    pass
            raise GitHubError(exc.code, msg, payload)
        except urllib.error.URLError as exc:
            raise GitHubError(0, "网络错误: %s" % exc.reason)
        except socket.timeout:
            raise GitHubError(0, "请求超时，请检查网络或稍后重试")
        except http.client.HTTPException as exc:
            # 连接被提前断开之类：GET 重试，其它直接报出来
            last_problem = str(exc)
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise GitHubError(0, "GitHub 连接中断，请重试（%s）" % exc)
    raise GitHubError(0, "请求失败：%s" % (last_problem or "未知原因"))


def contents_api_path(cfg: dict, repo_path: str) -> str:
    owner = cfg.get("owner", "")
    repo = cfg.get("repo", "")
    return "/repos/%s/%s/contents/%s" % (owner, repo, urllib.parse.quote(repo_path, safe="/"))


def get_remote_sha(cfg: dict, repo_path: str, token: str = None):
    branch = branch_of(cfg, token)
    path = contents_api_path(cfg, repo_path)
    if branch:                      # 留空时干脆不带 ref，让 GitHub 用默认分支
        path += "?ref=" + urllib.parse.quote(branch, safe="")
    try:
        info = gh_api(cfg, "GET", path, token=token)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    return info.get("sha") if isinstance(info, dict) else None


def resolve_conflict_path(cfg: dict, repo_path: str, token: str = None) -> str:
    """autoRename=True 时，为已存在的文件找一个不冲突的新名字。"""
    if not cfg.get("autoRename"):
        return repo_path
    if get_remote_sha(cfg, repo_path, token) is None:
        return repo_path
    stem, ext = os.path.splitext(repo_path)
    for i in range(1, 200):
        candidate = "%s-%d%s" % (stem, i, ext)
        if get_remote_sha(cfg, candidate, token) is None:
            return candidate
    return "%s-%d%s" % (stem, int(time.time()), ext)


def upload_via_contents(cfg: dict, token: str, repo_path: str, content: bytes, message: str) -> dict:
    branch = branch_of(cfg, token)
    api_path = contents_api_path(cfg, repo_path)

    body = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
    }
    if branch:                      # 不传 branch 时 GitHub 落在仓库默认分支上
        body["branch"] = branch
    sha = get_remote_sha(cfg, repo_path, token)
    if sha:
        body["sha"] = sha

    try:
        res = gh_api(cfg, "PUT", api_path, body, token=token, timeout=300)
    except GitHubError as exc:
        if exc.status in (409, 422):
            # 409：并发写冲突（重新取 sha 再试一次）
            # 422：体积/参数问题（交给 Git Data API）
            if exc.status == 409:
                fresh = get_remote_sha(cfg, repo_path, token)
                if fresh:
                    body["sha"] = fresh
                    res = gh_api(cfg, "PUT", api_path, body, token=token, timeout=300)
                else:
                    body.pop("sha", None)
                    res = gh_api(cfg, "PUT", api_path, body, token=token, timeout=300)
            else:
                return upload_via_git_data(cfg, token, repo_path, content, message)
        else:
            raise

    content_info = res.get("content") or {}
    commit_info = res.get("commit") or {}
    return {
        "path": repo_path,
        "sha": content_info.get("sha"),
        "commit": commit_info.get("sha"),
        "mode": "contents-api",
    }


def _ref_path(cfg: dict, token: str = None) -> str:
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    branch = branch_of(cfg, token)
    if not branch:
        # 分支名拼不出 ref 路径（不像 Contents API 可以留空），只能如实报错，
        # 而不是拿 "main" 去撞一个不存在的分支、把人引到错误的方向。
        raise GitHubError(400, "还没能确定要操作哪个分支：请先粘贴仓库地址，"
                               "并确认 Token 有效、能读到这个仓库")
    return "/repos/%s/%s/git/ref/heads/%s" % (owner, repo, urllib.parse.quote(branch, safe="/"))


def upload_via_git_data(cfg: dict, token: str, repo_path: str, content: bytes, message: str) -> dict:
    """大文件通道：blob -> tree -> commit -> 更新 ref。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    branch = branch_of(cfg, token)

    ref = gh_api(cfg, "GET", _ref_path(cfg, token), token=token)
    parent_sha = ref["object"]["sha"]
    parent = gh_api(cfg, "GET", "/repos/%s/%s/git/commits/%s" % (owner, repo, parent_sha), token=token)
    base_tree = parent["tree"]["sha"]

    blob = gh_api(cfg, "POST", "/repos/%s/%s/git/blobs" % (owner, repo), {
        "content": base64.b64encode(content).decode("ascii"),
        "encoding": "base64",
    }, token=token, timeout=300)

    tree = gh_api(cfg, "POST", "/repos/%s/%s/git/trees" % (owner, repo), {
        "base_tree": base_tree,
        "tree": [{"path": repo_path, "mode": "100644", "type": "blob", "sha": blob["sha"]}],
    }, token=token, timeout=300)

    commit = gh_api(cfg, "POST", "/repos/%s/%s/git/commits" % (owner, repo), {
        "message": message,
        "tree": tree["sha"],
        "parents": [parent_sha],
    }, token=token, timeout=300)

    gh_api(cfg, "PATCH", "/repos/%s/%s/git/refs/heads/%s"
           % (owner, repo, urllib.parse.quote(branch, safe="/")),
           {"sha": commit["sha"], "force": False}, token=token, timeout=120)

    return {
        "path": repo_path,
        "sha": blob.get("sha"),
        "commit": commit.get("sha"),
        "mode": "git-data-api",
    }


def gh_upload(cfg: dict, token: str, repo_path: str, content: bytes, message: str = None) -> dict:
    if len(content) > GITHUB_FILE_LIMIT:
        raise GitHubError(413, "文件 %.1f MB 超过 GitHub 单文件 100MB 上限"
                          % (len(content) / 1024 / 1024))
    message = (message or cfg.get("commitMessage") or "chore(upload): add {name}")
    message = message.replace("{name}", os.path.basename(repo_path))

    try:
        threshold = int(cfg.get("largeFileThresholdMB", 20)) * 1024 * 1024
    except (TypeError, ValueError):
        threshold = 20 * 1024 * 1024

    if len(content) > threshold:
        return upload_via_git_data(cfg, token, repo_path, content, message)
    return upload_via_contents(cfg, token, repo_path, content, message)


# --------------------------------------------------------------------------- #
# 仓库文件管理：删除 / 重命名 / 新建文件夹 / 清空
# --------------------------------------------------------------------------- #
def safe_repo_path(path: str) -> str:
    """清洗仓库内路径，挡掉 .. 之类的穿越写法。"""
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/")
                    if p not in ("", ".", ".."))


def gh_head_tree(cfg: dict, token: str = None, branch: str = None):
    """拿到分支最新 commit 与它的 tree（改文件/删目录都要基于它）。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    ref = branch_of(cfg, token, branch)
    if not ref:
        raise GitHubError(400, "还没能确定要操作哪个分支：请先粘贴仓库地址，"
                               "并确认 Token 有效、能读到这个仓库")
    info = gh_api(cfg, "GET", "/repos/%s/%s/git/ref/heads/%s"
                  % (owner, repo, urllib.parse.quote(ref, safe="/")), token=token)
    commit_sha = info["object"]["sha"]
    commit = gh_api(cfg, "GET", "/repos/%s/%s/git/commits/%s" % (owner, repo, commit_sha), token=token)
    return commit_sha, commit["tree"]["sha"]


def gh_recursive_tree(cfg: dict, token: str = None, branch: str = None):
    """一次拿到整棵树（最快，一个请求）。仓库大 / 网络被中途掐断时会失败，交给调用方兜底。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    commit_sha, tree_sha = gh_head_tree(cfg, token, branch)
    # 只试一次：外面有逐目录扫描兜底，同一份大响应反复重试纯属浪费时间
    data = gh_api(cfg, "GET", "/repos/%s/%s/git/trees/%s?recursive=1"
                  % (owner, repo, urllib.parse.quote(tree_sha, safe="")),
                  token=token, timeout=120, max_attempts=1)
    return commit_sha, tree_sha, (data.get("tree") or []), bool(data.get("truncated"))


def gh_subtree_sha(cfg: dict, token: str, root_tree: str, prefix: str) -> str:
    """沿 prefix 一段一段往下走，定位到某层目录的 tree sha（每段一个小请求）。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    tree_sha = root_tree
    walked = []
    for seg in [s for s in safe_repo_path(prefix).split("/") if s]:
        data = gh_api(cfg, "GET", "/repos/%s/%s/git/trees/%s"
                      % (owner, repo, urllib.parse.quote(tree_sha, safe="")), token=token)
        nxt = None
        for entry in data.get("tree") or []:
            if entry.get("path") == seg and entry.get("type") == "tree":
                nxt = entry.get("sha")
                break
        if not nxt:
            raise GitHubError(404, "找不到这个目录：%s" % "/".join(walked + [seg]))
        tree_sha = nxt
        walked.append(seg)
    return tree_sha


def gh_list_dir_entries(cfg: dict, token: str = None, branch: str = None, path: str = ""):
    """只列一层目录（一个小请求就能出结果，快）。

    返回 (commit_sha, 根 tree sha, 子目录里的 blob 列表, 直接子文件夹列表)。
    """
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    commit_sha, root_tree = gh_head_tree(cfg, token, branch)
    base = safe_repo_path(path)
    sub_tree = gh_subtree_sha(cfg, token, root_tree, base) if base else root_tree
    data = gh_api(cfg, "GET", "/repos/%s/%s/git/trees/%s"
                  % (owner, repo, urllib.parse.quote(sub_tree, safe="")), token=token)

    blobs, dirs = [], []
    for entry in data.get("tree") or []:
        name = entry.get("path") or ""
        if not name:
            continue
        full = (base + "/" + name) if base else name
        if entry.get("type") == "tree":
            dirs.append({"path": full, "name": name, "count": None})
        elif entry.get("type") == "blob":
            blobs.append({"path": full, "rel": name, "name": name, "dir": base,
                          "type": "blob", "size": entry.get("size") or 0,
                          "sha": entry.get("sha"), "mode": entry.get("mode") or "100644"})
    blobs.sort(key=lambda b: b["name"].lower())
    dirs.sort(key=lambda d: d["name"].lower())
    return commit_sha, root_tree, blobs, dirs


def gh_walk_blobs(cfg: dict, token: str, branch: str, prefix: str = "",
                  max_dirs: int = 1500, max_files: int = 40000, workers: int = 8):
    """逐目录把 prefix 下的文件收集齐（按层并发，一层一次往返）。

    为什么不直接一次性拉整棵树：整棵树动辄几百 KB ~ 几 MB，
    网络一被中途掐断就整份作废；这里每次只要一个小目录的响应，稳得多。
    返回 (commit_sha, 根 tree sha, blob 列表, 是否没走完)。
    """
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    commit_sha, root_tree = gh_head_tree(cfg, token, branch)
    prefix = safe_repo_path(prefix)
    start = gh_subtree_sha(cfg, token, root_tree, prefix) if prefix else root_tree

    def fetch(item):
        sha, cur = item
        try:
            data = gh_api(cfg, "GET", "/repos/%s/%s/git/trees/%s"
                          % (owner, repo, urllib.parse.quote(sha, safe="")), token=token)
            return cur, data, None
        except GitHubError as exc:                                # noqa: BLE001
            return cur, None, exc

    blobs, level, visited, incomplete = [], [(start, prefix)], 0, False
    while level and not incomplete:
        visited += len(level)
        if visited > max_dirs:
            incomplete = True
            break
        with ThreadPoolExecutor(max_workers=min(workers, len(level))) as pool:
            results = list(pool.map(fetch, level))

        errors = [err for _, _, err in results if err is not None]
        if errors and len(errors) == len(results) and not blobs:
            raise errors[0]           # 一个都没成功：多半是真出问题了，如实报错

        nxt = []
        for cur, data, err in results:
            if err or not data:
                incomplete = True     # 个别目录没拿到 -> 结果不完整，如实标记
                continue
            for entry in data.get("tree") or []:
                name = entry.get("path") or ""
                if not name:
                    continue
                full = (cur + "/" + name) if cur else name
                if entry.get("type") == "tree":
                    nxt.append((entry.get("sha"), full))
                elif entry.get("type") == "blob":
                    blobs.append({"path": full, "type": "blob",
                                  "mode": entry.get("mode") or "100644",
                                  "size": entry.get("size") or 0, "sha": entry.get("sha")})
        level = nxt
        if len(blobs) >= max_files:
            incomplete = True

    blobs.sort(key=lambda b: b["path"].lower())
    return commit_sha, root_tree, blobs, incomplete


def gh_subtree_entries(cfg: dict, token: str = None, branch: str = None, prefix: str = ""):
    """列出 prefix 下的全部文件（递归），两条腿走路：

    1. 先试「一次性递归 tree」——正常网络下一个请求就够；
    2. 失败（被掐断）或被 GitHub 截断时，退回逐目录走，每层只取一个小响应。

    递归这条路一旦失败，就按仓库记一笔冷却时间：接下来这段时间直接走逐目录扫描，
    省得每次都先白等一次超时。返回 (commit_sha, 根 tree sha, blob 列表, 是否截断, 用的哪种方式)。
    """
    prefix = safe_repo_path(prefix)
    ref = branch_of(cfg, token, branch)
    key = (cfg.get("owner"), cfg.get("repo"), ref)

    cool_until = _RECURSIVE_COOLDOWN.get(key, 0)
    if cool_until > time.time():
        commit_sha, root_tree, blobs, truncated = gh_walk_blobs(cfg, token, ref, prefix)
        return commit_sha, root_tree, blobs, truncated, "walk"

    try:
        commit_sha, tree_sha, tree, truncated = gh_recursive_tree(cfg, token, ref)
        if not truncated:
            head = (prefix + "/") if prefix else ""
            blobs = [t for t in tree if t.get("type") == "blob"
                     and (not prefix or t.get("path") == prefix
                          or str(t.get("path", "")).startswith(head))]
            blobs.sort(key=lambda b: str(b.get("path", "")).lower())
            _RECURSIVE_COOLDOWN.pop(key, None)
            return commit_sha, tree_sha, blobs, False, "recursive"
    except GitHubError:
        pass          # 网络掐断 / 数据不完整 —— 换稳的那条路，并记下这次失败

    _RECURSIVE_COOLDOWN[key] = time.time() + RECURSIVE_COOLDOWN_SECONDS
    commit_sha, root_tree, blobs, truncated = gh_walk_blobs(cfg, token, ref, prefix)
    return commit_sha, root_tree, blobs, truncated, "walk"


def gh_tree_entries(cfg: dict, token: str = None, branch: str = None, prefix: str = ""):
    """兼容老调用：递归列目录，返回 (commit_sha, 根 tree sha, 条目列表, 是否截断)。"""
    commit_sha, tree_sha, blobs, truncated, _mode = gh_subtree_entries(cfg, token, branch, prefix)
    return commit_sha, tree_sha, blobs, truncated


def gh_commit_tree(cfg: dict, token: str, base_tree: str, entries: list, message: str,
                   branch: str = None, parents=None) -> dict:
    """用一棵新 tree 提交一次改动：base_tree + entries（sha 为 None 表示删除）。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    ref = branch_of(cfg, token, branch)
    if not ref:
        raise GitHubError(400, "还没能确定要操作哪个分支：请先粘贴仓库地址，"
                               "并确认 Token 有效、能读到这个仓库")

    tree_body = {"tree": entries or []}
    if base_tree:
        tree_body["base_tree"] = base_tree
    tree = gh_api(cfg, "POST", "/repos/%s/%s/git/trees" % (owner, repo), tree_body,
                  token=token, timeout=180)

    commit_body = {"message": message, "tree": tree["sha"]}
    if parents:
        commit_body["parents"] = parents
    commit = gh_api(cfg, "POST", "/repos/%s/%s/git/commits" % (owner, repo), commit_body,
                    token=token, timeout=180)

    gh_api(cfg, "PATCH", "/repos/%s/%s/git/refs/heads/%s"
           % (owner, repo, urllib.parse.quote(ref, safe="/")),
           {"sha": commit["sha"], "force": False}, token=token, timeout=120)
    return {"commit": commit["sha"], "tree": tree["sha"]}


def gh_delete_file(cfg: dict, token: str, path: str, message: str = None, branch: str = None) -> dict:
    """删单个文件（Contents API，一次 commit）。"""
    path = safe_repo_path(path)
    if not path:
        raise GitHubError(400, "要删除的路径不能为空")
    sha = get_remote_sha(cfg, path, token)
    if not sha:
        raise GitHubError(404, "仓库里没有这个文件：%s" % path)
    body = {"message": message or "chore(delete): remove %s" % path, "sha": sha}
    ref = branch_of(cfg, token, branch)
    if ref:                         # 不传 branch 时 GitHub 落在仓库默认分支上
        body["branch"] = ref
    try:
        gh_api(cfg, "DELETE", contents_api_path(cfg, path), body, token=token, timeout=180)
    except GitHubError as exc:
        if exc.status in (409, 422):
            fresh = get_remote_sha(cfg, path, token)
            if not fresh:
                raise GitHubError(404, "文件已被删除：%s" % path)
            body["sha"] = fresh
            gh_api(cfg, "DELETE", contents_api_path(cfg, path), body, token=token, timeout=180)
        else:
            raise
    return {"mode": "contents-api", "deleted": [path]}


def gh_delete_dir(cfg: dict, token: str, path: str, message: str = None, branch: str = None) -> dict:
    """删整个文件夹：用 Git Data 一次 commit 删掉目录下所有文件（Git 里空目录本身不存在）。"""
    path = safe_repo_path(path)
    if not path:
        raise GitHubError(400, "要删除的文件夹路径不能为空")
    commit_sha, root_tree, blobs, truncated, mode = gh_subtree_entries(cfg, token, branch, path)
    if truncated:
        raise GitHubError(400, "这个文件夹太大，一次列不完（已到 %d 个文件），"
                               "请进入更具体的子文件夹再删" % len(blobs))
    if not blobs:
        raise GitHubError(404, "这个文件夹里没有文件（Git 不保存空目录）：%s" % path)
    payload = [{"path": t["path"], "mode": t.get("mode") or "100644",
                "type": "blob", "sha": None} for t in blobs]
    result = gh_commit_tree(cfg, token, root_tree, payload,
                            message or "chore(delete): remove folder %s" % path,
                            branch=branch, parents=[commit_sha])
    result["mode"] = "git-data-api"
    result["scanned"] = mode
    result["deleted"] = [t["path"] for t in blobs]
    return result


def gh_rename_path(cfg: dict, token: str, src: str, dst: str, kind: str = "file",
                   message: str = None, branch: str = None) -> dict:
    """重命名/移动文件或文件夹（Git Data，一次 commit）。"""
    src, dst = safe_repo_path(src), safe_repo_path(dst)
    if not src or not dst:
        raise GitHubError(400, "源路径和目标路径都不能为空")
    if src == dst:
        raise GitHubError(400, "新名字和旧名字一样")
    if dst == src + "/" or dst.startswith(src + "/"):
        raise GitHubError(400, "不能把文件夹移动到它自己的子目录里")

    # 目标已存在就不动，避免静默覆盖
    if get_remote_sha(cfg, dst, token) is not None:
        raise GitHubError(409, "目标位置已经有同名内容了：%s" % dst)

    src_sha = get_remote_sha(cfg, src, token)
    if src_sha:
        # 单个文件：不需要遍历任何目录树，最省事也最稳
        commit_sha, root_tree = gh_head_tree(cfg, token, branch)
        payload = [{"path": src, "mode": "100644", "type": "blob", "sha": None},
                   {"path": dst, "mode": "100644", "type": "blob", "sha": src_sha}]
        result = gh_commit_tree(cfg, token, root_tree, payload,
                                message or "chore(rename): %s -> %s" % (src, dst),
                                branch=branch, parents=[commit_sha])
        result["mode"] = "git-data-api"
        result["scanned"] = "single-file"
        result["renamed"] = {"from": src, "to": dst, "count": 1}
        return result

    # 文件夹：把子树里的文件整体搬过去
    commit_sha, root_tree, blobs, truncated, mode = gh_subtree_entries(cfg, token, branch, src)
    if truncated:
        raise GitHubError(400, "这个文件夹太大，一次列不完（已到 %d 个文件），"
                               "请进入更具体的子目录再改名" % len(blobs))
    if not blobs:
        raise GitHubError(404, "找不到要改名的内容：%s" % src)

    payload = []
    for item in blobs:
        payload.append({"path": item["path"], "mode": item.get("mode") or "100644",
                        "type": "blob", "sha": None})
        moved = dst + item["path"][len(src):] if item["path"] != src else dst
        payload.append({"path": moved, "mode": item.get("mode") or "100644",
                        "type": "blob", "sha": item["sha"]})

    result = gh_commit_tree(cfg, token, root_tree, payload,
                            message or "chore(rename): %s -> %s" % (src, dst),
                            branch=branch, parents=[commit_sha])
    result["mode"] = "git-data-api"
    result["scanned"] = mode
    result["renamed"] = {"from": src, "to": dst, "count": len(blobs)}
    return result


def gh_make_dir(cfg: dict, token: str, path: str, branch: str = None) -> dict:
    """新建文件夹：Git 不保存空目录，放一个 .gitkeep 占位。"""
    path = safe_repo_path(path)
    if not path:
        raise GitHubError(400, "文件夹路径不能为空")
    keep = path + "/.gitkeep"
    if get_remote_sha(cfg, path, token) is not None:
        raise GitHubError(409, "这个位置已经有同名文件或文件夹了：%s" % path)
    result = gh_upload(cfg, token, keep, b"", "chore(mkdir): create %s" % path)
    result["created"] = keep
    return result


def gh_clear_repo(cfg: dict, token: str, branch: str = None, message: str = None) -> dict:
    """清空仓库：一次 commit 把所有顶层条目删掉（内容和目录一起消失）。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    commit_sha, tree_sha = gh_head_tree(cfg, token, branch)
    data = gh_api(cfg, "GET", "/repos/%s/%s/git/trees/%s" % (owner, repo, tree_sha), token=token)
    top = data.get("tree") or []
    if not top:
        raise GitHubError(400, "仓库已经是空的了")
    payload = [{"path": t["path"], "mode": t.get("mode") or "100644",
                "type": t.get("type") or "blob", "sha": None} for t in top]
    result = gh_commit_tree(cfg, token, tree_sha, payload,
                            message or "chore(clear): empty the repository",
                            branch=branch, parents=[commit_sha])
    result["mode"] = "git-data-api"
    result["cleared"] = [t["path"] for t in top]
    return result


def build_urls(cfg: dict, repo_path: str) -> dict:
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    branch = branch_of(cfg)         # 不猜 main：认不出分支宁可不给链接，也不给死链
    if not branch:
        return {"html": "https://github.com/%s/%s" % (owner, repo), "raw": "", "cdn": ""}
    quoted = urllib.parse.quote(repo_path, safe="/")
    quoted_branch = urllib.parse.quote(branch, safe="/")
    return {
        "html": "https://github.com/%s/%s/blob/%s/%s" % (owner, repo, quoted_branch, quoted),
        "raw": "https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, quoted_branch, quoted),
        "cdn": "https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (owner, repo, quoted_branch, quoted),
    }


def gh_repo_info(cfg: dict, token: str = None) -> dict:
    return gh_api(cfg, "GET", "/repos/%s/%s" % (cfg.get("owner", ""), cfg.get("repo", "")), token=token)


def gh_list_branches(cfg: dict, token: str = None, limit: int = 100) -> dict:
    """默认分支 + 分支列表（默认分支永远排第一）。"""
    info = gh_repo_info(cfg, token)
    default = info.get("default_branch")
    count = max(1, min(int(limit or 100), 100))
    data = gh_api(cfg, "GET", "/repos/%s/%s/branches?per_page=%d"
                  % (cfg.get("owner", ""), cfg.get("repo", ""), count), token=token)
    if not isinstance(data, list):
        data = []
    names = [b.get("name") for b in data if isinstance(b, dict) and b.get("name")]
    if default:
        names = [default] + [n for n in names if n != default]
    return {
        "fullName": info.get("full_name"),
        "private": bool(info.get("private")),
        "defaultBranch": default,
        "branches": names,
    }


def gh_default_branch(cfg: dict, token: str = None, use_cache: bool = True) -> str:
    """仓库的**真实**默认分支。

    不要假设它叫 main —— 仓库的默认分支可以是任意名字（save / master / develop…），
    `GET /repos/{o}/{r}` 返回的 `default_branch` 才是权威答案。
    取不到（网络问题 / 私有仓库没 Token）时返回空串，由调用方决定怎么兜底。
    """
    key = (cfg.get("apiBase"), cfg.get("owner"), cfg.get("repo"))
    now = time.time()
    if use_cache:
        hit = _BRANCH_CACHE.get(key)
        if hit and hit[1] > now:
            return hit[0]
    try:
        info = gh_repo_info(cfg, token)
    except Exception:                                             # noqa: BLE001
        return ""
    name = str(info.get("default_branch") or "").strip()
    if name and use_cache:
        _BRANCH_CACHE[key] = (name, now + BRANCH_CACHE_SECONDS)
    return name


def gh_branch_exists(cfg: dict, token: str, branch: str) -> bool:
    """这个仓库里到底有没有这个分支（分支名带斜杠也要能查）。"""
    branch = str(branch or "").strip()
    if not branch:
        return False
    key = (cfg.get("apiBase"), cfg.get("owner"), cfg.get("repo"), branch)
    now = time.time()
    if _BRANCH_OK.get(key, 0) > now:
        return True
    api_path = "/repos/%s/%s/branches/%s" % (
        cfg.get("owner", ""), cfg.get("repo", ""), urllib.parse.quote(branch, safe=""))
    try:
        gh_api(cfg, "GET", api_path, token=token, max_attempts=1)
    except GitHubError as exc:
        if exc.status in (404, 422):
            return False
        return True                     # 网络/权限问题不能当成"分支不存在"
    _BRANCH_OK[key] = now + BRANCH_CACHE_SECONDS
    return True


def pick_branch(cfg: dict, token: str = None, explicit: str = None) -> tuple:
    """决定这次请求真正用哪个分支，返回 (分支名, 说明文字)。

    优先级：显式指定 > 配置里存的 > 仓库默认分支。

    关键是第三步之前要先校验：前两者只要在这个仓库里不存在，就退回仓库默认分支并说明原因。
    否则「默认写 main、仓库其实叫 save」这种必然报错的情况会一直报下去，
    而用户看到的只是一句 404。
    """
    wanted = str(explicit or "").strip() or str(cfg.get("branch") or "").strip()
    default = gh_default_branch(cfg, token)

    if not wanted:
        if default:
            return default, "未指定分支，已自动使用仓库默认分支 %s" % default
        # 连仓库信息都拿不到（网络断了 / 私有仓库还没配 Token）。
        # 这里**绝不能退回猜 "main"**：猜错就是一路 404，把真正的原因盖掉，
        # 用户看到的只是"分支不存在"，完全指不到病根。
        # 返回空串的语义是"交给 GitHub 自己用默认分支"（Contents API 不传 ref 就是这样）。
        return "", "拿不到这个仓库的信息（网络或权限问题），已交给 GitHub 使用默认分支"
    if not default or wanted == default:
        return wanted, ""
    if gh_branch_exists(cfg, token, wanted):
        return wanted, ""
    return default, "分支 %s 在这个仓库里不存在，已自动改用默认分支 %s" % (wanted, default)


def branch_of(cfg: dict, token: str = None, explicit: str = None) -> str:
    """这次操作真正要用的分支名。**任何情况下都不会硬猜 "main"**。

    优先级：显式传入 > 配置里存的 > 仓库的真实默认分支（走缓存，通常不多发请求）。

    三者都拿不到时返回空串，语义是"让 GitHub 自己用默认分支"——
    Contents API 不传 ref、PUT 不传 branch 时就是这个行为，比猜一个名字可靠得多。
    """
    for cand in (explicit, cfg.get("branch")):
        name = str(cand or "").strip()
        if name:
            return name
    return str(pick_branch(cfg, token)[0] or "").strip()


def gh_list_dir(cfg: dict, token: str = None, path: str = "", branch: str = None,
                limit: int = 1000) -> dict:
    """列出一个目录的内容（逐级加载，比递归拉整棵树更适合大仓库）。"""
    owner, repo = cfg.get("owner", ""), cfg.get("repo", "")
    ref = branch_of(cfg, token, branch)
    clean = "/".join(p for p in str(path or "").replace("\\", "/").split("/")
                     if p not in ("", ".", ".."))

    api_path = "/repos/%s/%s/contents" % (owner, repo)
    if clean:
        api_path += "/" + urllib.parse.quote(clean, safe="/")
    if ref:                         # 留空时干脆不带 ref，让 GitHub 用默认分支
        api_path += "?ref=" + urllib.parse.quote(ref, safe="")

    try:
        data = gh_api(cfg, "GET", api_path, token=token)
    except GitHubError as exc:
        if exc.status == 404:
            return {"path": clean, "branch": ref, "entries": [], "empty": True,
                    "truncated": False,
                    "message": "这个目录不存在，或者仓库还没有任何提交"}
        raise

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        data = []

    entries = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        entries.append({
            "name": item.get("name"),
            "path": item.get("path") or item.get("name"),
            "type": "dir" if item.get("type") == "dir" else "file",
            "size": item.get("size") or 0,
        })
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    return {"path": clean, "branch": ref, "entries": entries[:limit],
            "truncated": len(entries) > limit, "empty": False, "message": ""}


# --------------------------------------------------------------------------- #
# 运行状态
# --------------------------------------------------------------------------- #
class AppState:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = os.path.abspath(config_path)
        self.lock = threading.Lock()
        self.cfg = load_config(self.config_path)
        cfg_dir = os.path.dirname(self.config_path) or HERE
        self.history_path = os.path.join(cfg_dir, "upload-history.jsonl")
        self.history = self._load_history()

    def _load_history(self, limit: int = 100) -> list:
        items = []
        if os.path.isfile(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            items.append(json.loads(line))
                        except Exception:                             # noqa: BLE001
                            continue
            except Exception:                                         # noqa: BLE001
                pass
        return items[-limit:]

    def add_history(self, item: dict) -> None:
        with self.lock:
            self.history.append(item)
            self.history = self.history[-100:]
            try:
                with open(self.history_path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(item, ensure_ascii=False) + "\n")
            except Exception as exc:                                  # noqa: BLE001
                print("[warn] 写入历史失败: %s" % exc)


EDITABLE_FIELDS = (
    "owner", "repo", "branch", "targetDir", "apiBase",
    "commitMessage", "sanitizeNames", "autoRename", "keepFolder",
    "largeFileThresholdMB", "copyLink",
)


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
def make_handler(state: AppState, quiet: bool = True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GitHubDrop/%s" % VERSION
        protocol_version = "HTTP/1.1"

        # ---------------- 工具方法 ----------------
        def log_message(self, fmt, *args):                        # noqa: A003
            if not quiet:
                sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: bytes, ctype: str, extra=None, close=False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, obj, status: int = 200, close: bool = False):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", close=close)

        def _error(self, status: int, message: str, payload=None):
            self._json({"ok": False, "error": message, "status": status,
                        "details": payload or {}}, status, close=True)

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return b""
            if length > MAX_BODY_BYTES:
                self._error(413, "文件过大（本地单次上限 %d MB）" % (MAX_BODY_BYTES // 1024 // 1024))
                return b""
            return self.rfile.read(length)

        def _hdr(self, name: str, default: str = "") -> str:
            raw = self.headers.get(name) or default
            return urllib.parse.unquote(raw)

        def _request_token(self) -> str:
            return effective_token(state.cfg, (self.headers.get("X-GitHub-Token") or "").strip())

        # ---------------- GET ----------------
        def do_GET(self):                                          # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"

            if route in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if route == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if route == "/api/state":
                cfg_tok = (state.cfg.get("token") or "").strip()
                self._json({
                    "ok": True,
                    "version": VERSION,
                    "config": public_config(state.cfg),
                    "tokenSet": bool(effective_token(state.cfg)),
                    "tokenFromEnv": (not cfg_tok) and bool(effective_token(state.cfg)),
                    "configPath": state.config_path,
                    "history": state.history[-30:][::-1],
                })
                return
            if route == "/api/history":
                self._json({"ok": True, "history": state.history[-50:][::-1]})
                return
            if route == "/api/test":
                self._do_test(parsed.query)
                return
            if route == "/api/branches":
                self._do_branches(parsed.query)
                return
            if route == "/api/tree":
                self._do_tree(parsed.query)
                return
            if route == "/api/files":
                self._do_files(parsed.query)
                return
            self._error(404, "未知路径: %s" % route)

        # ---------------- POST ----------------
        def do_POST(self):                                         # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"

            if route == "/api/upload":
                self._do_upload()
                return
            if route == "/api/config":
                self._do_save_config()
                return
            if route == "/api/manage":
                self._do_manage()
                return
            if route == "/api/test":
                self._do_test(parsed.query)
                return
            self._read_body()
            self._error(404, "未知路径: %s" % route)

        # ---------------- 业务 ----------------
        def _override_cfg(self, query: str) -> dict:
            """用 query 参数临时覆盖 owner / repo / apiBase / branch：
            页面刚粘贴地址、还没保存时也能直接拉分支列表。

            顺便把分支定下来：query 里传了就用它，否则用配置里存的，两者都没有
            （或者在这个仓库里根本不存在）就换成仓库的真实默认分支。
            这样「仓库默认分支叫 save 而不是 main」也不会一路 404。
            定下来的分支写回 cfg，理由放在 cfg["_branchNote"]，后面的代码不用各自再猜。
            """
            params = urllib.parse.parse_qs(query or "")
            cfg = dict(state.cfg)
            for key in ("owner", "repo", "apiBase"):
                value = (params.get(key) or [""])[0].strip()
                if value:
                    cfg[key] = value
            cfg["_branchNote"] = ""
            if cfg.get("owner") and cfg.get("repo"):
                explicit = (params.get("branch") or [""])[0].strip()
                cfg["branch"], cfg["_branchNote"] = pick_branch(
                    cfg, self._request_token(), explicit)
            return cfg

        def _do_branches(self, query: str):
            try:
                cfg = self._override_cfg(query)
                if not cfg.get("owner") or not cfg.get("repo"):
                    self._error(400, "请先粘贴仓库地址，或填写 owner / repo")
                    return
                info = gh_list_branches(cfg, self._request_token())
                params = urllib.parse.parse_qs(query or "")
                candidates = params.get("candidates") or []          # 从 /tree/ 链接里带过来的候选
                payload = {"ok": True}
                payload.update(info)
                branch_hit, tree_path = resolve_tree_path(candidates, info["branches"])
                payload["resolvedBranch"] = branch_hit
                payload["resolvedPath"] = tree_path
                payload["message"] = "已加载 %d 个分支（默认 %s）" % (
                    len(info["branches"]), info.get("defaultBranch") or "?")
                self._json(payload)
            except GitHubError as exc:
                self._error(exc.status or 500, exc.message, exc.payload)

        def _do_tree(self, query: str):
            try:
                params = urllib.parse.parse_qs(query or "")
                cfg = self._override_cfg(query)
                if not cfg.get("owner") or not cfg.get("repo"):
                    self._error(400, "请先粘贴仓库地址，或填写 owner / repo")
                    return
                path = (params.get("path") or [""])[0]
                branch = cfg["branch"]
                info = gh_list_dir(cfg, self._request_token(), path=path, branch=branch)
                payload = {"ok": True}
                payload.update(info)
                note = cfg.get("_branchNote") or ""
                if info.get("message"):
                    payload["message"] = info["message"]
                else:
                    dirs = sum(1 for e in info["entries"] if e["type"] == "dir")
                    files = len(info["entries"]) - dirs
                    payload["message"] = "%s 分支：%d 个文件夹 / %d 个文件%s" % (
                        info["branch"], dirs, files, "（超出 1000 条已截断）" if info.get("truncated") else "")
                if note:
                    payload["notice"] = note
                    payload["message"] = "%s（%s）" % (payload["message"], note)
                self._json(payload)
            except GitHubError as exc:
                self._error(exc.status or 500, exc.message, exc.payload)

        def _do_files(self, query: str):
            """仓库文件管理用的列表。

            默认只列当前这一层（一个请求，秒回）；带 deep=1 才递归展开整棵子树
            （大仓库慢，但会自己找稳的路子，不容易整份作废）。
            """
            try:
                params = urllib.parse.parse_qs(query or "")
                cfg = self._override_cfg(query)
                if not cfg.get("owner") or not cfg.get("repo"):
                    self._error(400, "请先粘贴仓库地址，或填写 owner / repo")
                    return
                branch = cfg["branch"]
                prefix = (params.get("path") or [""])[0]
                deep = (params.get("deep") or ["0"])[0] in ("1", "true", "yes", "on")
                token = self._request_token()

                if deep:
                    commit_sha, tree_sha, entries, truncated, mode = gh_subtree_entries(
                        cfg, token, branch=branch, prefix=prefix)
                    raw_dirs = None
                else:
                    commit_sha, tree_sha, entries, raw_dirs = gh_list_dir_entries(
                        cfg, token, branch=branch, path=prefix)
                    truncated, mode = False, "listing"

                prefix = safe_repo_path(prefix)
                head = (prefix + "/") if prefix else ""

                files, dir_seen = [], {}
                for item in entries:
                    path = item.get("path") or ""
                    if not path or item.get("type") != "blob":
                        continue
                    rel = path[len(head):] if head and path.startswith(head) else path
                    files.append({
                        "path": path,
                        "rel": rel,
                        "name": rel.rsplit("/", 1)[-1],
                        "dir": rel.rsplit("/", 1)[0] if "/" in rel else "",
                        "size": item.get("size") or 0,
                        "sha": item.get("sha"),
                    })
                    # 收集所有中间目录，方便前端做文件夹树
                    parts = rel.split("/")[:-1]
                    for i in range(len(parts)):
                        sub = "/".join(parts[:i + 1])
                        full = (head + sub) if head else sub
                        dir_seen[full] = dir_seen.get(full, 0) + 1
                files.sort(key=lambda f: f["path"].lower())

                if raw_dirs is not None:
                    dirs = raw_dirs                      # 浅列：只有直接子文件夹，没有数量
                else:
                    dirs = [{"path": p, "name": p.rsplit("/", 1)[-1], "count": c}
                            for p, c in sorted(dir_seen.items())]

                if truncated:
                    note = "（没列完，只到 %d 个文件，建议进入子文件夹看）" % len(files)
                elif mode == "walk":
                    note = "（整棵树一次拉不下来，已逐目录扫完）"
                else:
                    note = ""
                bnote = cfg.get("_branchNote") or ""
                self._json({
                    "ok": True,
                    "branch": branch,
                    "prefix": prefix,
                    "commit": commit_sha,
                    "tree": tree_sha,
                    "deep": deep,
                    "truncated": truncated,
                    "mode": mode,
                    "files": files,
                    "dirs": dirs,
                    "totalSize": sum(f["size"] for f in files),
                    "notice": bnote,
                    "message": "%s分支 %s：%d 个文件 / %d 个文件夹%s%s" % (
                        branch, (prefix + "/" if prefix else "根目录"),
                        len(files), len(dirs), note,
                        ("（%s）" % bnote) if bnote else ""),
                })
            except GitHubError as exc:
                self._error(exc.status or 500, exc.message, exc.payload)

        def _do_manage(self):
            """仓库文件管理：删除文件 / 删除文件夹 / 重命名 / 新建文件夹 / 清空仓库。"""
            body = self._read_body()
            if not body and self.close_connection:
                return  # _read_body 已经回写了错误
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:                                          # noqa: BLE001
                self._error(400, "请求格式错误")
                return

            action = str(payload.get("action") or "").strip()
            cfg = dict(state.cfg)
            try:
                if not cfg.get("owner") or not cfg.get("repo"):
                    self._error(400, "请先配置仓库地址并保存")
                    return
                token = self._request_token()
                if not token:
                    self._error(401, "这些操作需要 GitHub Token（写入权限）")
                    return

                # 分支同样不能假设 main：显式传的 / 配置里的都不在这个仓库里，就用仓库默认分支
                branch, branch_note = pick_branch(
                    cfg, token, str(payload.get("branch") or "").strip())
                cfg["branch"] = branch
                path = safe_repo_path(payload.get("path") or "")
                new_path = safe_repo_path(payload.get("newPath") or "")
                kind = str(payload.get("kind") or "file").strip().lower()
                message = str(payload.get("message") or "").strip() or None

                lock = state.lock
                lock.acquire()
                try:
                    if action == "delete_file":
                        if not path:
                            raise GitHubError(400, "请先选择要删除的文件")
                        result = gh_delete_file(cfg, token, path, message, branch)
                        summary = "已删除文件 %s" % path
                    elif action == "delete_dir":
                        if not path:
                            raise GitHubError(400, "请先选择要删除的文件夹")
                        result = gh_delete_dir(cfg, token, path, message, branch)
                        summary = "已删除文件夹 %s（共 %d 个文件）" % (
                            path, len(result.get("deleted") or []))
                    elif action == "rename":
                        if not path or not new_path:
                            raise GitHubError(400, "请填写原路径和新路径")
                        result = gh_rename_path(cfg, token, path, new_path, kind, message, branch)
                        summary = "已重命名：%s → %s" % (path, new_path)
                    elif action == "mkdir":
                        if not path:
                            raise GitHubError(400, "请填写要新建的文件夹路径")
                        result = gh_make_dir(cfg, token, path, branch)
                        summary = "已新建文件夹 %s" % path
                    elif action == "clear":
                        if str(payload.get("confirm") or "").strip().upper() != "CLEAR":
                            raise GitHubError(400, "清空仓库需要在 confirm 字段里回填 CLEAR")
                        result = gh_clear_repo(cfg, token, branch, message)
                        summary = "已清空仓库（删除 %d 个顶层条目）" % len(
                            result.get("cleared") or [])
                    else:
                        raise GitHubError(400, "不支持的操作：%s" % (action or "(空)"))
                finally:
                    lock.release()

                item = {
                    "ok": True,
                    "action": action,
                    "branch": branch,
                    "path": path,
                    "newPath": new_path,
                    "mode": result.get("mode"),
                    "commit": result.get("commit"),
                    "message": summary,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                if branch_note:
                    item["notice"] = branch_note
                    item["message"] = "%s（%s）" % (summary, branch_note)
                # 把「到底动了哪些东西」一并回给页面，方便提示与核对
                for key in ("deleted", "cleared", "renamed", "created", "scanned"):
                    if key in result:
                        item[key] = result[key]
                state.add_history({
                    "name": "【%s】%s" % (action, summary), "path": path,
                    "size": 0, "time": item["time"], "mode": item.get("mode"),
                    "urls": build_urls(cfg, path) if path else {},
                })
                self._json(item, close=True)
            except GitHubError as exc:
                self._error(exc.status or 500, exc.message, exc.payload)
            except Exception as exc:                                   # noqa: BLE001
                self._error(500, "操作失败: %s" % exc)

        def _do_test(self, query: str = ""):
            try:
                cfg = self._override_cfg(query)
                if not cfg.get("owner") or not cfg.get("repo"):
                    self._error(400, "请先粘贴仓库地址，或填写 owner / repo")
                    return
                info = gh_repo_info(cfg, self._request_token())
                self._json({
                    "ok": True,
                    "fullName": info.get("full_name"),
                    "private": bool(info.get("private")),
                    "defaultBranch": info.get("default_branch"),
                    "permissions": info.get("permissions", {}),
                    "message": "连接成功：%s（%s，默认分支 %s）" % (
                        info.get("full_name"),
                        "私有仓库" if info.get("private") else "公开仓库",
                        info.get("default_branch") or "?"),
                })
            except GitHubError as exc:
                self._error(exc.status or 500, exc.message, exc.payload)

        def _do_save_config(self):
            body = self._read_body()
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:                                          # noqa: BLE001
                self._error(400, "配置格式错误")
                return
            incoming = payload.get("config") or {}
            parsed_url = None
            raw_url = str(incoming.get("repoUrl") or "").strip()
            if raw_url:
                parsed_url = parse_repo_url(raw_url)
                if not parsed_url:
                    self._error(400, "无法识别这个仓库地址，可以试试 https://github.com/owner/repo 或 owner/repo")
                    return
            with state.lock:
                for key in EDITABLE_FIELDS:
                    if key in incoming:
                        state.cfg[key] = incoming[key]
                if parsed_url:
                    state.cfg["owner"] = parsed_url["owner"]
                    state.cfg["repo"] = parsed_url["repo"]
                    state.cfg["apiBase"] = parsed_url["apiBase"]
                    if parsed_url.get("branch"):
                        state.cfg["branch"] = parsed_url["branch"]
                token = (payload.get("token") or "").strip()
                if token:
                    state.cfg["token"] = token
                elif payload.get("clearToken"):
                    state.cfg["token"] = ""
                state.cfg["owner"] = str(state.cfg.get("owner") or "").strip()
                state.cfg["repo"] = str(state.cfg.get("repo") or "").strip()
                # 分支留空 = 交给服务端自动用仓库的真实默认分支。
                # 这里千万别再硬填 "main"：仓库默认分支可能叫 save / master / develop，
                # 一填就把「自动识别」这条路堵死了。
                state.cfg["branch"] = str(state.cfg.get("branch") or "").strip()
                try:
                    save_config(state.cfg, state.config_path)
                except Exception as exc:                               # noqa: BLE001
                    self._error(500, "配置写入失败: %s" % exc)
                    return
                saved = bool(effective_token(state.cfg))
            self._json({"ok": True, "config": public_config(state.cfg), "tokenSet": saved,
                        "parsed": parsed_url,
                        "message": "配置已保存到 %s" % state.config_path})

        def _do_upload(self):
            content = self._read_body()
            if self.headers.get("Content-Length") is None and not content:
                self._error(411, "缺少 Content-Length")
                return
            if not content and self.close_connection:
                return  # _read_body 已经回写了错误

            raw_name = self._hdr("X-File-Name")
            if not raw_name:
                self._error(400, "缺少 X-File-Name 请求头")
                return

            try:
                cfg = dict(state.cfg)
                if not cfg.get("owner") or not cfg.get("repo"):
                    self._error(400, "请先在页面上填写 owner / repo 并保存配置")
                    return

                rel_path = self._hdr("X-Rel-Path")
                target_dir = self._hdr("X-Target-Dir")
                keep_rel = self.headers.get("X-Keep-Rel")
                if keep_rel is not None:
                    cfg = dict(cfg, keepFolder=keep_rel not in ("0", "false", "no"))

                token = self._request_token()
                if not token:
                    self._error(401, "缺少 GitHub Token")
                    return

                # 上传也必须落在真实存在的分支上：配置里没写分支 / 写了个不存在的分支，
                # 都用仓库的默认分支兜住（否则会一直报 branch not found）
                branch, branch_note = pick_branch(cfg, token)
                cfg["branch"] = branch

                repo_path = build_repo_path(
                    cfg, raw_name, rel_path,
                    target_dir=target_dir if target_dir else None)

                lock = state.lock
                lock.acquire()
                try:
                    repo_path = resolve_conflict_path(cfg, repo_path, token)
                    started = time.time()
                    result = gh_upload(cfg, token, repo_path, content)
                finally:
                    lock.release()

                item = {
                    "ok": True,
                    "name": raw_name,
                    "path": result["path"],
                    "branch": branch,
                    "size": len(content),
                    "mode": result.get("mode"),
                    "sha": result.get("sha"),
                    "commit": result.get("commit"),
                    "seconds": round(time.time() - started, 2),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "urls": build_urls(cfg, result["path"]),
                }
                if branch_note:
                    item["notice"] = branch_note
                state.add_history({
                    "name": raw_name, "path": item["path"], "size": item["size"],
                    "time": item["time"], "urls": item["urls"], "mode": item["mode"],
                })
                self._json(item, close=True)
            except GitHubError as exc:
                self._error(exc.status or 500, exc.message, exc.payload)
            except Exception as exc:                                   # noqa: BLE001
                self._error(500, "上传失败: %s" % exc)

    return Handler


def create_server(host: str, port: int, config_path: str = DEFAULT_CONFIG_PATH,
                  quiet: bool = True):
    state = AppState(config_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(state, quiet=quiet))
    httpd.daemon_threads = True
    return httpd, state


def port_is_serving(host: str, port: int, timeout: float = 0.4) -> bool:
    """端口上是否已经有服务在跑。

    这里用「连一下」判断，而不是「试着绑一下」，两条原因都很实在：

    1. Windows 的 SO_REUSEADDR 允许两个 socket 绑到同一个地址端口。如果用
       setsockopt(SO_REUSEADDR)+bind 去试探，port 已被占也会探测成「空闲」，
       于是第二次启动会静默绑上同一个端口 —— 两个实例一起监听，新开的标签页
       可能连到另一个进程，进度/历史看起来就「不见了」。
    2. 绑测试还会被上一次退出留下的 TIME_WAIT 干扰，导致明明没人在跑却白顺延。

    连接测试只回答「有没有人在监听」，语义正好，也不受 TIME_WAIT 影响。
    """
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((probe_host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, start: int, span: int = 20) -> int:
    for port in range(start, start + span):
        if not port_is_serving(host, port):
            return port
    raise SystemExit("端口 %d ~ %d 都被占用了" % (start, start + span))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="%s v%s - 拖拽文件自动上传到 GitHub 仓库" % (APP_NAME, VERSION))
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765，若被占用自动顺延）")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="打印访问日志")
    args = parser.parse_args(argv)

    # 中文输出必须跟随控制台自身的编码（Windows 中文系统通常是 cp936）。
    # 强行把 stdout 改成 utf-8 会让控制台里的中文全变乱码，这里只在必要时兜底。
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                              # noqa: BLE001
        pass

    if not os.path.isfile(args.config):
        try:
            save_config(default_config(), args.config)
        except Exception as exc:                                   # noqa: BLE001
            print("[warn] 无法创建配置文件: %s" % exc)

    port = pick_port(args.host, args.port)
    if port != args.port:
        print("[提示] 端口 %d 上已经有实例在运行（可能是你之前打开的那个窗口），" % args.port)
        print("       本次改用端口 %d，两个页面互不干扰。" % port)
        print("       想固定端口可以加 --port 指定一个没被占用的值。")
        print("")
    httpd, state = create_server(args.host, port, args.config, quiet=not args.verbose)
    url = "http://%s:%d/" % (args.host, httpd.server_address[1])

    print("=" * 62)
    print("  %s v%s" % (APP_NAME, VERSION))
    print("  页面地址 : %s" % url)
    print("  配置文件 : %s" % state.config_path)
    print("  目标仓库 : %s" % ("%s/%s @ %s" % (state.cfg.get("owner") or "?",
                                             state.cfg.get("repo") or "?",
                                             state.cfg.get("branch") or "自动（仓库默认分支）")))
    print("  目标目录 : %s" % state.cfg.get("targetDir"))
    print("  按 Ctrl+C 退出")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# 前端页面
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub Drop · 拖拽即上传</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --line:#e3e6ea; --text:#1f2328; --muted:#656d76;
    --accent:#1f6feb; --accent-soft:#ddeaff; --ok:#1a7f37; --ok-soft:#dcfce7;
    --err:#cf222e; --err-soft:#ffe9e9; --warn:#9a6700; --warn-soft:#fff8c5;
    --radius:12px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;}
  .wrap{max-width:960px;margin:0 auto;padding:28px 20px 64px}
  header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  h1{font-size:22px;margin:0;letter-spacing:.2px}
  h1 span{color:var(--accent)}
  .sub{color:var(--muted);font-size:13px;margin-top:4px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(27,31,36,.04)}
  .card h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;letter-spacing:.3px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
  label.f{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
  input[type=text],input[type=password],input[type=number],select{
    width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
    background:#fff;color:var(--text);font-size:13px;font-family:inherit;outline:none}
  input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .row.between{justify-content:space-between}
  button{cursor:pointer;font-family:inherit;font-size:13px;border-radius:8px;
    border:1px solid var(--line);background:#fff;color:var(--text);padding:8px 14px;transition:.15s}
  button:hover{border-color:#c9ced6;background:#fafbfc}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{background:#1a5fd0}
  button:disabled{opacity:.5;cursor:not-allowed}
  .switch{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);cursor:pointer;user-select:none}
  .switch input{accent-color:var(--accent);width:15px;height:15px}
  #drop{border:2px dashed #c3c9d1;border-radius:var(--radius);background:#fbfcfd;
    padding:38px 20px;text-align:center;transition:.18s;cursor:pointer}
  #drop:hover{border-color:#a8b1bb;background:#f7f9fb}
  #drop.hot{border-color:var(--accent);background:var(--accent-soft);transform:scale(1.004)}
  #drop .big{font-size:16px;font-weight:600;margin-bottom:6px}
  #drop .hint{color:var(--muted);font-size:12.5px}
  #drop .ico{width:42px;height:42px;margin:0 auto 12px;opacity:.85}
  .list{margin-top:14px;display:flex;flex-direction:column;gap:8px}
  .item{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fff}
  .item .top{display:flex;justify-content:space-between;gap:12px;align-items:center}
  .item .nm{font-weight:600;font-size:13px;word-break:break-all}
  .item .meta{color:var(--muted);font-size:12px;margin-top:2px;word-break:break-all}
  .bar{height:4px;background:#eef0f3;border-radius:99px;overflow:hidden;margin-top:8px}
  .bar > i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
  .chip{font-size:11.5px;padding:2px 8px;border-radius:99px;white-space:nowrap;border:1px solid transparent}
  .chip.wait{background:#f2f4f6;color:var(--muted)}
  .chip.up{background:var(--accent-soft);color:var(--accent)}
  .chip.ok{background:var(--ok-soft);color:var(--ok)}
  .chip.err{background:var(--err-soft);color:var(--err)}
  .links{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .links a,.links button{font-size:12px;padding:4px 10px;border-radius:6px;text-decoration:none}
  .links a{border:1px solid var(--line);color:var(--accent)}
  .links a:hover{background:var(--accent-soft)}
  .hist td,.hist th{padding:6px 10px;border-bottom:1px solid var(--line);font-size:12.5px;text-align:left}
  .hist th{color:var(--muted);font-weight:600}
  .hist a{color:var(--accent);text-decoration:none}
  .crumbs{font-size:12.5px;color:var(--muted);margin-bottom:8px;line-height:1.9}
  .crumbs a{color:var(--accent);cursor:pointer;text-decoration:none}
  .crumbs a:hover{text-decoration:underline}
  .crumbs b{color:var(--text)}
  .tree{border:1px solid var(--line);border-radius:10px;max-height:250px;overflow:auto;background:#fff}
  .tree .row-i{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid #f2f4f6;font-size:13px}
  .tree .row-i:last-child{border-bottom:none}
  .tree .dir{cursor:pointer}
  .tree .dir:hover{background:var(--accent-soft)}
  .tree .nm{flex:1;word-break:break-all}
  .tree .file .nm{color:var(--muted)}
  .tree .cnt{font-size:11.5px;color:var(--muted);white-space:nowrap}
  .btnmini{font-size:11.5px;padding:2px 9px;border-radius:6px;border:1px solid var(--line);
    background:#fff;cursor:pointer;color:var(--text);white-space:nowrap}
  .btnmini:hover{border-color:var(--accent);color:var(--accent)}
  .ico2{width:15px;text-align:center;flex:none}
  .empty{color:var(--muted);font-size:13px;text-align:center;padding:14px}
  #toast{position:fixed;right:22px;bottom:22px;display:flex;flex-direction:column;gap:8px;z-index:99}
  .toast{background:#1f2328;color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;
    box-shadow:0 6px 18px rgba(0,0,0,.18);max-width:380px;animation:in .2s ease}
  .toast.ok{background:var(--ok)} .toast.err{background:var(--err)}
  @keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:1px 8px}
  details summary{cursor:pointer;color:var(--muted);font-size:13px;outline:none}
  details[open] summary{margin-bottom:12px}

  /* 危险操作按钮 */
  button.danger{color:var(--err);border-color:#f0c2c6}
  button.danger:hover{background:var(--err-soft);border-color:var(--err)}
  button.danger:disabled{color:var(--muted);border-color:var(--line)}

  /* 仓库文件管理列表 */
  .frow{display:flex;align-items:center;gap:9px;padding:6px 10px;border-bottom:1px solid #f2f4f6;font-size:13px}
  .frow:last-child{border-bottom:none}
  .frow:hover{background:#fafbfc}
  .frow input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent);flex:none;margin:0}
  .frow .ico2{width:18px;text-align:center;flex:none}
  .frow .nm{flex:1;word-break:break-all}
  .frow .nm em{color:var(--muted);font-style:normal}
  .frow .cnt{font-size:11.5px;color:var(--muted);white-space:nowrap}
  .frow .ops{display:flex;gap:6px;flex:none}
  .mtag{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--accent-soft);
    color:var(--accent);border:1px solid transparent;white-space:nowrap}
  .mtag.dir{background:#fff4d6;color:var(--warn)}
  .mgridhead{display:flex;align-items:center;gap:9px;padding:6px 10px;background:#f6f8fa;
    border-bottom:1px solid var(--line);font-size:11.5px;color:var(--muted);font-weight:600}
  .mgridhead label{display:flex;align-items:center;gap:7px;cursor:pointer}

  /* 弹窗 */
  .modal-back{position:fixed;inset:0;background:rgba(31,35,40,.45);display:none;
    align-items:center;justify-content:center;z-index:200;padding:20px}
  .modal-back.on{display:flex}
  .modal{background:#fff;border-radius:var(--radius);border:1px solid var(--line);
    max-width:470px;width:100%;padding:18px;box-shadow:0 18px 48px rgba(0,0,0,.22)}
  .modal h3{margin:0 0 10px;font-size:15px}
  .modal p{margin:0 0 10px;font-size:13px;color:var(--muted);word-break:break-all}
  .modal .warnbox{background:var(--err-soft);border:1px solid #f5c2c7;border-radius:8px;
    padding:10px;font-size:12.5px;color:#8b1a24;margin-bottom:10px;line-height:1.7}
  .modal .warnbox b{color:#6e1218}
  .modal input[type=text]{margin-bottom:4px}
  .modal .hintline{font-size:12px;color:var(--muted);margin-bottom:10px}
  .modal .row.act{justify-content:flex-end;margin-top:14px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>GitHub <span>Drop</span></h1>
      <div class="sub" id="targetLine">正在读取配置…</div>
    </div>
    <div class="row">
      <span class="tag" id="verTag">v-</span>
      <button id="btnTest">测试连接</button>
      <button id="btnOpenRepo" disabled>打开仓库目录</button>
    </div>
  </header>

  <div class="card">
    <details id="cfgBox">
      <summary>仓库配置（第一次使用先填这里）</summary>
      <div style="margin-bottom:14px">
        <label class="f">仓库地址 —— 粘贴后自动识别 owner / repo，连链接里的分支也认</label>
        <div class="row">
          <input type="text" id="c_repoUrl" autocomplete="off"
                 placeholder="https://github.com/owner/repo  ·  也可以直接写 owner/repo"
                 style="flex:1;min-width:280px">
          <button id="btnLoadBranches">加载分支</button>
        </div>
        <div class="sub" id="urlHint" style="margin-top:6px">
          支持完整链接、带 .git 的、SSH 地址（git@github.com:owner/repo.git）、/tree/分支 链接，以及 owner/repo 简写
        </div>
      </div>
      <div class="grid">
        <div><label class="f">所有者 owner</label><input type="text" id="c_owner" placeholder="例如 octocat"></div>
        <div><label class="f">仓库 repo</label><input type="text" id="c_repo" placeholder="例如 my-assets"></div>
        <div>
          <label class="f">分支 branch</label>
          <input type="text" id="c_branch" list="branchOptions" placeholder="留空＝自动识别" autocomplete="off">
          <datalist id="branchOptions"></datalist>
        </div>
        <div><label class="f">目标目录（支持模板）</label><input type="text" id="c_targetDir" placeholder="uploads/{date}"></div>
        <div style="grid-column:1/-1">
          <label class="f">分支里的文件夹（加载分支后自动显示，点文件夹进去，点「选它」直接用）</label>
          <div class="row between" style="margin-bottom:8px">
            <div class="crumbs" id="treeCrumbs" style="margin-bottom:0">还没加载 —— 先粘贴仓库地址、填 Token，然后点上面的「加载分支」</div>
            <div class="row" style="flex:none">
              <input type="text" id="treeFilter" placeholder="筛选当前目录…" style="max-width:170px">
              <button id="btnTreeReload" class="btnmini" style="padding:8px 10px">刷新</button>
            </div>
          </div>
          <div class="tree" id="treeList"><div class="row-i"><span class="nm">—</span></div></div>
          <div class="row" style="margin-top:10px">
            <button id="btnTreeRoot">回到根目录</button>
            <button id="btnTreeAsDefault">把当前目录设为默认目标</button>
            <button id="btnTreeOpen">在 GitHub 打开</button>
            <span class="sub" id="treeHint"></span>
          </div>
        </div>
        <div style="grid-column:1/-1">
          <label class="f">GitHub Token（fine-grained 需 Contents: Read and write）</label>
          <div class="row">
            <input type="password" id="c_token" placeholder="ghp_… 或 github_pat_…（留空则沿用已保存的）" style="flex:1;min-width:240px">
            <label class="switch"><input type="checkbox" id="c_remember"> 保存到 config.json</label>
            <button id="btnClearToken">清除</button>
          </div>
        </div>
        <div style="grid-column:1/-1">
          <label class="f">提交信息模板</label>
          <input type="text" id="c_commitMessage" placeholder="chore(upload): add {name}">
        </div>
        <div style="grid-column:1/-1">
          <label class="f">API 地址（GitHub Enterprise / 镜像可改）</label>
          <input type="text" id="c_apiBase" placeholder="https://api.github.com">
        </div>
      </div>
      <div class="row" style="margin-top:14px">
        <button class="primary" id="btnSave">保存配置</button>
        <span class="sub" id="cfgHint"></span>
      </div>
    </details>
  </div>

  <div class="card">
    <h2>拖进来就上传</h2>
    <div id="drop">
      <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="#1f6feb" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/>
        <path d="M3.5 15v3A2.5 2.5 0 0 0 6 20.5h12a2.5 2.5 0 0 0 2.5-2.5v-3"/>
      </svg>
      <div class="big">把文件或文件夹拖到这里</div>
      <div class="hint">拖入文件夹会在仓库里复刻同名文件夹（含子目录）· 也可用下面的按钮选择</div>
      <input type="file" id="fileInput" multiple style="display:none">
      <input type="file" id="dirInput" webkitdirectory directory multiple style="display:none">
    </div>

    <div class="row" style="margin-top:14px">
      <div style="flex:1;min-width:300px">
        <label class="f">本次传到哪儿（留空就用上面的默认模板）</label>
        <div class="row">
          <input type="text" id="o_dir" placeholder="默认：使用上面的目标目录模板" style="flex:1;min-width:140px">
          <select id="o_dirPick" style="max-width:200px" title="从仓库里已有的文件夹中直接挑一个">
            <option value="">— 从仓库文件夹里挑 —</option>
          </select>
          <button id="btnBrowseDir">浏览仓库目录</button>
        </div>
      </div>
      <label class="switch" title="关闭后所有文件都平铺到目标目录">
        <input type="checkbox" id="o_keep"> 保留文件夹结构</label>
      <label class="switch" title="空目录 Git 无法保存，用一个 .gitkeep 文件占位">
        <input type="checkbox" id="o_gitkeep" checked> 空文件夹放 .gitkeep</label>
      <label class="switch"><input type="checkbox" id="o_rename"> 同名自动重命名</label>
      <label class="switch"><input type="checkbox" id="o_copy"> 完成后自动复制链接</label>
    </div>

    <div class="row between" style="margin-top:14px">
      <div class="row">
        <button class="primary" id="btnStart">开始上传</button>
        <button id="btnPick">选择文件</button>
        <button id="btnPickDir">选择文件夹</button>
        <button id="btnClear">清空列表</button>
      </div>
      <span class="sub" id="queueInfo"></span>
    </div>

    <div class="list" id="list"><div class="empty">还没有待上传的文件</div></div>
  </div>

  <div class="card">
    <h2>仓库文件管理 —— 删除 / 重命名 / 新建 / 清空</h2>
    <div class="row between" style="margin-bottom:8px">
      <div class="crumbs" id="fileCrumbs" style="margin-bottom:0">
        还没加载 —— 点右边的「加载文件列表」
      </div>
      <div class="row" style="flex:none">
        <input type="text" id="fileFilter" placeholder="筛选…" style="max-width:150px">
        <label class="switch" title="仓库大时会慢一些：把子文件夹也一起列出来">
          <input type="checkbox" id="fileDeep"> 连子文件夹一起列</label>
        <button class="primary" id="btnFilesLoad">加载文件列表</button>
      </div>
    </div>

    <div class="tree" id="filesList" style="max-height:360px">
      <div class="row-i"><span class="ico2">—</span><span class="nm">点「加载文件列表」把仓库里的文件列出来</span></div>
    </div>

    <div class="row" style="margin-top:10px">
      <button class="danger" id="btnDelFiles">删除选中的文件</button>
      <button id="btnRename">重命名…</button>
      <button id="btnMkdir">新建文件夹…</button>
      <button id="btnDelDir">删除整个文件夹…</button>
      <button class="danger" id="btnClearRepo">清空整个仓库…</button>
      <span class="sub" id="filesInfo"></span>
    </div>
    <div class="row" style="margin-top:6px">
      <label class="switch"><input type="checkbox" id="fileOnlyDirs"> 只看文件夹</label>
      <span class="sub" id="manageHint"></span>
    </div>
    <div class="sub" style="margin-top:8px">
      默认只列当前这一层（快）；勾上「连子文件夹一起列」才会把整棵子树展开。
      点文件夹进去，点「上传到此」把它设成本次上传目标。删除、清空都不可撤销，动手前会再确认一遍。
    </div>
  </div>

  <div class="card">
    <h2>最近上传</h2>
    <div id="histWrap"><div class="empty">暂无记录</div></div>
  </div>
</div>
<div id="modal" class="modal-back">
  <div class="modal">
    <h3 id="modalTitle">确认</h3>
    <div id="modalBody"></div>
    <div class="row act">
      <button id="modalCancel">取消</button>
      <button class="primary" id="modalOk">确定</button>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
let CFG = {}, TOKEN = '', QUEUE = [], SEQ = 0, RUNNING = false, TOKEN_SET = false,
    URL_TIMER = null, PARSED = null;

/* ---------- 小工具 ---------- */
function fmtSize(n){
  if(n < 1024) return n + ' B';
  if(n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  if(n < 1024*1024*1024) return (n/1024/1024).toFixed(2) + ' MB';
  return (n/1024/1024/1024).toFixed(2) + ' GB';
}
function toast(msg, kind){
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3200);
  setTimeout(() => el.remove(), 3600);
}
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function copyText(text){
  if(navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
  return Promise.resolve();
}
function pad(n){ return String(n).padStart(2,'0'); }
function previewPath(name, rel){
  const tpl = ($('#o_dir').value.trim() || CFG.targetDir || 'uploads/{date}');
  const d = new Date();
  const stem = name.replace(/\.[^.]+$/, ''), ext = (name.match(/\.([^.]+)$/) || ['',''])[1];
  const map = {
    date: d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()),
    yyyy: String(d.getFullYear()), mm: pad(d.getMonth()+1), dd: pad(d.getDate()),
    time: pad(d.getHours())+pad(d.getMinutes())+pad(d.getSeconds()),
    timestamp: String(Math.floor(d.getTime()/1000)),
    name: stem, ext: ext, filename: name
  };
  let out = tpl.replace(/\{(\w+)\}/g, (m,k) => (k in map ? map[k] : m));
  const dirs = out.split('/').filter(Boolean);
  if(rel && $('#o_keep').checked){
    const parts = rel.split('/').filter(Boolean);
    parts.pop();
    return dirs.concat(parts, [name]).join('/');
  }
  return dirs.concat([name]).join('/');
}

/* ---------- 仓库地址解析（与后端 parse_repo_url 同规则，后端为准） ---------- */
const RESERVED_OWNERS = new Set(['orgs','users','settings','marketplace','topics','collections',
  'sponsors','features','about','pricing','explore','notifications','new','login','apps','site',
  'security','enterprise','search','trending','codespaces','dashboard','pulls','issues','account',
  'organizations','sessions','logout']);
const BRANCH_PATH_HEADS = new Set(['tree','blob','commits','commit','branches','raw','edit']);
const TREE_LIKE = new Set(['tree']);
const FILE_LIKE = new Set(['blob','raw','edit']);
const BRANCH_ONLY = new Set(['commits','commit']);

function parseRepoUrl(text){
  let raw = String(text || '').trim().replace(/^["'<]+/, '').replace(/["'>]+$/, '');
  if(!raw) return null;
  let m = raw.match(/(?:https?|ssh|git):\/\/[^\s]+/);
  if(m) raw = m[0];
  else { m = raw.match(/[^\s/]+@[^\s:]+[:/][^\s]+/); if(m) raw = m[0]; }

  let host = 'github.com', rest = '';
  if(raw.startsWith('git@') || raw.startsWith('ssh://')){
    const scp = raw.match(/^(?:ssh:\/\/)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+)$/);
    if(!scp) return null;
    host = scp[1].toLowerCase(); rest = scp[2];
  } else if(raw.indexOf('://') >= 0){
    let u;
    try { u = new URL(raw); } catch(e){ return null; }
    host = u.hostname.toLowerCase(); rest = u.pathname;
  } else {
    const parts = raw.split('/').filter(Boolean);
    if(!parts.length) return null;
    if(parts[0].indexOf('.') >= 0){ host = parts[0].toLowerCase(); rest = parts.slice(1).join('/'); }
    else { host = 'github.com'; rest = parts.join('/'); }
  }
  const segs = rest.split('/').filter(Boolean).map(s => {
    try { return decodeURIComponent(s); } catch(e){ return s; }
  });
  if(segs.length < 2) return null;
  const owner = segs[0];
  let repo = segs[1];
  if(RESERVED_OWNERS.has(owner.toLowerCase())) return null;
  if(repo.toLowerCase().endsWith('.git')) repo = repo.slice(0, -4);
  if(!repo) return null;
  let branch = null, candidates = [], treePath = '';
  if(segs.length >= 3 && BRANCH_PATH_HEADS.has(segs[2].toLowerCase())){
    const head = segs[2].toLowerCase();
    const tail = segs.slice(3);
    if(TREE_LIKE.has(head) && tail.length){
      for(let i = tail.length; i >= 1; i--) candidates.push(tail.slice(0, i).join('/'));
      treePath = candidates[0];
    } else if(FILE_LIKE.has(head) && tail.length >= 2){
      candidates = [tail.slice(0, -1).join('/')];
    } else if(BRANCH_ONLY.has(head) && tail.length){
      candidates = [tail.join('/')];
    }
    branch = candidates.length ? candidates[0] : null;
  }
  const apiBase = (host === 'github.com' || host === 'www.github.com')
    ? 'https://api.github.com' : ('https://' + host + '/api/v3');
  return {owner: owner, repo: repo, branch: branch, branchCandidates: candidates,
          treePath: treePath, host: host, apiBase: apiBase};
}

function applyRepoUrl(opts){
  opts = opts || {};
  const raw = $('#c_repoUrl').value.trim();
  const hint = $('#urlHint');
  if(!raw){
    hint.textContent = '支持完整链接、带 .git 的、SSH 地址（git@github.com:owner/repo.git）、/tree/分支 链接，以及 owner/repo 简写';
    hint.style.color = '';
    return null;
  }
  const parsed = parseRepoUrl(raw);
  if(!parsed){
    hint.textContent = '✗ 没认出这是仓库地址：至少要有 owner/repo 两段';
    hint.style.color = 'var(--err)';
    PARSED = null;
    return null;
  }
  PARSED = parsed;
  $('#c_owner').value = parsed.owner;
  $('#c_repo').value = parsed.repo;
  if(parsed.branch) $('#c_branch').value = parsed.branch;
  const curApi = $('#c_apiBase').value.trim();
  if(parsed.host !== 'github.com' || !curApi || curApi === 'https://api.github.com'){
    $('#c_apiBase').value = parsed.apiBase;
  }
  const ambiguous = parsed.branchCandidates && parsed.branchCandidates.length > 1;
  hint.textContent = '✓ 已识别 ' + parsed.owner + '/' + parsed.repo
    + (parsed.branch ? ('　分支：' + parsed.branch) : '')
    + (parsed.host !== 'github.com' ? ('　·　' + parsed.host) : '')
    + (ambiguous ? '　（分支名可能带斜杠，加载分支列表会自动校正）' : '');
  hint.style.color = 'var(--ok)';
  updateTarget();
  if(opts.loadBranches) loadBranches(true);
  return parsed;
}

function debouncedRepoUrl(){
  clearTimeout(URL_TIMER);
  URL_TIMER = setTimeout(() => applyRepoUrl({loadBranches: hasToken()}), 400);
}
function hasToken(){
  return !!($('#c_token').value.trim() || TOKEN || TOKEN_SET);
}
/* 没填 Token 时也能看公开仓库，但要把原因说清楚 */
function anonHint(msg){
  return hasToken() ? msg : (msg + '（没填 Token 就只能读公开仓库，私有仓库请先填 Token）');
}

async function loadBranches(silent){
  const owner = $('#c_owner').value.trim();
  const repo = $('#c_repo').value.trim();
  const apiBase = $('#c_apiBase').value.trim();
  const tok = $('#c_token').value.trim() || TOKEN;
  if(!owner || !repo){ if(!silent) toast('先粘贴仓库地址，或填 owner / repo', 'err'); return null; }

  const btn = $('#btnLoadBranches');
  const oldText = btn.textContent;
  btn.disabled = true; btn.textContent = '加载中…';
  try {
    const qs = new URLSearchParams({owner: owner, repo: repo});
    if(apiBase) qs.set('apiBase', apiBase);
    if(PARSED && PARSED.branchCandidates && PARSED.branchCandidates.length){
      PARSED.branchCandidates.forEach(c => qs.append('candidates', c));   // 交给后端做最长前缀匹配
    }
    const res = await fetch('/api/branches?' + qs.toString(),
      {headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(!res.ok){
      if(!silent) toast(anonHint('加载分支失败：' + (res.error || '')), 'err');
      return null;
    }
    const dl = $('#branchOptions');
    dl.innerHTML = res.branches.map(b => '<option value="' + esc(b) + '"></option>').join('');
    const before = $('#c_branch').value.trim();
    const guess = (PARSED && PARSED.branch) || '';
    let note = '', noteKind = 'ok';
    if(res.resolvedBranch){
      $('#c_branch').value = res.resolvedBranch;
      note = '从链接里认出分支：' + res.resolvedBranch;
    } else if(!before){
      $('#c_branch').value = res.defaultBranch || '';
    } else if(res.branches.indexOf(before) < 0){
      if(guess && before === guess){
        $('#c_branch').value = res.defaultBranch || '';
        note = res.defaultBranch
          ? ('链接里的分支在仓库里没找到，已改用默认分支 ' + res.defaultBranch)
          : '链接里的分支在仓库里没找到，已清空分支（留空＝自动用仓库默认分支）';
        noteKind = 'err';
      } else {
        dl.innerHTML += '<option value="' + esc(before) + '"></option>';   // 保住手填的分支
      }
    }
    PARSED = null;
    toast(note || res.message || ('已加载 ' + res.branches.length + ' 个分支'), noteKind);
    updateTarget();
    // 顺手把分支里的文件夹显示出来；链接里带了目录就直接进到那一层
    let wantPath = res.resolvedPath || '';
    if(wantPath){
      const probe = await loadTree(wantPath, true);
      if(probe && probe.empty){
        // 链接可能指向的是一个文件（/tree/…/app.py），退到它所在的文件夹
        wantPath = wantPath.split('/').filter(Boolean).slice(0, -1).join('/');
        await loadTree(wantPath, true);
      }
    } else {
      loadTree('', true);
    }
    if(wantPath){
      $('#o_dir').value = wantPath;
      updateTarget();
      toast('链接里的目录：' + wantPath, 'ok');
    }
    // 顺手把仓库文件列表也拉出来，喂给「从仓库文件夹里挑」和文件管理
    if(hasToken() && !FILES.loaded) loadFiles(true);
    return res;
  } catch(e){
    if(!silent) toast('加载分支失败：' + e, 'err');
    return null;
  } finally {
    btn.disabled = false; btn.textContent = oldText;
  }
}

/* ---------- 状态加载 ---------- */
async function loadState(){
  const res = await fetch('/api/state').then(r => r.json());
  CFG = res.config || {};
  TOKEN_SET = !!res.tokenSet;
  $('#verTag').textContent = 'v' + res.version;
  $('#c_owner').value = CFG.owner || '';
  $('#c_repo').value = CFG.repo || '';
  $('#c_branch').value = CFG.branch || '';
  $('#c_targetDir').value = CFG.targetDir || 'uploads/{date}';
  $('#c_commitMessage').value = CFG.commitMessage || 'chore(upload): add {name}';
  $('#c_apiBase').value = CFG.apiBase || 'https://api.github.com';
  $('#c_token').placeholder = TOKEN_SET
    ? '已保存 Token（留空则继续使用）'
    : 'ghp_… 或 github_pat_…';
  $('#o_keep').checked = CFG.keepFolder !== false;
  $('#o_rename').checked = !!CFG.autoRename;
  $('#o_copy').checked = CFG.copyLink !== false;
  const saved = localStorage.getItem('gd_token');
  if(saved){ $('#c_token').value = saved; TOKEN = saved; $('#c_remember').checked = true; }
  if(res.tokenFromEnv){
    $('#c_token').placeholder = '已通过环境变量 GITHUB_TOKEN 提供（留空即可）';
    $('#cfgHint').textContent = 'Token 来自环境变量 GITHUB_TOKEN，不会写入 config.json';
  }
  if(CFG.owner && CFG.repo) $('#c_repoUrl').value = 'https://github.com/' + CFG.owner + '/' + CFG.repo;
  updateTarget();
  renderHistory(res.history || []);
  if(!CFG.owner || !CFG.repo){ $('#cfgBox').open = true; }
  else if(hasToken()) loadBranches(true);
}
function updateTarget(){
  const repo = (CFG.owner && CFG.repo) ? (CFG.owner + '/' + CFG.repo) : '未配置仓库';
  const dir = $('#o_dir').value.trim() || CFG.targetDir || 'uploads/{date}';
  const br = $('#c_branch').value.trim() || '自动（仓库默认分支）';
  $('#targetLine').textContent = '目标：' + repo + ' @ ' + br + '  →  ' + dir + '/';
  const ok = !!(CFG.owner && CFG.repo);
  $('#btnOpenRepo').disabled = !ok;
}

/* ---------- 保存配置 ---------- */
async function saveConfig(extra){
  const body = {
    config: {
      repoUrl: $('#c_repoUrl').value.trim(),
      owner: $('#c_owner').value.trim(),
      repo: $('#c_repo').value.trim(),
      branch: $('#c_branch').value.trim(),
      targetDir: $('#c_targetDir').value.trim() || 'uploads/{date}',
      commitMessage: $('#c_commitMessage').value.trim() || 'chore(upload): add {name}',
      apiBase: $('#c_apiBase').value.trim() || 'https://api.github.com',
      keepFolder: $('#o_keep').checked,
      autoRename: $('#o_rename').checked,
      copyLink: $('#o_copy').checked
    }
  };
  const tok = $('#c_token').value.trim();
  if(tok){
    TOKEN = tok;
    if($('#c_remember').checked){ body.token = tok; localStorage.setItem('gd_token', tok); }
    else { localStorage.removeItem('gd_token'); }
  }
  if(extra && extra.clearToken) body.clearToken = true;
  const res = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
  }).then(r => r.json());
  if(res.ok){
    CFG = res.config; TOKEN_SET = !!res.tokenSet;
    // 用服务端归一化后的结果回填（地址解析以后端为准）
    $('#c_owner').value = CFG.owner || '';
    $('#c_repo').value = CFG.repo || '';
    $('#c_branch').value = CFG.branch || '';
    $('#c_targetDir').value = CFG.targetDir || '';
    $('#c_apiBase').value = CFG.apiBase || 'https://api.github.com';
    if(CFG.owner && CFG.repo) $('#c_repoUrl').value = 'https://github.com/' + CFG.owner + '/' + CFG.repo;
    updateTarget();
    if(extra && extra.clearToken){ TOKEN = ''; localStorage.removeItem('gd_token'); $('#c_token').value = ''; }
    toast(res.message || '配置已保存', 'ok');
    if(res.tokenSet || tok) loadBranches(true);
  } else {
    toast(res.error || '保存失败', 'err');
    if($('#c_repoUrl').value.trim()){
      $('#urlHint').textContent = '✗ ' + (res.error || '地址无法识别');
      $('#urlHint').style.color = 'var(--err)';
    }
  }
}

/* ---------- 仓库目录浏览器 ---------- */
let TREE = {path: '', entries: [], branch: '', loaded: false};

async function loadTree(path, silent){
  const owner = $('#c_owner').value.trim(), repo = $('#c_repo').value.trim();
  // 留空就交给服务端自己认（它会用仓库的真实默认分支），别在这里替它填 main
  const branch = $('#c_branch').value.trim();
  const apiBase = $('#c_apiBase').value.trim();
  const tok = $('#c_token').value.trim() || TOKEN;
  if(!owner || !repo){ if(!silent) toast('先粘贴仓库地址，或填 owner / repo', 'err'); return null; }

  const qs = new URLSearchParams({owner: owner, repo: repo, branch: branch, path: path || ''});
  if(apiBase) qs.set('apiBase', apiBase);
  $('#treeList').innerHTML = '<div class="row-i"><span class="ico2">…</span><span class="nm">加载中…</span></div>';
  try {
    const res = await fetch('/api/tree?' + qs.toString(),
      {headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(!res.ok){
      $('#treeList').innerHTML = '<div class="row-i"><span class="ico2">✗</span><span class="nm" style="color:#cf222e">'
        + esc(res.error || '加载失败') + '</span></div>';
      if(!silent) toast(anonHint('加载目录失败：' + (res.error || '')), 'err');
      return null;
    }
    TREE = {path: res.path || '', entries: res.entries || [], branch: res.branch || branch,
            loaded: true, res: res};
    $('#treeFilter').value = '';
    renderTree();
    return res;
  } catch(e){
    $('#treeList').innerHTML = '<div class="row-i"><span class="ico2">✗</span><span class="nm" style="color:#cf222e">'
      + esc(String(e)) + '</span></div>';
    if(!silent) toast('加载目录失败：' + e, 'err');
    return null;
  }
}

function renderTree(){
  const res = TREE.res || {};
  const parts = (TREE.path || '').split('/').filter(Boolean);
  let crumb = '<a data-go="">仓库根目录</a>';
  let acc = '';
  parts.forEach(p => {
    acc = acc ? acc + '/' + p : p;
    crumb += '　/　<a data-go="' + esc(acc) + '">' + esc(p) + '</a>';
  });
  $('#treeCrumbs').innerHTML = '<span>分支 <b>' + esc(TREE.branch) + '</b>：</span>' + crumb;

  const kw = ($('#treeFilter').value || '').trim().toLowerCase();
  const all = TREE.entries || [];
  const list = kw ? all.filter(e => String(e.name).toLowerCase().indexOf(kw) >= 0) : all;

  const rows = list.map(e => {
    if(e.type === 'dir'){
      return '<div class="row-i dir" data-enter="' + esc(e.path) + '">'
        + '<span class="ico2">📁</span><span class="nm">' + esc(e.name) + '</span>'
        + '<button class="btnmini" data-pick="' + esc(e.path) + '">选它</button></div>';
    }
    return '<div class="row-i file"><span class="ico2">📄</span><span class="nm">' + esc(e.name)
      + '</span><span class="cnt">' + fmtSize(e.size || 0) + '</span></div>';
  });
  if(!rows.length){
    rows.push('<div class="row-i"><span class="ico2">∅</span><span class="nm">'
      + esc(kw ? ('没有匹配「' + kw + '」的条目') : (res.message || '这里还没有文件夹')) + '</span></div>');
  }
  $('#treeList').innerHTML = rows.join('');
  $('#treeList').querySelectorAll('[data-enter]').forEach(el => {
    el.onclick = () => enterDir(el.getAttribute('data-enter'));
  });
  $('#treeList').querySelectorAll('[data-pick]').forEach(el => {
    el.onclick = (ev) => { ev.stopPropagation(); pickDir(el.getAttribute('data-pick')); };
  });
  $('#treeCrumbs').querySelectorAll('[data-go]').forEach(el => {
    el.onclick = () => loadTree(el.getAttribute('data-go'));
  });
  $('#treeHint').textContent = kw
    ? ('筛选出 ' + list.length + ' / ' + all.length + ' 条')
    : (res.message || '');
}

function enterDir(path){ return loadTree(path); }

function pickDir(path){
  $('#o_dir').value = path || '';
  updateTarget();
  toast(path ? ('本次上传到 ' + path + '/') : '已切回默认目录', 'ok');
}

function setTreeAsDefault(){
  $('#c_targetDir').value = TREE.path || '';
  updateTarget();
  toast('默认目标目录已设为 ' + (TREE.path ? TREE.path + '/' : '仓库根目录') + '，记得点「保存配置」', 'ok');
}

function ghWebHost(){
  const api = ($('#c_apiBase').value.trim() || CFG.apiBase || 'https://api.github.com');
  try {
    const h = new URL(api).hostname;
    return (h === 'api.github.com') ? 'github.com' : h;
  } catch(e){ return 'github.com'; }
}

/* 页面自己在拼 GitHub 链接时需要具体分支名：优先用服务端算出来的那个 */
function liveBranch(){
  return ($('#c_branch').value.trim() || FILES.branch || TREE.branch || CFG.branch || '').trim();
}

function openTreeOnGitHub(){
  if(!(CFG.owner && CFG.repo)){ toast('先粘贴仓库地址', 'err'); return; }
  const branch = liveBranch();
  const host = 'https://' + ghWebHost() + '/' + CFG.owner + '/' + CFG.repo;
  // 分支还没认出来时退到仓库首页，总比拼一个错分支的 404 链接好
  const url = branch ? (host + '/tree/' + encodeURI(branch) + (TREE.path ? '/' + encodeURI(TREE.path) : '')) : host;
  window.open(url, '_blank');
}

/* ---------- 队列 ---------- */
function addItems(items, empties){
  empties = empties || [];
  if(!items.length && !empties.length) return;
  // 拖入文件夹时自动开启「保留文件夹结构」
  if(items.some(it => (it.rel || '').indexOf('/') >= 0) && !$('#o_keep').checked){
    $('#o_keep').checked = true;
    toast('检测到文件夹，已自动开启「保留文件夹结构」', 'ok');
  }
  let n = 0, f = 0;
  for(const it of items){
    const dup = QUEUE.some(q => q.file.name === it.file.name && q.file.size === it.file.size && q.rel === it.rel);
    if(dup) continue;
    QUEUE.push({id: ++SEQ, file: it.file, rel: it.rel || '', status: 'wait', pct: 0, result: null, error: ''});
    n++;
  }
  // 空目录：Git 不保存空目录，用一个 0 字节 .gitkeep 占位
  if($('#o_gitkeep').checked){
    for(const rel of empties){
      const keep = rel.replace(/\/+$/, '') + '/.gitkeep';
      if(QUEUE.some(q => q.rel === keep)) continue;
      let fileObj;
      try { fileObj = new File([''], '.gitkeep', {type: 'text/plain'}); }
      catch(e){ fileObj = new Blob([''], {type: 'text/plain'}); fileObj.name = '.gitkeep'; }
      QUEUE.push({id: ++SEQ, file: fileObj, rel: keep, status: 'wait', pct: 0, result: null, error: ''});
      n++; f++;
    }
  }
  renderQueue();
  if(n) toast('已加入 ' + (n - f) + ' 个文件' + (f ? (' · ' + f + ' 个空文件夹占位') : ''), 'ok');
}
function renderQueue(){
  const box = $('#list');
  if(!QUEUE.length){ box.innerHTML = '<div class="empty">还没有待上传的文件</div>'; $('#queueInfo').textContent = ''; return; }
  box.innerHTML = QUEUE.map(q => {
    const chip = {wait:['wait','等待'], up:['up','上传中'], ok:['ok','完成'], err:['err','失败']}[q.status];
    const links = q.result ? ('<div class="links">' +
      (q.result.urls.raw ? '<a href="' + q.result.urls.raw + '" target="_blank">直链</a>' : '') +
      (q.result.urls.html ? '<a href="' + q.result.urls.html + '" target="_blank">GitHub</a>' : '') +
      (q.result.urls.cdn ? '<a href="' + q.result.urls.cdn + '" target="_blank">CDN</a>' : '') +
      (q.result.urls.raw ? '<button data-copy="' + esc(q.result.urls.raw) + '">复制直链</button>' : '') + '</div>') : '';
    return '<div class="item">' +
      '<div class="top"><div><div class="nm">' + esc(q.rel || q.file.name) + '</div>' +
      '<div class="meta">' + fmtSize(q.file.size) + ' · → ' + esc(previewPath(q.file.name, q.rel)) +
      (q.result ? ' · ' + q.result.seconds + 's · ' + (q.result.mode === 'git-data-api' ? '大文件通道' : 'Contents API') : '') +
      (q.error ? ' · <span style="color:#cf222e">' + esc(q.error) + '</span>' : '') +
      '</div></div><span class="chip ' + chip[0] + '">' + chip[1] + '</span></div>' +
      '<div class="bar"><i style="width:' + Math.round(q.pct*100) + '%' +
      (q.status === 'ok' ? ';background:#1a7f37' : q.status === 'err' ? ';background:#cf222e' : '') + '"></i></div>' +
      links + '</div>';
  }).join('');
  box.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => {
    copyText(b.getAttribute('data-copy')).then(() => toast('链接已复制', 'ok'));
  });
  const done = QUEUE.filter(q => q.status === 'ok').length;
  const fail = QUEUE.filter(q => q.status === 'err').length;
  $('#queueInfo').textContent = QUEUE.length + ' 个文件' + (done ? ' · 成功 ' + done : '') + (fail ? ' · 失败 ' + fail : '');
}

function postFile(item, onProgress){
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(item.file.name));
    if(item.rel && $('#o_keep').checked) xhr.setRequestHeader('X-Rel-Path', encodeURIComponent(item.rel));
    const dir = $('#o_dir').value.trim();
    if(dir) xhr.setRequestHeader('X-Target-Dir', encodeURIComponent(dir));
    xhr.setRequestHeader('X-Keep-Rel', $('#o_keep').checked ? '1' : '0');
    const tok = $('#c_token').value.trim() || TOKEN;
    if(tok) xhr.setRequestHeader('X-GitHub-Token', tok);
    xhr.upload.onprogress = e => { if(e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch(e){ data = {ok:false, error:'服务端返回异常'}; }
      if(xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
      else reject(data.error || ('HTTP ' + xhr.status));
    };
    xhr.onerror = () => reject('网络错误：无法连接本地服务');
    xhr.ontimeout = () => reject('上传超时');
    xhr.send(item.file);
  });
}

async function startUpload(){
  if(RUNNING) return;
  const pending = QUEUE.filter(q => q.status === 'wait' || q.status === 'err');
  if(!pending.length){ toast('没有待上传的文件', 'err'); return; }
  if(!(($('#c_owner').value.trim()) && ($('#c_repo').value.trim()))){ toast('请先填写 owner / repo', 'err'); $('#cfgBox').open = true; return; }
  if(!(($('#c_token').value.trim()) || TOKEN || TOKEN_SET)){ toast('请先填写 GitHub Token', 'err'); $('#cfgBox').open = true; return; }
  RUNNING = true;
  $('#btnStart').disabled = true; $('#btnStart').textContent = '上传中…';
  let lastLink = '';
  for(const item of pending){
    item.status = 'up'; item.pct = 0; item.error = '';
    renderQueue();
    try {
      const res = await postFile(item, p => { item.pct = p; renderQueue(); });
      item.status = 'ok'; item.pct = 1; item.result = res;
      lastLink = res.urls.raw;
    } catch(err){
      item.status = 'err'; item.pct = 1; item.error = String(err);
    }
    renderQueue();
  }
  RUNNING = false;
  $('#btnStart').disabled = false; $('#btnStart').textContent = '开始上传';
  const fail = QUEUE.filter(q => q.status === 'err').length;
  toast(fail ? ('上传结束，' + fail + ' 个失败') : '全部上传完成', fail ? 'err' : 'ok');
  if(!fail && lastLink && $('#o_copy').checked) copyText(lastLink).then(() => toast('已复制最后一个文件的直链', 'ok'));
  loadHistory();
}

/* ---------- 文件夹拖拽 ---------- */
function readAllEntries(reader){
  return new Promise(resolve => {
    const acc = [];
    const step = () => reader.readEntries(batch => {
      if(!batch.length) return resolve(acc);
      acc.push(...batch); step();
    }, () => resolve(acc));
    step();
  });
}
function entryFile(entry){ return new Promise((res, rej) => entry.file(res, rej)); }
async function walkEntry(entry, prefix, out, empties){
  if(entry.isFile){
    try { const f = await entryFile(entry); out.push({file: f, rel: prefix + entry.name}); } catch(e){}
  } else if(entry.isDirectory){
    const kids = await readAllEntries(entry.createReader());
    const here = prefix + entry.name + '/';
    if(!kids.length){ empties.push(here); return; }
    for(const it of kids) await walkEntry(it, here, out, empties);
  }
}
async function itemsFromDataTransfer(dt){
  const out = [], empties = [];
  const items = dt.items ? Array.from(dt.items) : [];
  const entries = items.map(i => i.webkitGetAsEntry ? i.webkitGetAsEntry() : null).filter(Boolean);
  if(entries.length){
    for(const e of entries) await walkEntry(e, '', out, empties);
    if(out.length || empties.length) return {items: out, empties: empties};
  }
  for(const f of Array.from(dt.files || [])) out.push({file: f, rel: f.webkitRelativePath || ''});
  return {items: out, empties: empties};
}

/* ---------- 历史 ---------- */
function renderHistory(items){
  if(!items.length){ $('#histWrap').innerHTML = '<div class="empty">暂无记录</div>'; return; }
  const rows = items.map(it =>
    '<tr><td>' + esc(it.time || '') + '</td><td>' + esc(it.path) + '</td>' +
    '<td>' + fmtSize(it.size || 0) + '</td>' +
    '<td><a href="' + ((it.urls && it.urls.raw) || '#') + '" target="_blank">直链</a>' +
    ' · <a href="' + ((it.urls && it.urls.html) || '#') + '" target="_blank">GitHub</a></td></tr>').join('');
  $('#histWrap').innerHTML = '<table class="hist" style="width:100%;border-collapse:collapse">' +
    '<tr><th>时间</th><th>仓库路径</th><th>大小</th><th>链接</th></tr>' + rows + '</table>';
}
async function loadHistory(){
  const res = await fetch('/api/history').then(r => r.json()).catch(() => ({history: []}));
  renderHistory(res.history || []);
}

/* ---------- 弹窗（确认 / 输入） ---------- */
let MODAL_CTX = null;
function openModal(opts){
  const fields = opts.fields || [];
  let body = '';
  if(opts.warn) body += '<div class="warnbox">' + opts.warn + '</div>';
  if(opts.html) body += opts.html;
  fields.forEach((f, i) => {
    body += '<label class="f">' + esc(f.label || f.name) + '</label>'
      + '<input type="text" id="m_in' + i + '" value="' + esc(f.value || '') + '"'
      + ' placeholder="' + esc(f.placeholder || '') + '" autocomplete="off">';
  });
  $('#modalTitle').textContent = opts.title || '确认';
  $('#modalBody').innerHTML = body;
  const ok = $('#modalOk');
  ok.textContent = opts.okText || '确定';
  ok.className = opts.danger ? 'danger' : 'primary';
  ok.disabled = false;
  $('#modal').classList.add('on');
  fields.forEach((f, i) => {
    const el = document.querySelector('#m_in' + i);
    if(el) el.value = f.value || '';
  });
  const first = document.querySelector('#m_in0');
  if(first && first.focus) setTimeout(() => first.focus(), 30);
  return new Promise(resolve => { MODAL_CTX = {fields: fields, resolve: resolve}; });
}
function modalOk(){
  if(!MODAL_CTX) return;
  const ctx = MODAL_CTX;
  const vals = ctx.fields.map((f, i) => {
    const el = document.querySelector('#m_in' + i);
    return el ? String(el.value || '').trim() : '';
  });
  MODAL_CTX = null;
  $('#modal').classList.remove('on');
  ctx.resolve(vals);
}
function modalCancel(){
  if(!MODAL_CTX){ $('#modal').classList.remove('on'); return; }
  const ctx = MODAL_CTX;
  MODAL_CTX = null;
  $('#modal').classList.remove('on');
  ctx.resolve(null);
}
/* 确认框：确定 -> []，取消 -> null */
function askConfirm(opts){ return openModal(opts); }

/* ---------- 仓库文件管理 ---------- */
let FILES = {loaded: false, files: [], dirs: [], branch: '', path: '', truncated: false,
             deep: false, res: null};
let SEL = new Set();

function curBranch(){ return $('#c_branch').value.trim(); }   // 空 = 让服务端自动用仓库默认分支

async function loadFiles(silent, path, deep){
  const owner = $('#c_owner').value.trim(), repo = $('#c_repo').value.trim();
  if(!owner || !repo){ if(!silent) toast('先粘贴仓库地址，或填 owner / repo', 'err'); return null; }
  if(path === undefined) path = FILES.path || '';
  if(deep === undefined) deep = $('#fileDeep').checked;

  const tok = $('#c_token').value.trim() || TOKEN;
  const btn = $('#btnFilesLoad'), old = btn.textContent;
  btn.disabled = true; btn.textContent = deep ? '扫描中…' : '加载中…';
  $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">…</span><span class="nm">'
    + (deep ? '正在把子文件夹也一起列出来，仓库大时会慢一点…' : '加载中…') + '</span></div>';
  try {
    const qs = new URLSearchParams({owner: owner, repo: repo, branch: curBranch(), path: path || ''});
    if(deep) qs.set('deep', '1');
    const apiBase = $('#c_apiBase').value.trim();
    if(apiBase) qs.set('apiBase', apiBase);
    const res = await fetch('/api/files?' + qs.toString(),
      {headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(!res.ok){
      FILES = {loaded: false, files: [], dirs: [], branch: '', path: '', truncated: false,
               deep: false, res: null};
      SEL = new Set();
      $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">✗</span>'
        + '<span class="nm" style="color:var(--err)">' + esc(res.error || '加载失败') + '</span></div>';
      if(!silent) toast(anonHint('加载文件失败：' + (res.error || '')), 'err');
      return null;
    }
    FILES = {loaded: true, files: res.files || [], dirs: res.dirs || [],
             branch: res.branch || curBranch(), path: res.prefix || '',
             truncated: !!res.truncated, deep: !!res.deep, res: res};
    SEL = new Set();
    if(!deep) $('#fileFilter').value = '';
    fillDirPicker();
    renderFiles();
    if(!silent) toast(res.message || ('已列出 ' + FILES.files.length + ' 个文件'), 'ok');
    return res;
  } catch(e){
    $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">✗</span>'
      + '<span class="nm" style="color:var(--err)">' + esc(String(e)) + '</span></div>';
    if(!silent) toast('加载文件失败：' + e, 'err');
    return null;
  } finally { btn.disabled = false; btn.textContent = old; }
}

/* 把仓库里已有的文件夹塞进「本次传到哪儿」的下拉，挑一个就填进输入框 */
function fillDirPicker(){
  const sel = $('#o_dirPick');
  if(!sel) return;
  let html = '<option value="__pick__">— 从仓库文件夹里挑 —</option>'
    + '<option value="__default__">（用默认目标模板）</option>'
    + '<option value="">（仓库根目录）</option>';
  (FILES.dirs || []).forEach(d => {
    const n = (d.count === null || d.count === undefined) ? '' : '（' + d.count + '）';
    html += '<option value="' + esc(d.path) + '">' + esc(d.path) + '/' + n + '</option>';
  });
  sel.innerHTML = html;
  sel.value = '__pick__';
}

function onDirPickChange(){
  const v = $('#o_dirPick').value;
  if(v === '__pick__') return;
  if(v === '__default__') $('#o_dir').value = '';
  else $('#o_dir').value = v;
  updateTarget(); renderQueue();
  const d = $('#o_dir').value.trim();
  toast(d ? ('本次上传到 ' + d + '/') : '已切回默认目标目录', 'ok');
  $('#o_dirPick').value = '__pick__';
}

function fileEnterDir(path){ return loadFiles(false, path, $('#fileDeep').checked); }

function fileCrumbs(){
  const parts = (FILES.path || '').split('/').filter(Boolean);
  let crumb = '<a data-fgo="">仓库根目录</a>';
  let acc = '';
  parts.forEach(p => {
    acc = acc ? acc + '/' + p : p;
    crumb += '　/　<a data-fgo="' + esc(acc) + '">' + esc(p) + '</a>';
  });
  $('#fileCrumbs').innerHTML = '<span>分支 <b>' + esc(FILES.branch || curBranch()) + '</b>：</span>' + crumb;
  $('#fileCrumbs').querySelectorAll('[data-fgo]').forEach(el => {
    el.onclick = () => fileEnterDir(el.getAttribute('data-fgo'));
  });
}

function renderFiles(){
  if(!FILES.loaded){
    $('#filesList').innerHTML = '<div class="row-i"><span class="ico2">—</span>'
      + '<span class="nm">点「加载文件列表」把仓库里的文件列出来</span></div>';
    $('#fileCrumbs').textContent = '还没加载 —— 点右边的「加载文件列表」';
    $('#filesInfo').textContent = '';
    updateSelInfo();
    return;
  }
  fileCrumbs();

  const kw = ($('#fileFilter').value || '').trim().toLowerCase();
  const onlyDirs = $('#fileOnlyDirs').checked;
  const hit = (s) => !kw || String(s).toLowerCase().indexOf(kw) >= 0;
  const dList = (FILES.dirs || []).filter(d => hit(d.path));
  // 浅列时文件都在当前层；深列时带上相对路径
  const fList = onlyDirs ? [] : (FILES.files || []).filter(f => hit(FILES.deep ? f.path : f.name));

  const rows = [];
  if(FILES.deep){
    rows.push('<div class="mgridhead"><label><input type="checkbox" id="fileSelAll"> 全选（按当前筛选）</label>'
      + '<span style="flex:1"></span><span>' + fList.length + ' 文件 / ' + dList.length + ' 文件夹</span></div>');
  } else {
    rows.push('<div class="mgridhead"><label><input type="checkbox" id="fileSelAll"> 全选</label>'
      + '<span style="flex:1"></span><span>当前目录 ' + fList.length + ' 文件 / '
      + dList.length + ' 子文件夹</span></div>');
  }

  dList.forEach(d => {
    const n = (d.count === null || d.count === undefined) ? '' : (' · ' + d.count + ' 项');
    rows.push('<div class="frow" data-path="' + esc(d.path) + '">'
      + '<span class="ico2">📁</span>'
      + '<span class="nm" style="cursor:pointer" data-enter="' + esc(d.path) + '">'
      + esc(FILES.deep ? d.path : d.name) + '/<span class="mtag dir">文件夹' + n + '</span></span>'
      + '<span class="ops">'
      + (FILES.deep ? '' : '<button class="btnmini" data-act="enter" data-path="' + esc(d.path) + '">进入</button>')
      + '<button class="btnmini" data-act="target" data-path="' + esc(d.path) + '">上传到此</button>'
      + '<button class="btnmini" data-act="rename" data-kind="dir" data-path="' + esc(d.path) + '">重命名</button>'
      + '<button class="btnmini danger" data-act="del_dir" data-path="' + esc(d.path) + '">删除</button>'
      + '</span></div>');
  });

  fList.forEach(f => {
    const label = FILES.deep ? f.path : f.name;
    rows.push('<div class="frow" data-path="' + esc(f.path) + '">'
      + '<input type="checkbox" class="fchk" data-path="' + esc(f.path) + '"'
      + (SEL.has(f.path) ? ' checked' : '') + '>'
      + '<span class="ico2">📄</span>'
      + '<span class="nm">' + esc(label) + '</span>'
      + '<span class="cnt">' + fmtSize(f.size || 0) + '</span>'
      + '<span class="ops">'
      + '<button class="btnmini" data-act="rename" data-kind="file" data-path="' + esc(f.path) + '">重命名</button>'
      + '<button class="btnmini danger" data-act="del_file" data-path="' + esc(f.path) + '">删除</button>'
      + '</span></div>');
  });

  if(!dList.length && !fList.length){
    rows.push('<div class="row-i"><span class="ico2">∅</span><span class="nm">'
      + esc(kw ? ('没有匹配「' + kw + '」的内容')
               : (FILES.path ? '这个文件夹是空的' : '这个分支里还没有文件')) + '</span></div>');
  }
  $('#filesList').innerHTML = rows.join('');
  $('#filesInfo').textContent = (FILES.res && FILES.res.message) || '';

  const box = $('#filesList');
  box.querySelectorAll('[data-act]').forEach(el => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      rowAction(el.getAttribute('data-act'), el.getAttribute('data-path'), el.getAttribute('data-kind'));
    };
  });
  box.querySelectorAll('[data-enter]').forEach(el => {
    el.onclick = () => fileEnterDir(el.getAttribute('data-enter'));
  });
  box.querySelectorAll('.fchk').forEach(el => {
    el.onchange = () => {
      const p = el.getAttribute('data-path');
      if(el.checked) SEL.add(p); else SEL.delete(p);
      updateSelInfo();
    };
  });
  const sa = $('#fileSelAll');
  if(sa) sa.onchange = () => {
    const paths = fList.map(f => f.path);
    box.querySelectorAll('.fchk').forEach(el => { el.checked = sa.checked; });
    paths.forEach(p => { if(sa.checked) SEL.add(p); else SEL.delete(p); });
    updateSelInfo();
  };
  updateSelInfo();
}

function updateSelInfo(){
  const n = SEL.size;
  const del = $('#btnDelFiles'), ren = $('#btnRename');
  if(del) del.disabled = n === 0;
  if(ren) ren.disabled = n !== 1;
  $('#manageHint').textContent = n ? ('已勾选 ' + n + ' 个文件') : '';
}

function selectedFiles(){ return Array.from(SEL); }

async function manageAction(action, extra, successMsg, silent){
  const tok = $('#c_token').value.trim() || TOKEN;
  const body = Object.assign({action: action, branch: curBranch()}, extra || {});
  const res = await fetch('/api/manage', {
    method: 'POST',
    headers: Object.assign({'Content-Type': 'application/json'},
                           tok ? {'X-GitHub-Token': tok} : {}),
    body: JSON.stringify(body),
  }).then(r => r.json());
  if(!res.ok){
    const msg = res.error || '操作失败';
    if(!silent) toast(msg, 'err');
    const err = new Error(msg); err.status = res.status; err.res = res;
    throw err;
  }
  if(!silent) toast(successMsg || res.message || '操作完成', 'ok');
  return res;
}

function rowAction(act, path, kind){
  if(act === 'enter') return loadFiles(false, path, false);
  if(act === 'target'){
    $('#o_dir').value = path || '';
    updateTarget(); renderQueue();
    toast(path ? ('本次上传到 ' + path + '/') : '已切回默认（仓库根目录）', 'ok');
    return;
  }
  if(act === 'rename') return askRename(path, kind || 'file');
  if(act === 'del_file') return askDeleteFile(path);
  if(act === 'del_dir') return askDeleteDir(path);
}

async function askRename(path, kind){
  const isDir = kind === 'dir';
  const parts = String(path).split('/');
  const base = parts.pop() || '';
  const parent = parts.join('/');
  const vals = await openModal({
    title: '重命名' + (isDir ? '文件夹' : '文件'),
    html: '<p>原路径：<b>' + esc(path) + '</b>' + (isDir ? '/' : '') + '</p>',
    fields: [{name: 'newName',
              label: isDir ? '新文件夹名（含 / 表示顺便换位置）' : '新文件名（含 / 表示顺便移动）',
              value: base, placeholder: base}],
    okText: '重命名',
  });
  if(!vals) return;
  const newName = (vals[0] || '').replace(/^\/+|\/+$/g, '');
  if(!newName){ toast('新名字不能为空', 'err'); return; }
  const dst = newName.indexOf('/') >= 0 ? newName : (parent ? parent + '/' + newName : newName);
  if(dst === path){ toast('新名字和原来一样，没有改动', 'err'); return; }
  await manageAction('rename', {path: path, newPath: dst, kind: kind},
                     '已重命名：' + path + ' → ' + dst);
  // 改名的正是当前所在目录（或它的父级）就回到上一层，免得列表对不上
  if(FILES.path === path || FILES.path.indexOf(path + '/') === 0){
    FILES.path = path.split('/').slice(0, -1).join('/');
  }
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askDeleteFile(path){
  const ok = await askConfirm({
    title: '删除文件',
    warn: '⚠️ 删除后虽然提交历史里还能翻到，但仓库里就没这个文件了。',
    html: '<p>要删除的文件：<b>' + esc(path) + '</b></p>',
    okText: '确认删除', danger: true,
  });
  if(!ok) return;
  try {
    await manageAction('delete_file', {path: path}, '已删除文件：' + path);
  } catch(e) { /* 已经提示过 */ }
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askDeleteDir(path){
  const info = (FILES.dirs || []).find(d => d.path === path);
  const n = (info && info.count !== null && info.count !== undefined) ? (info.count + ' 个') : '全部';
  const ok = await askConfirm({
    title: '删除文件夹',
    warn: '⚠️ 会把这个文件夹下的 <b>' + n + '</b> 文件全部删掉，无法撤销。<br>'
      + 'Git 不保存空目录，内容删完目录本身也就没了。',
    html: '<p>要删除的文件夹：<b>' + esc(path) + '/</b></p>',
    fields: [{name: 'confirmPath', label: '防误删：把文件夹路径再完整打一遍',
              value: '', placeholder: path}],
    okText: '确认删除整个文件夹', danger: true,
  });
  if(!ok) return;
  if((ok[0] || '') !== path){
    toast('路径打得不一致，已取消删除（需完整输入 ' + path + '）', 'err');
    return;
  }
  try {
    const res = await manageAction('delete_dir', {path: path}, null);
    toast('已删除文件夹 ' + path + '（' + ((res.deleted || []).length) + ' 个文件）', 'ok');
  } catch(e) { /* 已经提示过 */ }
  // 刚删掉的正是当前所在目录（或它的父级）就回根目录
  if(FILES.path === path || FILES.path.indexOf(path + '/') === 0) FILES.path = '';
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askDeleteDirHere(){
  if(!FILES.loaded){ toast('先加载文件列表', 'err'); return; }
  if(!FILES.path){ toast('已经在仓库根目录了。要删某个子文件夹，点它那一行的「删除」', 'err'); return; }
  const target = FILES.path;
  let n = FILES.files.length;
  if(!FILES.deep){
    // 浅列时不知道里面有多少，先诚实地说「不确定」
    n = null;
  }
  const ok = await askConfirm({
    title: '删除当前文件夹',
    warn: '⚠️ 会删掉 <b>' + esc(target) + '/</b> 里面的'
      + (n === null ? '所有文件（会先扫一遍再删，可能稍慢）' : (' <b>' + n + ' 个</b>文件'))
      + '，无法撤销。',
    html: '<p>要删除的文件夹：<b>' + esc(target) + '/</b></p>',
    fields: [{name: 'confirmPath', label: '防误删：把文件夹路径再完整打一遍',
              value: '', placeholder: target}],
    okText: '确认删除整个文件夹', danger: true,
  });
  if(!ok) return;
  if((ok[0] || '') !== target){
    toast('路径打得不一致，已取消删除（需完整输入 ' + target + '）', 'err');
    return;
  }
  const btn = $('#btnDelDir'), old = btn.textContent;
  btn.disabled = true; btn.textContent = '删除中…';
  try {
    const res = await manageAction('delete_dir', {path: target}, null, true);
    toast('已删除文件夹 ' + target + '（' + ((res.deleted || []).length) + ' 个文件）', 'ok');
    FILES.path = '';
  } catch(e){
    toast('删除失败：' + ((e && e.message) || e), 'err');
  }
  btn.disabled = false; btn.textContent = old;
  await loadFiles(true, '', $('#fileDeep').checked);
  loadHistory();
}

async function askDeleteSelected(){
  const sel = selectedFiles();
  if(!sel.length){ toast('先在列表里勾选要删除的文件', 'err'); return; }
  const list = sel.slice(0, 12).map(p => '<br>· ' + esc(p)).join('');
  const more = sel.length > 12 ? ('<br>… 还有 ' + (sel.length - 12) + ' 个') : '';
  const ok = await askConfirm({
    title: '删除选中的 ' + sel.length + ' 个文件',
    warn: '⚠️ 删除后无法撤销。',
    html: '<p>将删除：' + list + more + '</p>',
    okText: '确认删除这 ' + sel.length + ' 个', danger: true,
  });
  if(!ok) return;
  let done = 0; const failed = [];
  const btn = $('#btnDelFiles'), old = btn.textContent;
  btn.disabled = true; btn.textContent = '删除中…';
  for(const p of sel){
    try { await manageAction('delete_file', {path: p}, null, true); done++; }
    catch(e){ failed.push(p + '：' + ((e && e.message) || e)); }
  }
  btn.disabled = false; btn.textContent = old;
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
  if(failed.length) toast('删除了 ' + done + ' 个，失败 ' + failed.length + ' 个：' + failed[0], 'err');
  else toast('已删除 ' + done + ' 个文件', 'ok');
  loadHistory();
}

async function askRenameSelected(){
  const sel = selectedFiles();
  if(sel.length !== 1){ toast('重命名一次只针对一个文件，请刚好勾选 1 个', 'err'); return; }
  await askRename(sel[0], 'file');
}

async function askMkdir(){
  const vals = await openModal({
    title: '新建文件夹',
    html: '<p>Git 存不了空目录，所以会放一个 <b>.gitkeep</b> 占位，这样子目录才留得住。</p>',
    fields: [{name: 'path', label: '文件夹路径（可带层级）', value: '', placeholder: 'assets/2026/秋'}],
    okText: '新建',
  });
  if(!vals) return;
  const p = (vals[0] || '').replace(/^\/+|\/+$/g, '');
  if(!p){ toast('路径不能为空', 'err'); return; }
  try {
    await manageAction('mkdir', {path: p}, '已新建文件夹：' + p);
  } catch(e) { /* 已经提示过 */ }
  await loadFiles(true, FILES.path, $('#fileDeep').checked);
}

async function askClearRepo(){
  const owner = $('#c_owner').value.trim(), repo = $('#c_repo').value.trim();
  if(!owner || !repo){ toast('先在配置里填好仓库地址', 'err'); return; }
  const ok = await askConfirm({
    title: '清空整个仓库',
    warn: '⚠️⚠️ 这会删掉 <b>' + esc(owner + '/' + repo) + '</b> 里 <b>' + esc(curBranch() || liveBranch() || '默认分支')
      + '</b> 分支上的<b>全部文件和文件夹</b>，仓库会变成空的。',
    html: '<p>真要继续的话，请把 <b>CLEAR</b> 打进下面的框里：</p>',
    fields: [{name: 'confirm', label: '输入 CLEAR 确认', value: '', placeholder: 'CLEAR'}],
    okText: '我确定，清空仓库', danger: true,
  });
  if(!ok) return;
  if((ok[0] || '').toUpperCase() !== 'CLEAR'){
    toast('没有输入 CLEAR，已取消', 'err');
    return;
  }
  try {
    const res = await manageAction('clear', {confirm: 'CLEAR'}, null);
    toast(res.message || '已清空仓库', 'ok');
  } catch(e) { /* 已经提示过 */ }
  FILES.path = '';
  await loadFiles(true, '', $('#fileDeep').checked);
  loadHistory();
}

/* ---------- 事件绑定 ---------- */
const drop = $('#drop');
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); e.stopPropagation(); drop.classList.add('hot');
}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); e.stopPropagation();
  if(ev === 'dragleave' && e.relatedTarget && drop.contains(e.relatedTarget)) return;
  drop.classList.remove('hot');
}));
drop.addEventListener('drop', async e => {
  const r = await itemsFromDataTransfer(e.dataTransfer);
  addItems(r.items, r.empties);
});
drop.addEventListener('click', () => $('#fileInput').click());
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', async e => {
  e.preventDefault();
  if(!e.dataTransfer) return;
  const r = await itemsFromDataTransfer(e.dataTransfer);
  addItems(r.items, r.empties);
});
$('#fileInput').addEventListener('change', e => {
  const files = Array.from(e.target.files || []);
  addItems(files.map(f => ({file: f, rel: f.webkitRelativePath || ''})));
  e.target.value = '';
});
$('#dirInput').addEventListener('change', e => {
  const files = Array.from(e.target.files || []);
  addItems(files.map(f => ({file: f, rel: f.webkitRelativePath || f.name})));
  e.target.value = '';
});
$('#btnPick').addEventListener('click', () => $('#fileInput').click());
$('#btnPickDir').addEventListener('click', () => $('#dirInput').click());
$('#c_repoUrl').addEventListener('input', debouncedRepoUrl);
$('#c_repoUrl').addEventListener('keydown', e => {
  if(e.key === 'Enter'){ clearTimeout(URL_TIMER); applyRepoUrl({loadBranches: hasToken()}); }
});
$('#btnLoadBranches').addEventListener('click', () => loadBranches(false));
$('#c_branch').addEventListener('input', updateTarget);
$('#c_branch').addEventListener('change', () => {
  if(TREE.loaded) loadTree(TREE.path, true);
  if(FILES.loaded) loadFiles(true);
});
$('#btnTreeRoot').addEventListener('click', () => loadTree(''));
$('#btnTreeReload').addEventListener('click', () => loadTree(TREE.path));
$('#treeFilter').addEventListener('input', renderTree);
$('#btnTreeAsDefault').addEventListener('click', setTreeAsDefault);
$('#btnTreeOpen').addEventListener('click', openTreeOnGitHub);
$('#btnBrowseDir').addEventListener('click', () => {
  $('#cfgBox').open = true;
  loadTree(TREE.loaded ? TREE.path : '', false);
  const box = $('#treeList');
  if(box && box.scrollIntoView) box.scrollIntoView({block: 'center'});
});
$('#c_owner').addEventListener('blur', () => { if(hasToken() && $('#c_repo').value.trim()) loadBranches(true); });
$('#c_repo').addEventListener('blur', () => { if(hasToken() && $('#c_owner').value.trim()) loadBranches(true); });
$('#btnStart').addEventListener('click', startUpload);
$('#btnClear').addEventListener('click', () => { QUEUE = []; renderQueue(); });
$('#btnSave').addEventListener('click', () => saveConfig());
$('#btnClearToken').addEventListener('click', () => saveConfig({clearToken: true}));
$('#o_dir').addEventListener('input', () => { updateTarget(); renderQueue(); });
$('#o_keep').addEventListener('change', () => renderQueue());
$('#btnOpenRepo').addEventListener('click', () => {
  const dir = ($('#o_dir').value.trim() || CFG.targetDir || '').replace(/\{(\w+)\}/g, (m,k) => {
    const d = new Date();
    const map = {date: d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()),
      yyyy: String(d.getFullYear()), mm: pad(d.getMonth()+1), dd: pad(d.getDate()),
      time: pad(d.getHours())+pad(d.getMinutes())+pad(d.getSeconds())};
    return (k in map) ? map[k] : m;
  }).split('/').filter(Boolean).slice(0,1).join('/');
  const base = 'https://github.com/' + CFG.owner + '/' + CFG.repo;
  const br = liveBranch();
  // 分支还没认出来时退到仓库首页，别拼一个 /tree/main/... 的死链
  window.open(br ? (base + '/tree/' + encodeURI(br) + '/' + dir) : base, '_blank');
});
$('#btnTest').addEventListener('click', async () => {
  $('#btnTest').disabled = true; $('#btnTest').textContent = '测试中…';
  try {
    const tok = $('#c_token').value.trim() || TOKEN;
    const qs = new URLSearchParams({owner: $('#c_owner').value.trim(), repo: $('#c_repo').value.trim()});
    const ab = $('#c_apiBase').value.trim();
    if(ab) qs.set('apiBase', ab);
    const res = await fetch('/api/test?' + qs.toString(),
      {method:'POST', headers: tok ? {'X-GitHub-Token': tok} : {}}).then(r => r.json());
    if(res.ok){
      toast(res.message, 'ok');
      if(res.defaultBranch && !$('#c_branch').value.trim()) $('#c_branch').value = res.defaultBranch;
      await saveConfig();            // 连接成功顺手存下来，省得再点一次
    } else {
      toast('连接失败：' + (res.error || ''), 'err');
    }
  } catch(e){ toast('连接失败', 'err'); }
  $('#btnTest').disabled = false; $('#btnTest').textContent = '测试连接';
});

/* ---------- 仓库文件管理：绑定 ---------- */
$('#btnFilesLoad').addEventListener('click', () => loadFiles(false, FILES.path || '', $('#fileDeep').checked));
$('#fileFilter').addEventListener('input', renderFiles);
$('#fileOnlyDirs').addEventListener('change', renderFiles);
$('#fileDeep').addEventListener('change', () => {
  if(FILES.loaded) loadFiles(false, FILES.path || '', $('#fileDeep').checked);
});
$('#btnDelFiles').addEventListener('click', askDeleteSelected);
$('#btnRename').addEventListener('click', askRenameSelected);
$('#btnMkdir').addEventListener('click', askMkdir);
$('#btnDelDir').addEventListener('click', askDeleteDirHere);
$('#btnClearRepo').addEventListener('click', askClearRepo);
$('#o_dirPick').addEventListener('change', onDirPickChange);
$('#modalOk').addEventListener('click', modalOk);
$('#modalCancel').addEventListener('click', modalCancel);
$('#modal').addEventListener('click', e => { if(e.target === $('#modal')) modalCancel(); });
document.addEventListener('keydown', e => {
  if(e.key === 'Escape' && MODAL_CTX) modalCancel();
  else if(e.key === 'Enter' && MODAL_CTX) modalOk();
});
updateSelInfo();

loadState().catch(e => toast('无法连接本地服务：' + e, 'err'));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
