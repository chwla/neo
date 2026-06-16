@echo off
cd /d "%~dp0"
echo Starting Neo Streamlit at http://127.0.0.1:8501
".venv\Scripts\streamlit.exe" run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false --server.headless true --server.fileWatcherType none
echo.
echo Streamlit stopped with exit code %ERRORLEVEL%.
pause
