@echo off
chcp 65001 >nul
cd /d "%~dp0"
for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%P"
if not defined PYW (
    echo Не найден Python. Сначала запустите УСТАНОВКА.bat
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Тестирование ЕГЭ.lnk'));" ^
  "$link.TargetPath = $env:PYW;" ^
  "$link.Arguments = 'launcher.py';" ^
  "$link.WorkingDirectory = (Get-Location).Path;" ^
  "$link.IconLocation = \"$env:SystemRoot\System32\imageres.dll,109\";" ^
  "$link.WindowStyle = 1;" ^
  "$link.Description = 'Запустить систему тестирования ЕГЭ';" ^
  "$link.Save()"
if errorlevel 1 (
    echo Не удалось создать ярлык.
    pause
    exit /b 1
)
echo Ярлык «Тестирование ЕГЭ» создан на рабочем столе.
timeout /t 3 >nul
