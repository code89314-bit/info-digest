# Switch this PC back to the LLM: stop ComfyUI, quit Docker Desktop, shut WSL down (frees its RAM), serve Strata.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
# Also run remotely: ssh code89314@omamori schtasks /run /tn StrataComfyMode (or StrataLLMMode); output -> D:\Strata\mode-switch.log

Start-Transcript -Path 'D:\Strata\mode-switch.log' -Append | Out-Null

$strata = 'D:\Strata\terminal\current\bin\strata.cmd'
$flag   = 'D:\Strata\comfy-mode.flag'

if (Get-Process 'llama-server' -ErrorAction SilentlyContinue) {   # Luna's model (luna-mode.ps1)
    Write-Host 'Stopping the Luna server (llama-server) ...' -ForegroundColor Cyan
    Get-Process 'llama-server' | Stop-Process -Force
}
Remove-Item 'D:\Strata\luna-mode.flag' -ErrorAction SilentlyContinue

if (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue) {
    Write-Host 'Stopping containers ...' -ForegroundColor Cyan
    $running = docker ps -q
    if ($running) { docker stop $running | Out-Null }   # stopped by hand: they stay stopped next time Docker starts
    Write-Host 'Quitting Docker Desktop ...' -ForegroundColor Cyan
    Get-Process 'Docker Desktop', 'com.docker.backend', 'com.docker.build' -ErrorAction SilentlyContinue | Stop-Process -Force
}
wsl --shutdown
Start-Sleep -Seconds 10    # let the driver hand ComfyUI's VRAM back before the engine sizes its cache

# The flag stays until this launch is done: removing it first let the autostart keepalive start a second
# launch at the same time, and the two stopped each other ("runtime control is unavailable").
Write-Host 'Starting Strata ...' -ForegroundColor Cyan
& $strata launch --preset 'Qwen36 32K' --no-tui --yes
Remove-Item $flag -ErrorAction SilentlyContinue    # from here the keepalive watches it again
Stop-Transcript | Out-Null
