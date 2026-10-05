@echo off
rem Запуск без чёрного окна: окно этого файла сразу закрывается, система работает в фоне (значок у часов).
cd /d "%~dp0"
where pythonw >nul 2>&1
if errorlevel 1 (
    msg "%username%" "Не найден Python. Запустите УСТАНОВКА.bat" 2>nul
    exit /b 1
)
start "" pythonw launcher.py
