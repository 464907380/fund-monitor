# Linux systemd 部署

## 安装

```bash
sudo ./deploy/install.sh
```

## 组件

| Service | Timer | 触发时间 | 说明 |
|---------|-------|----------|------|
| `global-briefing.service` | `global-briefing.timer` | 工作日 08:30 | 全球股市早报 |
| `fund-watch.service` | `fund-watch.timer` | 工作日 15:30 | 收盘晚报 |
| `fund-monitor.service` | `fund-monitor.timer` | 工作日 09:25 | 盘中实时监控（运行到 15:00 自动退出） |

## 管理命令

```bash
# 查看所有定时器
systemctl list-timers 'fund-*' 'global-*'

# 查看任务日志
journalctl -u fund-watch.service -n 50 --no-pager
journalctl -u fund-monitor.service -f   # 实时追踪盘中监控

# 手动触发
sudo systemctl start global-briefing.service

# 停止并禁用
sudo systemctl stop  fund-monitor.timer
sudo systemctl disable fund-monitor.timer
```
