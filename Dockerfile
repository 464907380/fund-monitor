# ═══════════════════════════════════════════════════
# 基金监控系统 — Docker 镜像
# ═══════════════════════════════════════════════════
# 构建：  docker build -t fund-monitor .
#
# 运行盘中监控（前台，9:30~15:00 自动轮询）：
#   docker run -d --restart=always --name fund-monitor \
#     -v ./config.json:/app/config.json \
#     -v ./fund_list.json:/app/fund_list.json \
#     -v ./.env:/app/.env \
#     -v ./data:/app/data \        # 持久化文件（需将 HISTORY_DIR 改为子目录 /app/data，见 fund_utils.py）
#     fund-monitor python /app/fund_monitor.py
#
# 运行一次晚报/简报（可配合宿主机 cron）：
#   docker run --rm fund-monitor python /app/fund_watch.py
#   docker run --rm fund-monitor python /app/global_briefing.py
# ═══════════════════════════════════════════════════

FROM python:3.12-slim

RUN groupadd -r fundmon && useradd -r -g fundmon -d /app -s /sbin/nologin fundmon

WORKDIR /app

# 复制所有源文件（基础监控+评分推荐+管理服务器）
COPY config.py fund_watch.py fund_monitor.py global_briefing.py fund_utils.py ./
COPY fund_alerts.py fund_manage.py fund_metrics.py fund_recommend.py fund_render.py fund_scoring.py fund_server.py ./
COPY config.json fund_list.json ./
COPY email_template.html ./

# 权限
RUN chown -R fundmon:fundmon /app

USER fundmon

# 默认命令：打印使用说明
CMD ["python", "-c", "print('基金监控 Docker 镜像\\n\\n使用方式:\\n  docker run --rm fund-monitor python /app/global_briefing.py\\n  docker run --rm fund-monitor python /app/fund_watch.py\\n  docker run -d fund-monitor python /app/fund_monitor.py')"]
