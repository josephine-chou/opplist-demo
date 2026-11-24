@echo off
rem OppList on synthetic demo data (JoJo Freight). Nothing here reads
rem Outlook or any real data: the data folder is .\demo.
rem   run_demo.cmd              serve on http://127.0.0.1:8766
rem   set PORT=8800 ^& run_demo.cmd   another port
rem Delete demo\opplist.db (or run: python -m opplist demo --out demo) to
rem regenerate; the data is relative to the day it is generated.
setlocal
cd /d "%~dp0"
set OPPLIST_DATA_DIR=demo
if "%PORT%"=="" set PORT=8766
if not exist "demo\opplist.db" (
    echo Generating demo data in demo\ ...
    python -m opplist demo --out demo --seed 42
    if errorlevel 1 exit /b 1
)
echo OppList demo on http://127.0.0.1:%PORT%   (Ctrl+C to stop)
python -c "import os; from opplist import logs; from opplist.web import create_app; logs.setup(); create_app().run(host='127.0.0.1', port=int(os.environ['PORT']), use_reloader=False)"
endlocal
