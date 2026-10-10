"""PyInstaller 引导入口；源码和 EXE 使用同一应用。"""

if __name__ == "__main__":
    try:
        from campusnet.__main__ import entry
        code = entry()
    except Exception:
        code = 2
    raise SystemExit(code)
