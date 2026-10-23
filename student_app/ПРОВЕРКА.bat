@echo off
rem Student app check: closes itself after 60 seconds and writes what it sees to diagnostics.txt
rem Exit earlier: Ctrl+Alt+Shift+Q. Latin letters only: cmd misreads Cyrillic in .bat on some code pages.
rem The program is the .exe next to this file (student_app.exe after the build, renamed in the kit).
set STUDENT_APP_DEBUG=1
set APP=
for %%F in ("%~dp0*.exe") do set "APP=%%~fF"
if not defined APP (
    echo No .exe next to this file.
    pause
    exit /b 1
)
echo Running %APP% for 60 seconds...
"%APP%" --exit-after 60 2> "%~dp0diagnostics.txt"
echo.
echo Done. Log: %~dp0diagnostics.txt
pause
