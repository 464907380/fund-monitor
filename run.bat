@echo off
chcp 65001 >nul
echo 🔧 基金优选 - 启动中...
python -X utf8 src\fund_server.py
pause
