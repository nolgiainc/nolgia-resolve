@echo off
rem NOLGIA for DaVinci Resolve: copies the plugin into Resolve's Scripts folder for this user.
setlocal
set "SOURCE=%~dp0Utility"
set "TARGET=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
if not exist "%SOURCE%\NOLGIA.py" (
  echo Could not find the Utility folder next to this installer. Unzip the whole download first.
  goto failed
)
tasklist /FI "IMAGENAME eq Resolve.exe" 2>nul | find /I "Resolve.exe" >nul && call :open
if not exist "%TARGET%" mkdir "%TARGET%" || goto failed
copy /Y "%SOURCE%\NOLGIA.py" "%TARGET%\NOLGIA.py" >nul || goto failed
copy /Y "%SOURCE%\nolgia_resolve.zip" "%TARGET%\nolgia_resolve.zip" >nul || goto failed
echo NOLGIA is installed in:
echo   %TARGET%
echo Restart DaVinci Resolve, then choose NOLGIA in the Workspace ^> Scripts menu.
if /I not "%~1"=="/quiet" pause
exit /b 0
:open
rem Resolve finds scripts when it starts, so it has to be quit and started again.
echo DaVinci Resolve is open. NOLGIA is installed anyway; to start using it:
echo   1. Close the NOLGIA window if it is open.
echo   2. Quit Resolve: DaVinci Resolve ^> Quit DaVinci Resolve.
echo With NOLGIA 0.2.2 or older open, that quit leaves Resolve running with no
echo windows; closing the NOLGIA window first avoids it. 0.2.3 and newer finish the
echo quit themselves. If Resolve is already left with no windows, end it in Task
echo Manager.
echo.
exit /b 0
:failed
echo NOLGIA was not installed.
if /I not "%~1"=="/quiet" pause
exit /b 1
