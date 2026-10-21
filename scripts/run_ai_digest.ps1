# Run the AI digest on the home LLM server (Windows Task Scheduler: task "info-digest AI").
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#
# Secrets and the local state dir come from .env in the repository root (not committed).
# If the LLM server is not answering yet (PC just booted, or the GPU is lent to ComfyUI), wait for it
# up to -WaitMinutes, then run anyway: ai_digest.py falls back to keyword matching by itself.
param([int]$WaitMinutes = 180)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root '.local\logs') | Out-Null
$log = Join-Path $root ('.local\logs\ai_digest-' + (Get-Date -Format 'yyyyMMdd') + '.log')
function Log($m) { Add-Content $log ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

# base_url and the key's env name from config.yaml / .env, so this script needs no settings of its own
$base = (& $python -c "import sys; sys.path.insert(0,'src'); import common; l=(common.load_config().get('ai_digest') or {}).get('llm') or {}; print((l.get('base_url') or '').rstrip('/') if l.get('provider','openai_compat')=='openai_compat' else '')").Trim()
$keyEnv = (& $python -c "import sys; sys.path.insert(0,'src'); import common; print(((common.load_config().get('ai_digest') or {}).get('llm') or {}).get('api_key_env') or '')").Trim()
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
& $python src\ai_digest.py 2>&1 | ForEach-Object { "$_" } | Add-Content $log -Encoding UTF8
Log "exit code $LASTEXITCODE"
exit $LASTEXITCODE
