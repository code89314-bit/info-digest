# SearXNG (home search, http://127.0.0.1:8888, not exposed). Started at logon by the task "SearXNG".
# Restarts after 60 s if it exits. Settings: D:\searxng\local\settings.yml, logs: D:\searxng\local\searxng.log
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI and garbles the lines.
$env:SEARXNG_SETTINGS_PATH = 'D:\searxng\local\settings.yml'
$env:PYTHONPATH = 'D:\searxng;D:\searxng\local\shim'   # shim\pwd.py stands in for the Unix-only pwd module
$env:PYTHONUTF8 = '1'
Set-Location 'D:\searxng'
while ($true) {
    cmd /c "D:\searxng\.venv\Scripts\python.exe -m searx.webapp >> D:\searxng\local\searxng.log 2>&1"
    Start-Sleep -Seconds 60
}
