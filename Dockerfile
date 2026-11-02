FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY rogue ./rogue

# 一次性测评 Job 入口（k8s Job 使用）:
#   python -m rogue.cli --config /etc/rogue/config.json
#   或注入环境变量 ROGUE_CONFIG_JSON 后: python -m rogue.cli
# 控制台入口（Deployment 使用）:
#   uvicorn rogue.console.app:app --host 0.0.0.0 --port 4080
