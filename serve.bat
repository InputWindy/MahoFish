@echo off
setlocal
cd /d "%~dp0"
python -m fish.serve --channels popo,qq,wechat,wecom --backend deepseek --verbose
pause
