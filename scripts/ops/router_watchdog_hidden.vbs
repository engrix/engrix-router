' EngrixBot router watchdog -- hidden launcher.
' wscript.exe has NO console of its own, so running powershell from here
' (window style 0) never flashes a console window, unlike launching
' powershell.exe directly from a Scheduled Task (conhost flashes ~1s
' before -WindowStyle Hidden applies).
CreateObject("WScript.Shell").Run _
  "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\dev\active\engrix-router\scripts\ops\router_watchdog.ps1"" -Once", 0, False
