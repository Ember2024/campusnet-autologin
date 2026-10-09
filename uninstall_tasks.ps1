<#
.SYNOPSIS
    卸载校园网自动认证的全部任务计划。

.DESCRIPTION
    删除三个计划任务（守护 / 连网触发 / 兜底巡检），并可选地结束正在运行的
    守护进程。不会碰 config.json，也不会碰日志。

.PARAMETER KeepLogs
    保留 logs 目录（默认保留）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall_tasks.ps1
#>
[CmdletBinding()]
param(
    [switch]$KeepLogs = $true
)

$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $ProjectDir 'logs\daemon.pid'

Write-Host ""
Write-Host "校园网自动认证 —— 卸载任务计划" -ForegroundColor White
Write-Host ("=" * 62) -ForegroundColor DarkGray

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$isAdmin = Test-Admin
if (-not $isAdmin) {
    Write-Host "[!] 非管理员：删除计划任务可能失败，请用管理员 PowerShell 重试。" -ForegroundColor Yellow
}

# ------------------------------------------------------------- 结束守护
if (Test-Path -LiteralPath $PidFile) {
    $daemonPid = 0
    try { $daemonPid = [int]((Get-Content -LiteralPath $PidFile -Raw) -replace '\s', '') } catch { $daemonPid = 0 }
    if ($daemonPid -gt 0) {
        $proc = Get-Process -Id $daemonPid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "[*] 结束守护进程 PID $daemonPid ..." -ForegroundColor Cyan
            Stop-Process -Id $daemonPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 600
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------- 删除任务
# 用通配符覆盖全部 CampusNet-AutoLogin-* 任务，这样即使以后新增了任务
# （比如连网触发从 1 个拆成了「已连接」「已断开」两个）也一定能卸干净。
$removed = 0
$targets = @(Get-ScheduledTask -TaskName 'CampusNet-AutoLogin-*' -ErrorAction SilentlyContinue |
             Select-Object -ExpandProperty TaskName)

if (-not $targets -or $targets.Count -eq 0) {
    Write-Host "[-] 没有找到任何 CampusNet-AutoLogin-* 任务。" -ForegroundColor DarkGray
}

foreach ($name in $targets) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Write-Host "[x] 删除失败：$name" -ForegroundColor Red
    } else {
        Write-Host "[+] 已删除：$name" -ForegroundColor Green
        $removed++
    }
}

# ------------------------------------------------------------- 清理日志
if (-not $KeepLogs) {
    $logDir = Join-Path $ProjectDir 'logs'
    if (Test-Path -LiteralPath $logDir) {
        Remove-Item -LiteralPath $logDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[+] 已删除日志目录：$logDir" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host ("=" * 62) -ForegroundColor DarkGray
Write-Host "[+] 卸载结束，共删除 $removed 个任务。" -ForegroundColor Green
Write-Host "    注意：config.json 里的账号密码仍然保留在磁盘上，如需清除请手动删除。" -ForegroundColor Yellow
Write-Host ""
