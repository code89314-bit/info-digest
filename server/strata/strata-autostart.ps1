# Strata autostart + keepalive. Run at logon by the scheduled task "Strata AutoStart".
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#
# - waits for Tailscale, then serves preset "Qwen36 32K" (Qwen3.6-35B-A3B UD-Q4_K_XL, access tailscale, API key on;
#   switched from "Q2_0 32K" on 2026-10-30: about 8x faster since the whole model fits in RAM;
#   Strata's refusal-direction vector is Flash-Next only, but oncology questions were not refused in tests)
# - checks every 60 s and starts it again if it is gone
# - does nothing while D:\Strata\comfy-mode.flag exists (ComfyUI owns the GPU; see comfy-mode.ps1)

$strata = 'D:\Strata\terminal\current\bin\strata.cmd'
$preset = 'Qwen36 32K'
$flag   = 'D:\Strata\comfy-mode.flag'
$log    = 'D:\Strata\autostart.log'

function Log($m) { Add-Content $log ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }

function Test-Ready {
    $j = & $strata launch --status --preset $preset --output json 2>$null | Out-String
    return ($j -match '"state":\s*"ready"')
}

Log 'autostart begin'

# Tailscale must be up so the tailnet address can be served (up to 3 minutes)
for ($i = 0; $i -lt 36; $i++) {
    $ip = & tailscale ip -4 2>$null
    if ($LASTEXITCODE -eq 0 -and $ip) { break }
    Start-Sleep -Seconds 5
}
Log "tailscale ip: $ip"

while ($true) {
    if (-not (Test-Path $flag) -and -not (Test-Ready)) {
        Log 'starting strata'
        & $strata launch --preset $preset --no-tui --yes 2>&1 | ForEach-Object { "$_" } | Add-Content $log
        Log ("launch exit code {0}, ready={1}" -f $LASTEXITCODE, (Test-Ready))
    }
    Start-Sleep -Seconds 60
}
