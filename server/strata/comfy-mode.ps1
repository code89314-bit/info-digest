# Switch this PC to ComfyUI: stop Strata, start Docker Desktop, start only the comfyui container.
# Back to the LLM with llm-mode.ps1.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
# Also run remotely: ssh code89314@omamori schtasks /run /tn StrataComfyMode (or StrataLLMMode); output -> D:\Strata\mode-switch.log

Start-Transcript -Path 'D:\Strata\mode-switch.log' -Append | Out-Null

$strata = 'D:\Strata\terminal\current\bin\strata.cmd'
$flag   = 'D:\Strata\comfy-mode.flag'

# 1. the flag first, so the autostart keepalive does not start Strata again
Set-Content $flag (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

Write-Host 'Stopping Strata ...' -ForegroundColor Cyan
& $strata launch --stop --preset 'Qwen36 32K'
if (Get-Process 'llama-server' -ErrorAction SilentlyContinue) {   # Luna's model (luna-mode.ps1); luna-mode.flag stays if set
    Write-Host 'Stopping the Luna server (llama-server) ...' -ForegroundColor Cyan
    Get-Process 'llama-server' | Stop-Process -Force
}

Write-Host 'Starting Docker Desktop ...' -ForegroundColor Cyan
if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
}
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $ok) { Write-Error 'Docker Desktop did not come up in 5 minutes'; exit 1 }

# Docker brings back containers that were running when it last stopped (restart: unless-stopped).
# Only comfyui (and its light web UIs, which use no GPU) is wanted here.
$keep = @('comfyui', 'moss-web')
$others = docker ps --format '{{.Names}}' | Where-Object { $keep -notcontains $_ }
if ($others) { Write-Host "Stopping other containers: $($others -join ', ')" -ForegroundColor Yellow; docker stop $others | Out-Null }

docker start comfyui | Out-Null
Write-Host 'ComfyUI: http://127.0.0.1:8188' -ForegroundColor Green
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
Stop-Transcript | Out-Null
