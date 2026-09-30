"""真机只读验证：浅列要快，深度扫描要能出结果（全程不写任何东西）。

目录不写死，避免把某个人的仓库结构留在脚本里：

    python tests/real_repo_check.py                    # 自动挑根目录下第一个子目录来测
    python tests/real_repo_check.py photos avatars      # 指定要测的目录（可多个）

跑之前请先把服务起起来（默认 http://127.0.0.1:8767），并在页面上配好仓库。
结果落在 tests/_real_check.txt（PowerShell 不回显，所以落盘再读）。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("GITHUB_DROP_BASE", "http://127.0.0.1:8767")
LINES = []


def get(path, timeout=400):
    t0 = time.time()
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), time.time() - t0


def show(tag, path):
    try:
        d, secs = get(path)
        LINES.append("%-26s ok=%s mode=%-9s deep=%-5s files=%-5d dirs=%-4d %.1fs" % (
            tag, d.get("ok"), d.get("mode"), d.get("deep"),
            len(d.get("files") or []), len(d.get("dirs") or []), secs))
        LINES.append("      %s" % d.get("message"))
        for f in (d.get("files") or [])[:4]:
            LINES.append("      %-50s %10d B" % (f["path"], f["size"]))
        for x in (d.get("dirs") or [])[:4]:
            LINES.append("      [dir] %-40s %s" % (x["path"], x.get("count")))
        return d
    except Exception as exc:                                      # noqa: BLE001
        LINES.append("%-26s ERROR %r" % (tag, exc))
        return None


def q(path, **kw):
    """拼 /api/files 的 query。path 为空表示仓库根目录。"""
    params = dict(kw)
    if path:
        params["path"] = path
    return "/api/files?" + urllib.parse.urlencode(params)


def main():
    st, _ = get("/api/state")
    cfg = st.get("config") or {}
    LINES.append("repo=%s/%s@%s" % (cfg.get("owner"), cfg.get("repo"), cfg.get("branch")))
    LINES.append("")

    root = show("浅列 根目录", "/api/files")
    dirs = [d["path"] for d in ((root or {}).get("dirs") or [])]
    targets = sys.argv[1:] or dirs[:1]
    if not targets:
        LINES.append("（根目录下没有子目录，跳过深扫对比）")
    else:
        LINES.append("测试目录: %s" % ", ".join(targets))
        LINES.append("")

    for target in targets:
        show("浅列 %s" % target, q(target))

    LINES.append("")
    show("深扫 根目录", q("", deep=1))
    for target in targets:
        show("深扫 %s" % target, q(target, deep=1))

    LINES.append("")
    show("深扫 再来一次(冷却)", q("", deep=1))
    for target in targets:
        show("浅列 再确认很快", q(target))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_real_check.txt")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(LINES) + "\n")
    print("done")


if __name__ == "__main__":
    main()
