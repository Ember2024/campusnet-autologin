#requires -Version 7.0
<#
.SYNOPSIS
为当前用户安装 Windows 登录自启动，并静默启动带托盘的 EXE。
.DESCRIPTION
日常使用 HKCU Run 启动项。只有清理旧计划任务时才需要一次 UAC。
-DryRun 仅预检；-NoStart 只安装启动项；结果默认写入 logs/install-result.json。
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoStart,
    [string]$ResultPath = '',
    [string]$InstallDir = '',
    [Parameter(DontShow)][switch]$CleanupLegacyOnly,
    [Parameter(DontShow)][string]$ExpectedUserSid = ''
)

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
$SourceExecutable = Join-Path $SourceDir 'campusnet_app\campusnet.exe'
$SourceConfig = Join-Path $SourceDir 'config.json'
$Executable = Join-Path $ProjectDir 'campusnet_app\campusnet.exe'
$ConfigPath = Join-Path $ProjectDir 'config.json'
$LogDir = Join-Path $ProjectDir 'logs'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$RunName = 'CampusNet-AutoLogin'
$BackgroundArguments = '--background --config "' + $ConfigPath + '"'
$RunCommand = '"' + $Executable + '" ' + $BackgroundArguments
$LegacyRunCommand = '"' + $Executable + '" --config "' + $ConfigPath + '"'
$PwshPath = Join-Path $PSHOME 'pwsh.exe'
$OldLauncherArguments = @($SourceDir, $ProjectDir) | ForEach-Object {
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $_ 'launch.ps1') + '" -ConfigPath "' + (Join-Path $_ 'config.json') + '"'
}
$LegacyNames = @($RunName, 'CampusNet-AutoLogin-Daemon', 'CampusNet-AutoLogin-Heartbeat',
    'CampusNet-AutoLogin-OnNetChange-Connected', 'CampusNet-AutoLogin-OnNetChange-Disconnected')
$OwnedPaths = @($SourceDir, $ProjectDir) | ForEach-Object {
    Join-Path $_ 'campusnet_app\campusnet.exe'
    Join-Path $_ 'daemon\daemon.exe'
    Join-Path $_ 'run_login\run_login.exe'
    Join-Path $_ 'run_login_hidden\run_login_hidden.exe'
}
$AllowedFiles = @('install.ps1', 'uninstall.ps1', 'status.ps1', 'README.md', 'NOTICE.md',
    'LICENSE', 'LICENSE.campusnet', 'config.example.json')
if (-not $ResultPath) { $ResultPath = Join-Path $LogDir 'install-result.json' }
$ResultPath = [IO.Path]::GetFullPath($ResultPath)
$result = [ordered]@{
    ok = $false; operation = $(if ($CleanupLegacyOnly) { 'cleanup_legacy' } else { 'install' })
    phase = 'preflight'; dry_run = [bool]$DryRun; executable = $Executable; install_dir = $ProjectDir; source_dir = $SourceDir
    run_key = $RunKey; run_name = $RunName; run_command = $RunCommand
    legacy_tasks = @(); cleanup = $null; started = $false; error = ''
}
$runWritten = $false
$startupWritten = $false
$hadInstalledProcess = $false
$previousRun = $null
$previousStartup = $null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Test-OwnedTask($Task) {
    if (@($Task.Actions).Count -ne 1) { return $false }
    try { $path = [IO.Path]::GetFullPath(([string]$Task.Actions[0].Execute).Trim('"')) }
    catch { return $false }
    return ($OwnedPaths -contains $path) -or
        ($path -eq $PwshPath -and $OldLauncherArguments -contains $Task.Actions[0].Arguments)
}

function Get-LegacyTasks {
    foreach ($name in $LegacyNames) {
        $task = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue
        if (-not $task) { continue }
        if (-not (Test-OwnedTask $task)) { throw "旧任务 $name 指向其他项目，未修改。" }
        $task
    }
}

function Clear-LegacyTasks($Tasks) {
    if (@($Tasks).Count -eq 0) { return @{ ok = $true; removed_tasks = @(); stopped_pids = @() } }
    if (-not $isAdmin) {
        if ($ExpectedUserSid) { throw '旧任务清理未获得管理员权限。' }
        $childResult = Join-Path $LogDir ('cleanup-legacy-' + [Guid]::NewGuid().ToString('N') + '.json')
        $requestedAt = [DateTimeOffset]::UtcNow
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $PSCommandPath +
            '" -CleanupLegacyOnly -InstallDir "' + $ProjectDir + '" -ResultPath "' + $childResult + '" -ExpectedUserSid "' + $identity.User.Value + '"'
        $child = Start-Process -FilePath $PwshPath -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru
        [void]$child.Handle
        if (-not $child.WaitForExit(60000)) { throw "旧任务清理仍在运行（PID $($child.Id)），请查看 $childResult" }
        $report = Get-Content -LiteralPath $childResult -Raw -Encoding utf8 | ConvertFrom-Json
        if ($child.ExitCode -ne 0 -or -not $report.ok -or $report.operation -ne 'cleanup_legacy' -or
            [DateTimeOffset]::Parse($report.finished_at) -lt $requestedAt) { throw "旧任务清理失败：$($report.error)；结果：$childResult" }
        return $report.cleanup
    }
    $backup = Join-Path $LogDir ('legacy-tasks-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($task in $Tasks) {
        Export-ScheduledTask -TaskName $task.TaskName -TaskPath '\' |
            Set-Content -LiteralPath (Join-Path $backup ($task.TaskName + '.xml')) -Encoding unicode
    }
    foreach ($task in $Tasks) {
        Disable-ScheduledTask -TaskName $task.TaskName -TaskPath '\' | Out-Null
        Stop-ScheduledTask -TaskName $task.TaskName -TaskPath '\'
    }
    $stopped = @()
    Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe' OR Name = 'daemon.exe' OR Name = 'run_login.exe' OR Name = 'run_login_hidden.exe'" |
        Where-Object { $_.ExecutablePath -and $OwnedPaths -contains $_.ExecutablePath } | ForEach-Object {
            $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if ($process -and $OwnedPaths -contains $process.Path) {
                Stop-Process -Id $process.Id -Force
                if (-not $process.WaitForExit(5000)) { throw "进程未退出：$($process.Id)" }
                $stopped += $process.Id
            }
        }
    foreach ($task in $Tasks) { Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -Confirm:$false }
    return @{ ok = $true; backup_path = $backup; removed_tasks = @($Tasks | ForEach-Object { $_.TaskName }); stopped_pids = $stopped }
}

function Read-IntegrityLabel([string]$Path) {
    $output = @(& (Join-Path $env:SystemRoot 'System32\icacls.exe') $Path 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "无法检查完整性标签：$Path" }
    $text = $output -join [Environment]::NewLine
    return @{ path = $Path; low = ($text -match 'S-1-16-(?:0|4096)\b|Low Mandatory|Untrusted Mandatory|低强制|低完整性|不受信任'); output = $text }
}

function Stop-InstallProcesses {
    $paths = @($SourceExecutable, $Executable)
    Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe'" |
        Where-Object { $_.ExecutablePath -and $paths -contains $_.ExecutablePath } | ForEach-Object {
            $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if ($process -and $paths -contains $process.Path) {
                if ($process.Path -eq $Executable) { $script:hadInstalledProcess = $true }
                Stop-Process -Id $process.Id -Force
                if (-not $process.WaitForExit(5000)) { throw "旧进程未退出：$($process.Id)" }
            }
        }
}

function Remove-Stage([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith(($ProjectDir + '\.stage-'), [StringComparison]::OrdinalIgnoreCase)) { throw '临时目录不在安装根中。' }
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

function Expand-SafeRelease([string]$Stage) {
    $archivePath = Join-Path $SourceDir 'release\campusnet-windows.zip'
    if (-not (Test-Path -LiteralPath $archivePath)) { throw '复制后的 EXE 仍为 Low，且未找到可用于重新提取的 release/campusnet-windows.zip。未修改安全标签。' }
    $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if ($name.StartsWith('/') -or $name.Contains(':') -or $name.Split('/') -contains '..') { throw '分发 ZIP 含不安全路径。' }
            if ($AllowedFiles -notcontains $name -and -not $name.StartsWith('campusnet_app/', [StringComparison]::OrdinalIgnoreCase)) { continue }
            $target = [IO.Path]::GetFullPath((Join-Path $Stage $name))
            if (-not $target.StartsWith(($Stage + '\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'ZIP 路径超出临时目录。' }
            if ($name.EndsWith('/')) { New-Item -ItemType Directory -Path $target -Force | Out-Null; continue }
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally { $archive.Dispose() }
}

function Deploy-Files {
    if ($SourceDir -eq $ProjectDir) {
        $result.deployment = 'already_in_install_directory'
    } else {
        $stage = Join-Path $ProjectDir ('.stage-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        try {
            Copy-Item -LiteralPath (Join-Path $SourceDir 'campusnet_app') -Destination $stage -Recurse
            foreach ($name in $AllowedFiles) { Copy-Item -LiteralPath (Join-Path $SourceDir $name) -Destination $stage }
            $stageExe = Join-Path $stage 'campusnet_app\campusnet.exe'
            if ((Read-IntegrityLabel $stageExe).low) {
                Remove-Stage $stage
                New-Item -ItemType Directory -Path $stage -Force | Out-Null
                Expand-SafeRelease $stage
                $result.deployment = 'release_zip'
            } else { $result.deployment = 'copy' }
            if ((Read-IntegrityLabel $stageExe).low) { throw '新安装文件仍带 Low 完整性标签，已停止；未修改任何安全标签。' }
            $files = @(Get-ChildItem -LiteralPath (Join-Path $SourceDir 'campusnet_app') -File -Recurse)
            $files += @($AllowedFiles | ForEach-Object { Get-Item -LiteralPath (Join-Path $SourceDir $_) })
            foreach ($file in $files) {
                $relative = [IO.Path]::GetRelativePath($SourceDir, $file.FullName)
                $copied = Join-Path $stage $relative
                if (-not (Test-Path -LiteralPath $copied) -or
                    (Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $copied).Hash) {
                    throw "部署校验失败：$relative。若使用 ZIP，请先重新构建分发包。"
                }
            }
            $destinationApp = Join-Path $ProjectDir 'campusnet_app'
            if (Test-Path -LiteralPath $destinationApp) {
                $backup = Join-Path $ProjectDir ('backups\program-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
                New-Item -ItemType Directory -Path $backup -Force | Out-Null
                Move-Item -LiteralPath $destinationApp -Destination (Join-Path $backup 'campusnet_app')
                $result.program_backup = $backup
            }
            Move-Item -LiteralPath (Join-Path $stage 'campusnet_app') -Destination $destinationApp
            foreach ($name in $AllowedFiles) { Copy-Item -LiteralPath (Join-Path $stage $name) -Destination (Join-Path $ProjectDir $name) -Force }
        } finally { Remove-Stage $stage }
    }
    $result.config_preserved = Test-Path -LiteralPath $ConfigPath -PathType Leaf
    if (-not $result.config_preserved) {
        if (Test-Path -LiteralPath $SourceConfig -PathType Leaf) {
            Copy-Item -LiteralPath $SourceConfig -Destination $ConfigPath
        } else {
            $template = Get-Content -LiteralPath (Join-Path $SourceDir 'config.example.json') -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
            $template.username = ''
            $template.password = ''
            $template | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ConfigPath -Encoding utf8
            $result.config_created = $true
        }
    }
    $result.integrity_check = Read-IntegrityLabel $Executable
    if ($result.integrity_check.low) { throw '目标 EXE 仍带 Low 完整性标签，已停止；未修改安全标签。' }
    @{ install_dir = $ProjectDir; source_dir = $SourceDir; updated_at = [DateTimeOffset]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ProjectDir '.campusnet-install.json') -Encoding utf8
}

function Invoke-DesktopSelfTest($Shell) {
    $report = Join-Path $LogDir ('desktop-self-test-' + [Guid]::NewGuid().ToString('N') + '.json')
    $arguments = '--config "' + $ConfigPath + '" --self-test --report "' + $report + '"'
    $Shell.ShellExecute($Executable, $arguments, $ProjectDir, 'open', 0)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        if (Test-Path -LiteralPath $report) {
            try {
                $check = Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json
                if ($check.ok -and $check.frozen -and $check.lock_ok) { return $check }
            } catch { }
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw '桌面上下文的 EXE 自检未在 30 秒内通过。'
}

function Wait-Started {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    $statePath = Join-Path $LogDir 'status.json'
    do {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
            $processes = @(Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe'" |
                Where-Object { $_.ExecutablePath -eq $Executable })
            $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($state.heartbeat)).TotalSeconds
            $ssidMatches = $state.state -eq 'needs_config' -or -not $result.wifi_readable -or
                (-not [string]::IsNullOrEmpty($state.ssid) -and $state.ssid -ceq $result.ssid)
            if ($processes.Count -eq 1 -and $state.pid -eq $processes[0].ProcessId -and
                $state.executable -eq $Executable -and $age -ge -5 -and $age -lt 180 -and
                $state.state -notin @('starting', 'stopped') -and $ssidMatches) {
                $result.started = $true
                $result.pid = [int]$state.pid
                $result.runtime_ssid = [string]$state.ssid
                $result.state = [string]$state.state
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw 'EXE 启动后 30 秒内未通过进程、心跳及 SSID 检查。'
}

try {
    $blockedRoots = @([IO.Path]::GetPathRoot($ProjectDir).TrimEnd('\'), $env:USERPROFILE,
        $env:LOCALAPPDATA, (Join-Path $env:LOCALAPPDATA 'Programs'))
    if ($blockedRoots -contains $ProjectDir) { throw 'InstallDir 必须是专门的应用目录。' }
    if ($SourceDir -ne $ProjectDir -and
        ($ProjectDir.StartsWith(($SourceDir + '\'), [StringComparison]::OrdinalIgnoreCase) -or
         $SourceDir.StartsWith(($ProjectDir + '\'), [StringComparison]::OrdinalIgnoreCase))) {
        throw '源目录和安装目录不能互相包含。'
    }
    foreach ($directory in @($ProjectDir, (Join-Path $ProjectDir 'campusnet_app'))) {
        if ((Test-Path -LiteralPath $directory) -and ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "安装目标不能是链接目录：$directory"
        }
    }
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    if ($ExpectedUserSid -and $identity.User.Value -ne $ExpectedUserSid) { throw '提权后不是原 Windows 用户，已停止清理。' }
    $tasks = @(Get-LegacyTasks)
    $result.legacy_tasks = @($tasks | ForEach-Object { $_.TaskName })
    if (-not $CleanupLegacyOnly) {
        if ($isAdmin -and -not $DryRun) { throw '请在普通用户 PowerShell 7 中安装；脚本会仅为清理旧任务申请一次 UAC。' }
        foreach ($path in @($SourceExecutable) + @($AllowedFiles | ForEach-Object { Join-Path $SourceDir $_ })) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "文件不存在：$path" }
        }
        if (((Get-Item -LiteralPath (Join-Path $SourceDir 'campusnet_app')).Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            @(Get-ChildItem -LiteralPath (Join-Path $SourceDir 'campusnet_app') -Recurse -Attributes ReparsePoint).Count) {
            throw '运行依赖目录含链接，已停止部署。'
        }
        if ((Read-IntegrityLabel $ProjectDir).low) { throw '安装目标目录带 Low 完整性标签，请选择普通用户目录。未修改安全标签。' }
        $bytes = [IO.File]::ReadAllBytes($SourceExecutable)
        $offset = [BitConverter]::ToInt32($bytes, 60)
        if ($offset -lt 0 -or $offset + 94 -gt $bytes.Length -or [BitConverter]::ToUInt16($bytes, $offset + 92) -ne 2) {
            throw 'campusnet.exe 必须是无窗口 GUI 子系统。'
        }
        $previousRun = if (Test-Path -LiteralPath $RunKey) { (Get-Item -LiteralPath $RunKey).GetValue($RunName) } else { $null }
        $previousStartup = if (Test-Path -LiteralPath $StartupKey) { (Get-Item -LiteralPath $StartupKey).GetValue($RunName) } else { $null }
        $sourceRunCommand = '"' + $SourceExecutable + '" --background --config "' + $SourceConfig + '"'
        $legacySourceRunCommand = '"' + $SourceExecutable + '" --config "' + $SourceConfig + '"'
        if ($previousRun -and $previousRun -notin @($RunCommand, $LegacyRunCommand, $sourceRunCommand, $legacySourceRunCommand)) {
            throw '同名 Windows 启动项指向其他路径，未修改。'
        }
    }
    if ($DryRun) {
        $result.phase = 'checked'
    } elseif ($CleanupLegacyOnly) {
        $result.cleanup = Clear-LegacyTasks $tasks
        $result.phase = 'legacy_removed'
    } else {
        $shell = New-Object -ComObject Shell.Application
        try {
            $result.cleanup = Clear-LegacyTasks $tasks
            Stop-InstallProcesses
            Deploy-Files
            $check = Invoke-DesktopSelfTest $shell
            $result.wifi_readable = [bool]$check.wifi_readable
            $result.ssid = [string]$check.ssid
            if ($check.PSObject.Properties.Name -contains 'config_ready') { $result.config_ready = [bool]$check.config_ready }
            if (-not (Test-Path -LiteralPath $RunKey)) { New-Item -Path $RunKey -Force | Out-Null }
            New-ItemProperty -LiteralPath $RunKey -Name $RunName -Value $RunCommand -PropertyType String -Force | Out-Null
            $runWritten = $true
            if ((Get-ItemPropertyValue -LiteralPath $RunKey -Name $RunName) -ne $RunCommand) { throw 'Windows 启动项复核失败。' }
            if (-not (Test-Path -LiteralPath $StartupKey)) { New-Item -Path $StartupKey -Force | Out-Null }
            $enabled = [byte[]]@(2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            New-ItemProperty -LiteralPath $StartupKey -Name $RunName -Value $enabled -PropertyType Binary -Force | Out-Null
            $startupWritten = $true
            if ((Get-ItemPropertyValue -LiteralPath $StartupKey -Name $RunName)[0] -ne 2) { throw 'Windows 启动项启用状态复核失败。' }
            $result.startup_approved = 'enabled'
            if (-not $NoStart) {
                $shell.ShellExecute($Executable, $BackgroundArguments, $ProjectDir, 'open', 0)
                Wait-Started
            }
            $result.phase = 'installed'
        } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    }
    $result.ok = $true
} catch {
    $result.error = $_.Exception.Message
    if ($result.program_backup) {
        try {
            $oldApp = Join-Path $result.program_backup 'campusnet_app'
            if (-not [IO.Path]::GetFullPath($oldApp).StartsWith(($ProjectDir + '\backups\'), [StringComparison]::OrdinalIgnoreCase)) { throw '程序备份路径不在安装目录。' }
            if (Test-Path -LiteralPath $oldApp) {
                Stop-InstallProcesses
                $appPath = Join-Path $ProjectDir 'campusnet_app'
                if (Test-Path -LiteralPath $appPath) {
                    $failed = Join-Path $ProjectDir ('backups\failed-' + [Guid]::NewGuid().ToString('N'))
                    Move-Item -LiteralPath $appPath -Destination $failed
                }
                Move-Item -LiteralPath $oldApp -Destination $appPath
                $result.program_restored = $true
            }
        } catch { $result.program_restore_error = $_.Exception.Message }
    }
    if ($runWritten) {
        try {
            if ($previousRun) { Set-ItemProperty -LiteralPath $RunKey -Name $RunName -Value $previousRun }
            else { Remove-ItemProperty -LiteralPath $RunKey -Name $RunName }
            $result.run_restored = $true
        } catch { $result.restore_error = $_.Exception.Message }
    }
    if ($startupWritten) {
        try {
            if ($null -ne $previousStartup) { Set-ItemProperty -LiteralPath $StartupKey -Name $RunName -Value $previousStartup }
            else { Remove-ItemProperty -LiteralPath $StartupKey -Name $RunName }
        } catch { $result.startup_restore_error = $_.Exception.Message }
    }
    if ($hadInstalledProcess -and (Test-Path -LiteralPath $Executable)) {
        try {
            $restartArguments = $BackgroundArguments
            if (($result.program_restored -or -not $result.program_backup) -and
                $previousRun -in @($LegacyRunCommand, $legacySourceRunCommand)) {
                # 旧版 EXE 本身就是静默守护，不认识新增的 --background 参数。
                $restartArguments = '--config "' + $ConfigPath + '"'
            }
            $restartShell = New-Object -ComObject Shell.Application
            try { $restartShell.ShellExecute($Executable, $restartArguments, $ProjectDir, 'open', 0) }
            finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($restartShell) }
            $result.previous_process_restart_requested = $true
        } catch { $result.restart_error = $_.Exception.Message }
    }
} finally {
    $result.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    New-Item -ItemType Directory -Path (Split-Path -Parent $ResultPath) -Force | Out-Null
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding utf8
}
if ($result.ok) { Write-Output "完成：$($result.phase)。结果：$ResultPath"; exit 0 }
Write-Error -Message "$($result.error) 结果：$ResultPath" -ErrorAction Continue
exit 1
