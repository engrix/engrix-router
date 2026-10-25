#requires -Version 5.1
<#
.SYNOPSIS
  EngrixBot router watchdog -- thin forwarder to scripts/ops/router_watchdog.py.
.DESCRIPTION
  All revival logic now lives in router_watchdog.py. This file exists only so
  an operator (or an old scheduled task) typing the historical path still
  lands on something that works. It deliberately contains NO logic of its own:
  two implementations of "should I revive?" is exactly how the marker-grace
  rule and the service script drifted apart before.

  -Once  : single check (called by Scheduled Task every 1 minute)
  -Loop  : continuous loop every $IntervalSec (Startup-folder fallback)
#>
[CmdletBinding()]
param(
  [switch]$Once,
  [switch]$Loop,
  [int]$IntervalSec = 60,
  [int]$Port = 8450,
  [string]$RouterRoot = "D:\dev\active\engrix-router"
)
$ErrorActionPreference = "Stop"
$WatchdogPy = Join-Path $RouterRoot "scripts\ops\router_watchdog.py"

# pythonw.exe = GUI subsystem, so the scheduled task never flashes a console.
# Fall back to python.exe when pythonw is missing rather than failing the tick.
$PythonW = "C:\Users\ariel\AppData\Local\Programs\Python\Python312\pythonw.exe"
if (-not (Test-Path $PythonW)) {
  $PythonW = "C:\Users\ariel\AppData\Local\Programs\Python\Python312\python.exe"
}
if (-not (Test-Path $WatchdogPy)) {
  Write-Error "watchdog module not found: $WatchdogPy"
  exit 2
}

$argv = @("-u", $WatchdogPy, "--port", "$Port")
if ($Loop) { $argv += @("--loop", "--interval", "$IntervalSec") } else { $argv += "--once" }

& $PythonW @argv
exit $LASTEXITCODE
