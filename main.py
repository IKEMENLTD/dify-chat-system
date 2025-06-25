import os
import json
import re
import requests
import psycopg2
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from psycopg2.extras import RealDictCursor
import logging
from functools import wraps
import time
import hashlib
import hmac
from urllib.parse import urlparse
import threading

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, FollowEvent, ImageMessage, FileMessage, TextSendMessage

# Supabase SDK
from supabase import create_client, Client

# =================================================================
# 1. 初期設定
# =================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.')
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_CONTENT_LENGTH', 16777216))

# CORS設定
allowed_origins = os.getenv('ALLOWED_ORIGINS', '*').split(',')
CORS(app, origins=allowed_origins)

# =================================================================
# 2. 環境変数とAPIクライアントの読み込み
# =================================================================
DATABASE_URL = os.getenv('DATABASE_URL')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_URL = os.getenv('DIFY_API_URL', 'https://api.dify.ai/v1')
SECRET_KEY = os.getenv('SECRET_KEY', 'your-super-secret-key')

# LINE設定
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

# Chatwork設定
CHATWORK_WEBHOOK_TOKEN = os.getenv('CHATWORK_WEBHOOK_TOKEN')
CHATWORK_API_TOKEN = os.getenv('CHATWORK_API_TOKEN')

# Supabase設定
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
SUPABASE_BUCKET_NAME = os.getenv('SUPABASE_BUCKET_NAME', 'chat-uploads')

# Claude API設定（Anthropic）
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', 'your-anthropic-api-key')

# APIクライアント初期化
line_bot_api = None
line_handler = None
supabase_client = None

if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    line_handler = WebhookHandler(LINE_CHANNEL_SECRET)

if SUPABASE_URL and SUPABASE_KEY:
    supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =================================================================
# 3. データベース初期化
# =================================================================
def init_database():
    """データベーステーブルを初期化"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.warning("データベース接続に失敗しました")
            return False
            
        cur = conn.cursor()
        
        # conversationsテーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                conversation_id VARCHAR(255),
                user_message TEXT NOT NULL,
                ai_response TEXT NOT NULL,
                keywords TEXT[],
                context_used TEXT,
                source_platform VARCHAR(50) DEFAULT 'web',
                response_time_ms INTEGER,
                satisfaction_rating INTEGER CHECK (satisfaction_rating >= 1 AND satisfaction_rating <= 5),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 基本インデックス作成
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at);
        """)
        
        # PostgreSQL拡張とインデックス（エラー時はスキップ）
        try:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_keywords ON conversations USING GIN(keywords);
            """)
        except Exception as gin_error:
            logger.warning(f"GINインデックス作成をスキップ: {gin_error}")
            
        try:
            # 日本語全文検索用（オプション）
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_search 
                ON conversations USING GIN(to_tsvector('english', user_message || ' ' || ai_response));
            """)
        except Exception as fts_error:
            logger.warning(f"全文検索インデックス作成をスキップ: {fts_error}")
        
        conn.commit()
        cur.close()
        conn.close()
        logger.info("データベース初期化完了")
        return True
        
    except Exception as e:
        logger.error(f"データベース初期化エラー: {e}")
        return False

# =================================================================
# 4. ヘルパー関数
# =================================================================
def get_db_connection():
    """データベース接続を取得"""
    try:
        if not DATABASE_URL:
            logger.error("DATABASE_URL が設定されていません")
            return None
            
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"データベース接続エラー: {e}")
        return None

def get_available_claude_model():
    """利用可能なClaudeモデルを取得"""
    # 2025年6月現在利用可能なモデル（優先順位順）
    models = [
        "claude-3-5-sonnet-20241022",  # Claude 3.5 Sonnet (安定版)
        "claude-3-5-haiku-20241022",   # Claude 3.5 Haiku (高速)
        "claude-3-opus-20240229",      # Claude 3 Opus (高性能)
        "claude-3-sonnet-20240229",    # Claude 3 Sonnet (バランス)
        "claude-3-haiku-20240307"      # Claude 3 Haiku (高速)
    ]
    return models[0]  # 現在最も安定したモデルを返す

def get_claude4_model():
    """Claude 4モデルを取得（利用可能な場合）"""
    # Claude 4は段階的リリース中のため、フォールバック付きで実装
    claude4_models = [
        "claude-4-sonnet-20250514",   # Claude Sonnet 4 (推測されるモデル名)
        "claude-4-opus-20250514",     # Claude Opus 4 (推測されるモデル名)
        "claude-sonnet-4",            # 可能性のある省略形
        "claude-opus-4"               # 可能性のある省略形
    ]
    return claude4_models[0]

def extract_keywords_with_ai(message):
    """Claude APIを使ってメッセージからキーワードを抽出"""
    try:
        # APIキーが設定されていない場合はフォールバック
        if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == 'your-anthropic-api-key':
            logger.warning("ANTHROPIC_API_KEYが設定されていません。フォールバック処理を使用します。")
            return extract_keywords_fallback(message)
            
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01'
        }
        
        prompt = f"""
以下のユーザーメッセージから、データベース検索に使用するキーワードを抽出してください。
重要な単語、固有名詞、技術用語、製品名、会社名などを重視してください。

ユーザーメッセージ: {message}

抽出したキーワードをJSON形式で返してください。例：
{{"keywords": ["キーワード1", "キーワード2", "キーワード3"]}}

レスポンスはJSONのみで、説明文は不要です。
"""

        # Claude 4を試行、失敗時は Claude 3.5にフォールバック
        models_to_try = [
            "claude-4-sonnet-20250514",    # Claude Sonnet 4 (最新)
            "claude-3-5-sonnet-20241022"   # Claude 3.5 Sonnet (フォールバック)
        ]
        
        for model in models_to_try:
            try:
                data = {
                    "model": model,
                    "max_tokens": 300,
                    "temperature": 0.1,  # 一貫性重視
                    "messages": [
                        {
                            "role": "user", 
                            "content": prompt
                        }
                    ]
                }
                
                response = requests.post(
                    'https://api.anthropic.com/v1/messages',
                    headers=headers,
                    json=data,
                    timeout=15
                )
                
                if response.status_code == 200:
                    result = response.json()
                    content = result['content'][0]['text']
                    
                    # JSONを抽出
                    try:
                        keywords_data = json.loads(content)
                        logger.info(f"キーワード抽出成功 (モデル: {model})")
                        return keywords_data.get('keywords', [])
                    except json.JSONDecodeError:
                        # JSONパースに失敗した場合、正規表現でキーワードを抽出
                        matches = re.findall(r'"([^"]+)"', content)
                        return matches[:5]  # 最大5個
                elif response.status_code == 404:
                    # モデルが見つからない場合、次のモデルを試行
                    logger.warning(f"モデル {model} が利用できません。次のモデルを試行中...")
                    continue
                else:
                    logger.warning(f"Claude API エラー: {response.status_code} (モデル: {model})")
                    continue
                    
            except Exception as model_error:
                logger.warning(f"モデル {model} でエラー: {model_error}")
                continue
        
        # 全てのモデルで失敗した場合
        logger.warning("全てのClaudeモデルで失敗。フォールバック処理を使用")
        return extract_keywords_fallback(message)
            
    except Exception as e:
        logger.error(f"キーワード抽出エラー: {e}")
        return extract_keywords_fallback(message)

def extract_keywords_fallback(message):
    """フォールバック用キーワード抽出"""
    # 基本的な日本語キーワード抽出
    import re
    
    # カタカナ、ひらがな、漢字、英数字の組み合わせ
    keywords = re.findall(r'[ァ-ヶー]+|[ぁ-ん]+|[一-龯]+|[A-Za-z0-9]+', message)
    
    # 長さでフィルタリング（2文字以上）
    keywords = [k for k in keywords if len(k) >= 2]
    
    # ストップワード除去
    stop_words = ['です', 'ます', 'した', 'ある', 'いる', 'する', 'なる', 'れる', 'られる', 'せる', 'させる']
    keywords = [k for k in keywords if k not in stop_words]
    
    return keywords[:5]

def search_database_for_context(keywords, user_id, limit=5):
    """キーワードを使ってデータベースから関連する会話を検索"""
    try:
        conn = get_db_connection()
        if not conn:
            return []
            
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if not keywords:
            # キーワードがない場合は最新の会話を返す（両テーブルから）
            try:
                # conversationsテーブルから
                cur.execute("""
                    SELECT user_message, ai_response, created_at, keywords, 'conversations' as source
                    FROM conversations 
                    ORDER BY created_at DESC 
                    LIMIT %s
                """, (limit//2,))
                conv_results = cur.fetchall()
                
                # external_chat_logsテーブルから
                cur.execute("""
                    SELECT message as user_message, raw_data as ai_response, created_at, 
                           NULL as keywords, 'external_chat_logs' as source
                    FROM external_chat_logs 
                    ORDER BY created_at DESC 
                    LIMIT %s
                """, (limit//2,))
                ext_results = cur.fetchall()
                
                all_results = [dict(row) for row in conv_results] + [dict(row) for row in ext_results]
                cur.close()
                conn.close()
                return sorted(all_results, key=lambda x: x['created_at'], reverse=True)[:limit]
            except Exception as e:
                logger.error(f"初期検索エラー: {e}")
                cur.close()
                conn.close()
                return []
        
        # 複数の検索方法を組み合わせる
        all_results = []
        
        # 1. conversationsテーブルでの検索
        try:
            # キーワード配列検索
            query1 = """
                SELECT user_message, ai_response, created_at, keywords, 1 as priority, 'conversations' as source
                FROM conversations 
                WHERE keywords && %s
                ORDER BY created_at DESC 
                LIMIT %s
            """
            cur.execute(query1, (keywords, limit))
            conv_array_results = cur.fetchall()
            all_results.extend([dict(row) for row in conv_array_results])
            logger.info(f"conversations配列検索で {len(conv_array_results)} 件見つかりました")
        except Exception as e:
            logger.warning(f"conversations配列検索エラー: {e}")
        
        # 2. external_chat_logsテーブルでの検索
        try:
            search_conditions = []
            search_params = []
            
            for keyword in keywords[:3]:
                search_conditions.append("(message ILIKE %s OR raw_data::text ILIKE %s)")
                search_params.extend([f'%{keyword}%', f'%{keyword}%'])
            
            if search_conditions:
                query2 = f"""
                    SELECT message as user_message, raw_data as ai_response, created_at, 
                           NULL as keywords, 1 as priority, 'external_chat_logs' as source
                    FROM external_chat_logs 
                    WHERE ({' OR '.join(search_conditions)})
                    ORDER BY created_at DESC 
                    LIMIT %s
                """
                search_params.append(limit)
                cur.execute(query2, search_params)
                ext_results = cur.fetchall()
                all_results.extend([dict(row) for row in ext_results])
                logger.info(f"external_chat_logs検索で {len(ext_results)} 件見つかりました")
        except Exception as e:
            logger.warning(f"external_chat_logs検索エラー: {e}")
        
        # 3. conversationsテーブルでのLIKE検索（フォールバック）
        try:
            search_conditions = []
            search_params = []
            
            for keyword in keywords[:3]:
                search_conditions.append("(user_message ILIKE %s OR ai_response ILIKE %s)")
                search_params.extend([f'%{keyword}%', f'%{keyword}%'])
            
            if search_conditions:
                query3 = f"""
                    SELECT user_message, ai_response, created_at, keywords, 2 as priority, 'conversations' as source
                    FROM conversations 
                    WHERE ({' OR '.join(search_conditions)})
                    AND NOT (keywords && %s)  -- 重複を避ける
                    ORDER BY created_at DESC 
                    LIMIT %s
                """
                search_params.extend([keywords, limit])
                cur.execute(query3, search_params)
                conv_like_results = cur.fetchall()
                all_results.extend([dict(row) for row in conv_like_results])
                logger.info(f"conversationsLIKE検索で {len(conv_like_results)} 件見つかりました")
        except Exception as e:
            logger.warning(f"conversationsLIKE検索エラー: {e}")
        
        cur.close()
        conn.close()
        
        # 重複を除去し、優先度でソート
        seen = set()
        unique_results = []
        for result in all_results:
            # raw_dataがJSONの場合は文字列として扱う
            ai_response = result.get('ai_response', '')
            if isinstance(ai_response, dict):
                ai_response = str(ai_response)
            result['ai_response'] = ai_response
            
            key = (result['user_message'], result['created_at'])
            if key not in seen:
                seen.add(key)
                unique_results.append(result)
        
        # 優先度と日時でソート
        unique_results.sort(key=lambda x: (x.get('priority', 999), x['created_at']), reverse=True)
        
        logger.info(f"最終的に {len(unique_results)} 件の結果を返します")
        return unique_results[:limit]
        
    except Exception as e:
        logger.error(f"データベース検索エラー: {e}")
        return []

def generate_ai_response_with_context(user_message, context_data, user_id):
    """文脈情報を使ってAI回答を生成"""
    try:
        # APIキーが設定されていない場合はフォールバック
        if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == 'your-anthropic-api-key':
            logger.warning("ANTHROPIC_API_KEYが設定されていません。基本的な回答を返します。")
            return generate_fallback_response(user_message, context_data)
            
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01'
        }
        
        # 文脈情報をフォーマット
        context_text = ""
        if context_data:
            context_text = "\n\n【参考情報（過去の会話から）】\n"
            for i, item in enumerate(context_data, 1):
                context_text += f"{i}. {item['created_at'].strftime('%Y-%m-%d')}: {item['user_message'][:100]}...\n"
                context_text += f"   回答: {item['ai_response'][:200]}...\n\n"
        
        prompt = f"""
あなたは優秀なAIアシスタントです。ユーザーの質問に対して、過去の会話履歴を参考にしながら、適切で役立つ回答をしてください。

ユーザーの質問: {user_message}

{context_text}

回答の指針:
- 過去の情報が関連している場合は、それを参考にして回答してください
- URL、ファイル名、具体的な情報が過去にあった場合は、それを含めて回答してください
- 過去の情報が関連していない場合は、一般的な知識で回答してください
- 親切で分かりやすい文章で回答してください
- 必要に応じてMarkdown形式を使用してください

回答:"""

        # Claude 4を優先的に試行、失敗時はフォールバック
        models_to_try = [
            {
                "model": "claude-4-sonnet-20250514",  # Claude Sonnet 4
                "max_tokens": 8000,
                "temperature": 0.3
            },
            {
                "model": "claude-4-opus-20250514",    # Claude Opus 4
                "max_tokens": 8000,
                "temperature": 0.2
            },
            {
                "model": "claude-3-5-sonnet-20241022", # Claude 3.5 Sonnet (フォールバック)
                "max_tokens": 4000,
                "temperature": 0.3
            }
        ]
        
        for model_config in models_to_try:
            try:
                data = {
                    "model": model_config["model"],
                    "max_tokens": model_config["max_tokens"],
                    "temperature": model_config["temperature"],
                    "messages": [
                        {
                            "role": "user", 
                            "content": prompt
                        }
                    ]
                }
                
                response = requests.post(
                    'https://api.anthropic.com/v1/messages',
                    headers=headers,
                    json=data,
                    timeout=60  # Claude 4は処理時間が長い可能性
                )
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"AI回答生成成功 (モデル: {model_config['model']})")
                    return result['content'][0]['text']
                elif response.status_code == 404:
                    # モデルが見つからない場合、次のモデルを試行
                    logger.warning(f"モデル {model_config['model']} が利用できません。次のモデルを試行中...")
                    continue
                else:
                    logger.warning(f"Claude API エラー: {response.status_code} (モデル: {model_config['model']})")
                    continue
                    
            except Exception as model_error:
                logger.warning(f"モデル {model_config['model']} でエラー: {model_error}")
                continue
        
        # 全てのモデルで失敗した場合
        logger.error("全てのClaudeモデルで失敗。フォールバック回答を生成")
        return generate_fallback_response(user_message, context_data)
            
    except Exception as e:
        logger.error(f"AI回答生成エラー: {e}")
        return generate_fallback_response(user_message, context_data)

def generate_fallback_response(user_message, context_data):
    """APIが利用できない場合のフォールバック回答"""
    if context_data:
        response = f"お探しの情報について、過去の会話から関連する内容を見つけました：\n\n"
        for i, item in enumerate(context_data[:2], 1):
            response += f"**{i}. {item['created_at'].strftime('%Y年%m月%d日')}の会話**\n"
            response += f"質問: {item['user_message'][:100]}...\n"
            response += f"回答: {item['ai_response'][:200]}...\n\n"
        response += "詳細な情報については、ANTHROPIC_API_KEYを設定してClaude APIを有効にしてください。"
    else:
        response = f"""
申し訳ございませんが、現在AIサービスが利用できません。

**お問い合わせ内容**: {user_message}

基本的な対応方法：
1. ANTHROPIC_API_KEYが正しく設定されているか確認してください
2. しばらく時間をおいてから再度お試しください
3. 詳細なサポートが必要な場合は管理者にお問い合わせください

過去の会話履歴からの関連情報は見つかりませんでした。
"""
    return response

def save_conversation_to_db(user_id, conversation_id, user_message, ai_response, keywords, context_used, response_time_ms, source_platform='web'):
    """会話をデータベースに保存"""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        cur = conn.cursor()
        
        # context_usedのdatetime型をstring型に変換
        context_used_json = None
        if context_used:
            # datetime型を文字列に変換
            for item in context_used:
                if isinstance(item.get('created_at'), datetime):
                    item['created_at'] = item['created_at'].isoformat()
            context_used_json = json.dumps(context_used, ensure_ascii=False)
        
        query = """
            INSERT INTO conversations 
            (user_id, conversation_id, user_message, ai_response, keywords, context_used, response_time_ms, source_platform, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        cur.execute(query, (
            user_id,
            conversation_id,
            user_message,
            ai_response,
            keywords,
            context_used_json,
            response_time_ms,
            source_platform,
            datetime.now()
        ))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
        
    except Exception as e:
        logger.error(f"会話保存エラー: {e}")
        return False

def rate_limit(max_requests=10, window_seconds=60):
    """レート制限デコレータ"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 簡単な実装（実際にはRedisを使うべき）
            return func(*args, **kwargs)
        return wrapper
    return decorator

# =================================================================
# 5. Webアプリケーションルート
# =================================================================
@app.route('/')
def index():
    """メインページ"""
    return send_from_directory('.', 'index.html')

@app.route('/dashboard')
def dashboard():
    """ダッシュボードページ"""
    return send_from_directory('.', 'dashboard.html')

@app.route('/health')
def health():
    """ヘルスチェック"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': 'connected' if get_db_connection() else 'disconnected'
    })

# =================================================================
# 6. チャットAPI（メイン機能）
# =================================================================
@app.route('/api/chat', methods=['POST'])
@rate_limit(max_requests=10, window_seconds=60)
def chat():
    """ストリーミング対応チャットAPI"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': '無効なリクエストです'}), 400

        user_id = data.get('user_id')
        user_message = data.get('message', '').strip()
        conversation_id = data.get('conversation_id')

        if not user_id or not user_message:
            return jsonify({'error': 'user_idとmessageは必須です'}), 400

        def generate_response():
            start_time = time.time()
            
            try:
                # ステップ1: キーワード抽出
                yield f"data: {json.dumps({'text': ''})}\n\n"  # 初期化
                
                keywords = extract_keywords_with_ai(user_message)
                logger.info(f"抽出されたキーワード: {keywords}")

                # ステップ2: データベース検索
                context_data = search_database_for_context(keywords, user_id)
                logger.info(f"検索された文脈データ: {len(context_data)}件")

                # ステップ3: AI回答生成（ストリーミング対応）
                full_response = generate_ai_response_with_context(user_message, context_data, user_id)
                
                # 文字ごとにストリーミング送信
                for char in full_response:
                    yield f"data: {json.dumps({'text': char})}\n\n"
                    time.sleep(0.01)  # 少し遅延を入れてストリーミング感を演出

                # ステップ4: データベースに保存
                response_time_ms = int((time.time() - start_time) * 1000)
                save_conversation_to_db(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    user_message=user_message,
                    ai_response=full_response,
                    keywords=keywords,
                    context_used=context_data,
                    response_time_ms=response_time_ms,
                    source_platform='web'
                )

                # ストリーム終了通知
                yield f"data: {json.dumps({'text': '', 'done': True})}\n\n"

            except Exception as e:
                logger.error(f"チャット処理エラー: {e}")
                error_message = f"エラーが発生しました: {str(e)}"
                yield f"data: {json.dumps({'text': error_message, 'error': True})}\n\n"

        return Response(generate_response(), mimetype='text/event-stream')

    except Exception as e:
        logger.error(f"チャットAPIエラー: {e}")
        return jsonify({'error': str(e)}), 500

# =================================================================
# 7. 統計API
# =================================================================
@app.route('/api/stats')
def get_stats():
    """統計情報を取得"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'データベース接続エラー'}), 500

        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 基本統計
        cur.execute("""
            SELECT 
                COUNT(*) as total_conversations,
                COUNT(DISTINCT user_id) as unique_users,
                AVG(response_time_ms) as avg_response_time,
                AVG(satisfaction_rating) * 20 as satisfaction_rate
            FROM conversations 
            WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
        """)
        basic_stats = dict(cur.fetchone())
        
        # 日別統計
        cur.execute("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as conversations
            FROM conversations 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """)
        daily_stats = [dict(row) for row in cur.fetchall()]
        
        # 時間別統計
        cur.execute("""
            SELECT 
                EXTRACT(HOUR FROM created_at) as hour,
                COUNT(*) as conversations
            FROM conversations 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY EXTRACT(HOUR FROM created_at)
            ORDER BY hour
        """)
        hourly_stats = [dict(row) for row in cur.fetchall()]
        
        cur.close()
        conn.close()
        
        return jsonify({
            'basic_stats': basic_stats,
            'daily_stats': daily_stats,
            'hourly_stats': hourly_stats
        })
        
    except Exception as e:
        logger.error(f"統計取得エラー: {e}")
        return jsonify({'error': str(e)}), 500

# =================================================================
# 8. LINE Webhook
# =================================================================
@app.route('/webhook/line', methods=['POST'])
@app.route('/api/line/webhook', methods=['POST'])  # 追加パス
def line_webhook():
    """LINE Webhook"""
    if not line_handler:
        return 'LINE not configured', 400

    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("LINE Webhook signature verification failed")
        return 'Invalid signature', 400
    except Exception as e:
        logger.error(f"LINE Webhook error: {e}")
        return 'Error', 500

    return 'OK'

@line_handler.add(MessageEvent, message=TextMessage)
def handle_line_message(event):
    """LINEメッセージハンドラ"""
    try:
        user_id = f"line_{event.source.user_id}"
        user_message = event.message.text
        
        # キーワード抽出
        keywords = extract_keywords_with_ai(user_message)
        
        # データベース検索
        context_data = search_database_for_context(keywords, user_id)
        
        # AI回答生成
        ai_response = generate_ai_response_with_context(user_message, context_data, user_id)
        
        # データベースに保存
        save_conversation_to_db(
            user_id=user_id,
            conversation_id=None,
            user_message=user_message,
            ai_response=ai_response,
            keywords=keywords,
            context_used=context_data,
            response_time_ms=0,
            source_platform='line'
        )
        
        # LINE返信
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=ai_response)
        )
        
    except Exception as e:
        logger.error(f"LINE メッセージ処理エラー: {e}")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="申し訳ございません。エラーが発生しました。")
        )

# =================================================================
# 9. Chatwork Webhook
# =================================================================
@app.route('/webhook/chatwork', methods=['POST'])
def chatwork_webhook():
    """Chatwork Webhook"""
    try:
        if not CHATWORK_WEBHOOK_TOKEN:
            return 'Chatwork not configured', 400
            
        # Webhook認証
        webhook_token = request.headers.get('X-ChatWorkWebhookToken')
        if webhook_token != CHATWORK_WEBHOOK_TOKEN:
            return 'Unauthorized', 401
            
        data = request.get_json()
        if not data:
            return 'No data', 400
            
        # メッセージ処理
        webhook_event = data.get('webhook_event')
        if webhook_event and webhook_event.get('type') == 'mention_to_me':
            body = webhook_event.get('body', '')
            account_id = webhook_event.get('from_account_id')
            room_id = webhook_event.get('room_id')
            
            user_id = f"chatwork_{account_id}"
            
            # AIが言及されている場合のみ処理
            if '[To:AI]' in body or 'AI' in body:
                # キーワード抽出
                keywords = extract_keywords_with_ai(body)
                
                # データベース検索
                context_data = search_database_for_context(keywords, user_id, limit=10)  # より多くの結果を取得
                
                # AI回答生成
                ai_response = generate_ai_response_with_context(body, context_data, user_id)
                
                # データベースに保存
                save_conversation_to_db(
                    user_id=user_id,
                    conversation_id=str(room_id),
                    user_message=body,
                    ai_response=ai_response,
                    keywords=keywords,
                    context_used=context_data,
                    response_time_ms=0,
                    source_platform='chatwork'
                )
                
                # Chatworkに返信
                chatwork_url = f"https://api.chatwork.com/v2/rooms/{room_id}/messages"
                chatwork_headers = {
                    'X-ChatWorkToken': CHATWORK_API_TOKEN,
                    'Content-Type': 'application/x-www-form-urlencoded'
                }
                chatwork_data = {'body': ai_response}
                
                requests.post(chatwork_url, headers=chatwork_headers, data=chatwork_data)
        
        return 'OK'
        
    except Exception as e:
        logger.error(f"Chatwork Webhook エラー: {e}")
        return 'Error', 500

# =================================================================
# 11. デバッグ・管理用API
# =================================================================
@app.route('/api/debug/conversations')
def debug_conversations():
    """デバッグ用：データベース内の会話を確認"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'データベース接続エラー'}), 500
            
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # conversationsテーブルから取得
        cur.execute("""
            SELECT id, user_id, user_message, ai_response, keywords, created_at, 'conversations' as source
            FROM conversations 
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        conversations = [dict(row) for row in cur.fetchall()]
        
        # external_chat_logsテーブルから取得
        cur.execute("""
            SELECT id, user_id, user_name, message, raw_data, created_at, 'external_chat_logs' as source
            FROM external_chat_logs 
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        external_logs = [dict(row) for row in cur.fetchall()]
        
        # 件数も取得
        cur.execute("SELECT COUNT(*) as total FROM conversations")
        conv_total = cur.fetchone()['total']
        
        cur.execute("SELECT COUNT(*) as total FROM external_chat_logs")
        ext_total = cur.fetchone()['total']
        
        cur.close()
        conn.close()
        
        return jsonify({
            'conversations_table': {
                'total': conv_total,
                'recent': conversations
            },
            'external_chat_logs_table': {
                'total': ext_total,
                'recent': external_logs
            }
        })
        
    except Exception as e:
        logger.error(f"デバッグ取得エラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/search/<query>')
def debug_search(query):
    """デバッグ用：検索処理の詳細を確認"""
    try:
        # キーワード抽出
        keywords = extract_keywords_with_ai(query)
        logger.info(f"デバッグ - 抽出キーワード: {keywords}")
        
        # データベース検索
        user_id = "debug_user"  # デバッグ用
        context_data = search_database_for_context(keywords, user_id, limit=10)
        
        return jsonify({
            'query': query,
            'extracted_keywords': keywords,
            'search_results_count': len(context_data),
            'search_results': context_data
        })
        
    except Exception as e:
        logger.error(f"デバッグ検索エラー: {e}")
        return jsonify({'error': str(e)}), 500
def create_app():
    """アプリケーションファクトリ"""
    # データベース初期化
    init_database()
    logger.info("アプリケーション初期化完了")
    return app

# アプリケーション初期化（本番環境用）
with app.app_context():
    init_database()

if __name__ == '__main__':
    # 環境に応じた設定
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'
    host = os.getenv('HOST', '0.0.0.0')
    
    logger.info(f"アプリケーションを起動中... Port: {port}, Debug: {debug}")
    
    # 開発環境では追加の初期化
    if debug:
        init_database()
    
    app.run(host=host, port=port, debug=debug)

# Gunicorn用のアプリケーションオブジェクト
application = app
