#requires -Version 7.0
<#
.SYNOPSIS
只读查看 Windows 登录启动项、EXE 心跳和残留旧计划任务。
#>
[CmdletBinding()]
param([ValidateRange(0, 200)][int]$LogLines = 15, [switch]$Json, [string]$ResultPath = '', [string]$InstallDir = '')

$ErrorActionPreference = 'Stop'
$SourceDir = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\CampusNet'
    $marker = Join-Path $SourceDir '.campusnet-install.json'
    if (Test-Path -LiteralPath $marker) {
        try {
            if ((Get-Content -LiteralPath $marker -Raw -Encoding utf8 | ConvertFrom-Json).install_dir -eq $SourceDir) { $InstallDir = $SourceDir }
        } catch { }
    }
}
$ProjectDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$Executable = Join-Path $ProjectDir 'campusnet_app\campusnet.exe'
$ConfigPath = Join-Path $ProjectDir 'config.json'
$LogDir = Join-Path $ProjectDir 'logs'
$StatePath = Join-Path $LogDir 'status.json'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$RunName = 'CampusNet-AutoLogin'
$RunCommand = '"' + $Executable + '" --background --config "' + $ConfigPath + '"'
$LegacyRunCommand = '"' + $Executable + '" --config "' + $ConfigPath + '"'
$result = [ordered]@{
    checked_at = [DateTimeOffset]::UtcNow.ToString('o'); running = $false
    autostart_enabled = $false; run_key = $RunKey; run_name = $RunName; run_command = ''
    executable = $Executable; install_dir = $ProjectDir; state_path = $StatePath; process_verified = $false
    heartbeat_age_seconds = $null; state = $null; legacy_tasks = @(); errors = @(); log = @()
}
try {
    $value = if (Test-Path -LiteralPath $RunKey) { (Get-Item -LiteralPath $RunKey).GetValue($RunName) } else { $null }
    $result.run_command = [string]$value
    $result.legacy_run_command = $value -eq $LegacyRunCommand
    $approved = if (Test-Path -LiteralPath $StartupKey) { (Get-Item -LiteralPath $StartupKey).GetValue($RunName) } else { $null }
    $result.startup_approved = if ($null -eq $approved) { 'not_recorded' }
        elseif ($approved.Length -gt 0 -and $approved[0] -in @(2, 6)) { 'enabled' }
        elseif ($approved.Length -gt 0 -and $approved[0] -in @(3, 7)) { 'disabled' } else { 'unknown' }
    $result.autostart_enabled = $value -eq $RunCommand -and $result.startup_approved -in @('enabled', 'not_recorded')
    $names = @($RunName, 'CampusNet-AutoLogin-Daemon', 'CampusNet-AutoLogin-Heartbeat',
        'CampusNet-AutoLogin-OnNetChange-Connected', 'CampusNet-AutoLogin-OnNetChange-Disconnected')
    foreach ($name in $names) {
        $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
        if ($task) {
            $result.legacy_tasks += @{ name = $name; state = [string]$task.State
                executable = [string]$task.Actions[0].Execute; arguments = [string]$task.Actions[0].Arguments }
        }
    }
} catch { $result.errors += '读取自启动信息失败：' + $_.Exception.Message }
try {
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding utf8 | ConvertFrom-Json
        $result.state = $state
        $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($state.heartbeat)).TotalSeconds
        $result.heartbeat_age_seconds = [int][Math]::Floor($age)
        $process = Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe'" |
            Where-Object { $_.ProcessId -eq $state.pid -and $_.ExecutablePath -eq $Executable }
        $result.process_verified = [bool]$process
        $result.running = [bool]($process -and $state.executable -eq $Executable -and
            $age -ge -5 -and $age -lt 180 -and $state.state -ne 'stopped')
    }
} catch { $result.errors += '读取运行状态失败：' + $_.Exception.Message }
$logPath = Join-Path $LogDir 'campusnet.log'
if ($LogLines -gt 0 -and (Test-Path -LiteralPath $logPath -PathType Leaf)) {
    try { $result.log = @(Get-Content -LiteralPath $logPath -Tail $LogLines -Encoding utf8) }
    catch { $result.errors += '读取日志失败：' + $_.Exception.Message }
}
$result.log_path = $logPath
$jsonText = $result | ConvertTo-Json -Depth 8
if ($ResultPath) {
    $path = [IO.Path]::GetFullPath($ResultPath)
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    $jsonText | Set-Content -LiteralPath $path -Encoding utf8
}
if ($Json) { Write-Output $jsonText; exit 0 }
Write-Output "Windows 登录自启动：$($result.autostart_enabled)"
if ($result.legacy_run_command) { Write-Output '启动项仍使用旧版参数，请重新运行 install.ps1 升级为后台启动。' }
Write-Output "安装目录：$ProjectDir"
Write-Output "启动命令：$($result.run_command)"
Write-Output "守护运行：$($result.running)；进程路径验证：$($result.process_verified)"
if ($result.state) {
    Write-Output "状态：$($result.state.state)；当前 Wi-Fi：$($result.state.ssid)；目标：$($result.state.target_ssid)"
    Write-Output "心跳：$($result.heartbeat_age_seconds) 秒前；PID：$($result.state.pid)"
    Write-Output "详情：$($result.state.message)"
}
foreach ($task in $result.legacy_tasks) { Write-Output "残留旧任务：$($task.name) ($($task.state))" }
foreach ($errorMessage in $result.errors) { Write-Output $errorMessage }
Write-Output "日志：$logPath"
foreach ($line in $result.log) { Write-Output $line }
