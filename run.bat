@echo off
REM Launch SectorSmith from source (needs Python 3.10+ from python.org)
cd /d "%~dp0"
py -3 -m pip install -q -r requirements.txt 2>nul || python -m pip install -q -r requirements.txt
where pyw >nul 2>nul && (start "" pyw -3 -m sectorsmith & exit /b)
python -m sectorsmith
