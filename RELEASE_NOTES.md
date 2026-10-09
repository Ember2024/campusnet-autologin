# v1.0.0

校园网自动登录认证工具的第一个可用版本 —— 面向**锐捷 ePortal** 网页认证型校园网。

连上校园网 Wi-Fi 自动认证 · 开机自启 · 断网自动重连。

## 功能

- **连网即认证**：订阅 Windows 网络事件（`NetworkProfile` 10000/10001），
  切 Wi-Fi / 插网线后立刻尝试认证，不等巡检周期
- **开机自启**：登录后 20 秒拉起常驻守护，启动后 5 分钟内每 15 秒抢一次网
- **断网自动重连**：每 5 分钟兜底巡检 + 常驻守护每 3 分钟检查
- **连错网自动切回**：配置 `wifi_ssid` 后，检测到不在校园网就切回去
- **已联网时完全安静**：不做任何多余动作，不打扰正在使用的连接
- **日志自动滚动**，一屏状态自检脚本

## 下载

| 文件 | 说明 |
|---|---|
| `run_login.zip` | 解压得到 `run_login\`，供任务计划触发的控制台版 |
| `daemon.zip` | 解压得到 `daemon\`，常驻守护（**无窗口**） |
| `config.example.json` | 配置模板，复制成 `config.json` 后填自己的账号密码 |

**目标机器不需要安装 Python。** 每个压缩包内已含完整运行时（约 20 MB）。

## 快速开始

1. 把 `run_login\`、`daemon\` 和 `config.example.json` 放到同一个目录
2. `config.example.json` → 复制为 `config.json`，填入账号密码与门户地址
3. 先手动验证一次：
   ```powershell
   .\run_login\run_login.exe --config .\config.json
   ```
4. 安装三个任务计划（需管理员）：
   ```powershell
   Start-Process powershell -Verb RunAs -ArgumentList `
     '-ExecutionPolicy Bypass -File .\install_tasks.ps1 -UseExe'
   ```
   > `install_tasks.ps1` 在源码仓库里；纯 exe 用户可只装「开机自启 + 定时」两项。

## 已知限制

- **仅 Windows**：依赖任务计划程序与 `netsh`
- **仅支持已适配的认证系统**：当前针对锐捷 ePortal（`InterFace.do` + `nasip` 会话令牌）
- **会话令牌会过期**：锐捷的 `nasip` 是会话级的，断网重连后可能失效。
  届时 `options.query_string` 需要刷新（见 README 第 1 节）
- **明文密码**：`config.json` 按用户选择以明文保存，请勿提交到任何仓库
  （仓库的 `.gitignore` 已排除它）
- **读取当前 Wi-Fi 名称需要管理员权限**：非管理员时无法确认 SSID，
  但**不影响认证**（认证只看联网与否），只是无法自动切回校园网

## 说明

认证协议实现来自开源项目 [campusnet](https://github.com/demo133/campusnet)（MIT），
本项目在其基础上补齐了触发时机与 Windows 部署，并修复了若干上游问题
（子进程输出编码判定、`nasip` 字段拼法、门户消息解码等）。

本项目以 MIT 许可发布，详见 `LICENSE`。
