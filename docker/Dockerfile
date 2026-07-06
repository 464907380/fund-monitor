# ═══════════════════════════════════════════════════
# 基金监控系统 — Docker 镜像
# ═══════════════════════════════════════════════════
# 构建：  docker build -t fund-monitor .
#
# 运行盘中监控（前台，9:30~15:00 自动轮询）：
#   docker run -d --restart=always --name fund-monitor \
#     -v ./config.json:/app/data/config.json \
#     -v ./fund_list.json:/app/data/fund_list.json \
#     -v ./.env:/app/.env \
#     -v ./data:/app/data \
#     fund-monitor python /app/src/fund_monitor.py
#
# 运行一次晚报/简报（可配合宿主机 cron）：
#   docker run --rm fund-monitor python /app/src/fund_watch.py
#   docker run --rm fund-monitor python /app/src/global_briefing.py
# ═══════════════════════════════════════════════════

FROM python:3.12-slim

RUN groupadd -r fundmon && useradd -r -g fundmon -d /app -s /sbin/nologin fundmon

WORKDIR /app

# 复制源文件
COPY src/ src/
# 复制配置文件/模板
COPY data/config.json data/fund_list.json data/
COPY templates/ templates/
COPY .env ./

# 权限
RUN chown -R fundmon:fundmon /app

USER fundmon

# 默认命令：打印使用说明
CMD ["python", "-c", "print('基金监控 Docker 镜像\\n\\n使用方式:\\n  docker run --rm fund-monitor python /app/global_briefing.py\\n  docker run --rm fund-monitor python /app/fund_watch.py\\n  docker run -d fund-monitor python /app/fund_monitor.py')"]
