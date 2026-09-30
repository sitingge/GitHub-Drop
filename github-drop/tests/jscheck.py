"""跑 node --check，把结果写进 tests/_jscheck.txt（PowerShell 不回显，所以落盘再读）。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from nodebin import find_node                                 # noqa: E402

NODE = find_node()
PAGE = os.path.join(HERE, "_page.js")
OUT = os.path.join(HERE, "_jscheck.txt")

lines = []
rc = subprocess.run([sys.executable, os.path.join(HERE, "extract_page.py")],
                    capture_output=True, text=True)
lines.append("extract rc=%d" % rc.returncode)
lines.append(rc.stdout.strip())
if rc.stderr.strip():
    lines.append("STDERR: " + rc.stderr.strip())

res = subprocess.run([NODE, "--check", PAGE], capture_output=True, text=True)
lines.append("node --check rc=%d" % res.returncode)
if res.stdout.strip():
    lines.append(res.stdout.strip())
if res.stderr.strip():
    lines.append("STDERR: " + res.stderr.strip())
lines.append("RESULT: " + ("JS SYNTAX OK" if res.returncode == 0 else "JS SYNTAX FAIL"))

with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(lines) + "\n")
print("done")
