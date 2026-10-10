# 第三方组件与归属声明

本项目的网络认证协议实现基于以下开源项目，按其许可证要求在此声明。

---

## campusnet

- 仓库：https://github.com/demo133/campusnet
- 许可证：MIT
- 版权：Copyright (c) 2026 campusnet contributors
- 许可证全文：见本目录下 `LICENSE.campusnet`

### 使用方式

`campusnet/` 基于该项目的代码修改而来，保留认证协议实现与所需组件，
并针对 Windows 静默自动认证重构。它不是未经修改的上游副本。

| 文件 | 修改内容 |
|---|---|
| `campusnet/providers/` | 保留协议适配；锐捷实现优先获取新门户会话参数，修复 `nasip` 兜底及门户消息解码 |
| `campusnet/session.py`、`detector.py`、`portal.py` | 明确重定向处理，检查实际联网，限制门户发现目标 |
| `campusnet/wifi.py`、`runner.py` | 只读取当前 Wi-Fi 并在名称匹配时认证；删除主动连接与切换 Wi-Fi 的逻辑 |
| `campusnet/cli.py`、`daemon.py`、`runtime.py`、`singleton.py` | 统一应用入口、单实例守护、退避重试、状态文件、滚动日志和静默异常处理 |

上游通用 GUI、自启配置界面和主动切网能力已移除。本项目提供独立的账号密码设置窗口与 Windows 托盘交互；关闭设置窗口后继续后台运行。

Windows 部署由本项目的 `install.ps1`、`uninstall.ps1` 与 `status.ps1` 管理；单个 EXE 在后台常驻，并由 Windows 当前用户 Run 启动项 `CampusNet-AutoLogin` 在登录桌面时以 `--background` 启动，只显示托盘图标。安装时迁移清理本项目旧版计划任务。

### 许可证保留

源代码与发布包均保留上游版权声明和 MIT 许可证全文 `LICENSE.campusnet`。
本项目自身许可证见 `LICENSE`。源码公开用于审阅和适配；MIT 许可证要求保留版权及许可声明，不要求发布全部源代码。
