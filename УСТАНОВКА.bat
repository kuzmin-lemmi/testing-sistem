@echo off
chcp 65001 >nul
title Установка системы тестирования
cd /d "%~dp0"
echo ============================================
echo  Установка системы тестирования
echo ============================================
echo.
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Не найден Python.
    echo Скачайте его с python.org и при установке поставьте галочку "Add Python to PATH".
    echo Потом запустите этот файл ещё раз.
    pause
    exit /b 1
)
echo Устанавливаю нужные компоненты (нужен интернет, один раз)...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить компоненты. Проверьте интернет и запустите файл ещё раз.
    pause
    exit /b 1
)
echo.
call "%~dp0СОЗДАТЬ_ЯРЛЫК.bat"
echo.
echo Готово! Запускайте систему ярлыком «Тестирование ЕГЭ» на рабочем столе.
pause
