# 협업 세션들의 상태(peer-*.json)와 대화 기록 끝부분을 읽어 비서용 요약을 출력한다.
param(
  [double]$Hours = 24,      # 이 시간 안에 소식이 있었던 세션만
  [int]$TailMB = 3,         # 대화 기록 끝에서 읽을 크기
  [int]$Turns = 3,          # 세션마다 보여줄 최근 사용자 요청 수
  [int]$MaxChars = 600      # 마지막 답변 미리보기 길이
)
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$claude = Join-Path $env:USERPROFILE '.claude'
$collab = Join-Path $claude 'session-collab'
$projects = Join-Path $claude 'projects'
$self = $env:CLAUDE_SESSION_ID
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

function Read-Tail($path, $bytes) {
  $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
  try {
    $start = [Math]::Max(0, $fs.Length - $bytes)
    $fs.Seek($start, 'Begin') | Out-Null
    $buf = New-Object byte[] ($fs.Length - $start)
    $n = $fs.Read($buf, 0, $buf.Length)
    $text = [Text.Encoding]::UTF8.GetString($buf, 0, $n)
    $lines = $text -split "`n"
    if ($start -gt 0) { $lines = $lines | Select-Object -Skip 1 }
    return $lines
  } finally { $fs.Close() }
}

function Get-Text($content) {
  if ($content -is [string]) { return $content }
  ($content | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n"
}

function Short($s, $max) {
  $s = ($s -replace '\s+', ' ').Trim()
  if ($s.Length -gt $max) { $s.Substring(0, $max) + '…' } else { $s }
}

$peers = Get-ChildItem $collab -Filter 'peer-*.json' | ForEach-Object {
  try { Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
} | Where-Object { $_ -and ($now - $_.updatedAt) -le $Hours * 3600000 } |
  Sort-Object updatedAt -Descending

foreach ($p in $peers) {
  $ago = [Math]::Round(($now - $p.updatedAt) / 60000)
  $mark = if ($p.sessionId -eq $self) { ' (이 세션)' } else { '' }
  "■ `"$($p.label)`"$mark — $($p.phase) — ${ago}분 전 — $($p.sessionId.Substring(0,8))"
  "  폴더: $($p.root)"
  if ($p.goal) { "  목표: $($p.goal)" }
  foreach ($t in $p.tasks) {
    $icon = @{ completed = '✓'; in_progress = '▶'; pending = '·' }[$t.status]
    $pct = if ($t.status -eq 'in_progress' -and $t.percent) { " $($t.percent)%" } else { '' }
    "  $icon $($t.title)$pct"
  }
  if ($p.activity) { "  지금: $($p.activity)" }

  $file = Get-ChildItem $projects -Recurse -Filter "$($p.sessionId).jsonl" | Select-Object -First 1
  if (-not $file) { ''; continue }
  $requests = New-Object System.Collections.Generic.List[string]
  $lastReply = $null
  foreach ($line in (Read-Tail $file.FullName ($TailMB * 1MB))) {
    if ($line.Length -lt 20) { continue }
    try { $e = $line | ConvertFrom-Json } catch { continue }
    if ($e.isSidechain -or $e.isMeta) { continue }
    if ($e.type -eq 'user') {
      $t = Get-Text $e.message.content
      if ($t -and $t -notmatch '^\s*(<(local-command|command-|system-reminder|task-notification|artifact-content)|This session is being continued|\[Request interrupted)') {
        $requests.Add((Short $t 200))
      }
    } elseif ($e.type -eq 'assistant') {
      $t = Get-Text $e.message.content
      if ($t -and $t.Trim()) { $lastReply = $t }
    }
  }
  if ($requests.Count) {
    "  최근 요청:"
    $requests | Select-Object -Last $Turns | ForEach-Object { "   - $_" }
  }
  if ($lastReply) { "  마지막 답변: $(Short $lastReply $MaxChars)" }
  ''
}
if (-not $peers) { "최근 ${Hours}시간 안에 소식이 있는 세션이 없습니다." }
