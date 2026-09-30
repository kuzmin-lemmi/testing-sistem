@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Тестирование ЕГЭ.lnk'));" ^
  "$link.TargetPath = (Join-Path (Get-Location) 'start.bat');" ^
  "$link.WorkingDirectory = (Get-Location).Path;" ^
  "$link.IconLocation = \"$env:SystemRoot\System32\imageres.dll,109\";" ^
  "$link.WindowStyle = 7;" ^
  "$link.Description = 'Запустить систему тестирования ЕГЭ';" ^
  "$link.Save()"
if errorlevel 1 (
    echo Не удалось создать ярлык.
    pause
    exit /b 1
)
echo Ярлык «Тестирование ЕГЭ» создан на рабочем столе.
timeout /t 3 >nul
