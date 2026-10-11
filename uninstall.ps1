#requires -Version 7.0
<#
.SYNOPSIS
完全移除 CampusNet 当前用户安装，包括程序、配置、日志、备份、自启动和桌面快捷方式。

.DESCRIPTION
默认只操作专用的 CampusNet 安装目录。卸载前会验证目录归属、停止对应进程，
并清理本项目旧版计划任务。卸载报告默认写入 TEMP，避免删除安装目录后再次创建它。
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$ResultPath = '',
    [string]$InstallDir = ''
)

$ErrorActionPreference = 'Stop'
$SourceDir = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$DefaultInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\CampusNet'
if (-not $InstallDir) {
    $InstallDir = $DefaultInstallDir
    $marker = Join-Path $SourceDir '.campusnet-install.json'
    if (Test-Path -LiteralPath $marker) {
        try {
            $marked = Get-Content -LiteralPath $marker -Raw -Encoding utf8 | ConvertFrom-Json
            if ($marked.install_dir) { $InstallDir = [string]$marked.install_dir }
        } catch { }
    }
}
$ProjectDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$Executable = Join-Path $ProjectDir 'campusnet_app\campusnet.exe'
$ConfigPath = Join-Path $ProjectDir 'config.json'
$MarkerPath = Join-Path $ProjectDir '.campusnet-install.json'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$DesktopPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
$DesktopShortcut = Join-Path $DesktopPath '校园网自动登录.lnk'
$RunName = 'CampusNet-AutoLogin'
$RunCommand = '"' + $Executable + '" --background --config "' + $ConfigPath + '"'
$LegacyRunCommand = '"' + $Executable + '" --config "' + $ConfigPath + '"'
$TempResultPath = Join-Path $env:TEMP ('campusnet-uninstall-' + [Guid]::NewGuid().ToString('N') + '.json')
if (-not $ResultPath) { $ResultPath = $TempResultPath }
$ResultPath = [IO.Path]::GetFullPath($ResultPath)

$result = [ordered]@{
    ok = $false
    operation = 'uninstall'
    dry_run = [bool]$DryRun
    install_dir = $ProjectDir
    install_dir_present = [bool](Test-Path -LiteralPath $ProjectDir -PathType Container)
    owned = $false
    run_present = $false
    run_removed = $false
    startup_approved_removed = $false
    desktop_shortcut_owned = $false
    desktop_shortcut_removed = $false
    stopped_pids = @()
    removed_files = $false
    legacy_tasks = @()
    cleanup = $null
    error = ''
}

function Test-UnderPath([string]$Child, [string]$Parent) {
    $childFull = [IO.Path]::GetFullPath($Child).TrimEnd('\')
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return [string]::Equals($childFull, $parentFull, [StringComparison]::OrdinalIgnoreCase) -or
        $childFull.StartsWith($parentFull + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-SafeInstallDirectory {
    $root = [IO.Path]::GetPathRoot($ProjectDir).TrimEnd('\')
    $blocked = @($root, $env:USERPROFILE, $env:LOCALAPPDATA,
        (Join-Path $env:LOCALAPPDATA 'Programs')) | Where-Object { $_ }
    foreach ($path in $blocked) {
        if ([string]::Equals($ProjectDir.TrimEnd('\'), ([IO.Path]::GetFullPath($path)).TrimEnd('\'),
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝删除非专用目录：$ProjectDir"
        }
    }
    $sameDirectory = [string]::Equals($ProjectDir, $SourceDir, [StringComparison]::OrdinalIgnoreCase)
    $sourceMarker = Join-Path $SourceDir '.campusnet-install.json'
    $sourceIsInstall = $false
    if ($sameDirectory -and (Test-Path -LiteralPath $sourceMarker -PathType Leaf)) {
        try {
            $sourceInstall = Get-Content -LiteralPath $sourceMarker -Raw -Encoding utf8 | ConvertFrom-Json
            $sourceIsInstall = [string]::Equals([IO.Path]::GetFullPath([string]$sourceInstall.install_dir).TrimEnd('\'),
                $ProjectDir, [StringComparison]::OrdinalIgnoreCase)
        } catch { $sourceIsInstall = $false }
    }
    $sourceHasGit = Test-Path -LiteralPath (Join-Path $SourceDir '.git')
    if ((Test-UnderPath $ProjectDir $SourceDir -or Test-UnderPath $SourceDir $ProjectDir) -and
        (-not $sourceIsInstall -or $sourceHasGit)) {
        throw "拒绝删除与卸载脚本来源重叠的目录：$ProjectDir"
    }
    if (Test-Path -LiteralPath $ProjectDir) {
        $item = Get-Item -LiteralPath $ProjectDir -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "安装目录不能是链接目录：$ProjectDir"
        }
        $links = @(Get-ChildItem -LiteralPath $ProjectDir -Force -Recurse -Attributes ReparsePoint -ErrorAction Stop)
        if ($links.Count -gt 0) { throw "安装目录包含链接，已拒绝递归删除：$($links[0].FullName)" }
    }
}

function Test-InstallationOwnership {
    if (-not (Test-Path -LiteralPath $ProjectDir -PathType Container)) { return $false }
    $markerOwned = $false
    if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) {
        try {
            $marked = Get-Content -LiteralPath $MarkerPath -Raw -Encoding utf8 | ConvertFrom-Json
            $markerOwned = [string]::Equals([IO.Path]::GetFullPath([string]$marked.install_dir).TrimEnd('\'),
                $ProjectDir, [StringComparison]::OrdinalIgnoreCase)
        } catch { $markerOwned = $false }
    }
    $hasExecutable = Test-Path -LiteralPath $Executable -PathType Leaf
    if (-not ($markerOwned -or $hasExecutable)) {
        throw "目标目录不是可识别的 CampusNet 安装目录：$ProjectDir"
    }
    return $true
}

function Get-OwnedShortcut {
    if (-not (Test-Path -LiteralPath $DesktopShortcut -PathType Leaf)) { return $false }
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($DesktopShortcut)
        $target = [IO.Path]::GetFullPath([string]$shortcut.TargetPath)
        return [string]::Equals($target, $Executable, [StringComparison]::OrdinalIgnoreCase)
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}

function Stop-TargetProcesses {
    $paths = @($Executable)
    $stopped = @()
    Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe'" |
        Where-Object { $_.ExecutablePath -and $paths -contains $_.ExecutablePath } |
        ForEach-Object {
            $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if ($process -and $paths -contains $process.Path) {
                Stop-Process -Id $process.Id -Force
                if (-not $process.WaitForExit(5000)) { throw "进程未退出：$($process.Id)" }
                $stopped += [int]$process.Id
            }
        }
    return $stopped
}

function Invoke-LegacyCleanup {
    if ($DryRun) { return @{ skipped = $true; reason = 'dry_run' } }
    $cleanupScript = Join-Path $SourceDir 'install.ps1'
    if (-not (Test-Path -LiteralPath $cleanupScript -PathType Leaf)) {
        throw "缺少旧任务清理脚本，无法保证完全卸载：$cleanupScript"
    }
    $cleanupPath = Join-Path $env:TEMP ('campusnet-cleanup-' + [Guid]::NewGuid().ToString('N') + '.json')
    try {
        $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $cleanupScript,
            '-CleanupLegacyOnly', '-InstallDir', $ProjectDir, '-ResultPath', $cleanupPath)
        & (Join-Path $PSHOME 'pwsh.exe') @arguments
        $exitCode = $LASTEXITCODE
        if (-not (Test-Path -LiteralPath $cleanupPath -PathType Leaf)) {
            throw '旧任务清理未生成结果。'
        }
        $cleanup = Get-Content -LiteralPath $cleanupPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ($exitCode -ne 0 -or -not $cleanup.ok) { throw "旧任务清理失败：$($cleanup.error)" }
        $result.legacy_tasks = @($cleanup.legacy_tasks)
        return $cleanup.cleanup
    } finally {
        if (Test-Path -LiteralPath $cleanupPath) { Remove-Item -LiteralPath $cleanupPath -Force -ErrorAction SilentlyContinue }
    }
}

try {
    Assert-SafeInstallDirectory
    $installPresent = Test-Path -LiteralPath $ProjectDir -PathType Container
    $value = if (Test-Path -LiteralPath $RunKey) { (Get-Item -LiteralPath $RunKey).GetValue($RunName) } else { $null }
    if ($value -and $value -notin @($RunCommand, $LegacyRunCommand)) {
        throw '同名 Windows 启动项指向其他路径，未修改。'
    }
    $result.run_present = [bool]$value
    $result.desktop_shortcut_owned = Get-OwnedShortcut
    if (-not $installPresent) {
        if (-not $DryRun) {
            if ($result.desktop_shortcut_owned) {
                Remove-Item -LiteralPath $DesktopShortcut -Force
                $result.desktop_shortcut_removed = $true
            }
            if ($value) {
                Remove-ItemProperty -LiteralPath $RunKey -Name $RunName
                $result.run_removed = $true
            }
            if (Test-Path -LiteralPath $StartupKey) {
                $startupValue = (Get-Item -LiteralPath $StartupKey).GetValue($RunName)
                if ($null -ne $startupValue) {
                    Remove-ItemProperty -LiteralPath $StartupKey -Name $RunName
                    $result.startup_approved_removed = $true
                }
            }
        }
        $result.ok = $true
    } else {
        $result.owned = Test-InstallationOwnership
        if (Test-UnderPath $ResultPath $ProjectDir -and -not $DryRun) {
            throw '卸载报告不能写入即将删除的安装目录，请使用 -ResultPath 指定其他路径。'
        }

        $result.cleanup = Invoke-LegacyCleanup
        if ($DryRun) {
            $result.ok = $true
        } else {
            $result.stopped_pids = @(Stop-TargetProcesses)
            if ($result.desktop_shortcut_owned) {
                Remove-Item -LiteralPath $DesktopShortcut -Force
                $result.desktop_shortcut_removed = $true
            }
            if ($value) {
                Remove-ItemProperty -LiteralPath $RunKey -Name $RunName
                $result.run_removed = $true
            }
            if (Test-Path -LiteralPath $StartupKey) {
                $startupValue = (Get-Item -LiteralPath $StartupKey).GetValue($RunName)
                if ($null -ne $startupValue) {
                    Remove-ItemProperty -LiteralPath $StartupKey -Name $RunName
                    $result.startup_approved_removed = $true
                }
            }
            if (Test-Path -LiteralPath $ProjectDir -PathType Container) {
                Remove-Item -LiteralPath $ProjectDir -Recurse -Force
                $result.removed_files = $true
            }
            $result.ok = -not (Test-Path -LiteralPath $ProjectDir)
            if (-not $result.ok) { throw "安装目录删除失败：$ProjectDir" }
        }
    }
} catch {
    $result.error = $_.Exception.Message
} finally {
    $result.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    $parent = Split-Path -Parent $ResultPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ResultPath -Encoding utf8
}

if ($result.ok) {
    Write-Output "完全卸载$(if ($DryRun) { '预检' })完成。结果：$ResultPath"
    exit 0
}
Write-Error -Message "$($result.error) 结果：$ResultPath" -ErrorAction Continue
exit 1
