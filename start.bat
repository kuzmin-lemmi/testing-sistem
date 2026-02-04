@echo off
chcp 65001 >nul
title Система тестирования ЕГЭ по информатике

echo ============================================
echo  Система тестирования ЕГЭ по информатике
echo ============================================
echo.

:: Проверяем наличие Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден!
    echo Пожалуйста, установите Python 3.8+ с сайта python.org
    pause
    exit /b 1
)

:: Переходим в папку со скриптом
cd /d "%~dp0"

:: Проверяем/устанавливаем зависимости
echo Проверка зависимостей...
pip show flask >nul 2>&1
if errorlevel 1 (
    echo Установка зависимостей...
    pip install -r requirements.txt
)

echo.
echo Запуск сервера...
echo Для остановки закройте это окно или нажмите Ctrl+C
echo.

:: Открываем браузер через 2 секунды (даём серверу время запуститься)
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8080"

python server.py

pause
