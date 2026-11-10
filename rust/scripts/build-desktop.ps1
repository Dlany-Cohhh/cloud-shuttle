#Requires -Version 5.1
<#
  云梭 CloudShuttle · 桌面原生窗口版（单文件 exe）构建脚本
  默认构建 webview 形态：把仓库根 static/ 内嵌进 exe，界面与 Web 控制台完全一致。
  产物：dist\desktop\cloud-shuttle-desktop.exe
        dist\cloud-shuttle-desktop.exe（供 publish_release.py 附带到统一 Release）
  用法：
    powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1 -ReleaseTag v3.57
    powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1 -LegacyGui
#>
[CmdletBinding()]
param(
    [string]$OutDir = "",
    [string]$ReleaseTag = "",
    [switch]$DebugBuild,
    [switch]$LegacyGui
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $OutDir) { $OutDir = Join-Path $Root 'dist\desktop' }
if (-not $ReleaseTag -and $env:CS_RELEASE_TAG) { $ReleaseTag = $env:CS_RELEASE_TAG }
if ($ReleaseTag) { $env:CS_RELEASE_TAG = $ReleaseTag }
else { Remove-Item Env:\CS_RELEASE_TAG -ErrorAction SilentlyContinue }

$Feature = if ($LegacyGui) { 'gui' } else { 'webview' }
Write-Host "==> 构建桌面原生窗口版 / feature=$Feature / tag=$ReleaseTag"
# cargo 把编译进度写到 stderr；PS 5.1 直接调用会把它包装成 NativeCommandError 红字。
# 交给 cmd 合并输出流，PowerShell 侧只会收到普通文本。
$profileFlag = if ($DebugBuild) { '' } else { '--release' }
$cargoLine = "cargo build $profileFlag --features $Feature --bin cloud-shuttle-desktop 2>&1"
& cmd.exe /c $cargoLine | ForEach-Object { Write-Host $_ }
$code = $LASTEXITCODE
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
Write-Host "  webview 形态复用仓库根 static/，界面与 Web 控制台一致（需系统 WebView2）"
Write-Host "  -LegacyGui 可构建零 Web 依赖的 egui 自绘轻量版"
Write-Host "  首次启动缺 rclone 时，窗口顶部引导条可一键从官方源安装"
Write-Host "  自检：.\dist\desktop\cloud-shuttle-desktop.exe --check"
