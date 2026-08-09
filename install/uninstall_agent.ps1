<#
    uninstall_agent.ps1 - VERIFY.md "Uninstall"
    ============================================================
    Removes the Scheduled Task and stops the agent from running again.

    It does not delete config.json, state.json or agent.log, and it does
    not touch anything in the cloud. Uploaded data stays exactly where it
    is, and the POS database was never written to at any point.

    Safe to run when nothing is installed: it says so and exits 0. An
    uninstall that fails because there was nothing to remove would push
    whoever is running it into deleting things by hand.
    ============================================================
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'thirdeyev'
)

$ErrorActionPreference = 'Stop'

Write-Host ('=' * 66)
Write-Host '  POSentine - remove the scheduled task'
Write-Host ('=' * 66)
Write-Host "  Task name: $TaskName"
Write-Host ''

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "  [ OK ] no task named '$TaskName' is registered - nothing to remove"
    Write-Host ''
    Write-Host '  Uploaded data is untouched. The POS database was never written to.'
    Write-Host ('=' * 66)
    exit 0
}

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
}
catch {
    Write-Host ''
    Write-Host "  STOPPED - could not remove the task: $($_.Exception.Message)"
    Write-Host '  The agent is still scheduled. Do not assume it stopped.'
    Write-Host ('=' * 66)
    exit 1
}

# Removing and having removed are two different claims.
$still = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($still) {
    Write-Host ''
    Write-Host "  STOPPED - '$TaskName' is still registered after removing it."
    Write-Host '  The agent is still scheduled. Call.'
    Write-Host ('=' * 66)
    exit 1
}

Write-Host "  [ OK ] removed '$TaskName' (checked: it is gone)"
Write-Host ''
Write-Host '  A cycle already running finishes on its own; nothing new starts.'
Write-Host '  config.json, state.json and agent.log were left in place.'
Write-Host '  Uploaded data is untouched. The POS database was never written to.'
Write-Host ('=' * 66)
exit 0
