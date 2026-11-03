@echo off
cd /d %~dp0
pip install -r requirements.txt -q
if "%ADMIN_PASSWORD%"=="" set ADMIN_PASSWORD=admin123
uvicorn app:app --host 0.0.0.0 --port 8000
