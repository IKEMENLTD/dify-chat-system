import os
import json
import re
import requests
import psycopg2
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from psycopg2.extras import RealDictCursor
import logging
from functools import wraps
import time

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, FollowEvent, ImageMessage, FileMessage
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi as MessagingApiV3

# Supabase SDK
from supabase import create_client, Client

# =================================================================
# 1. 初期設定
# =================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
app = Flask(__name__, static_folder='.')
CORS(app, origins=os.getenv('ALLOWED_ORIGINS', '*').split(','))

# =================================================================
# 2. 環境変数とAPIクライアントの読み込み
# =================================================================
# (このセクションは前回から変更なし)
DATABASE_URL = os.getenv('DATABASE_URL')
# ... 他の環境変数 ...

# =================================================================
# 3. ヘルパー関数
# =================================================================

def get_db_connection():
    # ... (変更なし) ...
    pass

def call_dify_api(prompt, user_id, conversation_id=None, stream=False):
    # ... (streamパラメータに対応) ...
    pass

def search_database_for_context(keywords):
    # ... (変更なし) ...
    pass

# (その他のヘルパー関数)

# =================================================================
# 4. APIエンドポイント
# =================================================================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# ▼▼▼ 最重要：ストリーミング応答に対応したチャットロジック ▼▼▼
@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    # ... (入力チェック) ...

    def generate_response():
        # --- ステップ1: キーワード抽出 ---
        # ... (前回と同じロジック) ...
        
        # --- ステップ2: DB検索 ---
        # ... (前回と同じロジック) ...

        # --- ステップ3: 最終プロンプト作成 ---
        # ... (前回と同じロジック) ...

        # --- ステップ4: Dify APIにストリーミング要求 ---
        full_response_text = ""
        try:
            dify_response = call_dify_api(final_prompt, user_id, conversation_id, stream=True)
            if dify_response:
                for chunk in dify_response.iter_content(chunk_size=None):
                    if chunk:
                        # SSE (Server-Sent Events) 形式でデータを送出
                        sse_data = chunk.decode('utf-8')
                        # Difyのストリーミング形式をパースして中身だけ取り出す
                        for line in sse_data.split('\n'):
                            if line.startswith('data: '):
                                content = line[6:]
                                try:
                                    json_content = json.loads(content)
                                    answer_chunk = json_content.get('answer', '')
                                    full_response_text += answer_chunk
                                    yield f"data: {json.dumps({'text': answer_chunk})}\n\n"
                                except json.JSONDecodeError:
                                    pass # pingやメタデータは無視
                
                # --- ステップ5: ストリーム終了後、完全な会話をDBに保存 ---
                conn = get_db_connection()
                # ... (DB保存ロジック) ...

            else:
                 yield f"data: {json.dumps({'text': 'AIサービスへの接続に失敗しました。'})}\n\n"

        except Exception as e:
            logger.error(f"ストリーミング処理中にエラー: {e}")
            yield f"data: {json.dumps({'text': f'エラーが発生しました: {e}'})}\n\n"

    # ストリーミング応答としてレスポンスを返す
    return Response(generate_response(), mimetype='text/event-stream')

# ... (他のAPIエンドポイントやWebhookハンドラは変更なし) ...

# =================================================================
# 5. アプリ起動
# =================================================================
if __name__ == '__main__':
    # ... (変更なし) ...
    pass
