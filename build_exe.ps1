#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstall,
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$BuildDir = Join-Path $ProjectDir 'build'
$DistDir = Join-Path $ProjectDir 'dist'
$AppDir = Join-Path $ProjectDir 'campusnet_app'
$ConfigPath = Join-Path $ProjectDir 'config.json'

function Find-Python3 {
    $candidates = [System.Collections.Generic.List[string]]::new()
    $versions = Join-Path $env:USERPROFILE '.pyenv\pyenv-win\versions'
    if (Test-Path -LiteralPath $versions) {
        Get-ChildItem -LiteralPath $versions -Directory |
            ForEach-Object {
                $match = [regex]::Match($_.Name, '^(\d+)\.(\d+)')
                if ($match.Success -and [int]$match.Groups[1].Value -ge 3) {
                    $candidates.Add((Join-Path $_.FullName 'python.exe'))
                }
            }
    }
    foreach ($pattern in @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe"
    )) {
        Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates.Add($command.Source) }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$version".Trim() -match '^3\.\d+$') {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        } catch {}
    }
    return ''
}

function Assert-Child([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($ProjectDir).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "路径不在项目目录内：$full"
    }
    return $full
}

function Invoke-Hidden([string]$Executable, [string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new($Executable)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WorkingDirectory = $env:TEMP
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($info)
    if (-not $process.WaitForExit(30000)) {
        $process.Kill($true)
        throw "程序自检超时：$Executable"
    }
    $code = $process.ExitCode
    $process.Dispose()
    return $code
}

function Assert-GuiExe([string]$Executable) {
    $bytes = [IO.File]::ReadAllBytes($Executable)
    $peOffset = [BitConverter]::ToInt32($bytes, 60)
    $subsystem = [BitConverter]::ToUInt16($bytes, $peOffset + 24 + 68)
    if ($subsystem -ne 2) { throw "EXE 不是无窗口 GUI 子系统：$Executable" }
}

if (-not $PythonPath) { $PythonPath = Find-Python3 }
if (-not $PythonPath) { throw '找不到可用的 Python 3。' }
$Python = (Resolve-Path -LiteralPath $PythonPath).Path
& $Python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    if ($SkipInstall) { throw '未安装 PyInstaller，请先安装或去掉 -SkipInstall。' }
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败。' }
}

Push-Location -LiteralPath $ProjectDir
try {
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw '回归测试失败，停止构建。' }
} finally { Pop-Location }

if ($Clean) {
    foreach ($path in @($BuildDir, $DistDir)) {
        $safe = Assert-Child $path
        if (Test-Path -LiteralPath $safe) { Remove-Item -LiteralPath $safe -Recurse -Force }
    }
}

$source = Join-Path $ProjectDir 'campusnet_app.py'
if (-not (Test-Path -LiteralPath $source)) { throw "缺少入口：$source" }
Write-Host '构建 campusnet.exe'
& $Python -m PyInstaller --noconfirm --clean --onedir --noconsole --paths $ProjectDir --distpath $DistDir --workpath $BuildDir --specpath $BuildDir --name campusnet $source
if ($LASTEXITCODE -ne 0) { throw 'campusnet.exe 构建失败。' }

$newExe = Join-Path $DistDir 'campusnet\campusnet.exe'
Assert-GuiExe $newExe
if ((Invoke-Hidden $newExe @('--help')) -ne 0) { throw 'campusnet.exe --help 自检失败。' }
if (Test-Path -LiteralPath $ConfigPath) {
    $report = Join-Path $BuildDir 'campusnet-self-test.json'
    if ((Invoke-Hidden $newExe @('--config', $ConfigPath, '--self-test', '--report', $report)) -ne 0) {
        throw 'campusnet.exe --self-test 自检失败。'
    }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (-not $result.ok -or -not $result.frozen) { throw 'EXE 自检报告无效。' }
    Write-Host "运行时自检通过；Wi-Fi 可读=$($result.wifi_readable)，SSID=$($result.ssid)"
}

Get-CimInstance Win32_Process -Filter "Name = 'campusnet.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq (Join-Path $AppDir 'campusnet.exe') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }

$backup = Join-Path $ProjectDir ('release\previous-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $AppDir) {
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Move-Item -LiteralPath $AppDir -Destination (Join-Path $backup 'campusnet_app')
}
Move-Item -LiteralPath (Join-Path $DistDir 'campusnet') -Destination $AppDir
Write-Host "构建完成：$AppDir\campusnet.exe"
if (Test-Path -LiteralPath $backup) { Write-Host "旧版本备份：$backup" }

# 分发包只加入明确列出的文件，绝不复制本机 config.json 或日志。
$release = Join-Path $ProjectDir 'release'
$stage = Join-Path $release ('package-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    Copy-Item -LiteralPath $AppDir -Destination $stage -Recurse
    foreach ($name in @('config.example.json', 'install.ps1', 'uninstall.ps1', 'status.ps1', 'README.md', 'NOTICE.md', 'LICENSE', 'LICENSE.campusnet')) {
        Copy-Item -LiteralPath (Join-Path $ProjectDir $name) -Destination $stage
    }
    $archive = Join-Path $release 'campusnet-windows.zip'
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $archive -Force
    Write-Host "分发包：$archive"
} finally {
    $safeStage = Assert-Child $stage
    Remove-Item -LiteralPath $safeStage -Recurse -Force
}
