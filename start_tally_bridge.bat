@echo off
rem Keeps the Tally bridge running; restarts it if it ever stops.
cd /d "%~dp0"
:loop
python tally_bridge.py
echo Bridge stopped. Restarting in 10 seconds... (close this window to stop it)
timeout /t 10 >nul
goto loop
