@echo off
echo Installing requirements (only needed the first time)...
pip install -r requirements.txt
echo.
if not exist .env (
    echo No .env file found.
    echo Create one with DATABASE_URL and SECRET_KEY before running ^(see README^).
    pause
    exit /b 1
)
echo Starting Shopify Purchase Tracker...
python app.py
pause
