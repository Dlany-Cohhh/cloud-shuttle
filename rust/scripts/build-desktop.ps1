#Requires -Version 5.1
<#
  云梭 CloudShuttle · 桌面原生窗口版（单文件 exe）构建脚本
  产物：dist\desktop\cloud-shuttle-desktop.exe
        dist\cloud-shuttle-desktop.exe（供 publish_release.py 附带到统一 Release）
  用法：
    powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1 -ReleaseTag v3.57
    powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1 -DebugBuild
#>
[CmdletBinding()]
param(
    [string]$OutDir = "",
    [string]$ReleaseTag = "",
    [switch]$DebugBuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $OutDir) { $OutDir = Join-Path $Root 'dist\desktop' }
if (-not $ReleaseTag -and $env:CS_RELEASE_TAG) { $ReleaseTag = $env:CS_RELEASE_TAG }
if ($ReleaseTag) { $env:CS_RELEASE_TAG = $ReleaseTag }
else { Remove-Item Env:\CS_RELEASE_TAG -ErrorAction SilentlyContinue }

Write-Host "==> 构建桌面原生窗口版 / tag=$ReleaseTag"
# cargo 把编译进度写到 stderr；PS 5.1 在 ErrorActionPreference=Stop 时会把它当异常中断。
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
if ($DebugBuild) { & cargo build --features gui --bin cloud-shuttle-desktop }
else { & cargo build --release --features gui --bin cloud-shuttle-desktop }
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($code -ne 0) { throw "cargo build 失败，退出码 $code" }

$profileDir = if ($DebugBuild) { 'debug' } else { 'release' }
$built = Join-Path $Root "target\$profileDir\cloud-shuttle-desktop.exe"
if (-not (Test-Path $built)) { throw "找不到编译产物：$built" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'dist') | Out-Null
Copy-Item -Force $built (Join-Path $OutDir 'cloud-shuttle-desktop.exe')
Copy-Item -Force $built (Join-Path $Root 'dist\cloud-shuttle-desktop.exe')

$hash = (Get-FileHash -Algorithm SHA256 -Path $built).Hash.ToLower()
Set-Content -Path (Join-Path $OutDir 'cloud-shuttle-desktop.exe.sha256') -Value "$hash  cloud-shuttle-desktop.exe" -Encoding ASCII
Set-Content -Path (Join-Path $Root 'dist\cloud-shuttle-desktop.exe.sha256') -Value "$hash  cloud-shuttle-desktop.exe" -Encoding ASCII

$size = [math]::Round((Get-Item $built).Length / 1MB, 2)
Write-Host ""
Write-Host "构建完成：$OutDir\cloud-shuttle-desktop.exe ($size MB)"
Write-Host "  单文件运行：不需要 static/ 目录、不启动 HTTP 服务、不需要浏览器"
Write-Host "  首次启动如缺少 rclone，界面里点「一键下载 rclone」即可"
Write-Host "  自检：.\dist\desktop\cloud-shuttle-desktop.exe --check"
