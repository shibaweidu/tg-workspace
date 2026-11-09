@echo off
cd /d "%~dp0"
python -u collect_tgegg.py --workers 6
pause
