#requires -Version 5.1
<#
.SYNOPSIS
  EngrixBot router (engrix_router.web.server) control script.
.DESCRIPTION
  start | stop | restart | status | install-autostart | uninstall-autostart
  Autostart = per-user Scheduled Task "EngrixRouterWatchdog" running the
  watchdog script every minute (fallback: Startup folder .vbs loop).

  Detection uses netstat + Get-Process (fast, no WMI/CIM) because
  Get-CimInstance is very slow inside sandboxed/non-interactive sessions.
  NOTE: never name a variable $pid -- it is a PowerShell automatic
  read-only variable (assignment throws SessionStateUnauthorizedAccessException).
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Command = "status",
  [int]$Port = 8450,
  [string]$RouterRoot = "D:\dev\active\engrix-router",
  [string]$PythonExe = "C:\Users\ariel\AppData\Local\Programs\Python\Python312\python.exe"
)
$ErrorActionPreference = "Stop"
$LogDir    = Join-Path $RouterRoot "logs"
$StdoutLog = Join-Path $LogDir "router-stdout.log"
$StderrLog = Join-Path $LogDir "router-stderr.log"
$TaskName  = "EngrixRouterWatchdog"
$Watchdog  = Join-Path $RouterRoot "scripts\ops\router_watchdog.ps1"

function Get-RouterPidPort {
  # PID of the process LISTENING on $P (netstat is fast; CIM is slow here).
  param([int]$P = $Port)
  $rows = netstat -ano | Select-String "LISTENING\s+(\d+)\s*$"
  foreach ($row in $rows) {
    if ($row.Line -match "[:.]$P\s") {
      $routerPid = [int]$row.Matches[0].Groups[1].Value
      if ($routerPid -ne 0) { return $routerPid }
    }
  }
  return $null
}

function Wait-RouterPort {
  param([int]$P = $Port, [int]$TimeoutSec = 40, [bool]$WantUp = $true)
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    if ( ([bool](Get-RouterPidPort -P $P)) -eq $WantUp ) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return ( ([bool](Get-RouterPidPort -P $P)) -eq $WantUp )
}

function Start-Router {
  if (Get-RouterPidPort -P $Port) { Write-Host "[start] already running"; return $true }
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $p = Start-Process -FilePath $PythonExe `
        -ArgumentList @("-u", "-m", "engrix_router.web.server") `
        -WorkingDirectory $RouterRoot -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru
  Write-Host "[start] launched pid=$($p.Id) (cwd=$RouterRoot)"
  if (-not (Wait-RouterPort -P $Port -TimeoutSec 40 -WantUp $true)) {
    Write-Host "[start] FAIL: port $Port not listening in 40s -- see $StderrLog"
    return $false
  }
  Write-Host "[start] OK: port $Port listening"
  return $true
}

function Stop-Router {
  $routerPid = Get-RouterPidPort -P $Port
  if (-not $routerPid) {
    Write-Host "[stop] port $Port already closed"
    return $true
  }
  Write-Host "[stop] killing pid=$routerPid"
  Stop-Process -Id $routerPid -Force -ErrorAction SilentlyContinue
  if (-not (Wait-RouterPort -P $Port -TimeoutSec 20 -WantUp $false)) {
    Write-Host "[stop] WARN: port $Port still listening"
    return $false
  }
  Write-Host "[stop] OK"
  return $true
}

function Show-Status {
  $routerPid = Get-RouterPidPort -P $Port
  $proc = $null
  if ($routerPid) { $proc = Get-Process -Id $routerPid -ErrorAction SilentlyContinue }
  if ($proc) {
    Write-Host ("process: pid={0} name={1} started={2}" -f $proc.Id, $proc.ProcessName, $proc.StartTime)
  } else { Write-Host "process: none" }
  Write-Host ("port {0}: {1}" -f $Port, $(if ($routerPid) { "LISTENING" } else { "closed" }))
  if ($proc -and $routerPid) { Write-Host "status: RUNNING" }
  elseif ($routerPid) { Write-Host "status: WEIRD (port up, process gone)" }
  else { Write-Host "status: DOWN" }
}

function Install-Autostart {
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`" -Once")
  $trigLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  try {
    Register-ScheduledTask -TaskName $TaskName -Action $action `
      -Trigger @($trigLogon, $trigRepeat) -Settings $settings `
      -Description "EngrixBot router watchdog: revive engrix_router.web.server when down (every 1 min)" -Force | Out-Null
    Write-Host "[autostart] scheduled task '$TaskName' registered (at logon + every 1 min)"
  } catch {
    Write-Host "[autostart] scheduled task FAILED: $($_.Exception.Message)"
    $startup = [Environment]::GetFolderPath("Startup")
    $vbs = Join-Path $startup "engrix-router-watchdog.vbs"
    $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`" -Loop"
    Set-Content -Path $vbs -Encoding ASCII `
      -Value "CreateObject(""Wscript.Shell"").Run ""$cmd"", 0, False"
    Write-Host "[autostart] fallback installed: $vbs (hidden watchdog loop at logon)"
  }
}

function Uninstall-Autostart {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[autostart] scheduled task '$TaskName' removed"
  }
  $vbs = Join-Path ([Environment]::GetFolderPath("Startup")) "engrix-router-watchdog.vbs"
  if (Test-Path $vbs) { Remove-Item $vbs -Force; Write-Host "[autostart] startup vbs removed" }
}

switch ($Command.ToLower()) {
  "start"              { if (Start-Router) { exit 0 } else { exit 1 } }
  "stop"               { if (Stop-Router) { exit 0 } else { exit 1 } }
  "restart"            { [void](Stop-Router); Start-Sleep -Seconds 2; if (Start-Router) { exit 0 } else { exit 1 } }
  "status"             { Show-Status; exit 0 }
  "install-autostart"  { Install-Autostart; exit 0 }
  "uninstall-autostart"{ Uninstall-Autostart; exit 0 }
  default              { Write-Host "unknown command '$Command' (start|stop|restart|status|install-autostart|uninstall-autostart)"; exit 2 }
}
