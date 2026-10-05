@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Сборка программы «Тестирование» (папка dist\Тестирование)...
python -m pip install -q -r requirements.txt pyinstaller || goto :error
python -m PyInstaller --noconfirm --clean testing.spec || goto :error
if exist "static\pyodide" (
    echo Копирую Python-редактор для учеников...
    xcopy /e /i /q /y "static\pyodide" "dist\Тестирование\pyodide" >nul
)
copy /y "ИНСТРУКЦИЯ.md" "dist\Тестирование\ИНСТРУКЦИЯ.txt" >nul 2>&1
echo.
echo Готово: dist\Тестирование\Тестирование.exe
echo Эту папку можно скопировать на другой компьютер — Python там не нужен.
pause
exit /b 0
:error
echo [ОШИБКА] Сборка не удалась.
pause
exit /b 1
