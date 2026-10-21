@echo off
rem Student app check: closes itself after 60 seconds and writes what it sees to diagnostics.txt
rem Exit earlier: Ctrl+Alt+Shift+Q. Latin letters only: cmd misreads Cyrillic in .bat on some code pages.
set STUDENT_APP_DEBUG=1
"%~dp0student_app.exe" --exit-after 60 2> "%~dp0diagnostics.txt"
echo.
echo Done. Log: %~dp0diagnostics.txt
pause
