# Discord bot for the question deep-dive channel (task "info-digest qa bot", at logon).
# Secrets (DISCORD_BOT_TOKEN, DISCORD_QA_CHANNEL_ID, the Strata key) come from .env in the repository root.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root '.local\logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
Set-Location $root
while ($true) {
    # the bot writes its own log (.local\logs\qa_bot-YYYYMMDD.log, UTF-8)
    & $python src\qa_bot.py --log
    Start-Sleep -Seconds 60
}
