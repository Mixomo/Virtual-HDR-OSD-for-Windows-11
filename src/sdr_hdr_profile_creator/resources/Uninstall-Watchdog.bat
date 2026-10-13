@echo off
setlocal EnableExtensions
title Color Profile Mode Watchdog - Uninstaller

echo.
echo Color Profile Mode Watchdog
echo Standalone uninstaller
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$app=Join-Path $env:LOCALAPPDATA 'ColorProfileModeWatchdog';" ^
  "$watchdog=Join-Path $app 'Watchdog.ps1';" ^
  "$task='Virtual HDR OSD - Color Profile Mode Watchdog';" ^
  "$fallback=$false; $resultPath=Join-Path $app 'install_result.json'; if(Test-Path -LiteralPath $resultPath){try{$result=Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json; $fallback=($result.ok -and $result.startup -like 'HKCU Run*')}catch{}};" ^
  "$svc=$null; $folder=$null; $connected=$false; try{$svc=New-Object -ComObject 'Schedule.Service'; $svc.Connect(); $folder=$svc.GetFolder('\'); $connected=$true; foreach($registered in $folder.GetTasks(0)){if($registered.Name -eq $task){$folder.DeleteTask($task,0)}}}catch{if(-not $fallback -or $connected){throw}; Write-Warning 'Task Scheduler is unavailable; removing the recorded HKCU fallback'}finally{if($folder){[Runtime.InteropServices.Marshal]::ReleaseComObject($folder) | Out-Null}; if($svc){[Runtime.InteropServices.Marshal]::ReleaseComObject($svc) | Out-Null}};" ^
  "Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Force -ErrorAction SilentlyContinue;" ^
  "$escaped=[Regex]::Escape($watchdog); $launcherPattern=[Regex]::Escape((Join-Path $app 'Launcher.vbs'));" ^
  "Get-CimInstance Win32_Process | Where-Object { ($_.Name -in @('powershell.exe','pwsh.exe','wscript.exe','cscript.exe')) -and $_.CommandLine -and (($_.CommandLine -match $escaped) -or ($_.CommandLine -match $launcherPattern)) } | Sort-Object @{Expression={if($_.Name -in @('wscript.exe','cscript.exe')){0}else{1}}} | ForEach-Object { $result=Invoke-CimMethod -InputObject $_ -MethodName Terminate; if($result.ReturnValue -ne 0){throw ('Could not stop watchdog PID ' + $_.ProcessId)} };" ^
  "Start-Sleep -Milliseconds 300;" ^
  "$running=$true; for($attempt=0;$attempt -lt 10;$attempt++){try{$held=[Threading.Mutex]::OpenExisting('Local\ColorProfileModeWatchdogStandalone');$held.Dispose();$running=$true}catch [Threading.WaitHandleCannotBeOpenedException]{$running=$false;break}catch [UnauthorizedAccessException]{$running=$true};Start-Sleep -Milliseconds 100}; if($running){throw 'The watchdog is still running or cannot be stopped; its files were kept'};" ^
  "$resolved=[IO.Path]::GetFullPath($app); $expected=[IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\'); if([IO.Path]::GetDirectoryName($resolved) -ine $expected -or [IO.Path]::GetFileName($resolved) -ne 'ColorProfileModeWatchdog'){throw 'Unexpected uninstall target'}; if(Test-Path -LiteralPath $resolved){Remove-Item -LiteralPath $resolved -Recurse -Force};" ^
  "$startup=[Environment]::GetFolderPath('Startup');" ^
  "Remove-Item -LiteralPath (Join-Path $startup 'Color Profile Mode Watchdog.lnk') -Force -ErrorAction SilentlyContinue;" ^
  "exit 0"

if errorlevel 1 (
    echo.
    echo Uninstall encountered an error.
    pause
    exit 1
)

echo Watchdog removed successfully.
echo No color profiles were deleted or modified by the uninstaller.
echo.
pause
exit 0
