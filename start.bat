@echo off
REM ============================================================
REM  ERM Daily Risk Intelligence Tool — one-click launcher
REM  Double-click this file to start the app.
REM ============================================================

cd /d "%~dp0backend"

REM activate the virtual environment
call ..\venv\Scripts\activate.bat
if errorlevel 1 (
  echo.
  echo  ERROR: could not activate the virtual environment.
  echo  Make sure you have run:  python -m venv venv
  echo  and:  pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

REM make sure all dependencies are installed (fast no-op if already satisfied)
pip install -r requirements.txt -q

REM model + provider settings
set LLM_PROVIDER=ollama
set LLM_MODEL=qwen3.5:9b
set EMBED_MODEL=BAAI/bge-m3
REM 0.82 = same-story similarity on bge-m3. The old 0.55 caught "vaguely
REM related" and silently overrode the code default — keep these in sync.
set EMBED_THRESHOLD=0.82

REM make sure the database has all tables/columns (safe to run every time)
python database.py

echo.
echo  ============================================================
echo   Starting the ERM Risk Intelligence Tool...
echo   Open this in your browser:  http://127.0.0.1:8000
echo   Login: admin / changeme
echo   (Close this window to stop the server.)
echo  ============================================================
echo.

uvicorn main:app --host 127.0.0.1 --port 8000

pause
