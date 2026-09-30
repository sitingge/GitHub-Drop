#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试包装脚本
============

本机 PowerShell 工具不回显命令输出，Python 的 print 也会丢，所以统一用它跑测试、
把结果落到 txt，再用编辑器 / Read 打开查看。

    python tests/run_tests.py                  # 跑全部用例 -> tests/result.txt（明细另存各自 .out.txt）
    python tests/run_tests.py check_bat.py     # 只跑启动脚本自检 -> tests/check_bat.out.txt
    python tests/run_tests.py test_upload_flow.py check_page.py

默认全套：
    test_upload_flow.py  后端全流程（本地 mock GitHub，不联网）
    check_page.py        页面 JS 逻辑（Node + 最小 DOM 桩）
    check_server.py      本地服务与 HTTP 细节
    check_bat.py         Windows 启动脚本自检（ASCII + CRLF）
    jscheck.py           页面 <script> 语法检查（node --check）
    check_privacy.py     隐私自检（Token 形态 / 个人配置 / 上传历史 / 本机路径）
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

ALL = [
    "test_upload_flow.py",
    "check_page.py",
    "check_server.py",
    "check_bat.py",
    "jscheck.py",
    "check_privacy.py",
]

names = sys.argv[1:] or list(ALL)
chunks = []
code = 0

for name in names:
    target = os.path.join(HERE, name)
    if not os.path.isfile(target):
        chunks.append("!! 找不到 %s" % name)
        code = code or 2
        continue
    proc = subprocess.run([sys.executable, target], cwd=HERE, capture_output=True, timeout=900)
    text = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    body = "=" * 66 + "\n### %s  (exit=%d)\n" % (name, proc.returncode) + "=" * 66 + "\n" + text
    if err.strip():
        body += "\n--- stderr ---\n" + err
    chunks.append(body)

    per_file = os.path.join(HERE, name.replace(".py", ".out.txt"))
    with open(per_file, "w", encoding="utf-8", newline="\n") as fp:
        fp.write("[%s] exitcode=%s\n" % (name, proc.returncode))
        fp.write(text)
        if err.strip():
            fp.write("\n--- stderr ---\n" + err)
    code = code or proc.returncode

with open(os.path.join(HERE, "result.txt"), "w", encoding="utf-8", newline="\n") as fp:
    fp.write("\n\n".join(chunks) + "\n")

print("done -> tests/result.txt")
