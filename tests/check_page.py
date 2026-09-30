#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
页面前端逻辑测试
================

把 index 页里真实的 <script> 抽出来，在 Node 里配一套最小 DOM 桩直接执行，
验证「粘贴仓库地址 -> 自动填 owner/repo/分支 -> 拉分支列表 -> 选中分支」这条链路。

不装浏览器、不联网，秒级出结果。

    python tests/check_page.py
"""

import importlib.util
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nodebin import find_node                                 # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    print("=" * 58)
    print(" 页面前端逻辑测试 (Node + 最小 DOM 桩)")
    print("=" * 58)

    spec = importlib.util.spec_from_file_location("github_drop", os.path.join(ROOT, "github_drop.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    page = mod.PAGE
    if "<script>" not in page or "</script>" not in page:
        print("  [FAIL] 页面里找不到 <script> 块")
        return 1
    script = page.split("<script>")[1].split("</script>")[0]

    js_path = os.path.join(HERE, "_page.js")
    with open(js_path, "w", encoding="utf-8") as fp:
        fp.write(script)

    node = find_node()
    harness = os.path.join(HERE, "page_harness.js")
    proc = subprocess.run([node, "page_harness.js", "_page.js"], cwd=HERE,
                          capture_output=True, timeout=180)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    print(out, end="")
    if err.strip():
        print("---- node stderr ----")
        print(err)
    if proc.returncode != 0 and "OK" not in out:
        print("  [FAIL] Node 执行失败 (exitcode=%s)" % proc.returncode)
        return 1
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
