#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
隐私 / 敏感信息自检
==================

分享、上传、交付这个目录之前跑一遍，确认里面没留下 Token、仓库名、本机用户名等私人信息。

    python tests/check_privacy.py
    python tests/check_privacy.py --term 某个仓库名 --term 某个 GitHub ID

检查项：

  * GitHub Token 形态 —— **按真实长度判定**，所以文档里的 `ghp_xxxx`、
    界面占位符 `ghp_…`、测试用的 `ghp_test_token` 这类说明性假值不会误报
  * config.json 里是否还留着非空的 owner / repo / token / apiBase
  * upload-history.jsonl 是否还有内容（上传历史本身就是私人数据）
  * 本机用户名与用户主目录路径（源码里写死绝对路径的常见残留）
  * `--term` 额外指定的词

结果分两档：

  * **阻断项**：源码 / 文档 / 配置里的残留，必须清干净才能真正对外
  * **提示项**：测试跑出来的产物（结果 txt、日志、抽出的 js），本来就含本机路径，
    交付前删掉即可（`.gitignore` 也已忽略）

退出码：0 = 没阻断项；1 = 有阻断项。报告同时写入 tests/_privacy.txt。
"""

import argparse
import getpass
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SELF = os.path.abspath(__file__)
REPORT = os.path.join(HERE, "_privacy.txt")

SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "node_modules"}

# 真实 GitHub Token：前缀 + 一长串随机字符。要求 20 位以上，避免把
# `ghp_xxxx`（文档示例）、`ghp_test_token`（测试假值）当成真 Token。
TOKEN_PATTERNS = [
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"gh[ousr]_[A-Za-z0-9]{20,}"),
]

# 生成物：含本机路径很正常，交付前清掉即可 => 提示项而不是阻断项
ARTIFACT_SUFFIXES = (".out.txt",)
ARTIFACT_NAMES = {"result.txt"}


def is_artifact(rel: str) -> bool:
    base = os.path.basename(rel)
    if base.startswith("_"):
        return True
    if base.endswith(ARTIFACT_SUFFIXES) or base in ARTIFACT_NAMES:
        return True
    return False


def literal_markers() -> list:
    """返回 [(标签, bytes), ...]，用于子串精确匹配（标签不打印命中的原文）。"""
    markers = []

    names = set()
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = (os.environ.get(key) or "").strip()
        if value:
            names.add(value)
    try:
        names.add(getpass.getuser())
    except Exception:                                             # noqa: BLE001
        pass
    for name in sorted(names):
        if len(name) >= 3:
            markers.append(("本机用户名", name.encode("utf-8")))

    home = os.path.expanduser("~")
    if home and home not in ("~", "/"):
        markers.append(("用户主目录路径", home.encode("utf-8")))

    return markers


def config_markers() -> tuple:
    """config.json 里若还有个人配置，也算标记。返回 (markers, 提示语列表)。"""
    markers, notes = [], []
    path = os.path.join(ROOT, "config.json")
    if not os.path.isfile(path):
        return markers, notes
    try:
        with open(path, "r", encoding="utf-8") as fp:
            cfg = json.load(fp)
    except Exception as exc:                                      # noqa: BLE001
        notes.append("config.json 解析失败: %r" % (exc,))
        return markers, notes

    for field in ("owner", "repo", "token", "apiBase", "targetDir", "branch"):
        value = cfg.get(field)
        if not (isinstance(value, str) and value.strip()):
            continue
        raw = value.strip().encode("utf-8")
        if field == "token":
            markers.append(("config.json 里的 token", raw))
        elif field in ("owner", "repo"):
            markers.append(("config.json 里的仓库信息", raw))
        notes.append("config.json 的 %s 非空: %s" % (field, value))
    if (cfg.get("token") or "").strip():
        notes.append("!! config.json 里存着明文 Token，务必清掉并去 GitHub 撤销重建")
    return markers, notes


def scan(literals: list) -> list:
    """返回 [(rel, 挡次, [标签...], 次数), ...]"""
    results = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.abspath(full) in (SELF, REPORT):
                continue            # 本脚本自己写着这些关键字；报告也是它刚生成的
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            try:
                with open(full, "rb") as fp:
                    data = fp.read()
            except OSError:
                continue

            hits, total = [], 0
            for pattern in TOKEN_PATTERNS:
                cnt = len(pattern.findall(data))
                if cnt:
                    total += cnt
                    label = "GitHub Token 形态"
                    if label not in hits:
                        hits.append(label)
            for label, needle in literals:
                cnt = data.count(needle)
                if cnt:
                    total += cnt
                    if label not in hits:
                        hits.append(label)
            if total:
                results.append((rel, "提示" if is_artifact(rel) else "阻断", hits, total))
    return results


def history_note() -> str:
    path = os.path.join(ROOT, "upload-history.jsonl")
    if not os.path.isfile(path):
        return "upload-history.jsonl: 不存在（干净）"
    lines = 0
    with open(path, "rb") as fp:
        for _ in fp:
            lines += 1
    size = os.path.getsize(path)
    if lines == 0:
        return "upload-history.jsonl: 0 条（干净）"
    return "upload-history.jsonl: 还有 %d 条上传记录 / %d 字节 —— 属于私人数据，建议清空" % (lines, size)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="隐私 / 敏感信息自检")
    parser.add_argument("--term", action="append", default=[],
                        help="额外要检查的词（可重复），例如仓库名、GitHub ID")
    args = parser.parse_args(argv)

    literals = literal_markers()
    for index, term in enumerate(args.term, 1):
        if term.strip():
            literals.append(("指定词 #%d" % index, term.strip().encode("utf-8")))
    cfg_literals, cfg_notes = config_markers()
    literals.extend(cfg_literals)

    results = scan(literals)
    blocking = [r for r in results if r[1] == "阻断"]
    advisory = [r for r in results if r[1] == "提示"]

    lines = []
    lines.append("=" * 62)
    lines.append(" 隐私 / 敏感信息自检  (根目录: %s)" % ROOT)
    lines.append("=" * 62)
    lines.append(" 检查了 %d 类标记" % (len(literals) + len(TOKEN_PATTERNS)))
    lines.append("")
    lines.append(" 上传历史: " + history_note())
    for note in cfg_notes:
        lines.append(" 配置: " + note)
    lines.append("")

    if blocking:
        lines.append(" 阻断项（源码 / 文档 / 配置里的残留，必须清掉）:")
        for rel, _, hits, total in sorted(blocking):
            lines.append("   - %-38s %d 处  [%s]" % (rel, total, ", ".join(hits)))
    else:
        lines.append(" 阻断项: 无 ✅")

    lines.append("")
    if advisory:
        lines.append(" 提示项（测试产物，含本机路径属正常，交付前删掉即可）:")
        for rel, _, hits, total in sorted(advisory):
            lines.append("   - %-38s %d 处  [%s]" % (rel, total, ", ".join(hits)))
    else:
        lines.append(" 提示项: 无")

    lines.append("")
    lines.append(" 结论: " + ("有 %d 个文件需要处理" % len(blocking) if blocking else "干净，可以对外分享"))
    lines.append("=" * 62)

    text = "\n".join(lines) + "\n"
    print(text)
    with open(REPORT, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
