# Weekly series (Windows Task Scheduler: task "info-digest series", daily 14:00; it was 02:00 until 2026-11-22).
# Runs the Claude Code skill "/series-script auto" without a person at the keyboard:
#   Mon-Sat: the next episode of the current week. Sun: prep of the next week. Nothing to do: it says so.
# The skill writes .local\series\last_run.json; src\series_notify.py posts the result to Discord.
# Output stays in .local\series\ (not committed). Nothing is published.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
param([string]$Prompt = '/series-script auto')

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root '.local\logs') | Out-Null
$log = Join-Path $root ('.local\logs\series-' + (Get-Date -Format 'yyyyMMdd') + '.log')
function Log($m) { Add-Content $log ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8 }

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
if (-not (Test-Path $claude)) { $claude = 'claude' }
$env:Path += ";$env:APPDATA\Python\Python310\Scripts"   # notebooklm CLI
$env:PYTHONIOENCODING = 'utf-8'

$started = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
Log "start: $Prompt"
# No one answers permission prompts at night, so the tools the skill needs are allowed up front.
# The skill itself says what it may touch (only .local\series\, no commit, no publish).
$tools = 'Skill Read Write Edit Glob Grep Bash PowerShell'
& $claude -p $Prompt --permission-mode acceptEdits --allowedTools $tools.Split(' ') --output-format text 2>&1 |
    ForEach-Object { Log $_ }
Log "claude exit: $LASTEXITCODE"

& $python (Join-Path $root 'src\series_notify.py') --started $started --log $log 2>&1 | ForEach-Object { Log $_ }
Log 'done'
