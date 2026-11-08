<#
.SYNOPSIS
    Registers the SVPI weekly report in Windows Task Scheduler:
    every Sunday at 20:00 Asia/Kuala_Lumpur (Malaysia Time, UTC+8).
.DESCRIPTION
    Task Scheduler triggers use the PC's local clock. If the PC is not set to
    Malaysia time, 20:00 MYT is converted to local time (this can fall on a
    different weekday, e.g. Sunday 12:00 UTC). The task runs as the current
    user, only when logged on, and never stores a password.
        powershell -ExecutionPolicy Bypass -File .\scripts\register_weekly_task.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SHARK V80 Performance Intelligence - Weekly Report",
    [string]$MalaysiaTime = "20:00",
    [switch]$WhatIf
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_weekly.ps1"
if (-not (Test-Path $Runner)) { throw "Missing $Runner" }

# Windows timezone id for Kuala Lumpur (UTC+8, no daylight saving)
try {
    $myt = [System.TimeZoneInfo]::FindSystemTimeZoneById("Singapore Standard Time")
} catch {
    $myt = [System.TimeZoneInfo]::FindSystemTimeZoneById("Asia/Kuala_Lumpur")
}

# Next Sunday at 20:00 in Malaysia, converted to this PC's local time
$nowMyt = [System.TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, $myt)
$daysAhead = (7 + [int][DayOfWeek]::Sunday - [int]$nowMyt.DayOfWeek) % 7
$parts = $MalaysiaTime.Split(":")
$targetMyt = $nowMyt.Date.AddDays($daysAhead).AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
if ($targetMyt -le $nowMyt) { $targetMyt = $targetMyt.AddDays(7) }
$targetMyt = [DateTime]::SpecifyKind($targetMyt, [DateTimeKind]::Unspecified)
$utc = [System.TimeZoneInfo]::ConvertTimeToUtc($targetMyt, $myt)
$local = [System.TimeZoneInfo]::ConvertTimeFromUtc($utc, [System.TimeZoneInfo]::Local)

Write-Host ("Malaysia schedule : {0:dddd HH:mm} (Asia/Kuala_Lumpur)" -f $targetMyt)
Write-Host ("Local trigger     : {0:dddd HH:mm} ({1})" -f $local, [System.TimeZoneInfo]::Local.Id)
if ([System.TimeZoneInfo]::Local.SupportsDaylightSavingTime -and
    [System.TimeZoneInfo]::Local.BaseUtcOffset -ne $myt.BaseUtcOffset) {
    Write-Warning ("This PC's timezone observes daylight saving; re-run this script after each " +
                   "DST change so the task stays at 20:00 Malaysia time.")
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $Runner) `
    -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $local.DayOfWeek -At $local
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

if ($WhatIf) {
    Write-Host "WhatIf: task not registered."
    return
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description ("Weekly SVPI audit report (Sunday {0} Malaysia time). Read-only: imports " +
                  "files from data\incoming; never connects to MT5 or trades." -f $MalaysiaTime) `
    -Force | Out-Null
Write-Host "Registered task '$TaskName'."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName '$TaskName'"
