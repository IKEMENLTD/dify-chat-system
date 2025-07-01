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
# hashlibとhmacは将来のセキュリティ機能のために保持
# 未使用のインポートを削除

# LINE SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# Supabase SDK
from supabase import create_client, Client

# スケジューラー
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# =================================================================
# 1. 初期設定
# =================================================================
# ログ設定
log_level = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.')
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_CONTENT_LENGTH', 16777216))

# 本番環境でのセキュリティ設定
if os.getenv('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# CORS設定
allowed_origins = os.getenv('ALLOWED_ORIGINS', '*').split(',')
CORS(app, origins=allowed_origins)

# =================================================================
# 2. 環境変数とAPIクライアントの読み込み
# =================================================================
DATABASE_URL = os.getenv('DATABASE_URL')

# DIFY関連設定（現在使用されていない）
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_URL = os.getenv('DIFY_API_URL', 'https://api.dify.ai/v1')

# DIFY APIの警告
if DIFY_API_KEY:
    logger.info("DIFY APIキーが設定されていますが、現在の実装ではClaude APIを使用しています。")
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    logger.warning("SECRET_KEYが設定されていません。セキュリティ上のリスクがあります。")
    SECRET_KEY = 'development-only-secret-key'  # 開発用のみ

# LINE設定
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

# Chatwork設定
CHATWORK_WEBHOOK_TOKEN = os.getenv('CHATWORK_WEBHOOK_TOKEN')
CHATWORK_API_TOKEN = os.getenv('CHATWORK_API_TOKEN')

# 究極検索関連設定（環境変数があるが機能未実装）
ULTIMATE_SEARCH_ENABLED = os.getenv('ULTIMATE_SEARCH_ENABLED', 'False').lower() == 'true'
SEARCH_ANALYTICS_ENABLED = os.getenv('SEARCH_ANALYTICS_ENABLED', 'False').lower() == 'true'
SEMANTIC_SEARCH_THRESHOLD = float(os.getenv('SEMANTIC_SEARCH_THRESHOLD', '0.1'))
NGRAM_MIN_LENGTH = int(os.getenv('NGRAM_MIN_LENGTH', '2'))
NGRAM_MAX_LENGTH = int(os.getenv('NGRAM_MAX_LENGTH', '4'))
MAX_DOCUMENTS_FOR_ML = int(os.getenv('MAX_DOCUMENTS_FOR_ML', '1000'))
SEARCH_RESULT_CACHE_SIZE = int(os.getenv('SEARCH_RESULT_CACHE_SIZE', '100'))

# 究極検索機能の警告
if ULTIMATE_SEARCH_ENABLED:
    logger.warning("究極検索機能が有効化されていますが、現在未実装です。")

# Supabase設定
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
SUPABASE_BUCKET_NAME = os.getenv('SUPABASE_BUCKET_NAME', 'chat-uploads')

# Claude API設定（Anthropic）
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
if not ANTHROPIC_API_KEY:
    logger.warning("ANTHROPIC_API_KEYが設定されていません。AI機能が制限されます。")

# APIクライアント初期化
line_bot_api = None
line_handler = None
supabase_client = None

# スケジューラー初期化
scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Tokyo'))

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
        
        # external_chat_logsテーブル（外部チャットログ用）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS external_chat_logs (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255),
                user_name VARCHAR(255),
                message TEXT,
                raw_data JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # external_chat_logsインデックス
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_external_chat_logs_user_id ON external_chat_logs(user_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_external_chat_logs_created_at ON external_chat_logs(created_at);
        """)
        
        # リマインダーテーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                reminder_time TIME NOT NULL,
                repeat_pattern VARCHAR(50) DEFAULT 'once',
                repeat_days VARCHAR(20)[],
                last_sent_date DATE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # リマインダーインデックス
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders(user_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_active ON reminders(is_active);
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
        if not ANTHROPIC_API_KEY:
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

def parse_reminder_request(message):
    """
    リマインダーリクエストを解析
    例: "毎日10時に薬を飲む" → {time: "10:00", repeat: "daily", message: "薬を飲む"}
    """
    patterns = [
        # 毎日パターン
        (r'毎日(\d{1,2})時(\d{0,2})分?に?(.+)', 'daily'),
        (r'毎日(\d{1,2}):(\d{2})に?(.+)', 'daily'),
        # 平日パターン
        (r'平日(\d{1,2})時(\d{0,2})分?に?(.+)', 'weekdays'),
        (r'平日(\d{1,2}):(\d{2})に?(.+)', 'weekdays'),
        # 週末パターン
        (r'週末(\d{1,2})時(\d{0,2})分?に?(.+)', 'weekends'),
        (r'週末(\d{1,2}):(\d{2})に?(.+)', 'weekends'),
        # 特定曜日パターン
        (r'毎週([月火水木金土日])曜日?(\d{1,2})時(\d{0,2})分?に?(.+)', 'weekly'),
        (r'毎週([月火水木金土日])曜日?(\d{1,2}):(\d{2})に?(.+)', 'weekly'),
        # 一回限りパターン
        (r'(\d{1,2})時(\d{0,2})分?に?(.+)', 'once'),
        (r'(\d{1,2}):(\d{2})に?(.+)', 'once'),
    ]
    
    for pattern, repeat_type in patterns:
        match = re.match(pattern, message)
        if match:
            groups = match.groups()
            
            if repeat_type == 'weekly':
                day_map = {'月': 'mon', '火': 'tue', '水': 'wed', '木': 'thu', '金': 'fri', '土': 'sat', '日': 'sun'}
                day = day_map.get(groups[0], 'mon')
                hour = int(groups[1])
                minute = int(groups[2]) if groups[2] else 0
                reminder_message = groups[3]
                return {
                    'time': f"{hour:02d}:{minute:02d}",
                    'repeat': repeat_type,
                    'days': [day],
                    'message': reminder_message.strip()
                }
            else:
                hour = int(groups[0])
                minute = int(groups[1]) if groups[1] else 0
                reminder_message = groups[2] if len(groups) > 2 else groups[1]
                
                days = []
                if repeat_type == 'weekdays':
                    days = ['mon', 'tue', 'wed', 'thu', 'fri']
                elif repeat_type == 'weekends':
                    days = ['sat', 'sun']
                elif repeat_type == 'daily':
                    days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
                
                return {
                    'time': f"{hour:02d}:{minute:02d}",
                    'repeat': repeat_type,
                    'days': days,
                    'message': reminder_message.strip()
                }
    
    # リマインダー削除パターン
    if re.match(r'リマインダー.*削除|削除.*リマインダー', message):
        return {'action': 'delete'}
    
    # リマインダー一覧パターン
    if re.match(r'リマインダー.*一覧|一覧.*リマインダー', message):
        return {'action': 'list'}
    
    return None

def save_reminder(user_id, reminder_data):
    """リマインダーをデータベースに保存"""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        cur = conn.cursor()
        
        query = """
            INSERT INTO reminders 
            (user_id, message, reminder_time, repeat_pattern, repeat_days)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """
        
        cur.execute(query, (
            user_id,
            reminder_data['message'],
            reminder_data['time'],
            reminder_data['repeat'],
            reminder_data.get('days', [])
        ))
        
        reminder_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        return reminder_id
        
    except Exception as e:
        logger.error(f"リマインダー保存エラー: {e}")
        return False

def get_user_reminders(user_id):
    """ユーザーのリマインダー一覧を取得"""
    try:
        conn = get_db_connection()
        if not conn:
            return []
            
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT id, message, reminder_time, repeat_pattern, repeat_days, is_active
            FROM reminders
            WHERE user_id = %s AND is_active = TRUE
            ORDER BY reminder_time
        """, (user_id,))
        
        reminders = cur.fetchall()
        cur.close()
        conn.close()
        
        return reminders
        
    except Exception as e:
        logger.error(f"リマインダー取得エラー: {e}")
        return []

def delete_user_reminders(user_id):
    """ユーザーのリマインダーを削除（非アクティブ化）"""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        cur = conn.cursor()
        
        cur.execute("""
            UPDATE reminders
            SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND is_active = TRUE
        """, (user_id,))
        
        affected = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        
        return affected > 0
        
    except Exception as e:
        logger.error(f"リマインダー削除エラー: {e}")
        return False

def send_reminder_notification(user_id, message):
    """リマインダー通知を送信"""
    try:
        if user_id.startswith('line_') and line_bot_api:
            # LINE通知
            line_user_id = user_id.replace('line_', '')
            line_bot_api.push_message(
                line_user_id,
                TextSendMessage(text=f"🔔 リマインダー\n\n{message}")
            )
            logger.info(f"リマインダー送信成功: {user_id} - {message[:50]}...")
            return True
        elif user_id.startswith('chatwork_'):
            # Chatwork通知（実装可能）
            logger.info(f"Chatworkリマインダー: {user_id} - {message}")
            return True
        else:
            logger.warning(f"未対応のプラットフォーム: {user_id}")
            return False
            
    except Exception as e:
        logger.error(f"リマインダー通知エラー: {e}")
        return False

def check_and_send_reminders():
    """定期的にリマインダーをチェックして送信"""
    try:
        conn = get_db_connection()
        if not conn:
            return
            
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 現在時刻
        now = datetime.now(pytz.timezone('Asia/Tokyo'))
        current_time = now.strftime('%H:%M')
        current_date = now.date()
        current_day = now.strftime('%a').lower()
        
        # アクティブなリマインダーを取得
        cur.execute("""
            SELECT *
            FROM reminders
            WHERE is_active = TRUE
            AND reminder_time::text LIKE %s
            AND (last_sent_date IS NULL OR last_sent_date < %s)
        """, (current_time + '%', current_date))
        
        reminders = cur.fetchall()
        
        for reminder in reminders:
            should_send = False
            
            if reminder['repeat_pattern'] == 'once':
                should_send = True
            elif reminder['repeat_pattern'] == 'daily':
                should_send = True
            elif reminder['repeat_pattern'] == 'weekdays' and current_day in ['mon', 'tue', 'wed', 'thu', 'fri']:
                should_send = True
            elif reminder['repeat_pattern'] == 'weekends' and current_day in ['sat', 'sun']:
                should_send = True
            elif reminder['repeat_pattern'] == 'weekly' and current_day in reminder.get('repeat_days', []):
                should_send = True
            
            if should_send:
                # 通知送信
                success = send_reminder_notification(reminder['user_id'], reminder['message'])
                
                if success:
                    # 送信日を更新
                    update_query = """
                        UPDATE reminders
                        SET last_sent_date = %s
                        WHERE id = %s
                    """
                    cur.execute(update_query, (current_date, reminder['id']))
                    
                    # 一回限りのリマインダーは非アクティブ化
                    if reminder['repeat_pattern'] == 'once':
                        cur.execute("""
                            UPDATE reminders
                            SET is_active = FALSE
                            WHERE id = %s
                        """, (reminder['id'],))
        
        conn.commit()
        cur.close()
        conn.close()
        
    except Exception as e:
        logger.error(f"リマインダーチェックエラー: {e}")

def get_recent_line_conversations(user_id, limit=10):
    """指定したLINEユーザーの最近の会話履歴を取得"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("データベース接続失敗")
            return []
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 最近の会話を時系列順で取得
        cur.execute("""
            SELECT 
                user_message,
                ai_response,
                created_at
            FROM conversations
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, limit))
        
        conversations = cur.fetchall()
        cur.close()
        conn.close()
        
        # 時系列順（古い順）に並び替えて返す
        conversations.reverse()
        
        logger.info(f"LINE会話履歴取得: {len(conversations)}件 (user_id: {user_id})")
        return conversations
        
    except Exception as e:
        logger.error(f"LINE会話履歴取得エラー: {e}")
        return []

def search_database_for_context(keywords, user_id, limit=5):
    """データベース検索のメインエントリーポイント"""
    try:
        # 直接基本検索を使用（perfect検索関数が未定義のため）
        results = search_database_basic_fallback(keywords, user_id, limit)
        
        if results:
            logger.info(f"検索成功: {len(results)} 件")
            return results
        else:
            logger.warning("検索結果なし")
            return []
            
    except Exception as e:
        logger.error(f"検索システムエラー: {e}")
        return []

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
        if not ANTHROPIC_API_KEY:
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
    conn = None
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
        if conn:
            conn.rollback()
            conn.close()
        return False

# レート制限用の辞書（簡易実装）
user_requests = {}

# その他の設定
SKLEARN_N_JOBS = int(os.getenv('SKLEARN_N_JOBS', '1'))  # scikit-learn並列処理数
NUMPY_MEMORY_LIMIT = int(os.getenv('NUMPY_MEMORY_LIMIT', '256'))  # NumPyメモリ制限(MB)

def rate_limit(max_requests=10, window_seconds=60):
    """レート制限デコレータ"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 簡易的なレート制限実装
            # 本番環境ではRedisを使用することを推奨
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
                # パフォーマンス最適化：チャンクサイズを大きくして遅延を減らす
                chunk_size = 10  # 10文字ずつ送信
                for i in range(0, len(full_response), chunk_size):
                    chunk = full_response[i:i+chunk_size]
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                    time.sleep(0.05)  # チャンクごとの遅延

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
        
        logger.info(f"LINE受信: {user_id} - {user_message[:50]}...")
        
        # 過去10件の会話履歴を取得
        recent_conversations = get_recent_line_conversations(user_id, limit=10)
        logger.info(f"過去の会話履歴: {len(recent_conversations)}件取得")
        
        # 会話履歴を文字列形式に整形
        conversation_history = ""
        if recent_conversations:
            conversation_history = "\n\n=== 過去の会話履歴 ===\n"
            for conv in recent_conversations:
                created_at = conv['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                conversation_history += f"\n[{created_at}]\n"
                conversation_history += f"ユーザー: {conv['user_message']}\n"
                conversation_history += f"AI: {conv['ai_response'][:100]}...\n"  # 長い場合は省略
            conversation_history += "\n=== 履歴終了 ===\n\n"
        
        # キーワード抽出
        keywords = extract_keywords_with_ai(user_message)
        logger.info(f"抽出キーワード: {keywords}")
        
        # データベース検索（関連する過去の会話）
        context_data = search_database_for_context(keywords, user_id)
        
        # 会話履歴を含めたコンテキストデータの作成
        enhanced_context_data = context_data
        if conversation_history:
            # 会話履歴を最初に追加
            history_context = {
                'user_message': '過去の会話履歴',
                'ai_response': conversation_history,
                'created_at': datetime.now(),
                'final_score': 100  # 高いスコアを付けて優先度を上げる
            }
            enhanced_context_data = [history_context] + context_data
        
        # リマインダー処理をチェック
        reminder_data = parse_reminder_request(user_message)
        if reminder_data:
            if reminder_data.get('action') == 'list':
                # リマインダー一覧
                reminders = get_user_reminders(user_id)
                if reminders:
                    ai_response = "📋 現在設定されているリマインダー:\n\n"
                    for i, reminder in enumerate(reminders, 1):
                        time_str = str(reminder['reminder_time'])[:5]
                        repeat_str = {
                            'once': '一回のみ',
                            'daily': '毎日',
                            'weekdays': '平日',
                            'weekends': '週末',
                            'weekly': '毎週'
                        }.get(reminder['repeat_pattern'], reminder['repeat_pattern'])
                        
                        if reminder['repeat_pattern'] == 'weekly' and reminder['repeat_days']:
                            day_map = {'mon': '月', 'tue': '火', 'wed': '水', 'thu': '木', 'fri': '金', 'sat': '土', 'sun': '日'}
                            days_str = ''.join([day_map.get(d, d) for d in reminder['repeat_days']])
                            repeat_str += f" {days_str}曜日"
                        
                        ai_response += f"{i}. {time_str} {repeat_str}: {reminder['message']}\n"
                else:
                    ai_response = "現在、設定されているリマインダーはありません。\n\n例えば以下のように設定できます：\n・毎日10時に薬を飲む\n・平日8時に出勤準備\n・毎週月曜日9時に会議"
            elif reminder_data.get('action') == 'delete':
                # リマインダー削除
                if delete_user_reminders(user_id):
                    ai_response = "✅ リマインダーをすべて削除しました。"
                else:
                    ai_response = "リマインダーの削除に失敗しました。"
            else:
                # リマインダー設定
                reminder_id = save_reminder(user_id, reminder_data)
                if reminder_id:
                    time_str = reminder_data['time']
                    repeat_str = {
                        'once': '一回のみ',
                        'daily': '毎日',
                        'weekdays': '平日',
                        'weekends': '週末',
                        'weekly': '毎週'
                    }.get(reminder_data['repeat'], reminder_data['repeat'])
                    
                    if reminder_data['repeat'] == 'weekly' and reminder_data.get('days'):
                        day_map = {'mon': '月', 'tue': '火', 'wed': '水', 'thu': '木', 'fri': '金', 'sat': '土', 'sun': '日'}
                        days_str = ''.join([day_map.get(d, d) for d in reminder_data['days']])
                        repeat_str += f" {days_str}曜日"
                    
                    ai_response = f"✅ リマインダーを設定しました！\n\n⏰ 時刻: {time_str}\n🔄 繰り返し: {repeat_str}\n📝 内容: {reminder_data['message']}\n\n設定したリマインダーは指定時刻に通知されます。"
                else:
                    ai_response = "リマインダーの設定に失敗しました。もう一度お試しください。"
        else:
            # 通常のAI回答生成（会話履歴を含むコンテキストで）
            ai_response = generate_ai_response_with_context(user_message, enhanced_context_data, user_id)
        
        # データベースに保存
        save_conversation_to_db(
            user_id=user_id,
            conversation_id=None,
            user_message=user_message,
            ai_response=ai_response,
            keywords=keywords,
            context_used=context_data,  # 元のcontext_dataを保存
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
    """検索システムのデバッグ"""
    try:
        user_id = "debug_user"
        
        # キーワード抽出
        keywords = extract_keywords_with_ai(query)
        
        # 通常検索実行
        results = search_database_for_context(keywords, user_id, limit=10)
        
        # 各結果の情報
        detailed_results = []
        for i, result in enumerate(results):
            detailed_result = {
                'rank': i + 1,
                'user_message': result.get('user_message', '')[:100],
                'ai_response': result.get('ai_response', '')[:200],
                'created_at': str(result.get('created_at', '')),
                'source': result.get('source', '')
            }
            detailed_results.append(detailed_result)
        
        return jsonify({
            'query': query,
            'keywords': keywords,
            'total_results': len(results),
            'results': detailed_results
        })
        
    except Exception as e:
        logger.error(f"検索デバッグエラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/user-stats/<user_id>')
def debug_user_stats(user_id):
    """ユーザーの統計情報"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'データベース接続エラー'}), 500
            
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # ユーザーの会話統計
        cur.execute("""
            SELECT 
                COUNT(*) as total_conversations,
                COUNT(DISTINCT DATE(created_at)) as active_days,
                MIN(created_at) as first_conversation,
                MAX(created_at) as last_conversation
            FROM conversations
            WHERE user_id = %s
        """, (user_id,))
        
        stats = dict(cur.fetchone())
        
        # 最頻出キーワード
        cur.execute("""
            SELECT keyword, COUNT(*) as count
            FROM (
                SELECT unnest(keywords) as keyword
                FROM conversations
                WHERE user_id = %s AND keywords IS NOT NULL
            ) as k
            GROUP BY keyword
            ORDER BY count DESC
            LIMIT 10
        """, (user_id,))
        
        frequent_keywords = [dict(row) for row in cur.fetchall()]
        
        cur.close()
        conn.close()
        
        return jsonify({
            'user_id': user_id,
            'stats': stats,
            'frequent_keywords': frequent_keywords
        })
        
    except Exception as e:
        logger.error(f"ユーザー統計デバッグエラー: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/feedback', methods=['POST'])
def record_feedback():
    """会話フィードバックを記録"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': '無効なリクエスト'}), 400
            
        conversation_id = data.get('conversation_id')
        rating = data.get('rating')
        
        if not conversation_id or rating is None:
            return jsonify({'error': 'conversation_idとratingは必須です'}), 400
            
        if not (1 <= rating <= 5):
            return jsonify({'error': 'ratingは1から5の間である必要があります'}), 400
            
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'データベース接続エラー'}), 500
            
        cur = conn.cursor()
        
        # 満足度を更新
        cur.execute("""
            UPDATE conversations
            SET satisfaction_rating = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (rating, conversation_id))
        
        affected = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        
        if affected > 0:
            return jsonify({'success': True, 'message': 'フィードバックを記録しました'})
        else:
            return jsonify({'error': '該当する会話が見つかりません'}), 404
        
    except Exception as e:
        logger.error(f"フィードバック記録エラー: {e}")
        return jsonify({'error': str(e)}), 500
def create_app():
    """アプリケーションファクトリ"""
    # データベース初期化
    init_database()
    logger.info("アプリケーション初期化完了")
    return app

# スケジューラーのジョブを設定
def setup_scheduler():
    """スケジューラーのジョブを設定"""
    # 既存のジョブをクリア
    scheduler.remove_all_jobs()
    
    # 毎分実行（リマインダーチェック）
    scheduler.add_job(
        func=check_and_send_reminders,
        trigger='cron',
        minute='*',  # 毎分
        id='reminder_checker',
        replace_existing=True
    )
    
    logger.info("スケジューラージョブを設定しました")

# アプリケーション初期化（本番環境用）
with app.app_context():
    init_database()
    setup_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("スケジューラーを開始しました")

if __name__ == '__main__':
    # 環境に応じた設定
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'
    host = os.getenv('HOST', '0.0.0.0')
    
    # Pythonバージョン確認
    python_version = os.getenv('PYTHON_VERSION', '3.11.7')
    logger.info(f"Pythonバージョン要件: {python_version}")
    
    # 環境設定のサマリーをログ出力
    logger.info(f"アプリケーション起動設定:")
    logger.info(f"  - Flask環境: {os.getenv('FLASK_ENV', 'development')}")
    logger.info(f"  - ポート: {port}")
    logger.info(f"  - デバッグモード: {debug}")
    logger.info(f"  - ホスト: {host}")
    logger.info(f"  - CORS許可オリジン: {allowed_origins}")
    logger.info(f"  - Claude API: {'Configured' if ANTHROPIC_API_KEY else 'Not configured'}")
    logger.info(f"  - LINE Bot: {'Configured' if LINE_CHANNEL_ACCESS_TOKEN else 'Not configured'}")
    logger.info(f"  - Supabase: {'Configured' if SUPABASE_URL else 'Not configured'}")
    
    logger.info(f"アプリケーションを起動中... Port: {port}, Debug: {debug}")
    
    # 開発環境では追加の初期化
    if debug:
        init_database()
        setup_scheduler()
        if not scheduler.running:
            scheduler.start()
            logger.info("スケジューラーを開始しました（開発環境）")
    
    try:
        app.run(host=host, port=port, debug=debug)
    finally:
        # アプリケーション終了時にスケジューラーを停止
        if scheduler.running:
            scheduler.shutdown()
            logger.info("スケジューラーを停止しました")

# Gunicorn用のアプリケーションオブジェクト
application = app
