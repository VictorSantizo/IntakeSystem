# ============================================================
#   Klickity Klack - register the resilient auto-start service
#   Invoked by install_service.bat. Run as Administrator on the server.
# ============================================================
# Registers a Scheduled Task that launches run_service.bat at every boot under the
# SYSTEM account, ONE MINUTE after startup (so the network stack is up before the app
# binds port 5050), with restart-on-failure and no battery conditions. This is what
# makes the app come back on its own after a power outage / reboot.
$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $here 'run_service.bat'
$task   = 'KlickityKlack'

if (-not (Test-Path $runner)) {
    Write-Error "run_service.bat not found next to this script ($runner)."
    exit 1
}

$action  = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT1M'   # wait 1 min after boot so the network/subsystems are ready
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3

Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered scheduled task '$task':"
Write-Host "  - starts at boot, +1 minute delay"
Write-Host "  - runs as SYSTEM (no window, survives logoff)"
Write-Host "  - restart-on-failure (every 1 min, up to 3x); no battery conditions"
Write-Host "  - runner: $runner"
Write-Host ""
Write-Host "Start it now without a reboot:  schtasks /Run /TN $task"
Write-Host "  (First CLOSE any open Klickity Klack window so two copies don't fight over port 5050.)"
Write-Host "Check it:                        schtasks /Query /TN $task /V /FO LIST"
Write-Host "Stop it:                         schtasks /End  /TN $task"
Write-Host "Remove it:                       schtasks /Delete /TN $task /F"
