#!/bin/bash
echo "🔧 基金优选 - 启动中..."
cd "$(dirname "$0")"
python3 -X utf8 src/fund_server.py
