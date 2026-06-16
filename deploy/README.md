# Linux systemd 部署

## 安装

```bash
sudo ./deploy/install.sh
```

## 组件

| Service | Timer | 触发时间 | 说明 |
|---------|-------|----------|------|
| `global-briefing.service` | `global-briefing.timer` | 工作日 09:30 | 全球股市早报 |
| `fund-watch.service` | `fund-watch.timer` | 工作日 15:30 | 先运行全市场推荐评分，再生成收盘晚报 |

> `fund-watch.service` 在启动晚报生成前会自动先执行 `fund_recommend.py`（全市场推荐评分），确保晚报使用的数据是最新的。推荐评分约需 4~16 分钟，晚报约需 5~15 秒。
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
