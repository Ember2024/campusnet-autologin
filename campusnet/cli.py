"""唯一应用接口：默认守护，显式执行一次认证或只读自检。"""

import argparse

from .config import Config
from .runner import Runner
from .runtime import find_config_path, load_config, make_logger, self_test, state_dir


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def start_desktop(config_path, log, background):
    from .desktop import run_app
    return run_app(config_path, log, background)


def main(argv=None):
    parser = Parser(description="校园网静默自动登录")
    parser.add_argument("--config", default="", help="外置配置文件路径")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="执行一次检查与认证")
    modes.add_argument("--self-test", action="store_true", help="检查运行环境，不认证")
    modes.add_argument("--check-config", action="store_true", help="检查配置，不访问网络")
    modes.add_argument("--background", action="store_true", help="只显示托盘图标，不打开设置窗口")
    parser.add_argument("--force", action="store_true", help="与 --once 一起使用，强制认证")
    parser.add_argument("--report", default="", help="自检 JSON 报告路径")
    args = parser.parse_args(argv)
    if args.force and not args.once:
        parser.error("--force 只能与 --once 一起使用")
    if args.report and not args.self_test:
        parser.error("--report 只能与 --self-test 一起使用")

    config_path = find_config_path(args.config)
    folder = state_dir(config_path)
    log = make_logger(folder / "campusnet.log", quiet=True)
    if not (args.once or args.self_test or args.check_config):
        return start_desktop(config_path, log, args.background)
    if args.self_test:
        # 环境自检不依赖账号密码；首次安装可先启动托盘，再填写配置。
        return self_test(Config.load(config_path), args.report or folder / "self-test.json")
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        log(str(exc), "error")
        return 2
    if args.check_config:
        return 0
    runner = Runner(cfg, logger=log)
    if args.once:
        result = runner.ensure_online(force=args.force)
        log(result.message, "info" if result.ok or result.skipped else "warn")
        return 0 if result.ok or result.skipped else 1
