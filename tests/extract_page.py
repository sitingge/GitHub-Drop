"""把页面里的 <script> 抠出来写成独立 js，交给 node --check / harness 用。
用法: python extract_page.py            -> 生成 tests/_page.js（与 check_page.py 同名复用）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import github_drop  # noqa: E402


def main() -> int:
    page = github_drop.PAGE
    start = page.index("<script>") + len("<script>")
    end = page.index("</script>")
    js = page[start:end]
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_page.js")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(js)
    print("wrote %s (%d chars)" % (out, len(js)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
