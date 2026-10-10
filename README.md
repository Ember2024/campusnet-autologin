# 校园网自动登录

面向 TJUS 锐捷 ePortal 校园网的 Windows 静默认证工具。连接 `tjus_wifi` 后自动登录，仍连接校园 Wi-Fi 但无法上网时持续重试。

一个 `campusnet.exe`、一个当前用户登录启动项、一份外置配置。自动启动后在托盘后台运行，不弹出窗口；手动打开 EXE 可以设置账号和密码。

## 运行方式

- 只在当前 Wi-Fi 名称与 `wifi_ssid` 完全一致时认证；不主动连接或切换 Wi-Fi。
- 约每 15 秒检查网络。认证失败后依次等待 5、15、30、60 秒，之后持续按 60 秒重试。
- 离开校园 Wi-Fi 或无法确认 Wi-Fi 名称时暂停认证；重新连接后恢复检查。
- 门户返回“已在线”后仍验证实际网络，验证失败继续重试。
- 使用单实例锁防止重复守护和并发认证；日志自动滚动。
- 关闭设置窗口后继续在托盘运行；从托盘菜单选择“退出程序”才会停止。

## 安装

下载并解压 `campusnet-windows.zip`。安装脚本会把程序复制到当前用户目录 `%LOCALAPPDATA%\Programs\CampusNet`，安装后的主要文件如下：

```text
%LOCALAPPDATA%\Programs\CampusNet/
  campusnet_app/
    campusnet.exe
    _internal/
  config.json
  config.example.json
  logs/                  # 首次运行时自动创建
  install.ps1
  uninstall.ps1
  status.ps1
  README.md
  LICENSE
  LICENSE.campusnet
  NOTICE.md
```

运行 EXE 不需要 Python 或 PowerShell。安装、卸载和状态脚本使用 **PowerShell 7**。

在解压目录运行以下命令。预检不修改启动项；正式安装复制文件、写入当前用户启动项并立即在托盘启动程序，不打开设置窗口。

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
pwsh -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

已有安装目录中的配置会保留；首次安装也可直接使用解压目录中的 `config.json`。两处都没有配置时，安装脚本根据示例创建空账号配置，程序在托盘等待填写，不会弹窗或提交占位账号。

安装后，Windows 在当前用户登录桌面时通过启动项 `CampusNet-AutoLogin` 直接以 `--background` 启动安装目录中的 EXE，只显示托盘图标。启动项位于 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，程序以当前用户权限运行。

正常安装不需要管理员权限。若检测到本项目旧版计划任务，安装脚本会请求一次 UAC 来清理旧任务；日常自启动直接运行 EXE。

三个管理脚本都支持 `-InstallDir <路径>` 指定安装目录；使用自定义目录时保持参数一致。`campusnet_app` 中的 `_internal` 是运行依赖，需要与 EXE 一起保留。

## 账号设置与托盘

手动打开 `%LOCALAPPDATA%\Programs\CampusNet\campusnet_app\campusnet.exe`，填写账号和密码后点击“保存并应用”。后台会在下一轮检查中使用新配置；若正在认证，会先完成本轮。

- 双击托盘图标，或右键选择“账号设置”，可再次打开设置窗口。
- 关闭设置窗口只收起界面，后台检查和自动认证继续运行。
- 右键选择“退出程序”会停止本次运行。下次登录 Windows 仍会自启；取消自启请使用卸载脚本。

## 配置

安装后的配置位于 `%LOCALAPPDATA%\Programs\CampusNet\config.json`。它保持外置，不会打进 EXE。程序优先使用 `--config` 指定的路径，否则依次查找 EXE 所在目录、其上一级目录中的 `config.json`。

| 字段 | 作用 |
|---|---|
| `username` / `password` | 校园网上网账号和密码 |
| `provider` | 当前使用 `ruijie` |
| `portal_ip` | 当前门户 `http://192.168.24.65` |
| `wifi_ssid` | 必填，只允许在这个 Wi-Fi 下认证，当前为 `tjus_wifi` |
| `timeout` | 单个网络请求超时秒数 |
| `use_proxy` | 默认 `false`，认证与检测不使用环境代理 |
| `options.service` | 门户要求的服务名，可留空 |
| `options.query_string` | 可选的门户查询串；优先使用本次发现的新查询串 |
| `options.online_check_seconds` | 网络检查间隔，默认 15 秒 |
| `options.verify_delay` | 认证后等待再验证的秒数，默认 2 秒 |

账号和密码可直接在设置窗口中修改，保存到安装目录中的 `config.json`。修改其他配置字段后，请从托盘退出并重新打开程序。升级安装会保留已有配置。真实配置包含凭据和可能过期的会话令牌，已从 Git 和发布包排除；发布包只带示例。

## 状态与手动检查

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\status.ps1
```

从源码目录、解压目录或安装目录运行状态脚本，默认都检查 `%LOCALAPPDATA%\Programs\CampusNet`；自定义安装请加 `-InstallDir <路径>`。

日志和运行状态位于安装目录的 `logs` 文件夹，首次运行时自动创建。状态脚本显示启动项、进程、最近状态及日志；EXE 的诊断结果写入文件。手动指定其他配置文件时，日志写入该配置同目录的 `logs`。

| 参数 | 行为 |
|---|---|
| 不带模式参数 | 打开账号设置窗口，并在托盘保持后台检查 |
| `--background` | 仅在托盘后台运行，不显示设置窗口；安装和自启动使用此模式 |
| `--once` | 检查并按需认证一次，然后退出 |
| `--once --force` | 即使网络已通也尝试认证一次，仍要求匹配校园 Wi-Fi |
| `--check-config` | 只检查配置，不认证 |
| `--self-test --report <路径>` | 生成环境自检 JSON，不认证 |
| `--config <路径>` | 明确指定外置配置文件 |

例如手动认证一次：

```powershell
& "$env:LOCALAPPDATA\Programs\CampusNet\campusnet_app\campusnet.exe" --once
```

若状态提示 Wi-Fi 不可读，程序会等待下一次检查，暂不认证。持续出现时，可提供 `status.ps1` 的状态结果排查。

若门户持续返回“设备未注册”，检查是否取得新的门户查询串；必要时将正常认证页面地址中 `?` 后的完整内容填入 `options.query_string`。不要将查询串写入公开问题或日志附件。

## 卸载

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

从源码目录、解压目录或安装目录运行卸载脚本，默认都操作上述用户安装目录；自定义安装请加 `-InstallDir <路径>`。卸载会取消本项目自启并停止对应进程，保留程序、配置和日志。

## 从源码构建

使用 Windows、PowerShell 7 和 Python 3.12。构建脚本会运行测试，使用 PyInstaller 生成一个无窗口 `onedir` 程序，并检查冻结程序的诊断模式。

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
```

构建结果为 `campusnet_app\campusnet.exe` 及依赖目录，发布包为 `release\campusnet-windows.zip`。构建不会安装自启，也不会自动提交或上传发布包。

源码入口与 EXE 共用同一套逻辑：

```powershell
python -m campusnet --background --config .\config.json
python -m campusnet --once --config .\config.json
python -m unittest discover -s tests -v
python tools/check_ps1.py
```

PowerShell 脚本沿用 UTF-8 BOM + CRLF 格式；修改后可运行 `python tools/check_ps1.py --fix` 统一格式。

## 归属与许可

认证协议基于 [demo133/campusnet](https://github.com/demo133/campusnet)（MIT），本项目重构了运行入口、网络检查、调度和 Windows 部署，保留并修复了锐捷协议适配。

本项目使用 MIT 许可证，见 [LICENSE](LICENSE)。上游许可证及归属见 [LICENSE.campusnet](LICENSE.campusnet) 和 [NOTICE.md](NOTICE.md)。
