#requires -Version 7.0
<#
.SYNOPSIS
删除当前用户的校园网登录启动项和本程序桌面快捷方式并停止 EXE，保留程序、配置和日志。
#>
[CmdletBinding()]
param([switch]$DryRun, [string]$ResultPath = '', [string]$InstallDir = '')

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
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$DesktopPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
$DesktopShortcut = Join-Path $DesktopPath '校园网自动登录.lnk'
$RunName = 'CampusNet-AutoLogin'
$RunCommand = '"' + $Executable + '" --background --config "' + $ConfigPath + '"'
$LegacyRunCommand = '"' + $Executable + '" --config "' + $ConfigPath + '"'
if (-not $ResultPath) { $ResultPath = Join-Path $LogDir 'uninstall-result.json' }
$ResultPath = [IO.Path]::GetFullPath($ResultPath)
$result = [ordered]@{ ok = $false; operation = 'uninstall'; dry_run = [bool]$DryRun; install_dir = $ProjectDir; run_removed = $false; desktop_shortcut_removed = $false; stopped_pids = @(); cleanup = $null; error = '' }

try {
    $value = if (Test-Path -LiteralPath $RunKey) { (Get-Item -LiteralPath $RunKey).GetValue($RunName) } else { $null }
    if ($value -and $value -notin @($RunCommand, $LegacyRunCommand)) { throw '同名 Windows 启动项指向其他路径，未修改。' }
    $result.run_present = [bool]$value
    if (Test-Path -LiteralPath $DesktopShortcut -PathType Leaf) {
        $shortcutShell = New-Object -ComObject WScript.Shell
        try {
            $shortcut = $shortcutShell.CreateShortcut($DesktopShortcut)
            $shortcutTarget = [IO.Path]::GetFullPath([string]$shortcut.TargetPath)
            $result.desktop_shortcut_owned = [string]::Equals($shortcutTarget, $Executable,
                [StringComparison]::OrdinalIgnoreCase)
        } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcutShell) }
    } else { $result.desktop_shortcut_owned = $false }
    $cleanupPath = Join-Path $LogDir ('uninstall-cleanup-' + [Guid]::NewGuid().ToString('N') + '.json')
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $SourceDir 'install.ps1'), '-CleanupLegacyOnly', '-InstallDir', $ProjectDir, '-ResultPath', $cleanupPath)
    if ($DryRun) { $arguments += '-DryRun' }
    & (Join-Path $PSHOME 'pwsh.exe') @arguments
    $cleanupExit = $LASTEXITCODE
    $cleanup = Get-Content -LiteralPath $cleanupPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($cleanupExit -ne 0 -or -not $cleanup.ok) { throw "旧任务清理失败：$($cleanup.error)" }
    $result.legacy_tasks = @($cleanup.legacy_tasks)
    $result.cleanup = $cleanup.cleanup
    if (-not $DryRun) {
        if ($result.desktop_shortcut_owned) {
            Remove-Item -LiteralPath $DesktopShortcut -Force
            $result.desktop_shortcut_removed = $true
        }
        if ($value) { Remove-ItemProperty -LiteralPath $RunKey -Name $RunName; $result.run_removed = $true }
        if ((Test-Path -LiteralPath $StartupKey) -and $null -ne (Get-Item -LiteralPath $StartupKey).GetValue($RunName)) {
            Remove-ItemProperty -LiteralPath $StartupKey -Name $RunName
        }
        Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe'" |
            Where-Object { $_.ExecutablePath -eq $Executable } | ForEach-Object {
                $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
                if ($process -and $process.Path -eq $Executable) {
                    Stop-Process -Id $process.Id -Force
                    if (-not $process.WaitForExit(5000)) { throw "进程未退出：$($process.Id)" }
                    $result.stopped_pids += $process.Id
                }
            }
    }
    $result.ok = $true
} catch { $result.error = $_.Exception.Message }
finally {
    $result.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    New-Item -ItemType Directory -Path (Split-Path -Parent $ResultPath) -Force | Out-Null
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding utf8
}
if ($result.ok) { Write-Output "卸载$(if ($DryRun) { '预检' })完成。程序、配置和日志保留。结果：$ResultPath"; exit 0 }
Write-Error -Message "$($result.error) 结果：$ResultPath" -ErrorAction Continue
exit 1
