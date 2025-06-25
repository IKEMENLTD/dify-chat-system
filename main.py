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
    """完璧検索システムのメインエントリーポイント"""
    try:
        # 完璧検索を使用
        results = search_database_for_context_perfect(keywords, user_id, limit)
        
        if results:
            logger.info(f"完璧検索成功: {len(results)} 件")
            # スコア情報をログ出力（デバッグ用）
            for i, result in enumerate(results[:3]):
                score = result.get('final_score', 0)
                msg_preview = (result.get('user_message', '') or '')[:30]
                logger.info(f"結果{i+1}(スコア:{score:.2f}): {msg_preview}...")
            return results
        else:
            # フォールバック：基本検索
            logger.warning("完璧検索で結果なし、基本検索にフォールバック")
            return search_database_basic_fallback(keywords, user_id, limit)
            
    except Exception as e:
        logger.error(f"完璧検索システムエラー: {e}")
        # 必ずフォールバック検索を実行
        return search_database_basic_fallback(keywords, user_id, limit)

def search_database_basic_fallback(keywords, user_id, limit=5):
    """基本検索（絶対に失敗しないフォールバック）"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("データベース接続失敗")
            return []
            
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # キーワード処理
        if isinstance(keywords, list):
            search_terms = [str(k) for k in keywords if k]
        elif isinstance(keywords, dict):
            search_terms = [str(k) for k in keywords.get('primary_keywords', []) if k]
        else:
            search_terms = [str(keywords)] if keywords else []
        
        if not search_terms:
            # キーワードがない場合は最新のデータを返す
            cur.execute("""
                SELECT 
                    message as user_message, 
                    raw_data::text as ai_response, 
                    created_at, 
                    user_name,
                    'external_chat_logs' as source
                FROM external_chat_logs 
                WHERE message IS NOT NULL
                ORDER BY created_at DESC 
                LIMIT %s
            """, (limit,))
            
            results = [dict(row) for row in cur.fetchall()]
            cur.close()
            conn.close()
            logger.info(f"基本検索（最新データ）: {len(results)} 件")
            return results
        
        # キーワード検索
        search_conditions = []
        search_params = []
        
        for term in search_terms[:5]:  # 最大5個のキーワード
            if len(term.strip()) >= 2:  # 2文字以上のキーワードのみ
                search_conditions.append("(message ILIKE %s OR raw_data::text ILIKE %s)")
                search_params.extend([f'%{term}%', f'%{term}%'])
        
        if not search_conditions:
            # 有効なキーワードがない場合
            cur.close()
            conn.close()
            return []
        
        query = f"""
            SELECT 
                message as user_message, 
                raw_data::text as ai_response, 
                created_at, 
                user_name,
                'external_chat_logs' as source
            FROM external_chat_logs 
            WHERE ({' OR '.join(search_conditions)})
            AND message IS NOT NULL
            ORDER BY created_at DESC 
            LIMIT %s
        """
        
        search_params.append(limit * 2)  # 余裕を持って取得
        
        cur.execute(query, search_params)
        results = [dict(row) for row in cur.fetchall()]
        
        cur.close()
        conn.close()
        
        # 重複除去
        unique_results = []
        seen_messages = set()
        
        for result in results:
            message = result.get('user_message', '') or ''
            message_key = message[:50]  # 最初の50文字
            
            if message_key and message_key not in seen_messages:
                seen_messages.add(message_key)
                unique_results.append(result)
        
        final_results = unique_results[:limit]
        logger.info(f"基本検索成功: {len(final_results)} 件")
        return final_results
        
    except Exception as e:
        logger.error(f"基本検索エラー: {e}")
        # 最後の手段：空の結果を返す
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
            context_text = "\n\n【過去の会話から見つかった関連情報】\n"
            for i, item in enumerate(context_data, 1):
                created_at = item.get('created_at', 'Unknown')
                if hasattr(created_at, 'strftime'):
                    date_str = created_at.strftime('%Y-%m-%d %H:%M')
                else:
                    date_str = str(created_at)[:16]  # 文字列の場合は最初の16文字
                
                user_msg = item.get('user_message', '') or ''
                ai_resp = item.get('ai_response', '') or ''
                
                context_text += f"【情報{i}】({date_str})\n"
                context_text += f"質問: {user_msg[:150]}...\n"
                context_text += f"内容: {ai_resp[:300]}...\n\n"
        
        prompt = f"""
あなたは優秀なAIアシスタントです。ユーザーの質問に対して、過去の会話履歴から見つかった具体的な情報を最大限活用して回答してください。

ユーザーの質問: {user_message}

{context_text}

重要な指針:
1. **具体的な情報を優先**: URL、ファイル名、日付、場所などの具体的な情報があれば必ず含める
2. **過去の情報を活用**: 見つかった過去の会話から関連する具体的な内容を抽出して回答に含める
3. **直接的な回答**: 一般論ではなく、実際に見つかった情報を使って具体的に答える
4. **URL抽出**: 過去のデータにURLやリンクがあれば必ず表示する
5. **ファイル情報**: ファイル名、保存場所、作成日などがあれば明記する
6. **見つからない場合のみ**: 本当に関連情報が見つからない場合のみ一般的な回答をする

過去のデータに具体的な情報（URL、ファイル、場所など）がある場合は、それを最優先で回答に含めてください。

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

@app.route('/api/debug/ultimate-search/<query>')
def debug_ultimate_search(query):
    """究極検索システムのデバッグ"""
    try:
        user_id = "debug_user"
        
        # 究極検索実行
        results = search_database_for_context_ultimate(query, user_id, limit=10)
        
        # 各結果の詳細スコア情報
        detailed_results = []
        for i, result in enumerate(results):
            detailed_result = {
                'rank': i + 1,
                'user_message': result.get('user_message', '')[:100],
                'ai_response': result.get('ai_response', '')[:200],
                'created_at': str(result.get('created_at', '')),
                'source': result.get('source', ''),
                'scores': {
                    'relevance_score': result.get('relevance_score', 0.0),
                    'semantic_score': result.get('semantic_score', 0.0),
                    'ngram_score': result.get('ngram_score', 0.0),
                    'temporal_score': result.get('temporal_score', 0.0),
                    'personalization_boost': result.get('personalization_boost', 0.0)
                }
            }
            detailed_results.append(detailed_result)
        
        return jsonify({
            'query': query,
            'total_results': len(results),
            'results': detailed_results,
            'system_status': {
                'ultimate_search_enabled': True,
                'semantic_engine_initialized': hasattr(ultimate_search_engine, 'semantic_engine'),
                'ngram_engine_initialized': hasattr(ultimate_search_engine, 'ngram_engine')
            }
        })
        
    except Exception as e:
        logger.error(f"究極検索デバッグエラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/search-analytics/<user_id>')
def debug_search_analytics(user_id):
    """ユーザーの検索分析情報"""
    try:
        # ユーザー行動パターン取得
        preferences = ultimate_search_engine.behavior_learner.get_user_preferences(user_id)
        
        return jsonify({
            'user_id': user_id,
            'search_patterns': preferences,
            'behavior_data': {
                'total_searches': preferences.get('search_count', 0),
                'frequent_keywords': preferences.get('frequent_words', {}),
                'preferred_content_types': preferences.get('preferred_content_types', {}),
                'active_hours': preferences.get('active_hours', [])
            }
        })
        
    except Exception as e:
        logger.error(f"検索分析デバッグエラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/search/feedback', methods=['POST'])
def record_search_feedback():
    """検索フィードバックを記録"""
    try:
        data = request.json
        user_id = data.get('user_id')
        query = data.get('query')
        document_id = data.get('document_id')
        clicked = data.get('clicked', True)
        
        # フィードバックを機械学習エンジンに記録
        ultimate_search_engine.ml_engine.record_user_feedback(
            user_id, query, document_id, clicked
        )
        
        return jsonify({'success': True, 'message': 'フィードバックを記録しました'})
        
    except Exception as e:
        logger.error(f"フィードバック記録エラー: {e}")
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
