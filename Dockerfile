FROM python:3.10-slim

WORKDIR /app

# 必要なパッケージをインストール
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# 依存関係をコピー
COPY requirements_bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# Pythonパスを設定
ENV PYTHONPATH="${PYTHONPATH}:/app/python"

# デフォルトコマンド
CMD ["python", "-m", "requirements_bot.main"] 