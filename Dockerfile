FROM python:3.12-slim

# HOME=/tmp: compose で別のUIDで動かしても、Streamlit が設定を書ける場所を確保する
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_ENABLE_STATIC_SERVING=false

# ログの時刻を TZ に合わせるため tzdata を入れる
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY db.py sender.py app.py ./

# DBの置き場（compose では ./data をここにマウントする）
RUN mkdir -p /app/data && chown 1000:1000 /app/data
USER 1000:1000

# 既定は送信担当。管理画面は docker-compose.yml の command で起動する
CMD ["python", "sender.py"]
