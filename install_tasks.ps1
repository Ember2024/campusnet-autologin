<#
.SYNOPSIS
    安装校园网自动认证的开机自启、连网触发与定期巡检任务计划。

.DESCRIPTION
    一共注册三样东西，互为保险（任一失效都还有别的兜住）：

    1. CampusNet-AutoLogin-Daemon
       开机（登录）后自动拉起的常驻守护，20 秒后启动。负责高频抢网
       （配置 wifi_warmup 秒内每 15 秒一次）+ 常规巡检（默认 3 分钟）。
       能用哪个 python 就用哪个，优先 pythonw.exe（无控制台窗口）。

    2. CampusNet-AutoLogin-OnNetChange
       **连上校园网 Wi-Fi / 插上网线就立刻抢跑一次认证。**
       触发源是事件日志：
         · Microsoft-Windows-NetworkProfile/Operational  事件 10000（网络已连接）
         · Microsoft-Windows-NetworkProfile/Operational  事件 10001（网络已断开）
       只订阅“已连接”，所以切网瞬间就会认证，不必等巡检周期。

    3. CampusNet-AutoLogin-Heartbeat
       每 5 分钟兜底检查一次（错过事件、守护被结束、系统休眠唤醒都能补回来）。

    注册需要管理员权限（任务计划程序的按需启动任务必须如此）。

.PARAMETER Interval
    守护进程联网正常时的巡检间隔（分钟），默认 3。

.PARAMETER HeartbeatMinutes
    兜底任务的重复间隔（分钟），默认 5。

.PARAMETER PythonPath
    指定 python.exe / pythonw.exe 路径；不指定则自动探测。

.PARAMETER NoDaemon
    不注册常驻守护，只装事件触发 + 兜底巡检（更省内存）。

.PARAMETER DryRun
    只做只读预检（查找 Python、验证引擎可导入），**不注册任何任务**，
    也不需要管理员权限。环境有问题时先跑这个。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
#>
[CmdletBinding()]
param(
    [int]$Interval = 3,
    [int]$HeartbeatMinutes = 5,
    [string]$PythonPath = "",
    [switch]$NoDaemon,
    [switch]$DryRun,
    [switch]$UseExe,
    [string]$ExeDir = ""
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ProjectDir 'config.json'
$RunLogin   = Join-Path $ProjectDir 'run_login.py'
$Daemon     = Join-Path $ProjectDir 'daemon.py'

#: 打包产物目录（``build_exe.ps1`` 生成）。
#: 指定 -UseExe 后，任务计划就指向这两个 exe，目标机器不需要 Python。
$ExeRoot      = if ($ExeDir) { $ExeDir } else { $ProjectDir }
$RunLoginExe  = Join-Path $ExeRoot 'run_login\run_login.exe'
$DaemonExe    = Join-Path $ExeRoot 'daemon\daemon.exe'

$TaskDaemon    = 'CampusNet-AutoLogin-Daemon'
$TaskNetChange = 'CampusNet-AutoLogin-OnNetChange'
$TaskHeartbeat = 'CampusNet-AutoLogin-Heartbeat'

function Write-Step($Message) { Write-Host "[*] $Message" -ForegroundColor Cyan }
function Write-Ok($Message)   { Write-Host "[+] $Message" -ForegroundColor Green }
function Write-Warn2($Message){ Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Err($Message)  { Write-Host "[x] $Message" -ForegroundColor Red }

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-RealPython([string]$Exe) {
    <#
        判断一个 python.exe 是不是「真的能跑」。

        这一步绝对必要：Windows 10/11 自带的「应用执行别名」会在
        %LOCALAPPDATA%\Microsoft\WindowsApps 放一个 python.exe 空壳，
        Get-Command 会命中它，但它只在你亲自去商店装过 Python 之后才转发，
        否则调用直接返回 9009（命令不存在）。把这种桩子注册进任务计划，
        开机自启就会静默失败 —— 用户看到的现象是「装了但没生效」。
    #>
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        $out = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        # 必须真的是 Python 3：本机 pyenv 下还装着 2.7.18，它跑不了本项目的代码
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -match '^3\.\d+$')
    } catch {
        return $false
    }
}

function Is-AppAliasStub([string]$Exe) {
    if (-not $Exe) { return $false }
    return ($Exe -like '*\Microsoft\WindowsApps\*')
}

function Resolve-Python([string]$Explicit, [switch]$Windowless) {
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) {
            throw "指定的 Python 不存在：$Explicit"
        }
        if (-not (Test-RealPython $Explicit)) {
            throw "指定的 Python 跑不起来（可能只是应用商店别名）：$Explicit"
        }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }

    # 候选按「可信度」排序：真实解释器目录 > PATH > 商店别名桩
    $candidates = New-Object System.Collections.Generic.List[string]

    # 1) pyenv-win 的真实解释器（本机就是这种安装方式）
    #
    #    注意两个坑：
    #    · pyenv-win 的 shims 目录里是 python.bat，**没有** python.exe，
    #      所以不能只找 shims；
    #    · versions 目录下可能同时装了 Python 2（例如 2.7.18-win32），
    #      而字符串降序会把 "2.7.18-win32" 排在 "3.12.9" 前面 ——
    #      所以必须按「主.次」版本号**数字**排序，并只保留 Python 3。
    $pyenvVersions = "$env:USERPROFILE\.pyenv\pyenv-win\versions"
    if (Test-Path -LiteralPath $pyenvVersions) {
        $ranked = Get-ChildItem -Path $pyenvVersions -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $m = [regex]::Match($_.Name, '^(\d+)\.(\d+)')
                if ($m.Success) {
                    [pscustomobject]@{
                        Dir   = $_.FullName
                        Major = [int]$m.Groups[1].Value
                        Minor = [int]$m.Groups[2].Value
                    }
                }
            } |
            Where-Object { $_.Major -ge 3 } |
            Sort-Object -Property @{Expression='Major';Descending=$true},
                                  @{Expression='Minor';Descending=$true}

        foreach ($item in $ranked) {
            $candidates.Add((Join-Path $item.Dir 'pythonw.exe'))
            $candidates.Add((Join-Path $item.Dir 'python.exe'))
        }
    }

    # 2) 常见官方安装位置
    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe",
        "$env:USERPROFILE\.pyenv\pyenv-win\shims\python.exe"
    )
    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add($_.FullName) }
    }

    # 3) PATH 里的（可能是别名桩，放在最后）
    foreach ($name in @('pythonw.exe', 'python.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates.Add($cmd.Source) }
    }

    $rejected = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        if (Test-RealPython $candidate) {
            if ($Windowless -and ([IO.Path]::GetFileName($candidate) -ieq 'python.exe')) {
                # 想要无窗口版却只找到 python.exe：看看同目录有没有 pythonw.exe
                $pw = Join-Path (Split-Path -Parent $candidate) 'pythonw.exe'
                if (Test-Path -LiteralPath $pw) { return (Resolve-Path -LiteralPath $pw).Path }
            }
            return (Resolve-Path -LiteralPath $candidate).Path
        }
        $tag = if (Is-AppAliasStub $candidate) { '应用商店别名桩，不可用' } else { '执行失败' }
        $rejected.Add("$candidate（$tag）")
    }

    if ($rejected.Count -gt 0) {
        Write-Host "[!] 以下候选 Python 被跳过：" -ForegroundColor Yellow
        $rejected | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    }
    return ""
}

function Get-Pythonw([string]$PythonExe) {
    $dir = Split-Path -Parent $PythonExe
    $name = [IO.Path]::GetFileNameWithoutExtension($PythonExe)
    if ($name -ieq 'pythonw') { return $PythonExe }
    $candidate = Join-Path $dir 'pythonw.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $PythonExe
}

function ConvertTo-XmlText([string]$Text) {
    if ($null -eq $Text) { return '' }
    return $Text.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;').Replace('"', '&quot;')
}

function New-EventTaskXml {
    <#
        生成「事件触发」任务的原生 XML。

        为什么走 XML 而不是 New-CimInstance + MSFT_TaskEventTrigger：
        PowerShell 5.1 会对该对象的 PSTypeName 做严格校验，手工构造的实例
        会在 New-ScheduledTask 处报「参量类型不匹配」而无法注册。
        XML 是任务计划程序的原生交换格式，最稳且字段最全。
    #>
    param(
        [string]$TaskName,
        [int]$EventId,
        [string]$Delay = 'PT3S',
        [string]$Title = '',
        [string]$Description = '',
        [string]$Execute,
        [string]$Arguments,
        [string]$WorkingDirectory
    )

    $logName = 'Microsoft-Windows-NetworkProfile/Operational'
    $userId = "$env:USERDOMAIN\$env:USERNAME"
    $author = ConvertTo-XmlText $userId

    $command = ConvertTo-XmlText $Execute
    $argsXml = ConvertTo-XmlText $Arguments
    $workDir = ConvertTo-XmlText $WorkingDirectory
    $descXml = ConvertTo-XmlText $Description
    $titleXml = ConvertTo-XmlText $Title

    # 注意：路径里的 & 必须转义成 &amp;，否则 XML 非法。
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$author</Author>
    <Description>$descXml</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="$logName"&gt;&lt;Select Path="$logName"&gt;*[System[EventID=$EventId]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>$Delay</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$author</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$command</Command>
      <Arguments>$argsXml</Arguments>
      <WorkingDirectory>$workDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function New-OrReplaceTask {
    param(
        [string]$Name,
        [object[]]$Triggers,
        [string]$Execute,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$Description,
        [string]$UserId,
        [switch]$RunAsSystem
    )

    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue

    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $WorkingDirectory

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10))

    if ($RunAsSystem) {
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } else {
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest
    }

    # 注意：principal 必须交给 New-ScheduledTask，**不能**再给
    # Register-ScheduledTask 传 -Principal —— 同时传 -InputObject 和
    # -Principal 会撞参数集，报「无法使用指定的命名参数解析参数集」。
    $task = New-ScheduledTask -Action $action -Trigger $Triggers -Settings $settings `
        -Principal $principal -Description $Description
    Register-ScheduledTask -TaskName $Name -InputObject $task -Force | Out-Null
}

# ----------------------------------------------------------------- 前置检查
Write-Host ""
Write-Host "校园网自动认证 —— 任务计划安装" -ForegroundColor White
Write-Host ("=" * 62) -ForegroundColor DarkGray

foreach ($required in @($ConfigPath, $RunLogin, $Daemon)) {
    if (-not (Test-Path -LiteralPath $required)) {
        Write-Err "缺少必要文件：$required"
        exit 2
    }
}

$currentUser = "$env:USERDOMAIN\$env:USERNAME"

# ------------------------------------------------- 只读预检（不需要管理员）
# Python 解析与引擎自检刻意放在权限检查**之前**：
# 这样可以用 -DryRun 在普通权限下先把环境问题查清楚，
# 不必每次都弹一次 UAC 才发现 Python 路径不对。
Write-Step "当前用户：$currentUser"

if ($UseExe) {
    # ------- 免 Python 模式：直接指向打包好的 exe -------
    Write-Step "运行模式：独立 exe（目标机器不需要 Python）"
    foreach ($exe in @($RunLoginExe, $DaemonExe)) {
        if (-not (Test-Path -LiteralPath $exe)) {
            Write-Err "找不到打包产物：$exe"
            Write-Host "    请先运行 build_exe.ps1 生成 exe：" -ForegroundColor Yellow
            Write-Host "      powershell -ExecutionPolicy Bypass -File `"$ProjectDir\build_exe.ps1`"" -ForegroundColor Gray
            exit 2
        }
    }
    Write-Ok "run_login.exe：$RunLoginExe"
    Write-Ok "daemon.exe   ：$DaemonExe"

    # 真正跑一次，确认 exe 能启动、能找到旁边的 config.json
    Write-Step "预检：实际执行 run_login.exe 一次…"
    & $RunLoginExe --config $ConfigPath --quiet
    $exeCode = $LASTEXITCODE
    if ($exeCode -eq 0) {
        Write-Ok "run_login.exe 运行正常（已联网或认证成功，exit 0）"
    } elseif ($exeCode -eq 1) {
        Write-Warn2 "run_login.exe 能跑，但认证未成功（exit 1）—— 请看 logs\campusnet.log"
    } else {
        Write-Err "run_login.exe 执行异常（exit $exeCode）"
        Write-Host "    日志：$(Join-Path $ProjectDir 'logs\campusnet.log')" -ForegroundColor Gray
        exit 2
    }

    # 任务计划要执行的东西
    $execRunLogin = $RunLoginExe
    $argsForRun  = "--config `"$ConfigPath`" --quiet"
    $execDaemon  = $DaemonExe
    $argsForDaemon = "--config `"$ConfigPath`" --interval $Interval"
} else {
    # ------- 脚本模式：需要本机 Python -------
    Write-Step "运行模式：Python 脚本（本机需已安装 Python 3）"
    Write-Step "预检 1/2：查找可用的 Python 3 解释器…"
    $pythonPlain = Resolve-Python -Explicit $PythonPath
    if (-not $pythonPlain) {
        Write-Err "没找到可用的 Python 3。"
        Write-Host "    常见原因：PATH 里只有 Windows 应用商店的 python.exe 别名桩，它调用会返回 9009。" -ForegroundColor Yellow
        Write-Host "    两个办法：" -ForegroundColor Yellow
        Write-Host "      A) 指定真实解释器：-PythonPath `"$env:USERPROFILE\.pyenv\pyenv-win\versions\3.12.9\python.exe`"" -ForegroundColor Gray
        Write-Host "      B) 打包成免 Python 的 exe：先跑 build_exe.ps1，再用 -UseExe 安装" -ForegroundColor Gray
        exit 2
    }
    $pythonWindowless = Get-Pythonw -PythonExe $pythonPlain
    Write-Ok "Python（控制台版）：$pythonPlain"
    Write-Ok "Python（无窗口版）：$pythonWindowless"

    Write-Step "预检 2/2：确认 campusnet 引擎可导入…"
    $probe = & $pythonPlain -c "import sys; sys.path.insert(0, r'$ProjectDir'); import campusnet; print(campusnet.__version__ if hasattr(campusnet,'__version__') else 'ok')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "campusnet 引擎导入失败（exit $LASTEXITCODE）："
        Write-Host "    解释器：$pythonPlain" -ForegroundColor DarkGray
        Write-Host "    输出  ：$probe" -ForegroundColor DarkGray
        exit 2
    }
    Write-Ok "引擎版本：$probe"

    $execRunLogin = $pythonPlain
    $argsForRun  = "`"$RunLogin`" --config `"$ConfigPath`" --quiet"
    $execDaemon  = $pythonWindowless
    $argsForDaemon = "`"$Daemon`" --config `"$ConfigPath`" --interval $Interval"
}

if ($DryRun) {
    Write-Host ""
    Write-Host ("=" * 62) -ForegroundColor DarkGray
    Write-Ok "预检全部通过（-DryRun 模式，未注册任何任务计划）。"
    Write-Host "    账号配置：$ConfigPath" -ForegroundColor Gray
    Write-Host "    执行方式：$(if ($UseExe) { '独立 exe' } else { 'Python 脚本' })" -ForegroundColor Gray
    Write-Host "    去掉 -DryRun 并以管理员身份运行即可正式安装。" -ForegroundColor Gray
    exit 0
}

if (-not (Test-Admin)) {
    Write-Err "需要管理员权限：任务计划程序注册“按需/事件触发”任务必须管理员。"
    Write-Warn2 "请以管理员身份打开 PowerShell 再运行本脚本，例如："
    Write-Host "    Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy Bypass -File `"$PSCommandPath`"'" -ForegroundColor Gray
    Write-Host "    （想先检查环境可以加 -DryRun，那样不需要管理员）" -ForegroundColor Gray
    exit 1
}

# Python 解析与引擎自检已在上面（权限检查之前）做过，这里不再重复。

# ----------------------------------------------------------------- 1) 守护
if (-not $NoDaemon) {
    Write-Step "注册常驻守护：$TaskDaemon（登录后 20 秒启动，巡检 ${Interval} 分钟）"

    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    # AtLogOn 触发器默认没有延迟，补 20 秒让 WlanSvc / 无线驱动先就绪
    $logonTrigger.Delay = 'PT20S'

    New-OrReplaceTask -Name $TaskDaemon -Triggers @($logonTrigger) `
        -Execute $execDaemon `
        -Arguments $argsForDaemon `
        -WorkingDirectory $ProjectDir `
        -Description "校园网自动认证常驻守护：开机抢网 + 定期巡检 + 断网重连" `
        -UserId $currentUser

    Write-Ok "已注册 $TaskDaemon"
} else {
    Unregister-ScheduledTask -TaskName $TaskDaemon -Confirm:$false -ErrorAction SilentlyContinue
    Write-Warn2 "按 -NoDaemon 要求，未注册常驻守护"
}

# ------------------------------------------------- 2) 连上网络就抢跑一次认证
#
# 这里改用**任务计划原生 XML** 注册，而不是 New-CimInstance 造 MSFT_TaskEventTrigger。
# 原因：PowerShell 5.1 的 Register-ScheduledTask/New-ScheduledTask 对
# MSFT_TaskTrigger 的 PSTypeName 校验很挑，手工 New-CimInstance 出来的对象
# 会被判「参量类型不匹配」而注册失败。XML 是这个场景下唯一可靠的路子，
# 而且能把 EventTrigger 的 Subscription 精确写进去。
# 另外 10000（已连接）和 10001（已断开）拆成两个任务：
# 事件触发器对象的 Delay/Enabled 属性在一个对象里不能重复赋值。
Write-Step "注册连网触发：网络已连接/已断开事件 → 立即认证"

$netChangeTasks = @(
    [pscustomobject]@{
        Name   = "$TaskNetChange-Connected"
        Event  = 10000
        Title  = '网络已连接（连上校园网 Wi-Fi / 插网线）'
        Delay  = 'PT3S'      # 给 DHCP / 路由就绪留 3 秒
    },
    [pscustomobject]@{
        Name   = "$TaskNetChange-Disconnected"
        Event  = 10001
        Title  = '网络已断开'
        Delay  = 'PT5S'      # 断开后系统常会自动重连，稍等一下再认证
    }
)

foreach ($item in $netChangeTasks) {
    $xml = New-EventTaskXml -TaskName $item.Name -EventId $item.Event `
        -Delay $item.Delay -Title $item.Title -Description "$($item.Title)：校园网自动认证" `
        -Execute $execRunLogin -Arguments $argsForRun `
        -WorkingDirectory $ProjectDir
    Register-ScheduledTask -TaskName $item.Name -Xml $xml -Force | Out-Null
    Write-Ok "已注册 $($item.Name)（事件 $($item.Event)）"
}

# ----------------------------------------------------- 3) 心跳兜底巡检
Write-Step "注册兜底巡检：$TaskHeartbeat（每 ${HeartbeatMinutes} 分钟一次）"

$start = (Get-Date).AddMinutes(1)
$heartbeatTriggers = @()
$heartbeatTriggers += New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $HeartbeatMinutes)

# RepetitionDuration 必须是**有限**值。传 [TimeSpan]::MaxValue 会让
# 任务计划程序报「任务 XML 包含格式不正确或超出范围的值」——
# 因为它会被序列化成 P99999999DT23H59M59S 这种非法时长（实测踩到）。
# 这里用 ISO8601 写「一年」，到期后靠 StartWhenAvailable 与其它任务继续兜底。
$heartbeatTriggers[0].Repetition.Duration = 'P365D'

New-OrReplaceTask -Name $TaskHeartbeat -Triggers $heartbeatTriggers `
    -Execute $execRunLogin `
    -Arguments $argsForRun `
    -WorkingDirectory $ProjectDir `
    -Description "校园网自动认证兜底巡检：每 ${HeartbeatMinutes} 分钟确认一次联网状态，掉线即重连" `
    -UserId $currentUser

Write-Ok "已注册 $TaskHeartbeat"

# ----------------------------------------------------------------- 汇总与验证
Write-Host ""
Write-Host ("=" * 62) -ForegroundColor DarkGray
Write-Ok "安装完成，逐项验证如下："

$registered = @(Get-ScheduledTask -TaskName 'CampusNet-AutoLogin-*' -ErrorAction SilentlyContinue)
if ($registered.Count -eq 0) {
    Write-Err "一个任务都没注册成功，请把上面的报错发给我。"
    exit 1
}

foreach ($task in $registered) {
    Write-Host ("  ● {0}  [{1}]" -f $task.TaskName, $task.State) -ForegroundColor White
    foreach ($trigger in $task.Triggers) {
        $kind = $trigger.CimClass.CimClassName
        switch -Wildcard ($kind) {
            '*LogonTrigger' {
                Write-Host ("      触发：登录时（延迟 {0}）" -f $trigger.Delay) -ForegroundColor Gray
            }
            '*EventTrigger' {
                $ids = ([regex]::Matches($trigger.Subscription, 'EventID=(\d+)') |
                        ForEach-Object { $_.Groups[1].Value }) -join ','
                Write-Host ("      触发：事件日志 EventID={0}（延迟 {1}）" -f $ids, $trigger.Delay) -ForegroundColor Gray
            }
            '*TimeTrigger' {
                $rep = if ($trigger.Repetition) { $trigger.Repetition.Interval } else { '-' }
                Write-Host ("      触发：定时，每 {0}" -f $rep) -ForegroundColor Gray
            }
            default {
                Write-Host ("      触发：{0}" -f $kind) -ForegroundColor Gray
            }
        }
    }
    $exec = $task.Actions[0]
    Write-Host ("      执行：{0}" -f (Split-Path -Leaf $exec.Execute)) -ForegroundColor Gray
    Write-Host ("      参数：{0}" -f $exec.Arguments) -ForegroundColor DarkGray
    Write-Host ("      目录：{0}" -f $exec.WorkingDirectory) -ForegroundColor DarkGray
}

$expected = 3
if (-not $NoDaemon) { $expected = 4 }
if ($registered.Count -lt 3) {
    Write-Warn2 "只注册了 $($registered.Count) 个任务，少于预期的至少 3 个。"
}

Write-Host ""
Write-Host "常用命令：" -ForegroundColor White
if ($UseExe) {
    # exe 模式下提示 python 命令会让人以为还得装 Python，这里按实际模式给命令
    Write-Host "  立即认证一次    : & `"$RunLoginExe`" --config `"$ConfigPath`"" -ForegroundColor Gray
    Write-Host "  强制重新认证    : & `"$RunLoginExe`" --config `"$ConfigPath`" --force" -ForegroundColor Gray
    Write-Host "  守护自检一轮    : & `"$DaemonExe`" --config `"$ConfigPath`" --once-check" -ForegroundColor Gray
} else {
    Write-Host "  立即认证一次    : python `"$RunLogin`" --config `"$ConfigPath`"" -ForegroundColor Gray
    Write-Host "  强制重新认证    : python `"$RunLogin`" --config `"$ConfigPath`" --force" -ForegroundColor Gray
}
Write-Host "  查看状态        : powershell -File `"$(Join-Path $ProjectDir 'status.ps1')`"" -ForegroundColor Gray
Write-Host "  卸载全部任务    : powershell -File `"$(Join-Path $ProjectDir 'uninstall_tasks.ps1')`"" -ForegroundColor Gray
Write-Host "  日志目录        : $(Join-Path $ProjectDir 'logs')" -ForegroundColor Gray
Write-Host ""
