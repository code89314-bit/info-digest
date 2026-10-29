# Run an AI digest on the home LLM server (Windows Task Scheduler: tasks "info-digest AI" / "info-digest AI global").
#   -Section ai_digest (default): Japanese articles -> #ai-llm      -Section ai_global: overseas topics
#   -Section news: morning news (NHK RSS, task "info-digest news" 06:50)
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#
# Secrets and the local state dir come from .env in the repository root (not committed).
# If the LLM server is not answering yet (PC just booted, or the GPU is lent to ComfyUI), wait for it
# up to -WaitMinutes, then run anyway: ai_digest.py falls back to keyword matching by itself.
#   -Module onco_news -Section onco: weekly oncology news (collect daily, themed episode per weekday)
#   -Module note_weekly -Section note_weekly: weekly note draft (task "info-digest note", Sat 07:00)
#   -Module note_hoken -Section note_hoken: Wednesday note draft (task "info-digest note hoken", Wed 07:00)
param([string]$Section = 'ai_digest', [string]$Module = 'ai_digest', [int]$WaitMinutes = 180)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root '.local\logs') | Out-Null
$log = Join-Path $root ('.local\logs\' + $Section + '-' + (Get-Date -Format 'yyyyMMdd') + '.log')
function Log($m) { Add-Content $log ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

# base_url and the key's env name from config.yaml (the section's llm, or ai_digest's), so this script needs no settings of its own
$py = "import sys; sys.path.insert(0,'src'); import ai_digest; l=ai_digest.llm_settings(ai_digest.section_config('$Section')); "
$base = (& $python -c ($py + "print(l.get('base_url','').rstrip('/') if l['provider']=='openai_compat' else '')")).Trim()
$keyEnv = (& $python -c ($py + "print(l.get('api_key_env') or '')")).Trim()
$key = ''
if ($keyEnv) {
    # -Encoding UTF8: PowerShell 5.1 reads it as ANSI otherwise, and a Japanese comment swallows the next line
    $line = Get-Content (Join-Path $root '.env') -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_ -match "^\s*$keyEnv\s*=" } | Select-Object -First 1
    if ($line) { $key = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
}

Log "start (llm: $base)"
# Scheduled jobs come first: while this runs, the Discord bot must not lend the GPU to Luna / ComfyUI
# (src/gpu_mode.py reads this lock). If the bot holds the GPU now (luna-mode.flag), take it back to Strata.
# One lock per section: jobs can overlap (note at 07:00 while onco from 06:30 still runs), and the first one
# to finish must not remove the lock of the other.
$lock = Join-Path $root ('.local\state\llm_busy.' + $Section + '.lock')
New-Item -ItemType Directory -Force (Split-Path $lock) | Out-Null
Set-Content $lock "$Section $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
# Same when ComfyUI was switched on by hand (comfy-mode.flag): a forgotten comfy-mode left the morning digests
# waiting for Strata until -WaitMinutes ran out (2026-10-29). An image in progress is stopped.
if ((Test-Path 'D:\Strata\luna-mode.flag') -or (Test-Path 'D:\Strata\comfy-mode.flag')) {
    Log 'GPU is lent to Luna / ComfyUI: switching back to Strata'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'D:\Strata\scripts\llm-mode.ps1' | Out-Null
}
# The note drafts (07:00) run after the morning digests: note_weekly reads the week's onco (Saturday's episode),
# and Strata answers one request at a time anyway. Wait while another section's lock (younger than 4 h) is there.
if ($Section -like 'note*') {
    $until = (Get-Date).AddMinutes(120)
    while ((Get-Date) -lt $until) {
        $others = Get-ChildItem (Split-Path $lock) -Filter 'llm_busy*.lock' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -ne $lock -and $_.LastWriteTime -gt (Get-Date).AddHours(-4) }
        if (-not $others) { break }
        Log "waiting for $(($others | ForEach-Object { $_.Name }) -join ', ')"
        Start-Sleep -Seconds 60
    }
}
if ($base) {
    $headers = @{}
    if ($key) { $headers['Authorization'] = "Bearer $key" }
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while ($true) {
        try { Invoke-RestMethod -Uri "$base/models" -Headers $headers -TimeoutSec 15 | Out-Null; Log 'llm server is up'; break }
        catch {
            if ((Get-Date) -ge $deadline) { Log "llm server still down after $WaitMinutes min: running anyway (keyword fallback)"; break }
            Start-Sleep -Seconds ([math]::Max(5, [math]::Min(300, ($deadline - (Get-Date)).TotalSeconds)))
        }
    }
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'    # write progress to the log as it happens (not all at the end)
[Console]::OutputEncoding = [Text.Encoding]::UTF8    # read python's UTF-8 output as UTF-8 (not the ANSI code page)
& $python "src\$Module.py" $Section 2>&1 | ForEach-Object { "$_" } | Add-Content $log -Encoding UTF8
$code = $LASTEXITCODE
Remove-Item $lock -ErrorAction SilentlyContinue
Log "exit code $code"
exit $code
