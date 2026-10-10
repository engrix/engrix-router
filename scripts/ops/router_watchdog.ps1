#requires -Version 5.1
<#
.SYNOPSIS
  EngrixBot router watchdog: if engrix_router.web.server is down, bring it back.
.DESCRIPTION
  -Once  : single check (called by Scheduled Task every 1 minute)
  -Loop  : continuous loop every $IntervalSec (used by the Startup-folder fallback;
           guarded by a mutex so two loops never double-start the router)
  Detection = netstat LISTENING on $Port + Get-Process (fast, no WMI/CIM).
  NOTE: never name a variable $pid -- it is a PowerShell automatic read-only
  variable (assignment throws SessionStateUnauthorizedAccessException).
  Logs actions only (no idle heartbeat) to logs\watchdog.log, rotated at 1 MB.
#>
[CmdletBinding()]
param(
  [switch]$Once,
  [switch]$Loop,
  [int]$IntervalSec = 60,
  [int]$Port = 8450,
  [string]$RouterRoot = "D:\dev\active\engrix-router"
)
$ErrorActionPreference = "Continue"
$Log     = Join-Path $RouterRoot "logs\watchdog.log"
$Service = Join-Path $RouterRoot "scripts\ops\router_service.ps1"
New-Item -ItemType Directory -Force -Path (Join-Path $RouterRoot "logs") | Out-Null

function Write-Log {
  param([string]$Msg)
  Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Msg) -Encoding UTF8
  try {
    if ((Get-Item $Log -ErrorAction SilentlyContinue).Length -gt 1MB) {
      Move-Item -Path $Log -Destination "$Log.old" -Force
    }
  } catch { }
}

function Get-RouterPid {
  $rows = netstat -ano | Select-String "LISTENING\s+(\d+)\s*$"
  foreach ($row in $rows) {
    if ($row.Line -match "[:.]$Port\s") {
      $routerPid = [int]$row.Matches[0].Groups[1].Value
      if ($routerPid -ne 0) { return $routerPid }
    }
  }
  return $null
}

function Test-Alive {
  $routerPid = Get-RouterPid
  if (-not $routerPid) { return $false }
  $proc = Get-Process -Id $routerPid -ErrorAction SilentlyContinue
  return [bool]($proc -and $proc.ProcessName -match "python")
}

function Test-RestartPending {
  # Dashboard /api/system/restart writes this marker; while it is fresh the
  # restart helper owns the down window -- the watchdog must not race it.
  $marker = Join-Path $RouterRoot "logs\restart-requested.json"
  if (-not (Test-Path $marker)) { return $false }
  try {
    $j = Get-Content $marker -Raw | ConvertFrom-Json
    $ageMs = [DateTimeOffset]::Now.ToUnixTimeMilliseconds() - [int64]$j.requested_at_ms
    return ($ageMs -ge 0 -and $ageMs -lt 90000)
  } catch { return $false }
}

function Invoke-Check {
  if (Test-Alive) { return $false }
  if (Test-RestartPending) { Write-Log "restart requested via dashboard -- watchdog skips this cycle"; return $false }
  Write-Log "ROUTER DOWN (no process / port $Port closed) -> starting via router_service.ps1"
  & $Service -Command start 2>&1 | ForEach-Object { Write-Log ("  " + $_) }
  if (Test-Alive) {
    Write-Log "REVIVED OK"
  } else {
    Write-Log "REVIVE FAILED -- will retry next cycle"
  }
  return $true
}

if ($Loop) {
  $created = $false
  $mutex = New-Object System.Threading.Mutex($true, "Local\EngrixRouterWatchdogLoop", [ref]$created)
  if (-not $created) {
    Write-Log "another watchdog loop already running -> exit"
    exit 0
  }
  Write-Log "watchdog loop started (check every ${IntervalSec}s)"
  try {
    while ($true) { [void](Invoke-Check); Start-Sleep -Seconds $IntervalSec }
  } finally { [void]$mutex.ReleaseMutex() }
} else {
  # default = single check (-Once or bare invocation)
  [void](Invoke-Check)
}
