@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo Virtual HDR OSD for Windows - Portable EXE Builder
echo.
echo Output:
echo   release\Virtual HDR OSD for Windows.exe
echo.

set "NEEDS_RUNTIME=0"
if not exist ".venv\Scripts\python.exe" set "NEEDS_RUNTIME=1"
if "%NEEDS_RUNTIME%"=="0" ".venv\Scripts\python.exe" --version >nul 2>&1
if not "%NEEDS_RUNTIME%"=="1" if errorlevel 1 set "NEEDS_RUNTIME=1"
if "%NEEDS_RUNTIME%"=="1" (
    echo Preparing the project-local runtime first...
    if exist ".venv" rmdir /s /q ".venv"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1"
    if errorlevel 1 goto :fail
)

if not exist ".uv\uv.exe" (
    echo ERROR: Project-local uv.exe was not found.
    echo Run "1- Install ^& Run.bat" once, then retry this builder.
    goto :fail
)

if exist ".build-venv" rmdir /s /q ".build-venv"
if exist "build-portable" rmdir /s /q "build-portable"
if not exist "release" mkdir "release"

echo Validating the embedded watchdog resources...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$installer=Join-Path '%~dp0' 'src\sdr_hdr_profile_creator\resources\2- OPTIONAL - Install-Watchdog.bat'; $uninstaller=Join-Path '%~dp0' 'src\sdr_hdr_profile_creator\resources\Uninstall-Watchdog.bat'; $i=Get-Content -Raw -LiteralPath $installer; $u=Get-Content -Raw -LiteralPath $uninstaller; if($i -notmatch 'Schedule.Service' -or $i -notmatch 'RegisterTaskDefinition' -or $i -notmatch 'currentSid' -or $i -match 'Register-ScheduledTask' -or $i -notmatch 'HKCU Run fallback \(shared hidden launcher\)' -or $i -notmatch 'failures >= 3' -or $u -notmatch 'Schedule.Service'){ throw 'Embedded watchdog resources are stale. Expected SID-based Task Scheduler registration and the shared bounded HKCU fallback.' }"
if errorlevel 1 goto :fail

set "UV_NO_CACHE=1"
set "UV_LINK_MODE=copy"
set "UV_PYTHON_INSTALL_DIR=%~dp0.python"
set "UV_PYTHON_BIN_DIR=%~dp0.python\bin"
set "UV_MANAGED_PYTHON=1"
set "UV_PYTHON_INSTALL_REGISTRY=0"

".uv\uv.exe" venv ".build-venv" --python ".venv\Scripts\python.exe"
if errorlevel 1 goto :fail

".uv\uv.exe" pip install --python ".build-venv\Scripts\python.exe" --no-cache "nuitka==4.1.3" "ordered-set==4.1.0" "zstandard==0.25.0"
if errorlevel 1 goto :fail

".uv\uv.exe" export --locked --no-dev --no-emit-project --format requirements-txt -o ".build-venv\locked-requirements.txt"
if errorlevel 1 goto :fail
".uv\uv.exe" pip install --python ".build-venv\Scripts\python.exe" --no-cache --require-hashes -r ".build-venv\locked-requirements.txt"
if errorlevel 1 goto :fail
".uv\uv.exe" pip install --python ".build-venv\Scripts\python.exe" --no-cache --no-deps -e .
if errorlevel 1 goto :fail

echo Running tests...
set "PYTHONPATH=%~dp0src"
".build-venv\Scripts\python.exe" tools\run_tests.py
if errorlevel 1 goto :fail

echo.
echo Compiling one-file portable EXE...
".build-venv\Scripts\python.exe" -m nuitka ^
  --onefile ^
  --disable-cache=ccache ^
  --jobs=8 ^
  --enable-plugin=pyside6 ^
  --windows-console-mode=disable ^
  --assume-yes-for-downloads ^
  --include-package=qfluentwidgets ^
  --include-package=vhdr_color ^
  --include-package-data=qfluentwidgets ^
  --include-data-dir="src\sdr_hdr_profile_creator\resources=sdr_hdr_profile_creator/resources" ^
  --output-dir="build-portable" ^
  --output-filename="Virtual HDR OSD for Windows.exe" ^
  "tools\portable_entry.py"
if errorlevel 1 goto :fail

if not exist "build-portable\Virtual HDR OSD for Windows.exe" (
    echo ERROR: Nuitka completed without producing the expected EXE.
    goto :fail
)

copy /y "build-portable\Virtual HDR OSD for Windows.exe" "release\Virtual HDR OSD for Windows.exe" >nul
if errorlevel 1 goto :fail
powershell.exe -NoProfile -Command "$p='%~dp0release\Virtual HDR OSD for Windows.exe'; $sha=[Security.Cryptography.SHA256]::Create(); $stream=[IO.File]::OpenRead($p); try{$h=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}finally{$stream.Dispose();$sha.Dispose()}; Set-Content -Encoding ASCII -LiteralPath '%~dp0release\Virtual HDR OSD for Windows.sha256.txt' -Value ($h + '  Virtual HDR OSD for Windows.exe')"
if errorlevel 1 goto :fail

echo.
echo BUILD COMPLETE
echo.
echo   %~dp0release\Virtual HDR OSD for Windows.exe
echo.
echo The EXE is portable and does not require Python, uv, or the source tree on the destination PC.
echo The optional watchdog, when installed from Watchdog Settings, is intentionally deployed to LocalAppData so it can persist after the portable EXE closes or moves.
echo.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Review the error above.
pause
exit /b 1
