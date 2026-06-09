#!/usr/bin/env bash
# ═══════════════════════════════════════════════════
# 基金监控 — Linux systemd 定时任务安装脚本
# ═══════════════════════════════════════════════════
# 用法：
#   sudo ./deploy/install.sh
#
# 前置条件：
#   - Python 3.10+
#   - systemd (Ubuntu / Debian / CentOS)
#   - 可选：pip install mypy pytest (开发)
# ═══════════════════════════════════════════════════

set -euo pipefail

INSTALL_DIR="/opt/fund-monitor"
SERVICE_DIR="/etc/systemd/system"

echo "📦 安装基金监控到 $INSTALL_DIR"

# 1. 复制文件
sudo mkdir -p "$INSTALL_DIR"
sudo cp -v *.py *.json *.html *.toml "$INSTALL_DIR/"
sudo mkdir -p "$INSTALL_DIR/tests"
sudo cp -v tests/*.py "$INSTALL_DIR/tests/" 2>/dev/null || true
sudo cp -v .env.example "$INSTALL_DIR/.env"

# 2. 安装 systemd service + timer
for name in global-briefing fund-watch fund-monitor; do
    sudo cp -v "deploy/${name}.service"  "$SERVICE_DIR/"
    sudo cp -v "deploy/${name}.timer"    "$SERVICE_DIR/"
done

sudo systemctl daemon-reload

# 3. 启用并启动定时器
for name in global-briefing fund-watch fund-monitor; do
    sudo systemctl enable "${name}.timer"
    sudo systemctl start  "${name}.timer"
done

# 4. 提示配置
echo ""
echo "✅ 安装完成！"
echo ""
echo "下一步："
echo "  1. 编辑环境变量:  sudo nano $INSTALL_DIR/.env"
echo "  2. 编辑基金列表:  sudo nano $INSTALL_DIR/fund_list.json"
echo "  3. 编辑配置:      sudo nano $INSTALL_DIR/config.json"
echo ""
echo "查看定时器状态:"
echo "  systemctl list-timers 'fund-*' 'global-*'"
echo ""
echo "手动测试（查看输出）:"
echo "  sudo systemctl start global-briefing.service; journalctl -u global-briefing.service -n 50 --no-pager"
