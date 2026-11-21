# Register every scheduled task this PC runs (Windows Task Scheduler), for a rebuild from scratch.
# Run once in a normal (not administrator) PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\info-digest\scripts\register_tasks.ps1
# A task that already exists is replaced with these settings. Paths assume the layout in README.md
# (D:\info-digest, D:\Strata, D:\searxng). -Only 'name' registers one task.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads .ps1 as ANSI).
param([string]$Only = '')

$digest = 'D:\info-digest\scripts\run_ai_digest.ps1'

# Not "Ps": that is a built-in alias of Get-Process, and an alias wins over a function of the same name.
function New-PsAction($file, $more) {
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
Add 'Strata AutoStart'  (New-PsAction 'D:\Strata\scripts\strata-autostart.ps1' '') $logon $none
Add 'SearXNG'           (New-PsAction 'D:\searxng\local\run.ps1' '') $logon $none
Add 'info-digest qa bot' (New-PsAction 'D:\info-digest\scripts\run_qa_bot.ps1' '') $logon $none

# ---- run by hand or remotely (schtasks /run /tn StrataComfyMode) ----
Add 'StrataComfyMode' (New-PsAction 'D:\Strata\scripts\comfy-mode.ps1' '') $null (New-TimeSpan -Minutes 30)
Add 'StrataLLMMode'   (New-PsAction 'D:\Strata\scripts\llm-mode.ps1' '') $null (New-TimeSpan -Minutes 30)

# ---- morning digests (Strata answers one request at a time; they queue up) ----
Add 'info-digest AI'        (New-PsAction $digest '') (New-ScheduledTaskTrigger -Daily -At '05:30') $h4
Add 'info-digest AI global' (New-PsAction $digest '-Section ai_global') (New-ScheduledTaskTrigger -Daily -At '05:35') $h4
# learn starts at 04:00 so its NotebookLM audio is done early; it waits and posts to Discord at learn.post_at (05:50)
Add 'info-digest learn'     (New-PsAction $digest '-Module learn -Section learn') (New-ScheduledTaskTrigger -Daily -At '04:00') $h4
Add 'info-digest onco'      (New-PsAction $digest '-Module onco_news -Section onco') (New-ScheduledTaskTrigger -Daily -At '06:00') $h4
Add 'info-digest news'      (New-PsAction $digest '-Section news') (New-ScheduledTaskTrigger -Daily -At '06:50') $h4

# ---- weekly series: Claude Code skill /series-script auto (Mon-Sat next episode, Sun prep of the next week) ----
Add 'info-digest series'    (New-PsAction 'D:\info-digest\scripts\run_series.ps1' '') (New-ScheduledTaskTrigger -Daily -At '02:00') $h3

# ---- note drafts ----
Add 'info-digest note'       (New-PsAction $digest '-Module note_weekly -Section note_weekly') (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '07:00') $h3
Add 'info-digest note hoken' (New-PsAction $digest '-Module note_hoken -Section note_hoken') (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At '08:00') $h3
