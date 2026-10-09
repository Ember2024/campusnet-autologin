#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验（并可选修复）PowerShell 脚本的编码与换行。

为什么需要这个脚本
------------------
Windows PowerShell 5.1 读取 ``.ps1`` 时：

* **没有 UTF-8 BOM** 就按系统 ANSI 代码页（简体中文 Windows 是 GBK）解读，
  于是脚本里的中文全部乱码，解析器随即报出
  ``缺少右"}"`` / ``意外的标记`` 之类**与真实问题毫无关系**的语法错误；
* **换行不一致**（CRLF 与 LF 混用）同样会让多行语句解析失败。

本项目已经被这个坑绊了两次：每次用编辑器改完 ``.ps1``，BOM 会被去掉、
换行会被改成 LF，然后脚本在 PowerShell 5.1 里直接崩。因此把它固化成检查项。

用法
----
    python tools/check_ps1.py            # 只检查，有问题就退出码 1
    python tools/check_ps1.py --fix      # 自动补 BOM 并统一为 CRLF
"""

from __future__ import annotations

import argparse
import io
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
                                    "__pycache__", "run_login", "daemon")]
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
            print("[x] {} 个脚本会破坏 PowerShell 5.1 解析：{}".format(len(bad), ", ".join(bad)))
            print("    修复：python tools/check_ps1.py --fix")
        return 1

    print("[+] 全部 {} 个脚本均为 UTF-8 BOM + CRLF，PowerShell 5.1 可正常解析".format(len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
