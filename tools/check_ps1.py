#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验（并可选修复）PowerShell 脚本的编码与换行。

项目脚本使用 PowerShell 7，沿用 UTF-8 BOM + CRLF 的文件格式。
本检查只检查编码和换行，不代替 PowerShell 解析器的语法检查。

用法
----
    python tools/check_ps1.py            # 只检查，有问题就退出码 1
    python tools/check_ps1.py --fix      # 自动补 BOM 并统一为 CRLF
"""

from __future__ import annotations

import argparse
import os
import sys

BOM = b"\xef\xbb\xbf"

#: 相对本文件定位项目根目录（本脚本位于 <root>/tools/）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ps1_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过版本库与打包产物
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "build", "dist", "release",
                                    "__pycache__", "run_login", "run_login_hidden",
                                    "daemon", "campusnet_app")]
        for name in sorted(filenames):
            if name.lower().endswith(".ps1"):
                yield os.path.join(dirpath, name)


def inspect(path: str) -> dict:
    raw = open(path, "rb").read()
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    lone_cr = raw.count(b"\r") - crlf
    return {
        "has_bom": raw.startswith(BOM),
        "crlf": crlf,
        "lone_lf": lone_lf,
        "lone_cr": lone_cr,
        "ok": raw.startswith(BOM) and lone_lf == 0 and lone_cr == 0,
    }


def fix(path: str) -> None:
    raw = open(path, "rb").read()
    if raw.startswith(BOM):
        raw = raw[len(BOM):]
    # 先把所有 CRLF / 裸 CR 归一到 LF，再统一成 CRLF
    raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    if not raw.endswith(b"\r\n"):
        raw += b"\r\n"
    open(path, "wb").write(BOM + raw)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="检查 PowerShell 脚本的 BOM 与换行")
    parser.add_argument("--fix", action="store_true", help="自动修复而不是只报告")
    parser.add_argument("--root", default=PROJECT_ROOT, help="项目根目录")
    args = parser.parse_args()

    files = list(ps1_files(args.root))
    if not files:
        print("[!] 没找到任何 .ps1 文件（根目录：{}）".format(args.root))
        return 1

    bad = []
    for path in files:
        rel = os.path.relpath(path, args.root)
        state = inspect(path)
        if state["ok"]:
            print("  OK   {}".format(rel))
            continue

        detail = "BOM={} 裸LF={} 裸CR={}".format(
            "有" if state["has_bom"] else "**缺**", state["lone_lf"], state["lone_cr"])
        if args.fix:
            fix(path)
            after = inspect(path)
            status = "已修复" if after["ok"] else "修复后仍异常"
            print("  {} {}  ({})".format("FIX " if after["ok"] else "FAIL", rel, detail))
            if not after["ok"]:
                bad.append(rel)
        else:
            print("  FAIL {}  ({})".format(rel, detail))
            bad.append(rel)

    print()
    if bad:
        if args.fix:
            print("[x] 以下文件修复失败：{}".format(", ".join(bad)))
        else:
            print("[x] {} 个脚本格式不合规：{}".format(len(bad), ", ".join(bad)))
            print("    修复：python tools/check_ps1.py --fix")
        return 1

    print("[+] 全部 {} 个脚本均为 UTF-8 BOM + CRLF".format(len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
