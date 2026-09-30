#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动脚本自检
============

Windows 的 cmd 按系统代码页（中文系统 = GBK/936）读取 .bat，文件里只要有 UTF-8 中文字节
就会错位，把后面的行切碎成 `'b_drop.py" ' 不是内部或外部命令` 这种报错。这个脚本守住三条底线：

  1. 文件只含 ASCII 字符（不含 BOM）
  2. 行尾统一 CRLF
  3. 真的能被 cmd 执行（标签跳转正常、参数能透传）

运行:
    python tests/check_bat.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(ROOT, "start-github-drop.bat")

FAILED = []


def check(name, cond, detail=""):
    print("  [%s] %s  %s" % ("PASS" if cond else "FAIL", name, "" if cond else detail))
    if not cond:
        FAILED.append(name)


def main():
    print("=" * 58)
    print(" 启动脚本自检: %s" % BAT)
    print("=" * 58)

    if not os.path.isfile(BAT):
        print("  [FAIL] 文件不存在")
        return 1
    raw = open(BAT, "rb").read()

    non_ascii = [i for i, b in enumerate(raw) if b > 127]
    check("只含 ASCII 字符", not non_ascii,
          "第 %s 字节起出现非 ASCII（cmd 会错位，中文提示请交给 Python 打印）" % (non_ascii[:3] or "?"))
    check("没有 UTF-8 BOM", raw[:3] != b"\xef\xbb\xbf")
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    check("行尾统一 CRLF", lone_lf == 0, "发现 %d 个孤立 LF" % lone_lf)
    check("首行是 @echo off", raw.split(b"\r\n")[0].strip() == b"@echo off")
    check("结尾有 pause", b"pause" in raw)

    # 真正跑一遍：--help 会立刻退出，不会进服务循环
    if sys.platform == "win32":
        try:
            proc = subprocess.run(["cmd.exe", "/c", BAT, "--help"],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
            check("cmd 能执行且退出码为 0", proc.returncode == 0, "exitcode=%s" % proc.returncode)
            check("参数已透传给 Python", "usage: github_drop.py" in out, out[-300:])
            check("没有出现碎片式命令错误",
                  "not recognized" not in out and "不是内部或外部命令" not in out, out[-300:])
        except Exception as exc:                                   # noqa: BLE001
            check("cmd 能执行", False, repr(exc))
    else:
        print("  [SKIP] 非 Windows，跳过实际执行")

    print("=" * 58)
    if FAILED:
        print(" 未通过: %s" % ", ".join(FAILED))
        return 1
    print(" 全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
