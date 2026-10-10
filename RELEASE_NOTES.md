# 校园网自动登录：单程序重构

本版将账号设置、托盘运行、后台检查与认证重试统一为一个 `campusnet.exe`。通过 Windows 当前用户 Run 启动项自启，登录后仅在托盘后台运行。

- 只在已连接 `tjus_wifi` 时认证，不主动切换 Wi-Fi。
- 约每 15 秒检查联网；认证失败按 5、15、30、60 秒退避，随后持续重试。
- 门户“已在线”仍需通过实际联网验证。
- 手动打开 EXE 显示账号密码设置；关闭窗口后继续在托盘运行。
- 双击托盘或选择“账号设置”打开窗口，选择“退出程序”停止本次运行。
- 单实例运行，异常与诊断写入文件；安装和自启动不弹出设置窗口。
- 安装脚本设置当前用户启动项并清理旧版任务，卸载保留程序、配置和日志。

## 下载与安装

唯一发布附件为 `campusnet-windows.zip`，包含 EXE、依赖目录、有效的 JSON 配置示例、安装 / 卸载 / 状态脚本及许可证。运行 EXE 不需要 Python 或 PowerShell；管理脚本需要 PowerShell 7。

解压后运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

正式安装将程序复制到 `%LOCALAPPDATA%\Programs\CampusNet`，保留该安装目录中已有的 `config.json`，并立即在托盘启动。首次无配置时根据示例生成空账号配置，等待用户手动打开 EXE 或托盘“账号设置”填写。

`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `CampusNet-AutoLogin` 启动项指向安装目录中的 EXE，并带 `--background`。之后登录桌面时以当前用户权限启动，只显示托盘图标；退出程序保留下次登录自启，取消自启使用卸载脚本。

正常安装不需要管理员权限；清理本项目旧版计划任务时才请求一次 UAC。安装、状态与卸载脚本均支持 `-InstallDir <路径>` 指定自定义安装目录。

`campusnet_app` 文件夹必须完整保留，不能只取 EXE。发布包不含真实配置、凭据或运行日志。

账号密码可在设置窗口中修改并应用，其他配置位于安装目录中的 `config.json`。日志与运行状态位于安装目录的 `logs` 文件夹，首次运行时自动创建；从源码或解压目录运行 `status.ps1`、`uninstall.ps1` 也默认操作上述用户安装目录。

## 使用边界

当前部署面向 Windows 上的 TJUS 锐捷 ePortal。未连接目标 Wi-Fi 或无法确认 Wi-Fi 名称时暂停认证。门户会话令牌可能过期，程序优先发现新查询串，必要时可在外置配置中更新备用查询串。

认证协议基于 [demo133/campusnet](https://github.com/demo133/campusnet)（MIT）。完整使用说明见 README，许可证见 `LICENSE`、`LICENSE.campusnet` 与 `NOTICE.md`。
