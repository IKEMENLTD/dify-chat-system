# Dify Chat System - デプロイ設定ファイル集

このファイルには各プラットフォーム用のデプロイ設定が含まれています。
使用するプラットフォームに応じて、該当する設定をコピーして個別ファイルを作成してください。

---

## 🚀 Heroku用設定

### Procfile
```
web: gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 30 --keep-alive 2 --max-requests 100 --log-level info
```

### runtime.txt
```
python-3.11.7
```

---

## ⚡ Vercel用設定

### vercel.json
```json
{
  "version": 2,
  "builds": [
    {
      "src": "main.py",
      "use": "@vercel/python"
    },
    {
      "src": "index.html",
      "use": "@vercel/static"
    },
    {
      "src": "dashboard.html",
      "use": "@vercel/static"
    }
  ],
  "routes": [
    {
      "src": "/",
      "dest": "/index.html"
    },
    {
      "src": "/dashboard",
      "dest": "/dashboard.html"
    },
    {
      "src": "/api/(.*)",
      "dest": "/main.py"
    },
    {
      "src": "/health",
      "dest": "/main.py"
    }
  ],
  "env": {
    "DATABASE_URL": "@database_url",
    "DIFY_API_KEY": "@dify_api_key",
    "DIFY_API_URL": "@dify_api_url"
  }
}
```

---

## ☁️ Google Cloud App Engine用設定

### app.yaml
```yaml
runtime: python311

env_variables:
  DATABASE_URL: "your-database-url"
  DIFY_API_KEY: "your-dify-api-key"
  DIFY_API_URL: "https://api.dify.ai/v1"
  FLASK_ENV: "production"

automatic_scaling:
  min_instances: 1
  max_instances: 10
```

---

## 🐳 Docker用設定

### docker-compose.yml
```yaml
version: '3.8'

services:
  web:
    build: .
    ports:
      - "5000:5000"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - DIFY_API_KEY=${DIFY_API_KEY}
      - DIFY_API_URL=${DIFY_API_URL}
      - FLASK_ENV=production
    volumes:
      - .:/app
    restart: unless-stopped
```

### Dockerfile
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "main:app"]
```

---

## 📋 デプロイ手順

### Heroku
```bash
heroku create your-app-name
heroku config:set DATABASE_URL="your-db-url"
heroku config:set DIFY_API_KEY="your-api-key"
git push heroku main
```

### Vercel
```bash
vercel login
vercel
# 環境変数をWebダッシュボードで設定
vercel --prod
```

### Google Cloud
```bash
gcloud app deploy
```

### Docker
```bash
docker-compose up -d
```