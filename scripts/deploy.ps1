<#
.SYNOPSIS
  PikPak 多账号下载器（云梭 CloudShuttle）· Windows Docker Desktop 一键部署

.DESCRIPTION
  交互式询问下载目录、rclone 配置目录与代理地址，生成 .env，然后用
  docker-compose.example.yml + docker-compose.windows.yml 构建并启动容器。
  Windows 版使用 bridge 端口映射（不依赖 host 网络），代理默认指向宿主机
  http://host.docker.internal:7890，可填任意自定义地址或 off 关闭。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Yes -Proxy http://192.168.1.10:7890
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Command status
#>
[CmdletBinding()]
param(
  [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
  [string]$Command = 'start',
  [string]$Root = '',
  [string]$Downloads = '',
  [string]$ConfigDir = '',
  [string]$Proxy = '',
  [switch]$NoProxy,
  [int]$Port = 0,
  [string]$PublicHost = '',
  [switch]$Yes,
  [switch]$Force,
  [switch]$DryRun,
  [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$script:ComposeKind = 'plugin'

function Write-Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Note([string]$m) { Write-Host "[提示] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "`n[错误] $m" -ForegroundColor Red; exit 1 }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($Root)) { $Root = Join-Path $scriptDir '..' }
try { $Root = [System.IO.Path]::GetFullPath($Root) } catch { Fail "项目目录无效: $Root" }
$envFile = Join-Path $Root '.env'
$composeFile = Join-Path $Root 'docker-compose.example.yml'
$winOverride = Join-Path $Root 'docker-compose.windows.yml'
if (-not (Test-Path -LiteralPath $composeFile)) { Fail "找不到 docker-compose.example.yml：$composeFile" }
if (-not (Test-Path -LiteralPath $winOverride)) { Fail "找不到 docker-compose.windows.yml：$winOverride" }
if (-not (Test-Path -LiteralPath (Join-Path $Root 'download-console.py'))) { Fail "项目目录里没有 download-console.py：$Root" }

function Get-EnvValue([string]$key, [string]$default) {
  if (Test-Path -LiteralPath $envFile) {
    foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
      if ($line -match ('^\s*' + [regex]::Escape($key) + '=(.*)$')) { return $Matches[1] }
    }
  }
  return $default
}

function Ask([string]$prompt, [string]$default) {
  if ($Yes) { return $default }
  $s = Read-Host "$prompt [回车默认: $default]"
  if ([string]::IsNullOrWhiteSpace($s)) { return $default }
  return $s.Trim()
}

function To-DockerPath([string]$p) {
  $full = [System.IO.Path]::GetFullPath($p)
  if (-not (Test-Path -LiteralPath $full)) { New-Item -ItemType Directory -Path $full -Force | Out-Null }
  return $full.Replace('\', '/')
}

function New-Secret {
  $bytes = New-Object byte[] 24
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  return (-join ($bytes | ForEach-Object { $_.ToString('x2') }))
}

function Invoke-Compose([string[]]$Rest) {
  $all = @('-f', $composeFile, '-f', $winOverride, '--env-file', $envFile) + $Rest
  if ($script:ComposeKind -eq 'legacy') { & docker-compose @all } else { & docker compose @all }
  if ($LASTEXITCODE -ne 0 -and $Rest[0] -ne 'logs') {
    Fail "docker compose 执行失败（退出码 $LASTEXITCODE）"
  }
}

# ── 检测 Docker Desktop ────────────────────────────────────────────────
if (-not $DryRun) {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail '未找到 docker 命令，请先安装并启动 Docker Desktop'
  }
  & docker compose version 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { $script:ComposeKind = 'plugin' }
  elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) { $script:ComposeKind = 'legacy' }
  else { Fail '未找到 docker compose 插件，请升级 Docker Desktop' }
  & docker info 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'Docker 守护进程不可用，请先启动 Docker Desktop 并等它就绪' }
}

if ($Command -ne 'start') {
  if (-not (Test-Path -LiteralPath $envFile)) { Fail '还没有 .env，请先运行一键部署（start）' }
  switch ($Command) {
    'status'  { Invoke-Compose @('ps') }
    'logs'    { Invoke-Compose @('logs', '-f', 'download-console') }
    'stop'    { Invoke-Compose @('stop') }
    'restart' { Invoke-Compose @('restart', 'download-console', 'rclone-backend') }
  }
  exit 0
}

Write-Host ''
Write-Host '==========================================================' -ForegroundColor Green
Write-Host ' PikPak 多账号下载器 · 云梭 CloudShuttle' -ForegroundColor Green
Write-Host ' Docker 一键部署（Windows / Docker Desktop）' -ForegroundColor Green
Write-Host '==========================================================' -ForegroundColor Green
Write-Step "项目目录：$Root"

if (Test-Path -LiteralPath $envFile) {
  if ($Force) {
    Copy-Item -LiteralPath $envFile -Destination ("$envFile.bak." + (Get-Date -Format 'yyyyMMddHHmmss')) -Force
    Write-Note '已备份原 .env 为 *.bak.*，将重新生成'
  } else {
    Write-Note '检测到已有 .env，将沿用其中的路径与凭据（要重新生成请加 -Force）'
  }
}

# ── 收集参数 ───────────────────────────────────────────────────────────
$defDownloads = Get-EnvValue 'DOWNLOAD_ROOT' (Join-Path $Root 'downloads')
$defConfig    = Get-EnvValue 'RCLONE_CONFIG_DIR' (Join-Path $Root 'rclone-config')
$defProxy     = Get-EnvValue 'PROXY_URL' 'http://host.docker.internal:7890'
$defPort      = Get-EnvValue 'WEB_PORT' '5566'
$defPublic    = Get-EnvValue 'CONSOLE_PUBLIC_HOST' 'localhost:5566'

if ([string]::IsNullOrWhiteSpace($Downloads)) { $Downloads = Ask '下载文件保存目录' $defDownloads }
if ([string]::IsNullOrWhiteSpace($ConfigDir)) { $ConfigDir = Ask 'rclone.conf 所在目录' $defConfig }
if ([string]::IsNullOrWhiteSpace($PublicHost)) { $PublicHost = Ask '采集脚本回推地址（一般直接回车）' $defPublic }
if ($Port -eq 0) { $Port = [int]$defPort }

if ($NoProxy) {
  $Proxy = 'off'
} elseif ([string]::IsNullOrWhiteSpace($Proxy)) {
  $Proxy = Ask '代理地址（回车用默认；可填 http(s):// 或 socks5://；off = 不走代理）' $defProxy
}
if ([string]::IsNullOrWhiteSpace($Proxy)) { $Proxy = $defProxy }

$Downloads = To-DockerPath $Downloads
$ConfigDir = To-DockerPath $ConfigDir
$rootForEnv = $Root.Replace('\', '/')
$rcUser = Get-EnvValue 'RCLONE_RC_USER' 'console'
$rcPass = Get-EnvValue 'RCLONE_RC_PASS' ''
if ([string]::IsNullOrWhiteSpace($rcPass) -or $rcPass -like 'change-me*') {
  $rcPass = New-Secret
  Write-Step '已生成随机 rclone RC 凭据'
}
$openlistPass = Get-EnvValue 'OPENLIST_ADMIN_PASSWORD' (New-Secret)
$aria2Secret  = Get-EnvValue 'ARIA2_RPC_SECRET' (New-Secret)

# ── 准备 rclone.conf ────────────────────────────────────────────────────
$confPath = Join-Path $ConfigDir 'rclone.conf'
if (-not (Test-Path -LiteralPath $confPath)) {
  [System.IO.File]::WriteAllText($confPath, '', [System.Text.UTF8Encoding]::new($false))
  Write-Note "已创建空的 rclone.conf：$confPath"
  Write-Note '启动后进「账号」页，用「导入 / 采集 Token」或「重新登录」添加 PikPak 账号'
} else {
  Write-Note '沿用已有 rclone.conf（含账号 refresh_token，请勿提交到仓库）'
}

# ── 生成 .env ──────────────────────────────────────────────────────────
$lines = @(
  ('# 由 scripts/deploy.ps1 生成于 ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
  '# 此文件含 RC 密码，已被 .gitignore 忽略，切勿提交。',
  "PROJECT_ROOT=$rootForEnv",
  "DOWNLOAD_ROOT=$Downloads",
  "RCLONE_CONFIG_DIR=$ConfigDir",
  "RCLONE_RC_USER=$rcUser",
  "RCLONE_RC_PASS=$rcPass",
  "PROXY_URL=$Proxy",
  "WEB_PORT=$Port",
  "CONSOLE_PUBLIC_HOST=$PublicHost",
  'UPDATE_REPO=Dlany-Cohhh/cloud-shuttle',
  'UPDATE_AUTO_RESTART=1',
  "OPENLIST_ADMIN_PASSWORD=$openlistPass",
  "ARIA2_RPC_SECRET=$aria2Secret"
)
[System.IO.File]::WriteAllText($envFile, (($lines -join "`n") + "`n"),
                               [System.Text.UTF8Encoding]::new($false))
Write-Step "已写入 $envFile"
Write-Host "   下载目录   : $Downloads"
Write-Host "   配置目录   : $ConfigDir"
Write-Host "   代理地址   : $Proxy"
Write-Host "   访问端口   : $Port"

$upArgs = @('up', '-d')
if (-not $NoBuild) { $upArgs += '--build' }
if ($DryRun) {
  Write-Note '-DryRun：只生成配置，跳过容器启动'
  Write-Host "   将要执行 : docker compose -f $composeFile -f $winOverride --env-file $envFile $($upArgs -join ' ')"
  exit 0
}

Write-Step '构建并启动容器（首次构建镜像需要几分钟）'
Invoke-Compose $upArgs

# ── 健康检查 ───────────────────────────────────────────────────────────
Write-Step '等待控制台就绪'
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/")
    $req.Proxy = $null
    $req.Timeout = 2000
    $resp = $req.GetResponse()
    $resp.Close()
    $ready = $true
    break
  } catch { Start-Sleep -Seconds 1 }
}

Write-Host ''
Write-Host '==========================================================' -ForegroundColor Green
if ($ready) {
  Write-Host " 部署完成 ✓  打开 http://127.0.0.1:$Port/" -ForegroundColor Green
} else {
  Write-Host ' 容器已启动，但接口暂未响应；用 logs 查看：' -ForegroundColor Yellow
  Write-Host '   powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Command logs'
  Write-Host " 浏览器打开 http://127.0.0.1:$Port/"
}
Write-Host ''
Write-Host ' 下一步：'
Write-Host '   1. 浏览器按 Ctrl+F5 强刷一次，进「账号」页导入 PikPak 账号'
Write-Host "   2. 代理地址可在「设置 → 代理下载」随时改；部署时用的是 $Proxy"
Write-Host '   3. 常用命令：-Command status | logs | stop | restart'
Write-Host "   4. .env 含 RC 密码与路径，位置：$envFile"
Write-Host '==========================================================' -ForegroundColor Green
