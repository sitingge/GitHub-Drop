#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务启动冒烟测试
================

真的把 github_drop.py 当子进程拉起来，探测页面和接口，跑完自动关掉，不留后台进程。

    python tests/check_server.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAILED = []


def free_port() -> int:
    """先占 0 号端口拿一个系统分配的空闲端口。

    之前写死 8799，一旦这个端口被别的程序占着，服务端会顺延到 8800，
    而这个脚本还在轮询 8799 —— 表现成「服务起不来」，其实是端口选错。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()


def check(name, cond, detail=""):
    print("  [%s] %s  %s" % ("PASS" if cond else "FAIL", name, "" if cond else detail))
    if not cond:
        FAILED.append(name)


def main():
    print("=" * 58)
    print(" 启动冒烟测试 (port %d)" % PORT)
    print("=" * 58)

    # 关键：必须用临时配置，绝不能用用户真实的 config.json
    # （否则读到的 owner/token 会让断言失真，也可能误触真实 GitHub）
    tmpdir = tempfile.mkdtemp(prefix="gd-smoke-")
    cfg_path = os.path.join(tmpdir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fp:
        # branch 留空 = 让服务端自己认仓库默认分支（这里不预设 main，避免把
        # "默认分支叫 main" 这个错误假设写进冒烟测试）
        json.dump({"owner": "", "repo": "", "branch": "", "token": ""}, fp)

    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "github_drop.py"),
                             "--no-browser", "--port", str(PORT), "--config", cfg_path],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    base = "http://127.0.0.1:%d" % PORT
    try:
        up = False
        for _ in range(24):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(base + "/api/state", timeout=3) as resp:
                    state = json.loads(resp.read().decode("utf-8"))
                up = True
                break
            except Exception:                                      # noqa: BLE001
                continue
        check("服务能起来", up, "10 秒内没起来")
        if not up:
            return 1

        check("版本号正确", state.get("version") == "1.0.0", str(state.get("version")))
        check("配置不回传 token", "token" not in state.get("config", {}), "")
        check("用的是临时配置（没碰真实 config.json）",
              state.get("configPath") == cfg_path, str(state.get("configPath")))
        check("空配置时 tokenSet 为 false", state.get("tokenSet") is False, str(state.get("tokenSet")))

        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            page = resp.read().decode("utf-8")
        check("首页是完整 HTML", page.startswith("<!DOCTYPE html>") and page.rstrip().endswith("</html>"), "")
        for probe in ("把文件或文件夹拖到这里", "webkitdirectory", "webkitGetAsEntry",
                      "X-Rel-Path", "o_gitkeep", "保留文件夹结构", "开始上传"):
            check("页面含 %s" % probe, probe in page, "")

        # 配置没填时，接口要给出可读的提示而不是 500
        try:
            urllib.request.urlopen(base + "/api/test", timeout=5)
            check("空配置调用 /api/test 被拒绝", False, "居然成功了")
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            check("空配置调用 /api/test 返回 400 + 中文提示",
                  exc.code == 400 and "owner" in body.get("error", ""), "%s %s" % (exc.code, body))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                          # noqa: BLE001
            proc.kill()

    print("=" * 58)
    if FAILED:
        print(" 未通过: %s" % ", ".join(FAILED))
        return 1
    print(" 全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
