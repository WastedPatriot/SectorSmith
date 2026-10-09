@echo off
REM Builds a single portable SectorSmith.exe (asks for admin automatically) into the dist folder
cd /d "%~dp0"
set PY=py -3
where py >nul 2>nul || set PY=python
%PY% -m pip install --upgrade pyinstaller -r requirements.txt || goto :fail
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name SectorSmith --icon sectorsmith\ui\assets\mark.ico --add-data "sectorsmith\ui\assets;sectorsmith\ui\assets" --collect-all customtkinter --collect-all tkinterdnd2 run_sectorsmith.py || goto :fail
echo.
echo Done! Your app is here:
echo   %~dp0dist\SectorSmith.exe
explorer "%~dp0dist"
pause
exit /b 0
:fail
echo.
echo Build failed - scroll up for the error. Is Python installed and on PATH?
pause
exit /b 1
