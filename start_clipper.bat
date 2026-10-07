@echo off
rem Starts the clipper dashboard + monitor. Double-click, or run at logon via Task Scheduler.
cd /d "%~dp0"
python -m clipper serve
pause
