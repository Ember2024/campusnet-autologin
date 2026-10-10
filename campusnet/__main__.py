"""允许 `python -m campusnet ...`。"""

def entry():
    # 连导入阶段的异常也不能触发窗口版打包器的错误对话框。
    try:
        from .runtime import safe_main

        def run():
            from .cli import main
            return main()

        return safe_main(run)
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(entry())
