# ProfileX PikPak Token 采集器（由下载控制台现场生成；不装 Python、不开端口、只读浏览器数据）
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$ConsoleUrl = '__CONSOLE_URL__'

function Done($code) { exit $code }

Write-Host ''
Write-Host '  ==================================================' -ForegroundColor DarkCyan
Write-Host '   ProfileX PikPak Token 采集器' -ForegroundColor Cyan
Write-Host "   → 推送到：$ConsoleUrl" -ForegroundColor DarkGray
Write-Host '  ==================================================' -ForegroundColor DarkCyan
Write-Host ''

$base = Join-Path $env:APPDATA 'com.bujic.profilex\profiles'
if (-not (Test-Path -LiteralPath $base)) {
  Write-Host "  × 没找到 ProfileX 数据目录：$base" -ForegroundColor Red
  Write-Host '    这台电脑需要装过 ProfileX 并在里面登录过 PikPak。' -ForegroundColor DarkGray
  Read-Host '  按回车退出'
  Done 1
}

$enc = [Text.Encoding]::GetEncoding(28591)
$rows = New-Object System.Collections.ArrayList
$dirs = @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)
Write-Host "  扫描到 $($dirs.Count) 个浏览器环境，正在读取登录态…" -ForegroundColor DarkGray

foreach ($d in $dirs) {
  # 环境目录名（ProfileX 用 UUID）截断成短码，仅用于定位「哪个环境要重新登录」
  $envTag = 'env-' + ((($d.Name -replace '[^A-Za-z0-9]', '') + '00000000').Substring(0, 8))
  $hit = $null
  foreach ($subName in @('Profile 1', 'Default', 'Profile 2', 'Profile 3')) {
    $ls = Join-Path $d.FullName ($subName + '\Local Storage\leveldb')
    if (-not (Test-Path -LiteralPath $ls)) { continue }
    $files = @(Get-ChildItem -LiteralPath $ls -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Extension -eq '.log' -or $_.Extension -eq '.ldb' } |
      Sort-Object -Property LastWriteTime -Descending)
    foreach ($f in $files) {
      $txt = $null
      try { $txt = $enc.GetString([IO.File]::ReadAllBytes($f.FullName)) } catch { continue }
      $m = [regex]::Matches($txt, '"refresh_token"\s*:\s*"(os\.[^"\x00]{20,600})"')
      if ($m.Count -gt 0) {
        $rt = $m[$m.Count - 1].Groups[1].Value
        $sid = ''
        # sub 必须和 refresh_token 取自同一条 JSON 记录，避免张冠李戴
        $m2 = [regex]::Match($txt, '"refresh_token"\s*:\s*"' + [regex]::Escape($rt) + '"[^}]{0,300}?"sub"\s*:\s*"([A-Za-z0-9_\-]{4,40})"')
        if ($m2.Success) { $sid = $m2.Groups[1].Value }
        if (-not $sid) {
          # 记录被压缩拆散时的兜底：整个文件里只有一个 sub 才算数，多个就宁可留空
          $uniq = @([regex]::Matches($txt, '"sub"\s*:\s*"([A-Za-z0-9_\-]{6,40})"') |
            ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
          if ($uniq.Count -eq 1) { $sid = $uniq[0] }
        }
        # 邮箱只在同一文件里唯一时才采用，避免多个账号混存时张冠李戴
        $mail = ''
        $emails = @([regex]::Matches($txt, '"(?:email|mail|account)"\s*:\s*"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"') |
          ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
        if ($emails.Count -eq 1) { $mail = $emails[0] }
        $hit = @{ refresh_token = $rt; sub = $sid; email = $mail; env = $envTag }
        break
      }
    }
    if ($hit) { break }
  }
  if ($hit) { [void]$rows.Add($hit) }
}

if ($rows.Count -eq 0) {
  Write-Host '  × 没在浏览器环境里找到 PikPak 登录态。' -ForegroundColor Red
  Write-Host '    请先在 ProfileX 的某个环境里打开并登录 PikPak，再运行本工具。' -ForegroundColor DarkGray
  Read-Host '  按回车退出'
  Done 1
}

Write-Host "  提取到 $($rows.Count) 个 refresh_token，正在推送（服务端会换发并实测，约 5-20 秒）…" -ForegroundColor DarkGray

$payload = (($rows | ForEach-Object {
  '{"env":"' + $_.env + '","sub":"' + $_.sub + '","email":"' + $_.email + '","refresh_token":"' + $_.refresh_token + '"}'
}) -join ',')
$body = '{"action":"acctimport","create":true,"items":[' + $payload + ']}'

try {
  $resp = Invoke-RestMethod -Uri $ConsoleUrl -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
    -ContentType 'application/json; charset=utf-8' -TimeoutSec 300
} catch {
  Write-Host '  × 连不上下载控制台：' -ForegroundColor Red
  Write-Host ('    ' + $_.Exception.Message) -ForegroundColor DarkGray
  Write-Host "    地址：$ConsoleUrl （确认 NAS 开机、控制台端口 5566 可达）" -ForegroundColor DarkGray
  Read-Host '  按回车退出'
  Done 1
}

if ($resp.error) {
  Write-Host ('  × ' + $resp.error) -ForegroundColor Red
  Read-Host '  按回车退出'
  Done 1
}

$ok = 0
$n = 0
foreach ($r in $resp.results) {
  $n++
  $label = $r.target
  if (-not $label -or $label -eq '-') { $label = $r.env }
  if (-not $label -or $label -eq '-') { $label = '(未知环境)' }
  $from = ''
  if ($r.env -and $r.env -ne '-' -and $r.env -ne $label) { $from = '   ← ' + $r.env }
  if ($r.ok) { $ok++; Write-Host ("  ✓ " + $label + "  [" + $r.action + "]  " + $r.msg + $from) -ForegroundColor Green }
  else { Write-Host ("  × " + $label + "  [" + $r.action + "]  " + $r.msg + $from) -ForegroundColor Red }
}

Write-Host ''
Write-Host "  完成：$ok / $n 个账号已连通。" -ForegroundColor Cyan
Write-Host '  回到控制台「账号」页点一下“刷新”即可看到最新状态。' -ForegroundColor DarkGray
