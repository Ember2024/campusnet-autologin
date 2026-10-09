<#
.SYNOPSIS
    把校园网自动认证工具打包成**不依赖 Python 环境**的独立 exe。

.DESCRIPTION
    产出两个可执行文件，直接放在项目根目录：

      run_login.exe    控制台版 —— 被任务计划按事件/定时触发，认证一次就退出
      daemon.exe       无窗口版 —— 常驻守护，开机抢网 + 定期巡检 + 断网重连

    之后目标机器**完全不需要安装 Python**：解释器和标准库都打进去了。

    为什么是两个而不是一个 exe：
      · 守护进程要长期后台运行，绝不能弹黑窗 —— 必须 --noconsole；
      · 触发式任务需要能看到输出（排障时有用），但被任务计划调起时
        本来也不显示窗口 —— 用控制台版更灵活（手动双击也能看日志）。
      把无窗口版当守护用、控制台版当触发用，是最贴合实际场景的组合。

.PARAMETER Clean
    先删掉之前的 build / dist 再重新打包。

.PARAMETER SkipInstall
    不尝试自动安装 PyInstaller（已装好时用，省时间）。

.PARAMETER KeepSpec
    保留 PyInstaller 生成的 .spec 文件（默认删除，保持目录整洁）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstall,
    [switch]$KeepSpec
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildDir = Join-Path $ProjectDir 'build'
$DistDir = Join-Path $ProjectDir 'dist'

function Write-Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[x] $m" -ForegroundColor Red }

function Find-Python3 {
    <#
        找一个真正能跑的 Python 3。
        注意：PATH 里的 python.exe 很可能是 Windows 应用商店的别名桩
        （调用直接返回 9009），不能直接用，必须实际执行一次验证。
    #>
    $candidates = New-Object System.Collections.Generic.List[string]

    $pyenvVersions = "$env:USERPROFILE\.pyenv\pyenv-win\versions"
    if (Test-Path -LiteralPath $pyenvVersions) {
        Get-ChildItem -Path $pyenvVersions -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $m = [regex]::Match($_.Name, '^(\d+)\.(\d+)')
                if ($m.Success -and [int]$m.Groups[1].Value -ge 3) {
                    [pscustomobject]@{
                        Exe   = Join-Path $_.FullName 'python.exe'
                        Major = [int]$m.Groups[1].Value
                        Minor = [int]$m.Groups[2].Value
                    }
                }
            } |
            Sort-Object -Property @{Expression='Major';Descending=$true},
                                  @{Expression='Minor';Descending=$true} |
            ForEach-Object { $candidates.Add($_.Exe) }
    }

    foreach ($pattern in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                           "$env:ProgramFiles\Python3*\python.exe")) {
        Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add($_.FullName) }
    }

    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }

    foreach ($exe in $candidates) {
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }
        try {
            $out = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$out".Trim() -match '^3\.\d+$') {
                return (Resolve-Path -LiteralPath $exe).Path
            }
        } catch { }
    }
    return ""
}

Write-Host ""
Write-Host "校园网自动认证 —— 打包为独立 exe（免 Python 环境）" -ForegroundColor White
Write-Host ("=" * 64) -ForegroundColor DarkGray

foreach ($required in @('run_login.py', 'daemon.py', 'config.json')) {
    $path = Join-Path $ProjectDir $required
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Err "缺少必要文件：$path"
        exit 2
    }
    if ($required -eq 'config.json') {
        Write-Warn2 "注意：config.json 不会被装进 exe —— 它必须留在 exe 旁边。"
        Write-Host "      （这样改账号密码不用重新打包）" -ForegroundColor DarkGray
    }
}

# ------------------------------------------------------------------ Python
Write-Step "查找可用的 Python 3 解释器…"
$python = Find-Python3
if (-not $python) {
    Write-Err "找不到可用的 Python 3。打包本身需要一个 Python 环境（只有目标机器不需要）。"
    exit 2
}
Write-Ok "使用解释器：$python"

# --------------------------------------------------------------- PyInstaller
$havePyInstaller = $false
try {
    $ver = & $python -m PyInstaller --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver) {
        $havePyInstaller = $true
        Write-Ok "PyInstaller 已安装：$ver"
    }
} catch { }

if (-not $havePyInstaller) {
    if ($SkipInstall) {
        Write-Err "未安装 PyInstaller，且指定了 -SkipInstall。请先：$python -m pip install pyinstaller"
        exit 2
    }
    Write-Step "安装 PyInstaller（需要联网，走官方 PyPI）…"
    & $python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Err "PyInstaller 安装失败。若是网络问题，可换国内镜像："
        Write-Host "    $python -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple" -ForegroundColor Gray
        exit 2
    }
    Write-Ok "PyInstaller 安装完成"
}

if ($Clean) {
    Write-Step "清理旧的 build / dist …"
    foreach ($dir in @($BuildDir, $DistDir)) {
        if (Test-Path -LiteralPath $dir) {
            Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# ------------------------------------------------------------------- 打包
# --onedir：启动快、不每次解压、杀软误报少。相比 --onefile 更适合
#           「开机就要立刻抢网」和「每 5 分钟被触发一次」这两个场景。
$commonArgs = @(
    '--noconfirm', '--clean',
    '--onedir',
    '--paths', $ProjectDir,
    '--distpath', $DistDir,
    '--workpath', $BuildDir
)

$targets = @(
    [pscustomobject]@{
        Name     = 'run_login'
        Script   = Join-Path $ProjectDir 'run_login.py'
        Console  = $true
        Title    = '校园网自动认证（控制台版，供任务计划触发）'
    },
    [pscustomobject]@{
        Name     = 'daemon'
        Script   = Join-Path $ProjectDir 'daemon.py'
        Console  = $false
        Title    = '校园网自动认证守护（无窗口版）'
    }
)

foreach ($target in $targets) {
    Write-Step "打包 $($target.Name).exe …"
    $args = @($commonArgs) + @(
        '--name', $target.Name,
        '--specpath', $BuildDir
    )
    if ($target.Console) { $args += '--console' } else { $args += '--noconsole' }
    $args += $target.Script

    & $python -m PyInstaller @args
    if ($LASTEXITCODE -ne 0) {
        Write-Err "$($target.Name) 打包失败"
        exit 1
    }
    Write-Ok "$($target.Name).exe 打包完成"
}

# ------------------------------------------------- 把 exe 放到项目根目录
Write-Step "把产物搬到项目根目录…"
foreach ($target in $targets) {
    $srcDir = Join-Path $DistDir $target.Name
    if (-not (Test-Path -LiteralPath $srcDir)) {
        Write-Err "找不到打包产物：$srcDir"
        exit 1
    }

    # onedir 产物是一整个目录，需要连同依赖一起保留。
    # 用固定目录名，避免新版覆盖旧版后残留无关文件。
    $destDir = Join-Path $ProjectDir $target.Name
    if (Test-Path -LiteralPath $destDir) {
        Remove-Item -LiteralPath $destDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $srcDir -Destination $destDir
    Write-Ok "$($target.Name)\ 已就位（$( (Get-ChildItem $destDir -Recurse -File | Measure-Object).Count ) 个文件）"
}

if (-not $KeepSpec) {
    Get-ChildItem -Path $ProjectDir -Filter '*.spec' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------- 验证
Write-Host ""
Write-Host ("=" * 64) -ForegroundColor DarkGray
Write-Step "验证产物（直接运行 exe，不经过 Python）…"

$runExe = Join-Path $ProjectDir 'run_login\run_login.exe'
$daemonExe = Join-Path $ProjectDir 'daemon\daemon.exe'

$allOk = $true

if (Test-Path -LiteralPath $runExe) {
    $out = & $runExe --help 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -and $out -match 'config') {
        Write-Ok "run_login.exe 可执行（--help 正常）"
    } else {
        Write-Err "run_login.exe 执行异常：$out"
        $allOk = $false
    }
} else {
    Write-Err "缺少 $runExe"
    $allOk = $false
}

if (Test-Path -LiteralPath $daemonExe) {
    # 无窗口版不能靠 --help 拿输出（没有控制台），改用 --once-check 的退出码判断：
    # 已联网时为 0，未联网为 1 —— 只要不是启动崩溃（非 0/1）就算通过。
    & $daemonExe --once-check 2>&1 | Out-Null
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 1) {
        Write-Ok "daemon.exe 可执行（--once-check 退出码 $code）"
    } else {
        Write-Err "daemon.exe 执行异常，退出码 $code"
        $allOk = $false
    }
} else {
    Write-Err "缺少 $daemonExe"
    $allOk = $false
}

Write-Host ""
if ($allOk) {
    Write-Ok "打包完成。目标机器不需要安装 Python。"
    Write-Host ""
    Write-Host "接下来（需管理员）把任务计划指向 exe：" -ForegroundColor White
    Write-Host "    Start-Process powershell -Verb RunAs -ArgumentList ``" -ForegroundColor Gray
    Write-Host "      '-ExecutionPolicy Bypass -File `"$ProjectDir\install_tasks.ps1`" -UseExe'``" -ForegroundColor Gray
    Write-Host ""
    Write-Host "注意：config.json 与 logs\\ 必须和 run_login\\ 、daemon\\ 放在同一个目录下。" -ForegroundColor Yellow
} else {
    Write-Err "有产物未通过验证，请把上面的报错发出来。"
    exit 1
}
Write-Host ""
