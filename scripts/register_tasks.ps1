# Register every scheduled task this PC runs (Windows Task Scheduler), for a rebuild from scratch.
# Run once in a normal (not administrator) PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\info-digest\scripts\register_tasks.ps1
# A task that already exists is replaced with these settings. Paths assume the layout in README.md
# (D:\info-digest, D:\Strata, D:\searxng). -Only 'name' registers one task.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
param([string]$Only = '')

$digest = 'D:\info-digest\scripts\run_ai_digest.ps1'

function Ps($file, $more) {
    New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$file`" $more").Trim()
}
function Add($name, $action, $trigger, $limit) {
    if ($Only -and $Only -ne $name) { return }
    # limit 0 = no time limit (the resident programs: Strata keepalive, SearXNG, Discord bot)
    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit $limit -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $p = @{ TaskName = $name; Action = $action; Settings = $s; Force = $true }
    if ($trigger) { $p.Trigger = $trigger }
    Register-ScheduledTask @p | Out-Null
    Write-Host "registered: $name"
}
$h3 = New-TimeSpan -Hours 3
$h4 = New-TimeSpan -Hours 4
$none = New-TimeSpan -Seconds 0
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ---- resident at logon ----
Add 'Strata AutoStart'  (Ps 'D:\Strata\scripts\strata-autostart.ps1' '') $logon $none
Add 'SearXNG'           (Ps 'D:\searxng\local\run.ps1' '') $logon $none
Add 'info-digest qa bot' (Ps 'D:\info-digest\scripts\run_qa_bot.ps1' '') $logon $none

# ---- run by hand or remotely (schtasks /run /tn StrataComfyMode) ----
Add 'StrataComfyMode' (Ps 'D:\Strata\scripts\comfy-mode.ps1' '') $null (New-TimeSpan -Minutes 30)
Add 'StrataLLMMode'   (Ps 'D:\Strata\scripts\llm-mode.ps1' '') $null (New-TimeSpan -Minutes 30)

# ---- morning digests (Strata answers one request at a time; they queue up) ----
Add 'info-digest AI'        (Ps $digest '') (New-ScheduledTaskTrigger -Daily -At '05:30') $h4
Add 'info-digest AI global' (Ps $digest '-Section ai_global') (New-ScheduledTaskTrigger -Daily -At '05:35') $h4
Add 'info-digest learn'     (Ps $digest '-Module learn -Section learn') (New-ScheduledTaskTrigger -Daily -At '06:00') $h4
Add 'info-digest onco'      (Ps $digest '-Module onco_news -Section onco') (New-ScheduledTaskTrigger -Daily -At '06:30') $h4
Add 'info-digest news'      (Ps $digest '-Section news') (New-ScheduledTaskTrigger -Daily -At '06:50') $h4

# ---- note drafts ----
Add 'info-digest note'       (Ps $digest '-Module note_weekly -Section note_weekly') (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '07:00') $h3
Add 'info-digest note hoken' (Ps $digest '-Module note_hoken -Section note_hoken') (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At '07:00') $h3
