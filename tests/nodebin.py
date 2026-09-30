# -*- coding: utf-8 -*-
"""
找一个可用的 node
=================

测试脚本要跑 `node --check` / 执行 DOM 桩，但不能把本机的绝对路径写死
（写死 `C:\\Users\\<用户名>\\...` 既是隐私残留，换台机器也直接失效）。

查找顺序：
  1. WorkBuddy 托管目录下的任意版本
  2. PATH 里的 node
  3. Windows 常见安装位置

都找不到就返回 "node"，让报错发生在调用处（信息更直观）。
"""

import glob
import os
import shutil


def find_node() -> str:
    home = os.path.expanduser("~")

    # 1) 托管目录：.../.workbuddy/binaries/node/versions/<版本>/node(.exe)
    for pattern in (
        os.path.join(home, ".workbuddy", "binaries", "node", "versions", "*", "node.exe"),
        os.path.join(home, ".workbuddy", "binaries", "node", "versions", "*", "bin", "node"),
        os.path.join(home, ".workbuddy", "binaries", "node", "versions", "*", "node"),
    ):
        hits = [p for p in glob.glob(pattern) if os.path.isfile(p)]
        if hits:
            # 版本号倒序，优先用较新的
            return sorted(hits, reverse=True)[0]

    # 2) PATH
    found = shutil.which("node")
    if found:
        return found

    # 3) 常见安装位置
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    for path in (
        os.path.join(program_files, "nodejs", "node.exe"),
        "/usr/local/bin/node",
        "/usr/bin/node",
    ):
        if os.path.isfile(path):
            return path

    return "node"


if __name__ == "__main__":
    print(find_node())
