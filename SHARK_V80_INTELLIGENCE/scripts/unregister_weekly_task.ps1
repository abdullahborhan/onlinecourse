<#
.SYNOPSIS
    Removes the SVPI weekly scheduled task (asks for confirmation).
#>
[CmdletBinding()]
param([string]$TaskName = "SHARK V80 Performance Intelligence - Weekly Report")
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Host "Task '$TaskName' is not registered."; return }
$answer = Read-Host "Type YES to remove scheduled task '$TaskName'"
if ($answer -ne "YES") { Write-Host "Cancelled."; return }
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed."
