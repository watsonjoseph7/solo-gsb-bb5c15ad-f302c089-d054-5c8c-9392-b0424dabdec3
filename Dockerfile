FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DNS_AUDIT_DB=/data/audits.db

WORKDIR /app

# 依赖安装（构建期完成）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app ./app

# SQLite 数据目录（可挂载卷持久化）；数据库表在应用启动时自动初始化
RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
