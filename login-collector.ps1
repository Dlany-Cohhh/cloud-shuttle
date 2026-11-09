# PikPak 干净浏览器登录采集器（由下载控制台现场生成）v2
# 只在一个全新的临时 profile 里工作，绝不读取用户现有浏览器的任何数据。
# v2：改用 CDP 实时读取页面存储（不再依赖 leveldb 落盘延迟），
#     并在结束时关闭浏览器做一次全量文件兜底扫描 + 明确诊断。
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$ConsoleUrl = '__CONSOLE_URL__'

function Done($code) { exit $code }
function Say($msg, $color) { if ($color) { Write-Host $msg -ForegroundColor $color } else { Write-Host $msg } }
function As-Array($x) {
  if ($null -eq $x) { return @() }
  if ($x -is [array]) {
    $r = @()
    foreach ($e in $x) { if ($e -is [array]) { $r += @(As-Array $e) } else { $r += @($e) } }
    return $r
  }
  return @($x)
}

$logPath = Join-Path $env:TEMP 'pikpak-collect-log.txt'
try { [IO.File]::WriteAllText($logPath, '', [Text.Encoding]::UTF8) } catch {}
$script:logLines = New-Object System.Collections.ArrayList
function Log($msg) {
  $line = (Get-Date -Format 'HH:mm:ss') + ' ' + $msg
  [void]$script:logLines.Add($line)
  try { [IO.File]::AppendAllText($logPath, $line + "`r`n", [Text.Encoding]::UTF8) } catch {}
}

# ---- token 提取：兼容明文 JSON、转义 JSON、驼峰字段，以及 PikPak 特征前缀 ----
$TOKEN_PATTERNS = @(
  '\\?"refresh_token\\?"\s*:\s*\\?"([A-Za-z0-9._\-]{20,600})\\?"',
  '\\?"(?:refreshToken|refresh|rt)\\?"\s*:\s*\\?"([A-Za-z0-9._\-]{20,600})\\?"',
  '(os\.[A-Za-z0-9._\-]{30,600})'
)
function Extract-Tokens($txt) {
  $found = @()
  foreach ($p in $TOKEN_PATTERNS) {
    foreach ($m in [regex]::Matches($txt, $p)) {
      $v = $m.Groups[1].Value
      if ($v.Length -ge 20) { $found += $v }
    }
  }
  return @($found | Sort-Object -Unique)
}
function Extract-Identity($txt, $rt) {
  $sid = ''
  $m2 = [regex]::Match($txt, '"refresh_token"\s*:\s*"' + [regex]::Escape($rt) + '"[^}]{0,400}?"sub"\s*:\s*"([A-Za-z0-9_\-]{4,40})"')
  if ($m2.Success) { $sid = $m2.Groups[1].Value }
  if (-not $sid) {
    $uniq = @([regex]::Matches($txt, '\\?"sub\\?"\s*:\s*\\?"([A-Za-z0-9_\-]{6,40})\\?"') |
      ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
    if ($uniq.Count -eq 1) { $sid = $uniq[0] }
  }
  $mail = ''
  $emails = @([regex]::Matches($txt, '\\?"(?:email|mail|account)\\?"\s*:\s*\\?"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\\?"') |
    ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
  if ($emails.Count -eq 1) { $mail = $emails[0] }
  return @{ sub = $sid; email = $mail }
}

$script:seen = @{}
$script:count = 0
$script:diagKeys = @{}
function Push-Token($rt, $txt, $source) {
  if ($script:seen.ContainsKey($rt)) { return }
  $script:seen[$rt] = $true
  $id = Extract-Identity $txt $rt
  # env 留空：它只是来源标记，不能当账号名（后端会用邮箱前缀 / 真实身份命名）
  $item = [ordered]@{ env = ''; sub = $id.sub; email = $id.email; refresh_token = $rt }
  $body = ([ordered]@{ action = 'acctimport'; create = $true; items = @($item) }) |
    ConvertTo-Json -Depth 5 -Compress
  Say ('  发现凭证（来源：' + $source + '），正在换发并写入…') 'DarkGray'
  Log ('token found via ' + $source + ' len=' + $rt.Length + ' sub=' + $id.sub)
  try {
    $resp = Invoke-RestMethod -Uri $ConsoleUrl -Method Post `
      -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
      -ContentType 'application/json; charset=utf-8' -TimeoutSec 300
    $rs = @(As-Array $resp.results)
    $r = if ($rs.Count -gt 0) { $rs[0] } else { $null }
    if ($resp.error) {
      Say ('  × 写入失败：' + $resp.error) 'Red'
      Log ('server error: ' + $resp.error)
    } elseif ($r -and $r.ok) {
      $script:count++
      Say ('  ✓ 已采集并写入：' + $r.target + '  [' + $r.action + ']  ' + $r.msg) 'Green'
      Say '    可以继续在这个浏览器里退出、登录下一个账号。' 'DarkGray'
      Log ('written: ' + $r.target)
    } else {
      $why = '无返回'
      if ($r -and $r.msg) { $why = $r.msg }
      Say ('  × 采集到凭证，但服务端校验未通过：' + $why) 'Red'
      Log ('server rejected: ' + $why)
    }
  } catch {
    Say ('  × 连不上控制台：' + $_.Exception.Message) 'Red'
    Say "    地址：$ConsoleUrl （确认 NAS 开机、控制台 5566 端口可达）" 'DarkGray'
    Log ('http error: ' + $_.Exception.Message)
  }
}

# ---- CDP：直连浏览器页面，实时读取 localStorage / sessionStorage / cookie ----
$JS_READ = '(()=>{let o="";try{for(let i=0;i<localStorage.length;i++){let k=localStorage.key(i);o+=k+"="+localStorage.getItem(k)+"\n"}}catch(e){};try{for(let i=0;i<sessionStorage.length;i++){let k=sessionStorage.key(i);o+=k+"="+sessionStorage.getItem(k)+"\n"}}catch(e){};try{o+="COOKIE="+document.cookie+"\n"}catch(e){};return o})()'

function Cdp-Targets($port) {
  try {
    $raw = Invoke-RestMethod -Uri ("http://127.0.0.1:$port/json/list") -TimeoutSec 4
    return @(As-Array $raw)
  } catch { return @() }
}
function Cdp-Eval($wsUrl, $expr) {
  $cts = [Threading.CancellationTokenSource]::new()
  $cts.CancelAfter(6000)
  $ws = $null
  try {
    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    [void]$ws.ConnectAsync([Uri]$wsUrl, $cts.Token).GetAwaiter().GetResult()
    $payload = ([ordered]@{
      id = 1; method = 'Runtime.evaluate'
      params = [ordered]@{ expression = $expr; returnByValue = $true; awaitPromise = $false }
    }) | ConvertTo-Json -Depth 8 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    [void]$ws.SendAsync([System.ArraySegment[byte]]::new($bytes),
      [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).GetAwaiter().GetResult()
    $buf = New-Object byte[] 262144
    for ($n = 0; $n -lt 25; $n++) {
      $sb = New-Object System.Text.StringBuilder
      do {
        $r = $ws.ReceiveAsync([System.ArraySegment[byte]]::new($buf), $cts.Token).GetAwaiter().GetResult()
        if ($r.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { return '' }
        [void]$sb.Append([Text.Encoding]::UTF8.GetString($buf, 0, $r.Count))
      } while (-not $r.EndOfMessage)
      $m = $sb.ToString() | ConvertFrom-Json
      if ($m.id -eq 1) {
        if ($m.result -and $m.result.result -and $null -ne $m.result.result.value) {
          return [string]$m.result.result.value
        }
        return ''
      }
    }
    return ''
  } catch { return '' }
  finally {
    if ($ws) { try { $ws.Dispose() } catch {} }
    try { $cts.Dispose() } catch {}
  }
}
function Scan-Live($port) {
  $targets = @(Cdp-Targets $port)
  $pages = @($targets | Where-Object { $_.type -eq 'page' -and $_.webSocketDebuggerUrl })
  $keyCount = 0
  foreach ($t in $pages) {
    $txt = Cdp-Eval $t.webSocketDebuggerUrl $JS_READ
    if (-not $txt) { continue }
    foreach ($ln in ($txt -split "`n")) {
      $eq = $ln.IndexOf('=')
      if ($eq -gt 0) { $k = $ln.Substring(0, $eq); if ($k) { $script:diagKeys[$k] = $true; $keyCount++ } }
    }
    foreach ($rt in (Extract-Tokens $txt)) { Push-Token $rt $txt 'CDP 实时存储' }
  }
  return @{ pages = $pages.Count; keys = $keyCount }
}

# ---- 文件兜底：浏览器关闭后 leveldb 已完整落盘 ----
function Read-Shared($path) {
  try {
    $fs = [IO.FileStream]::new($path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
      ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
      $ms = New-Object IO.MemoryStream
      $fs.CopyTo($ms)
      return [Text.Encoding]::GetEncoding(28591).GetString($ms.ToArray())
    } finally { $fs.Dispose() }
  } catch { return '' }
}
function Scan-Files($profileDir) {
  $files = @(Get-ChildItem -LiteralPath $profileDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Extension -eq '.log' -or $_.Extension -eq '.ldb') -and
                   $_.FullName -match 'Local Storage|IndexedDB|Session Storage' })
  $stat = @{ files = $files.Count; bytes = 0; hits = 0 }
  foreach ($f in $files) {
    $txt = Read-Shared $f.FullName
    if (-not $txt) { continue }
    $stat.bytes += $txt.Length
    # leveldb 是二进制混合文本，key 不一定以 '=' 分隔；按常见形态抓可读键名用于诊断
    foreach ($m in [regex]::Matches($txt,
      '(?:pp_web_|pp_)[A-Za-z0-9_\-]{2,40}|\b(?:refresh_token|access_token|deviceid|global_config|userInfo|sub|email)\b')) {
      $script:diagKeys[$m.Value] = $true
    }
    $tks = @(Extract-Tokens $txt)
    $stat.hits += $tks.Count
    foreach ($rt in $tks) { Push-Token $rt $txt '关闭后文件扫描' }
  }
  return $stat
}

Say '' ''
Say '  ==================================================' 'DarkCyan'
Say '   PikPak 干净浏览器登录采集器 v2' 'Cyan'
Say "   → 推送到：$ConsoleUrl" 'DarkGray'
Say '  ==================================================' 'DarkCyan'
Say '' ''
Log '=== collector v2 start ==='

$roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) | Where-Object { $_ }
$paths = @()
foreach ($r in $roots) {
  $paths += (Join-Path $r 'Microsoft\Edge\Application\msedge.exe')
  $paths += (Join-Path $r 'Google\Chrome\Application\chrome.exe')
}
$browser = $null
foreach ($p in $paths) { if (Test-Path -LiteralPath $p) { $browser = $p; break } }
if (-not $browser) {
  Say '  × 没找到 Edge 或 Chrome，请先安装其中一个。' 'Red'
  Read-Host '  按回车退出'
  Done 1
}

$profileDir = Join-Path $env:TEMP ('pikpak-clean-' + [guid]::NewGuid().ToString('N').Substring(0, 10))
New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
$port = Get-Random -Minimum 21000 -Maximum 45000
Say "  临时浏览器目录：$profileDir" 'DarkGray'
Say '  这是全新的独立环境，不会读取你现有浏览器的任何数据。' 'DarkGray'
Say '' ''
Log ('profile=' + $profileDir + ' port=' + $port)

$proc = $null
$cdpOK = $false
$rounds = 0
$lastBeat = Get-Date

try {
  # 必须是 /drive/login —— 根路径 / 会跳到介绍页，登录态根本不写在那里
  $argLine = '--user-data-dir="' + $profileDir + '" --no-first-run --no-default-browser-check ' +
    '--disable-extensions --remote-debugging-port=' + $port + ' https://mypikpak.com/drive/login'
  $proc = Start-Process -FilePath $browser -ArgumentList $argLine -PassThru
  Say '  已打开一个全新的浏览器窗口。' 'Green'
  Say '  请在里面手动登录 PikPak（验证码 / 二次验证都能正常过）。' 'Cyan'
  Say '  登录成功后本窗口会自动采集凭证，无需等待。' 'Cyan'
  Say '  可以继续退出并登录下一个账号；按 Q 结束并清理。' 'Cyan'
  Say '  提示：按 Q 后工具会先关闭浏览器再补扫一次，请勿直接关窗口。' 'DarkGray'
  Say '' ''
  Log 'browser launched'

  Start-Sleep -Seconds 2
  $probe = @(Cdp-Targets $port)
  if ($probe.Count -gt 0) {
    Say '  · 已连上浏览器实时存储通道，登录完成后会立即采集。' 'Green'
    $cdpOK = $true
  } else {
    Say '  ! 浏览器实时通道暂不可用（端口占用或企业策略禁用）。' 'Yellow'
    Say '    别担心：登录完成后按 Q，工具会关窗并做完整文件采集。' 'DarkGray'
  }
  Log ('cdp probe targets=' + $probe.Count)

  $deadline = (Get-Date).AddMinutes(10)
  while ((Get-Date) -lt $deadline) {
    $rounds++
    try {
      if ([Console]::KeyAvailable) {
        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::Q) { break }
      }
    } catch {}

    $s = Scan-Live $port
    if ($s.pages -gt 0) { $cdpOK = $true }
    if (((Get-Date) - $lastBeat).TotalSeconds -ge 20 -and $script:count -eq 0) {
      $lastBeat = Get-Date
      Say ('  · 已扫描 ' + $rounds + ' 轮，页面 ' + $s.pages + ' 个，存储键 ' +
        $script:diagKeys.Count + ' 个，尚未发现凭证（登录完成后会自动捕获）') 'DarkGray'
    }
    Start-Sleep -Seconds 2
  }
}
catch {
  Say ('  ! 采集过程异常：' + $_.Exception.Message) 'Yellow'
  Log ('loop error: ' + $_.Exception.Message)
}
finally {
  # 关浏览器之前再读一次，抓住「刚登录就按 Q」的情况
  try { $s = Scan-Live $port; if ($s.pages -gt 0) { $cdpOK = $true } } catch {}
  Say '' ''
  Say '  正在关闭临时浏览器，然后做最后一次完整采集…' 'DarkGray'
  if ($proc) {
    try { & taskkill /PID $proc.Id /T /F 2>$null | Out-Null } catch {}
  }
  Start-Sleep -Seconds 3
  $fs = Scan-Files $profileDir
  Log ('final: cdp=' + $cdpOK + ' rounds=' + $rounds + ' files=' + $fs.files +
    ' bytes=' + $fs.bytes + ' hits=' + $fs.hits + ' written=' + $script:count)
}

Say '' ''
if ($script:count -eq 0) {
  Log ('diag keys: ' + ((@($script:diagKeys.Keys) | Select-Object -First 40) -join ','))
  Say '  × 本次没有采集到任何可用凭证。' 'Red'
  Say ('  诊断：CDP ' + $(if ($cdpOK) { '已连通' } else { '未连通（可能被浏览器策略禁用）' }) +
    '，扫描 ' + $rounds + ' 轮，文件 ' + $fs.files + ' 个 / ' + $fs.bytes + ' 字节。') 'DarkGray'
  if ($script:diagKeys.Count -gt 0) {
    Say ('  读到的存储键：' + ((@($script:diagKeys.Keys) | Select-Object -First 25) -join ', ')) 'DarkGray'
  } else {
    Say '  没有读到任何页面存储键 —— 请确认是在工具打开的窗口里完成登录。' 'DarkGray'
  }
  Say '' ''
  Say '  临时目录已保留，便于排查（里面含登录态，确认后请手动删除）：' 'Yellow'
  Say "    $profileDir" 'Yellow'
  Say ("  诊断日志：$logPath") 'DarkGray'
  Say '  也可以改用弹窗里的「粘贴导入」：在浏览器 F12 → Application →' 'DarkGray'
  Say '  Local Storage，复制含 refresh_token 的那条值粘贴进来。' 'DarkGray'
} else {
  Say ("  ✓ 完成：共写入 $script:count 个账号。") 'Cyan'
  Say '  正在清理临时浏览器数据…' 'DarkGray'
  try { Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue } catch {}
  if (Test-Path -LiteralPath $profileDir) {
    Say '  ! 临时目录未能完全删除，请手动清理：' 'Yellow'
    Say "    $profileDir" 'Yellow'
  } else {
    Say '  ✓ 临时数据已清除，凭证只保存在 NAS 上。' 'Green'
  }
}
Say '  回到控制台「账号」页点一下「刷新」即可。' 'DarkGray'
Say ("  诊断日志：$logPath") 'DarkGray'
Read-Host '  按回车退出'
