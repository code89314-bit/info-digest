# Switch this PC to Luna's model: stop Strata (and ComfyUI if it is up), serve Gemma 4 26B (uncensored, vision)
# with llama.cpp on http://127.0.0.1:8090. Back to Strata with llm-mode.ps1.
# Called by the Discord bot (info-digest src/gpu_mode.py) when the persona Luna is chosen; also by hand.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#
# Flags: comfy-mode.flag stops the Strata autostart keepalive (shared with comfy-mode.ps1);
#        luna-mode.flag marks that the bot owns the GPU, so the scheduled info-digest jobs may take it back
#        (run_ai_digest.ps1 runs llm-mode.ps1 when this flag exists).

Start-Transcript -Path 'D:\Strata\mode-switch.log' -Append | Out-Null

$strata = 'D:\Strata\terminal\current\bin\strata.cmd'
$flag   = 'D:\Strata\comfy-mode.flag'
$luna   = 'D:\Strata\luna-mode.flag'
$server = 'D:\llama.cpp\bin\llama-server.exe'
# 2026-10-28: Q6_K (22.6 GB, 8 expert layers on the CPU) gave ~20 tok/s; i1-IQ4_XS (13.9 GB) almost fits the
# 16 GB card: 2 layers on the CPU measured 122 tok/s, 1 layer 102 tok/s (VRAM at the limit), 0 does not start.
# 3 leaves room for the desktop's own VRAM use. Q6_K is still in the folder if quality is ever worth the speed.
$model  = 'D:\models\gemma4-26b-heretic\gemma-4-26B-A4B-it-uncensored-heretic.i1-IQ4_XS.gguf'
$mmproj = 'D:\models\gemma4-26b-heretic\gemma-4-26B-A4B-it-uncensored-heretic.mmproj-f16.gguf'
$log    = 'D:\llama.cpp\luna-server.log'
# --n-cpu-moe: how many layers keep their experts in RAM (the rest on the 16 GB card).
$cpuMoe = 3
$ctx    = 32768
# thinking stays on and is NOT capped: --reasoning-budget cut a long thought mid-sentence and the answer after it
# fell into an 8000-token loop of broken text (2026-10-28). At ~100 tok/s a long thought is about a minute.

Set-Content $flag (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Set-Content $luna (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

if (Get-Process 'llama-server' -ErrorAction SilentlyContinue) {
    try { Invoke-RestMethod 'http://127.0.0.1:8090/health' -TimeoutSec 5 | Out-Null; Write-Host 'Luna server already up'; Stop-Transcript | Out-Null; exit 0 } catch {}
    Get-Process 'llama-server' | Stop-Process -Force
}

Write-Host 'Stopping Strata ...' -ForegroundColor Cyan
& $strata launch --stop --preset 'Qwen36 32K'

if (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue) {
    Write-Host 'Stopping ComfyUI / Docker ...' -ForegroundColor Cyan
    $running = docker ps -q
    if ($running) { docker stop $running | Out-Null }
    Get-Process 'Docker Desktop', 'com.docker.backend', 'com.docker.build' -ErrorAction SilentlyContinue | Stop-Process -Force
    wsl --shutdown
}
Start-Sleep -Seconds 5
# Strata gives its RAM back a while after --stop returns; loading Gemma before that made llama-server die
# silently ~1 min in (2026-10-28). Wait until 20 GB is free, at most 2 min.
for ($i = 0; $i -lt 24; $i++) {
    $freeGB = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
    if ($freeGB -ge 20) { break }
    Start-Sleep -Seconds 5
}
Write-Host ('Free RAM: {0:N1} GB' -f $freeGB)

Write-Host 'Starting Gemma 4 26B (llama-server :8090) ...' -ForegroundColor Cyan
$sargs = @('-m', $model, '--mmproj', $mmproj, '--host', '127.0.0.1', '--port', '8090', '-c', $ctx,
          '-ngl', '99', '--n-cpu-moe', $cpuMoe, '-fa', 'on', '--jinja', '--no-webui', '--alias', 'gemma-4-26b-heretic',
          '--load-mode', 'none')   # read the CPU-side experts into RAM once (mmap re-read them from disk under memory pressure)
Start-Process -FilePath $server -ArgumentList $sargs -WindowStyle Hidden -RedirectStandardError $log -RedirectStandardOutput "$log.out"

for ($i = 0; $i -lt 150; $i++) {   # up to 12.5 min: margin for a cold load (took ~6 min from the HDD; the model moved to the D: SSD 2026-10-28)
    Start-Sleep -Seconds 5
    try { Invoke-RestMethod 'http://127.0.0.1:8090/health' -TimeoutSec 5 | Out-Null; Write-Host 'Luna server ready' -ForegroundColor Green; Stop-Transcript | Out-Null; exit 0 } catch {}
    if (-not (Get-Process 'llama-server' -ErrorAction SilentlyContinue)) { break }
}
Write-Error "llama-server did not come up (see $log)"
Stop-Transcript | Out-Null
exit 1
