<#
.SYNOPSIS
    校园网自动认证 —— 一屏状态自检。

.DESCRIPTION
    一次性回答这几个问题：
      · 现在联网了吗？认证门户还认得出来吗？
      · 三个计划任务在不在、跑没跑过、上次结果是什么？
      · 守护进程活着吗（PID + 心跳时间）？
      · 最近 20 行日志说了什么？

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\status.ps1
#>
[CmdletBinding()]
param(
    [int]$LogLines = 20
)

$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ProjectDir 'config.json'
$RunLogin   = Join-Path $ProjectDir 'run_login.py'
$LogDir     = Join-Path $ProjectDir 'logs'
$PidFile    = Join-Path $LogDir 'daemon.pid'
$Heartbeat  = Join-Path $LogDir 'heartbeat.txt'

function Section($Title) {
    Write-Host ""
    Write-Host ("=" * 66) -ForegroundColor DarkGray
    Write-Host "  $Title" -ForegroundColor White
    Write-Host ("=" * 66) -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "校园网自动认证 —— 状态自检" -ForegroundColor White
Write-Host "项目目录：$ProjectDir" -ForegroundColor Gray

# ----------------------------------------------------------------- 配置
Section "1. 配置"
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $pwdShown = if ($cfg.password) { '已填写（明文）' } else { '（空）' }
        Write-Host ("  账号        : {0}" -f $cfg.username)
        Write-Host ("  密码        : {0}" -f $pwdShown)
        Write-Host ("  认证方式    : {0}" -f $cfg.provider)
        Write-Host ("  门户地址    : {0}" -f $cfg.portal_ip)
        Write-Host ("  校园 Wi-Fi  : {0}" -f $cfg.wifi_ssid)
    } catch {
        Write-Host "  [!] config.json 解析失败：$_" -ForegroundColor Yellow
    }
} else {
    Write-Host "  [x] 找不到 $ConfigPath" -ForegroundColor Red
}

# ------------------------------------------------------------- 联网状态
Section "2. 当前联网状态"
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }

try {
    $probe = Invoke-WebRequest -Uri 'http://connect.rom.miui.com/generate_204' `
        -TimeoutSec 6 -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
    if ($probe.StatusCode -eq 204) {
        Write-Host "  [+] 已联网（generate_204 返回 204）" -ForegroundColor Green
    } else {
        Write-Host "  [!] 返回 $($probe.StatusCode) —— 可能被 Portal 劫持" -ForegroundColor Yellow
    }
} catch {
    $resp = $_.Exception.Response
    if ($resp -and [int]$resp.StatusCode -eq 204) {
        Write-Host "  [+] 已联网（generate_204 返回 204）" -ForegroundColor Green
    } elseif ($resp) {
        Write-Host "  [!] 返回 $([int]$resp.StatusCode) —— 未认证，可能被重定向到门户" -ForegroundColor Yellow
        $loc = $resp.Headers['Location']
        if ($loc) { Write-Host "      跳转目标：$loc" -ForegroundColor Gray }
    } else {
        Write-Host "  [x] 探测失败：$($_.Exception.Message)" -ForegroundColor Red
    }
}

# 门户可达性
if ($cfg -and $cfg.portal_ip) {
    try {
        $portal = Invoke-WebRequest -Uri ($cfg.portal_ip.TrimEnd('/') + '/eportal/') `
            -TimeoutSec 6 -UseBasicParsing -ErrorAction Stop
        $server = $portal.Headers['Server']
        Write-Host ("  [+] 门户可达：{0}（Server: {1}，HTTP {2}）" -f $cfg.portal_ip, $server, $portal.StatusCode) -ForegroundColor Green
    } catch {
        Write-Host ("  [!] 门户不可达：{0}（{1}）" -f $cfg.portal_ip, $_.Exception.Message) -ForegroundColor Yellow
    }
}

# ------------------------------------------------------- Wi-Fi 当前连接
Section "3. 无线连接"
$wlan = netsh wlan show interfaces 2>&1 | Out-String
if ($wlan -match 'error 5' -or $wlan -match 'requires elevation') {
    Write-Host "  [!] 读取当前 SSID 需要管理员权限（netsh error 5）。" -ForegroundColor Yellow
    Write-Host "      这不影响认证本身：认证靠的是联网探测，不看 SSID。" -ForegroundColor Gray
    $profiles = netsh wlan show profiles 2>&1 | Out-String
    $saved = ([regex]::Matches($profiles, ':\s*(\S.*?)\s*$', 'Multiline') |
              ForEach-Object { $_.Groups[1].Value.Trim() } |
              Where-Object { $_ -and $_ -ne '<None>' })
    if ($saved) { Write-Host ("      已保存的网络：{0}" -f ($saved -join ', ')) -ForegroundColor Gray }
} else {
    $ssid = ([regex]::Match($wlan, '(?m)^\s*SSID\s*:\s*(.+?)\s*$')).Groups[1].Value
    $state = ([regex]::Match($wlan, '(?m)^\s*State\s*:\s*(.+?)\s*$')).Groups[1].Value
    if (-not $ssid) {
        $ssid = ([regex]::Match($wlan, '(?m)^\s*SSID\s*名称\s*:\s*(.+?)\s*$')).Groups[1].Value
    }
    if ($ssid) {
        $isTarget = $cfg -and ($ssid -eq $cfg.wifi_ssid)
        $color = if ($isTarget) { 'Green' } else { 'Yellow' }
        Write-Host ("  当前 SSID : {0}  {1}" -f $ssid, $(if ($isTarget) { '(目标网络 ✔)' } else { '(不是配置里的校园网)' })) -ForegroundColor $color
    } else {
        Write-Host "  当前 SSID : （未连接）" -ForegroundColor Yellow
    }
    if ($state) { Write-Host "  接口状态  : $state" -ForegroundColor Gray }
}

# ------------------------------------------------------------- 计划任务
Section "4. 计划任务"
$tasks = Get-ScheduledTask -TaskName 'CampusNet-AutoLogin-*' -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "  [x] 一个任务都没注册。运行安装脚本：" -ForegroundColor Red
    Write-Host "      powershell -ExecutionPolicy Bypass -File `"$(Join-Path $ProjectDir 'install_tasks.ps1')`"" -ForegroundColor Gray
} else {
    foreach ($task in $tasks) {
        $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -ErrorAction SilentlyContinue
        Write-Host ("  {0}" -f $task.TaskName) -ForegroundColor White
        Write-Host ("      状态      : {0}" -f $task.State) -ForegroundColor Gray
        if ($info) {
            Write-Host ("      上次运行  : {0}  结果: 0x{1:X}" -f $info.LastRunTime, $info.LastTaskResult) -ForegroundColor Gray
            Write-Host ("      下次运行  : {0}" -f $info.NextRunTime) -ForegroundColor Gray
        }
    }
}

# --------------------------------------------------------------- 守护
Section "5. 守护进程"
if (Test-Path -LiteralPath $PidFile) {
    $daemonPid = 0
    try { $daemonPid = [int]((Get-Content -LiteralPath $PidFile -Raw) -replace '\s', '') } catch { $daemonPid = 0 }
    $proc = if ($daemonPid -gt 0) { Get-Process -Id $daemonPid -ErrorAction SilentlyContinue } else { $null }
    if ($proc) {
        Write-Host ("  [+] 正在运行：PID {0}（{1}）" -f $daemonPid, $proc.ProcessName) -ForegroundColor Green
    } else {
        Write-Host ("  [!] PID 文件存在（{0}）但进程已不在" -f $daemonPid) -ForegroundColor Yellow
    }
    if (Test-Path -LiteralPath $Heartbeat) {
        $beat = (Get-Content -LiteralPath $Heartbeat -Raw).Trim()
        $stampText = ($beat -split "`t")[0]
        $stamp = [datetime]::MinValue
        [void][datetime]::TryParse($stampText, [ref]$stamp)
        if ($stamp -ne [datetime]::MinValue) {
            $age = [int]((Get-Date) - $stamp).TotalSeconds
            $color = if ($age -lt 90) { 'Green' } else { 'Yellow' }
            Write-Host ("      心跳      : {0}（{1} 秒前）" -f $stamp, $age) -ForegroundColor $color
        } else {
            Write-Host ("      心跳      : {0}" -f $beat) -ForegroundColor Gray
        }
    }
} else {
    Write-Host "  [-] 没有守护进程在运行（若装了 -NoDaemon 或没装守护，属正常）" -ForegroundColor DarkGray
}

# --------------------------------------------------------------- 日志
Section "6. 最近的日志"
$logs = @()
foreach ($name in 'daemon.log', 'campusnet.log') {
    $path = Join-Path $LogDir $name
    if (Test-Path -LiteralPath $path) {
        $logs += Get-Item -LiteralPath $path
    }
}
if (-not $logs) {
    Write-Host "  [-] 还没有日志（尚未认证过）" -ForegroundColor DarkGray
    Write-Host "      日志目录：$LogDir" -ForegroundColor Gray
} else {
    foreach ($file in $logs) {
        Write-Host ""
        Write-Host ("  --- {0}（{1:N0} 字节，改于 {2}）---" -f $file.Name, $file.Length, $file.LastWriteTime) -ForegroundColor Cyan
        Get-Content -LiteralPath $file.FullName -Tail $LogLines -Encoding UTF8 |
            ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
    }
}

Write-Host ""
Write-Host "手动认证一次：" -ForegroundColor White
Write-Host "  python `"$RunLogin`"" -ForegroundColor Gray
Write-Host ""
