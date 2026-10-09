# 校园网自动登录认证工具（TJUS / 锐捷 ePortal）

连上校园网 Wi-Fi 自动认证 · 开机自启 · 断网自动重连 · 账号密码已预置。

**本工具不重复造轮子**：认证协议由成熟开源项目
[`demo133/campusnet`](https://github.com/demo133/campusnet)（MIT）驱动，
本项目在它之上补齐了「触发时机」和「部署」这两块。

**两种运行方式，任选其一**：

| 方式 | 目标机器需要 Python？ | 说明 |
|---|---|---|
| 脚本模式 | **需要**（Python 3.8+） | 直接跑 `*.py`，改代码方便 |
| **独立 exe 模式** | **完全不需要** | 跑 `build_exe.ps1` 打包，之后只留 exe |

---

## 0. 目标机器没有 Python 怎么办

用 `build_exe.ps1` 打包成独立 exe —— 解释器和标准库都会打进去，
**目标机器不需要装 Python、不需要装任何依赖**。

```powershell
# 在「有 Python」的机器上打包（打包这一步需要 Python，只有目标机器不需要）
powershell -ExecutionPolicy Bypass -File .\campusnet-autologin\build_exe.ps1
```

产出（放在项目根目录）：

```
run_login\run_login.exe     控制台版 —— 供任务计划触发，认证一次就退出
daemon\daemon.exe           无窗口版 —— 常驻守护，开机抢网 + 定期巡检
```

然后用 `-UseExe` 安装任务计划，让它指向 exe：

```powershell
# 先用 -DryRun 免费检查一遍（不需要管理员）
powershell -ExecutionPolicy Bypass -File .\campusnet-autologin\install_tasks.ps1 -UseExe -DryRun

# 正式安装（会弹 UAC）
Start-Process powershell -Verb RunAs -ArgumentList `
  '-ExecutionPolicy Bypass -File "C:\Users\ember\Downloads\test\campusnet-autologin\install_tasks.ps1" -UseExe'
```

### 搬到别的电脑时要带哪些文件

```
campusnet-autologin\
├─ config.json          ← 必须（账号密码、门户、query_string）
├─ logs\                ← 可选（没有会自动创建）
├─ run_login\           ← 必须（整个目录，不只 exe）
└─ daemon\              ← 必须（整个目录，不只 exe）
```

`campusnet\`（Python 源码）、`*.py`、`*.ps1` 在 exe 模式下**都不是必需的**，
但建议保留 —— 换电脑/换学校时还要用它们改配置和重装任务。

### 为什么选独立 exe 而不是别的方案

- **不选 `--onefile`**：每次启动都要解压到临时目录，慢；而这两个程序分别是
  「开机就要立刻抢网」和「每 5 分钟被触发一次」，启动开销很敏感。
  `--onedir` 启动快、杀软误报也少。
- **不选 Windows 服务**：服务跑在 Session 0，拿不到用户会话的网络上下文，
  且改配置要管理员权限。任务计划 + 常驻进程更适合这个场景。
- **为什么要两个 exe**：守护进程必须无窗口（`--noconsole`），
  否则你屏幕上会一直挂着一个黑框；而触发式任务用控制台版，
  手动双击时还能看到输出，排障方便。

---

## 1. 适用环境（实测确认）

以下是本工具在真实环境中实测确认的参数。**换学校/换认证系统时需要相应修改 `config.json`。**

| 项目 | 值 | 说明 |
|---|---|---|
| 认证门户 | `http://192.168.24.65` | 认证服务器地址 |
| 认证系统 | **锐捷 ePortal**（`Server: ruijie`，`RG-SAM+ Portal 组件`） | 由响应头与页面标题识别 |
| 校园 Wi-Fi | `tjus_wifi` | 用于「连错网时自动切回」 |
| 客户端网络 | 校园网 DHCP 段（如 `10.x.x.x`） | 由 `ipconfig` 读取 |

> ⚠️ 本仓库**不包含任何真实账号、密码或会话令牌**。
> 真实凭据只存在于本地 `config.json`（已被 `.gitignore` 排除）。
> 你需要复制 `config.example.json` 为 `config.json` 并填入自己的账号密码。

### 关于锐捷 ePortal 的 `nasip` 字段 —— 一个关键坑

**现象**：登录接口一律返回 `设备未注册,请在ePortal上添加认证设备`，
而且**错误密码和正确密码返回得一模一样** —— 看起来像是账号密码问题，
实际上是**请求本身拼错了**。

用真实地址栏地址做对照实验，结论非常明确：

| `queryString` 内容 | 门户返回 |
|---|---|
| **真实地址栏原样**（含 `nasip=<会话令牌>`） | ✅ `{"result":"success","message":"账号XXXX已经在线！"}` |
| `nasip` 清空 | ❌ `设备未注册,请在ePortal上添加认证设备` |
| 删掉 `nasip` 字段 | ❌ 同上 |
| `nasip` 填门户 URL（**上游旧版的错误兜底**） | ❌ 同上 |
| 空 | ❌ 同上 |

**根因**：锐捷 ePortal 的 `nasip` 字段必须填**会话令牌**（一串 hex），
而只有「网关跳转过来的那个带参数地址」里才有它。上游 `campusnet`
在门户页面里抠不到 `queryString` 时，会退化成自己拼：

```
wlanuserip=<本机IP>&wlanacname=&nasip=http://192.168.24.65   ← nasip 填成了门户 URL
```

于是门户一律回「设备未注册」。**看起来像账号密码错，实际是请求本身拼错了** ——
这也是为什么错误密码和正确密码返回得一模一样。

顺带一提，`success.jsp?userIndex=` 里那串解开是：

```
<会话令牌> _ <客户端IP> _ <上网账号>
```

中间那段（`nasip` 该填的令牌）就是登录成功的关键。

**已做的修复**（`campusnet/providers/ruijie.py` 与 `campusnet/runner.py`）：

1. `_fetch_query_string` 现在**优先从门户/跳转 URL 取**完整查询串 → 再往页面里抠
   → 最后才自拼（且 `nasip` 留空，不再填门户 URL），失败信息里附带补救办法；
2. 新增 `discover_portal_url()`：认证前主动索取一次「带令牌的门户地址」；
3. `_decode_message()` 修掉门户消息的编码问题 —— 它把 UTF-8 字节当 latin-1 塞进
   JSON，导致 `账号…已经在线` 显示成乱码，**并使「已在线」判定永远不成立**；
4. `already_online` 与「刚认证成功」在日志里分开表述，不再互相掩盖。

### ⚠️ 关于 `options.query_string`（会话令牌会过期）

`config.json` 里 `options.query_string` 就是那条**真实地址栏查询串**，当前可正常认证。注意：

- 它内含一个**会话令牌**（`nasip=xxxxxxxx…`）。令牌是会话级的，
  **断网重连或退出登录后可能失效**，届时又会报「设备未注册」；
- 失效时刷新办法：**断网后在浏览器打开认证页，把地址栏 `?` 后面那一整串
  拷贝出来，替换 `options.query_string` 的值**；
- 也可以把它清空：那时工具会走「主动索取 + 页面抠取」路径，能拿到就用，
  拿不到会在日志里明确告诉你该怎么做。

---

## 2. 目录结构

```
campusnet-autologin/
├─ config.json               ← 账号密码/门户/Wi-Fi 全在这里（唯一需要改的文件）
├─ run_login.py              ← 触发式：认证一次就退出（被任务计划调用）
├─ daemon.py                 ← 常驻守护：开机抢网 + 定期巡检 + 断网重连
├─ discover_portal.py        ← 门户探测：换校区/换认证地址时用来找正确地址
├─ install_tasks.ps1         ← 一键安装（开机自启 + 连网触发 + 兜底巡检）
├─ uninstall_tasks.ps1       ← 一键卸载
├─ status.ps1                ← 一屏状态自检（最常用的排障入口）
├─ campusnet/                ← 引擎：vendor 自 demo133/campusnet（MIT）
├─ LICENSE.campusnet         ← 上游 MIT 许可证（保留）
└─ logs/                     ← 运行日志（自动创建、自动滚动）
```

---

## 3. 安装状态：**已完成**

四个任务计划已于本机注册并逐项验证通过（`State = Ready`）：

| 任务名 | 触发时机 | 执行 | 状态 |
|---|---|---|---|
| `CampusNet-AutoLogin-Daemon` | 登录后 20 秒（`PT20S`） | `pythonw.exe daemon.py --interval 3` | Ready |
| `CampusNet-AutoLogin-OnNetChange-Connected` | **事件 10000**（网络已连接），延迟 3 秒 | `python.exe run_login.py --quiet` | Ready |
| `CampusNet-AutoLogin-OnNetChange-Disconnected` | **事件 10001**（网络已断开），延迟 5 秒 | `python.exe run_login.py --quiet` | Ready |
| `CampusNet-AutoLogin-Heartbeat` | 每 5 分钟（`PT5M`） | `python.exe run_login.py --quiet` | Ready |

事件源：`Microsoft-Windows-NetworkProfile/Operational`。

守护进程刻意用 `pythonw.exe`（无控制台窗口），触发式任务用 `python.exe`。

### 需要重装 / 换机器时

```powershell
# 先只读预检（不需要管理员，可反复跑）
powershell -ExecutionPolicy Bypass -File .\campusnet-autologin\install_tasks.ps1 -DryRun

# 正式安装（会弹 UAC）
Start-Process powershell -Verb RunAs -ArgumentList `
  '-ExecutionPolicy Bypass -File "C:\Users\ember\Downloads\test\campusnet-autologin\install_tasks.ps1"'
```

### 关于「连上校园网 WiFi 自动认证」是怎么做到的

认证的必要条件只有一个：**没网**。所以本工具的判定依据是「有没有网」，
而不是「Wi-Fi 连没连上」——这样无论你用无线还是有线，都会自动认证。

负责「立刻」的那一环是任务计划绑定的网络事件（上面那两个 `OnNetChange-*`）。

---

## 4. 日常使用

```powershell
# 看状态（联网了吗、任务在不在、守护活着吗、最近日志）
powershell -ExecutionPolicy Bypass -File .\campusnet-autologin\status.ps1

# 手动认证一次
python .\campusnet-autologin\run_login.py

# 强制重新认证（即使已联网）
python .\campusnet-autologin\run_login.py --force

# 只测认证、不碰 Wi-Fi
python .\campusnet-autologin\run_login.py --no-wifi

# 卸载（结束守护 + 删除三个任务）
Start-Process powershell -Verb RunAs -ArgumentList `
  '-ExecutionPolicy Bypass -File "C:\Users\ember\Downloads\test\campusnet-autologin\uninstall_tasks.ps1"'
```

日志位置：`logs\campusnet.log`（触发式）、`logs\daemon.log`（守护）、
`logs\heartbeat.txt`（守护心跳，用于判断守护是否活着）。

---

## 5. 密码存储说明（你选择了明文）

`config.json` 里 `password` 字段是**明文**。这意味着：

- ✅ 直观、好排查，任何脚本都能直接读；
- ⚠️ 文件被复制、被 OneDrive/网盘同步、误传 Git，密码就泄露了；
- ⚠️ 同机其它用户若能读该目录，也能看到密码。

缓解措施：本目录在 `C:\Users\ember\Downloads\test\campusnet-autologin\`，
只在你自己的用户目录下。**请勿把 `config.json` 提交到任何 Git 仓库或发到聊天群。**

如果以后想换成加密存储，把 `config.json` 里的 `password` 清空，
改用环境变量即可（引擎原生支持，优先级：环境变量 > 配置文件）：

```powershell
[Environment]::SetEnvironmentVariable('CAMPUSNET_PASSWORD', '你的密码', 'User')
```

---

## 6. 排障

### 认证失败：`设备未注册` / `Web认证接入设备不存在`

**这不是账号密码错，是 `queryString` 里的会话令牌没给对。** 按顺序试：

1. **刷新 `options.query_string`（最常见的原因）**
   会话令牌会过期。断网后在浏览器打开认证页，把地址栏 `?` 后面那一整串复制出来，
   替换 `config.json` → `options.query_string` 的值。

2. **让工具自己去拿**
   清空 `options.query_string`，工具会在认证前主动索取「带令牌的门户地址」，
   并优先从网关跳转的 `Location` 里抓。看日志里有没有
   `取到带会话令牌的门户地址：…` / `网关劫持目标即为门户：…`。

3. **扫一遍网段，确认门户地址本身没变**
   ```powershell
   python .\campusnet-autologin\discover_portal.py
   ```
   它会排除代理 fake-ip 段，并逐个判断哪个地址「认你这台设备」。

4. 以上都不行才需要考虑门户侧问题（接入设备未登记），这时找校园网管理员。

### 日志说「读不到当前 Wi-Fi 名称：netsh 需要管理员权限」

**正常现象，不影响认证。** 认证只看联网与否。
这条只是因为 Windows 限制：普通用户跑 `netsh wlan show interfaces` 会返回 error 5，
所以工具无法确认「当前 SSID 是不是校园网」。若你希望它在连错 Wi-Fi 时
自动切回 `tjus_wifi`，请把 `CampusNet-AutoLogin-Daemon` 任务改成
「使用最高权限运行」（以管理员身份运行 `status.ps1` 会提示状态）。

### 想换校园网 / 换 Wi-Fi 名称

改 `config.json` 的 `wifi_ssid`。留空则完全不做 Wi-Fi 检查（只认证）。

### 想调整巡检频率

```powershell
# 守护巡检间隔 2 分钟、兜底每 10 分钟（需管理员）
Start-Process powershell -Verb RunAs -ArgumentList `
  '-ExecutionPolicy Bypass -File ".\campusnet-autologin\install_tasks.ps1" -Interval 2 -HeartbeatMinutes 10'
```

### 只想省内存（不要常驻守护）

```powershell
... -File ".\campusnet-autologin\install_tasks.ps1" -NoDaemon
```

---

## 7. 本机实测记录（用于说明「为什么这么写」）

这些都是在本机实测踩到并修掉的真实问题，不是理论推测：

| 现象 | 根因 | 处理 | 效果 |
|---|---|---|---|
| 守护每轮空等 40 秒 + `netsh wlan connect` 徒劳重试 3 次 | 本机控制台代码页是 65001（UTF-8），netsh 在 `CREATE_NO_WINDOW` 下**改吐 UTF-8 中文**；上游 `text=True` 用解释器默认编码去解，中文全成乱码（`璇锋眰…`），导致按中文关键词的权限判定**静默失效** | `wifi.py` 新增 `_decode_output()`：**UTF-8 优先、GBK 兜底**，不再写死编码；识别「权限不足」后诚实降级 | 38.4 秒 → **1.6 秒** |
| 无管理员权限时日志吓唬人：「网络没切过去，认证一定不会成功」 | 上游把「查不到 SSID」当成「没连上」 | `runner.py`：权限受限时降级为 debug（认证本就不依赖它） | 日志不再误导 |
| 门户探测扫到了 `198.18.0.x`（代理 fake-ip 段） | 本机有两条默认路由：代理 `198.18.0.2`（metric 0）排在校园网真实网关（metric 45）前面 | 改读 `route print -4` 取**全部**默认路由并按 metric 排序，排除 `198.18/198.19` | 扫描命中正确网段 |
| 安装脚本报 exit 2「没找到 Python」 | `Get-Command python.exe` 命中 **Windows 应用商店别名桩**（`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`），它调用返回 **9009**，不是真解释器 | 新增 `Test-RealPython()`：**实际执行一次**并校验输出是 `3.x`；候选按 pyenv `versions`（按主次版本**数字**排序，排除 2.7）→ 常见安装位置 → PATH 排序 | 正确选中 `…\versions\3.12.9\python.exe` 与 `pythonw.exe` |
| 安装脚本报「无法使用指定的命名参数解析参数集」 | `Register-ScheduledTask` 同时传 `-InputObject` 和 `-Principal` 会撞参数集 | 把 principal 交给 `New-ScheduledTask` | 修复 |
| 事件触发器报「参量类型不匹配 `MSFT_TaskTrigger`」 | PowerShell 5.1 对 `New-CimInstance -ClassName MSFT_TaskEventTrigger` 造的实例做 PSTypeName 严格校验 | 改用**任务计划原生 XML** 注册事件任务（`Register-ScheduledTask -Xml`），并把 10000/10001 拆成两个任务（触发器属性不能重复赋值） | 事件触发任务可注册 |
| 心跳任务报「任务 XML 包含格式不正确或超出范围的值：`P99999999DT23H59M59S`」 | `-RepetitionDuration ([TimeSpan]::MaxValue)` 会被序列化成非法时长 | 改用合法 ISO8601 `P365D` | 心跳任务可注册 |
| `status.ps1` / 安装脚本中文全乱码、甚至语法报错 | ① PowerShell 5.1 按 ANSI 读取无 BOM 的 `.ps1`；② 编辑工具写回文件时会**去掉 BOM 并把换行改成 LF**，与原有 CRLF 混用后解析错乱 | 所有 `.ps1` 统一 **UTF-8 BOM + CRLF**（`recon/normalize_ps1.py` 可复现） | 中文正常、语法通过 |
| **登录一律返回「设备未注册」**，错误密码与正确密码无差别 | 上游 `_fetch_query_string` 抠不到 `queryString` 时自拼，把**门户 URL 塞进了 `nasip`** —— 而这个字段必须是**会话令牌** | 改为优先从门户/跳转 URL 取，自拼时 `nasip` 留空并给出补救提示；新增 `discover_portal_url()`；真实 `queryString` 写入 `options.query_string` | 门户返回 **`result=success`** |
| 门户消息在日志里是乱码，且「已在线」判定失效 | 门户把 UTF-8 字节当 latin-1 塞进 JSON，`requests` 按 charset 解出乱码，`"已在线" in message` 永远不成立 | 新增 `_decode_message()`：latin-1→utf-8 逆变换，且只在「修完出现中文而原文没有」时采用 | 消息可读，`already_online` 正确识别 |

> ⚠️ 维护提示：**只要用编辑器改过 `*.ps1`，就要重新补 BOM 并统一 CRLF**，
> 否则 Windows PowerShell 5.1 会乱码或直接语法报错。

---

## 8. 上游项目与参考

- 引擎：[demo133/campusnet](https://github.com/demo133/campusnet) — MIT，零依赖，
  自动识别 Dr.COM／深澜／锐捷／华为 ePortal，本项目的认证逻辑全部来自它。
- 同类参考：[Redlnn/Ruijie-ePorta-Tool](https://github.com/Redlnn/Ruijie-ePorta-Tool)（AGPL-3.0）、
  [Zhanghaohao666/hust-campus-autologin](https://github.com/Zhanghaohao666/hust-campus-autologin)（MIT）、
  [impecme/SchoolWeb-AutoConnect](https://github.com/impecme/SchoolWeb-AutoConnect)（MIT）、
  [heragehome/haust-auto-login](https://github.com/heragehome/haust-auto-login)（MIT，事件触发写法参考）。

上游改动清单（本项目对 `campusnet/` 的修改，均已在代码里注明原因）：

1. `campusnet/wifi.py`
   - 新增 `_decode_output()`：自动判定子进程输出是 UTF-8 还是 GBK；
   - `_run()` 不再用 `text=True`（那会交给解释器默认编码，随代码页变化）；
   - 新增 `permission_denied()` / `_warn_elevation_once()`：权限受限时跳过
     无意义的等待与重试，并只提示一次。
2. `campusnet/runner.py`
   - 权限受限导致的 Wi-Fi 跳过不再输出「认证一定不会成功」的误导日志；
   - `ensure_online()` 新增 `portal_url` 参数（带令牌的门户地址优先）；
   - `already_online` 与「刚认证成功」分开表述。
3. `campusnet/providers/ruijie.py`
   - `_fetch_query_string()` 改为：**门户/跳转 URL 优先** → 页面抠取 → 自拼兜底，
     自拼时 `nasip` 不再被填成门户 URL，失败信息附带补救办法；
   - 新增 `_query_from_url()` / `_query_from_text()` / `_decode_message()`。

`run_login.py` 新增：

- `discover_portal_url()` —— 认证前主动索取带令牌的门户地址；
- `--no-web-probe` —— 关闭「访问公网地址试探网关劫持」这一步；
- `--quiet` 现在只静音控制台，**文件日志始终完整**。

---

## 9. 许可证

本项目以 **MIT** 许可发布，详见 [`LICENSE`](LICENSE)。

认证协议实现来自 [campusnet](https://github.com/demo133/campusnet)（MIT），
其完整许可证见 [`LICENSE.campusnet`](LICENSE.campusnet)，
源码副本与改动说明见 [`NOTICE.md`](NOTICE.md)。

---

## 10. 免责声明

本工具仅用于**自己账号在自己设备上**的自动认证，目的是省去每次手动打开认证页的麻烦。

- 请勿用于他人账号、批量代认证或任何绕过计费/认证机制的行为；
- 自动认证会把账号密码保存在本机 `config.json`（明文），请自行评估风险；
- 因使用本工具导致的账号异常、网络故障或与校方规定的冲突，由使用者自行承担。

