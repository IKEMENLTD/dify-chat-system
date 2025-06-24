# Dify Chat System - デプロイ設定ファイル集

このファイルには各プラットフォーム用のデプロイ設定が含まれています。
使用するプラットフォームに応じて、該当する設定をコピーして個別ファイルを作成してください。

---

## 🚀 Render用設定（推奨）

### Procfile
```
web: gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --keep-alive 2 --max-requests 100 --log-level info --preload
```

### render.yaml
```yaml
services:
  - type: web
    name: veteran-ai-chat
    env: python
    plan: free
    buildCommand: "pip install -r requirements.txt"
    startCommand: "gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120"
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.7
      - key: DATABASE_URL
        fromDatabase:
          name: veteran-ai-db
          property: connectionString
      - key: ANTHROPIC_API_KEY
        sync: false
      - key: FLASK_ENV
        value: production
      - key: FLASK_DEBUG
        value: False

databases:
  - name: veteran-ai-db
    databaseName: veteran_ai
    user: veteran_ai_user
    plan: free
```

---

## 🚀 Heroku用設定

### Procfile
```
web: gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --keep-alive 2 --max-requests 100 --log-level info
release: python -c "from main import init_database; init_database()"
```

### runtime.txt
```
python-3.11.7
```

### Aptfile (PostgreSQL日本語検索用)
```
postgresql-contrib
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
      "use": "@vercel/python",
      "config": {
        "maxLambdaSize": "50mb"
      }
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
      "src": "/webhook/(.*)",
      "dest": "/main.py"
    },
    {
      "src": "/health",
      "dest": "/main.py"
    }
  ],
  "env": {
    "DATABASE_URL": "@database_url",
    "ANTHROPIC_API_KEY": "@anthropic_api_key",
    "FLASK_ENV": "production",
    "FLASK_DEBUG": "False"
  },
  "functions": {
    "main.py": {
      "maxDuration": 60
    }
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
  ANTHROPIC_API_KEY: "your-anthropic-api-key"
  FLASK_ENV: "production"
  FLASK_DEBUG: "False"

automatic_scaling:
  min_instances: 1
  max_instances: 10
  target_cpu_utilization: 0.6

resources:
  cpu: 1
  memory_gb: 1
  disk_size_gb: 10

handlers:
- url: /.*
  script: auto
  secure: always
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
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - FLASK_ENV=production
      - FLASK_DEBUG=False
    volumes:
      - .:/app
    restart: unless-stopped
    depends_on:
      - postgres

  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: veteran_ai
      POSTGRES_USER: veteran_ai_user
      POSTGRES_PASSWORD: your_password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

volumes:
  postgres_data:
```

### Dockerfile
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# システム依存関係をインストール
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションファイルをコピー
COPY . .

# ポートを公開
EXPOSE 5000

# アプリケーションを実行
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "main:app"]
```

---

## 📋 デプロイ手順

### Render（推奨）
1. GitHubにリポジトリをプッシュ
2. Render.comでWebサービスを作成
3. GitHubリポジトリを接続
4. 環境変数を設定：
   - `ANTHROPIC_API_KEY`: Claude APIキー
   - `DATABASE_URL`: PostgreSQLデータベースURL
5. デプロイ実行

### Heroku
```bash
heroku create your-app-name
heroku addons:create heroku-postgresql:hobby-dev
heroku config:set ANTHROPIC_API_KEY="your-anthropic-api-key"
heroku config:set FLASK_ENV="production"
heroku config:set FLASK_DEBUG="False"
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

---

## 🔧 必須環境変数

全てのデプロイ方法で以下の環境変数が必要です：

```bash
# 必須
DATABASE_URL=postgresql://user:password@host:port/dbname
ANTHROPIC_API_KEY=your-anthropic-api-key

# 本番環境用
FLASK_ENV=production
FLASK_DEBUG=False
SECRET_KEY=your-super-secret-key

# オプション（LINE、Chatwork連携用）
LINE_CHANNEL_SECRET=your-line-channel-secret
LINE_CHANNEL_ACCESS_TOKEN=your-line-access-token
CHATWORK_WEBHOOK_TOKEN=your-chatwork-webhook-token
CHATWORK_API_TOKEN=your-chatwork-api-token

# Supabase（ファイルアップロード用）
SUPABASE_URL=your-supabase-url
SUPABASE_KEY=your-supabase-key
SUPABASE_BUCKET_NAME=chat-uploads
```

---

## 🚨 デプロイ前チェックリスト

- [ ] PostgreSQLデータベースが作成済み
- [ ] Anthropic Claude APIキーを取得済み
- [ ] 全ての必須環境変数を設定済み
- [ ] requirements.txtに全ての依存関係を記載
- [ ] データベースの日本語検索設定が完了
- [ ] セキュリティ設定（SECRET_KEY等）が本番用に変更済み
