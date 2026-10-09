<#
.SYNOPSIS
    一键把本项目上传到 GitHub（含 push 与 Release）。

.DESCRIPTION
    这是给**沙箱外**用的脚本 —— 交互式 git 推送需要创建命名管道，
    在某些受限环境里会失败，所以拿到普通终端里跑最稳。

    脚本按顺序做四件事，每步都会先检查、失败就明确告诉你怎么办：

      1. 选认证方式（优先用你 ~/.ssh/config 里的 github.com.<账号> 别名走 SSH）
      2. 仓库不存在就用 gh 创建
      3. push main
      4. 创建 Release 并上传 zip 附件

.PARAMETER Owner
    GitHub 账号名，默认 Ember2024。

.PARAMETER Repo
    仓库名，默认 campusnet-autologin。

.PARAMETER Tag
    Release 标签，默认 v1.0.0。

.PARAMETER UseHttps
    强制用 HTTPS 而不是 SSH。

.PARAMETER SkipRelease
    只 push，不创建 Release。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\upload_to_github.ps1
#>
[CmdletBinding()]
param(
    [string]$Owner = 'Ember2024',
    [string]$Repo  = 'campusnet-autologin',
    [string]$Tag   = 'v1.0.0',
    [switch]$UseHttps,
    [switch]$SkipRelease
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseDir = Join-Path $ProjectDir 'release'

function Write-Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[x] $m" -ForegroundColor Red }

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

Write-Host ""
Write-Host "上传到 GitHub —— $Owner/$Repo" -ForegroundColor White
Write-Host ("=" * 64) -ForegroundColor DarkGray

if (-not (Test-Command git)) {
    Write-Err "找不到 git，请先安装 Git for Windows。"
    exit 2
}

if (-not (Test-Path (Join-Path $ProjectDir '.git'))) {
    Write-Err "这个目录还不是 git 仓库：$ProjectDir"
    exit 2
}

Push-Location $ProjectDir
try {
    # ---------------------------------------------------------- 1) 认证方式
    Write-Step "选择认证方式…"

    $sshAlias = "github.com.$($Owner.ToLower())"
    $sshConfig = Join-Path $env:USERPROFILE '.ssh\config'
    $hasAlias = $false
    if (Test-Path -LiteralPath $sshConfig) {
        $hasAlias = (Get-Content -LiteralPath $sshConfig -Raw) -match [regex]::Escape($sshAlias)
    }

    $remoteUrl = ""
    if (-not $UseHttps -and $hasAlias) {
        $remoteUrl = "git@${sshAlias}:$Owner/$Repo.git"
        Write-Ok "检测到 SSH 别名 $sshAlias，改用 SSH：$remoteUrl"

        # 验证这个别名确实能认证成目标账号
        $probe = & ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "git@$sshAlias" 2>&1 | Out-String
        if ($probe -match "Hi $Owner!") {
            Write-Ok "SSH 认证成功：$($probe.Trim())"
        } else {
            Write-Warn2 "SSH 探测输出不符合预期：$($probe.Trim())"
            Write-Warn2 "若后续 push 失败，请加 -UseHttps 重试。"
        }
    } else {
        $remoteUrl = "https://github.com/$Owner/$Repo.git"
        Write-Ok "使用 HTTPS：$remoteUrl"
        if (-not $UseHttps) {
            Write-Warn2 "没找到 SSH 别名 $sshAlias，退回 HTTPS。"
            Write-Host "    若提示 could not read Username，请先执行 gh auth login" -ForegroundColor Gray
            Write-Host "    或在凭据管理器里为 github.com 存一个 Personal Access Token。" -ForegroundColor Gray
        }
        # 这台机器上 schannel 可能不可用，openssl 更稳
        git config http.sslBackend openssl
    }

    git remote remove origin 2>$null | Out-Null
    git remote add origin $remoteUrl
    Write-Host "    origin = $(git remote get-url origin)" -ForegroundColor Gray

    # ------------------------------------------------------------ 2) 建仓库
    Write-Step "确认仓库是否存在…"
    $exists = $false
    try {
        git ls-remote --heads origin 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $exists = $true }
    } catch { }

    if ($exists) {
        Write-Ok "仓库已存在"
    } else {
        Write-Warn2 "仓库不存在或读不到。"
        if (Test-Command gh) {
            $authOk = $false
            try {
                gh auth status 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) { $authOk = $true }
            } catch { }

            if (-not $authOk) {
                Write-Err "gh 未登录，无法自动创建仓库。请任选其一："
                Write-Host "    A) gh auth login                                  然后重跑本脚本" -ForegroundColor Gray
                Write-Host "    B) 浏览器打开 https://github.com/new 手动创建空仓库 $Repo（不要勾选 README）" -ForegroundColor Gray
                exit 1
            }

            Write-Step "用 gh 创建仓库 $Owner/$Repo …"
            gh repo create "$Owner/$Repo" --public `
                --description "校园网自动登录认证工具：连上校园网 Wi-Fi 自动认证 / 开机自启 / 断网自动重连（锐捷 ePortal）"
            if ($LASTEXITCODE -ne 0) {
                Write-Err "创建仓库失败。"
                exit 1
            }
            Write-Ok "仓库创建成功"
        } else {
            Write-Err "gh 不可用，请手动创建空仓库：https://github.com/new"
            exit 1
        }
    }

    # ---------------------------------------------------------------- 3) push
    Write-Step "推送 main …"
    git push -u origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Err "push 失败。常见原因与对策："
        Write-Host "    · could not read Username  -> HTTPS 没凭据：gh auth login，或改用 SSH（-UseHttps 去掉）" -ForegroundColor Gray
        Write-Host "    · Permission denied        -> SSH 密钥不属于 $Owner" -ForegroundColor Gray
        Write-Host "    · non-fast-forward         -> 远端已有提交，先 git pull --rebase origin main" -ForegroundColor Gray
        exit 1
    }
    Write-Ok "推送完成"

    # ------------------------------------------------------------- 4) Release
    if ($SkipRelease) {
        Write-Ok "按 -SkipRelease 跳过 Release 创建。"
        exit 0
    }

    Write-Step "创建 Release $Tag …"

    $assets = @()
    foreach ($name in 'run_login.zip', 'daemon.zip', 'config.example.json') {
        $path = Join-Path $ReleaseDir $name
        if (Test-Path -LiteralPath $path) {
            $assets += $path
            Write-Host ("    附件：{0}  ({1:N2} MB)" -f $name, ((Get-Item $path).Length / 1MB)) -ForegroundColor Gray
        } else {
            Write-Warn2 "缺少附件：$name（可先跑 build_exe.ps1 再重新打包）"
        }
    }

    if (-not (Test-Command gh)) {
        Write-Warn2 "没有 gh，无法自动创建 Release。请手动在网页上传 $ReleaseDir 里的文件。"
        exit 0
    }

    $notesFile = Join-Path $ProjectDir 'RELEASE_NOTES.md'
    $ghArgs = @('release', 'create', $Tag, '--title', $Tag)
    if (Test-Path -LiteralPath $notesFile) {
        $ghArgs += @('--notes-file', $notesFile)
    } else {
        $ghArgs += @('--notes', "校园网自动认证工具 $Tag")
    }
    $ghArgs += $assets

    & gh @ghArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "Release 创建失败（可能该 tag 已存在）。可改用：gh release upload $Tag <文件> --clobber"
    } else {
        Write-Ok "Release $Tag 创建完成"
    }

    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor DarkGray
    Write-Ok "全部完成：https://github.com/$Owner/$Repo"
} finally {
    Pop-Location
}
Write-Host ""
