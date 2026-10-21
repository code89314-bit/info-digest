# Run an AI digest on the home LLM server (Windows Task Scheduler: tasks "info-digest AI" / "info-digest AI global").
#   -Section ai_digest (default): Japanese articles -> #ai-llm      -Section ai_global: overseas topics
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#
# Secrets and the local state dir come from .env in the repository root (not committed).
# If the LLM server is not answering yet (PC just booted, or the GPU is lent to ComfyUI), wait for it
# up to -WaitMinutes, then run anyway: ai_digest.py falls back to keyword matching by itself.
#   -Module onco_news -Section onco: weekly oncology news (collect daily, themed episode per weekday)
#   -Module note_weekly -Section note_weekly: weekly note draft (task "info-digest note", Sat 10:30)
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
Log "exit code $LASTEXITCODE"
exit $LASTEXITCODE
