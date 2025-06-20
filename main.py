import os
import json
import requests
import psycopg2
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from psycopg2.extras import RealDictCursor
import logging
from functools import wraps
import time

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.')

# CORS設定
allowed_origins = os.getenv('ALLOWED_ORIGINS', '*').split(',')
CORS(app, origins=allowed_origins)

# 環境変数
DATABASE_URL = os.getenv('DATABASE_URL')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')
DIFY_API_URL = os.getenv('DIFY_API_URL', 'https://api.dify.ai/v1')

# 必須環境変数チェック
if not DATABASE_URL or not DIFY_API_KEY:
    logger.error("DATABASE_URL and DIFY_API_KEY must be set")
    raise ValueError("Missing required environment variables")

# レート制限設定
RATE_LIMIT_REQUESTS = 10  # 10分間のリクエスト数
RATE_LIMIT_WINDOW = 600   # 10分（秒）
user_requests = {}

def rate_limit(f):
    """レート制限デコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = request.json.get('user_id') if request.json else request.remote_addr
        current_time = time.time()
        
        if user_id not in user_requests:
            user_requests[user_id] = []
        
        # 古いリクエストを削除
        user_requests[user_id] = [
            req_time for req_time in user_requests[user_id] 
            if current_time - req_time < RATE_LIMIT_WINDOW
        ]
        
        # レート制限チェック
        if len(user_requests[user_id]) >= RATE_LIMIT_REQUESTS:
            return jsonify({
                'error': 'Rate limit exceeded. Please try again later.',
                'retry_after': RATE_LIMIT_WINDOW
            }), 429
        
        user_requests[user_id].append(current_time)
        return f(*args, **kwargs)
    
    return decorated_function

def get_db_connection():
    """データベース接続取得"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None

def call_dify_api(message, user_id, conversation_id=None):
    """Dify APIを呼び出す"""
    headers = {
        'Authorization': f'Bearer {DIFY_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        'inputs': {
            'query': message,
            'user_id': user_id
        },
        'response_mode': 'blocking',
        'user': user_id
    }
    
    if conversation_id:
        payload['conversation_id'] = conversation_id
    
    try:
        start_time = datetime.now()
        response = requests.post(
            f'{DIFY_API_URL}/chat-messages',
            headers=headers,
            json=payload,
            timeout=30
        )
        end_time = datetime.now()
        
        if response.status_code == 200:
            result = response.json()
            response_time = int((end_time - start_time).total_seconds() * 1000)
            
            return {
                'success': True,
                'response': result.get('answer', '申し訳ございませんが、回答を生成できませんでした。'),
                'conversation_id': result.get('conversation_id'),
                'response_time_ms': response_time,
                'tokens_used': result.get('metadata', {}).get('usage', {}).get('total_tokens', 0)
            }
        else:
            logger.error(f"Dify API error: {response.status_code} - {response.text}")
            return {
                'success': False,
                'error': f'API Error: {response.status_code}',
                'details': response.text[:200]
            }
            
    except requests.exceptions.Timeout:
        logger.error("Dify API timeout")
        return {'success': False, 'error': 'API request timeout'}
    except requests.exceptions.ConnectionError:
        logger.error("Dify API connection error")
        return {'success': False, 'error': 'Connection error to AI service'}
    except Exception as e:
        logger.error(f"Dify API call error: {e}")
        return {'success': False, 'error': str(e)}

# 静的ファイル配信
@app.route('/')
def index():
    """メインのチャットページ"""
    try:
        return send_from_directory('.', 'index.html')
    except FileNotFoundError:
        return jsonify({'error': 'index.html not found'}), 404

@app.route('/dashboard')
def dashboard():
    """ダッシュボードページ"""
    try:
        return send_from_directory('.', 'dashboard.html')
    except FileNotFoundError:
        return jsonify({'error': 'dashboard.html not found'}), 404

@app.route('/health')
def health_check():
    """ヘルスチェックエンドポイント"""
    try:
        # データベース接続テスト
        conn = get_db_connection()
        db_status = 'disconnected'
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
                conn.close()
                db_status = 'connected'
            except Exception as e:
                logger.error(f"Database health check error: {e}")
                db_status = f'error: {str(e)[:50]}'
        
        # Dify API接続テスト（簡易）
        dify_status = 'configured' if DIFY_API_KEY else 'not_configured'
        
        health_data = {
            'status': 'healthy' if db_status == 'connected' else 'unhealthy',
            'timestamp': datetime.now().isoformat(),
            'database': db_status,
            'dify_api': dify_status,
            'version': '1.0.0'
        }
        
        status_code = 200 if db_status == 'connected' else 503
        return jsonify(health_data), status_code
        
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 503

@app.route('/api/chat', methods=['POST'])
@rate_limit
def chat():
    """チャットエンドポイント"""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        user_id = data.get('user_id')
        user_message = data.get('message')
        conversation_id = data.get('conversation_id')
        
        # バリデーション
        if not user_id or not user_message:
            return jsonify({'error': 'user_id and message are required'}), 400
        
        if len(user_message.strip()) == 0:
            return jsonify({'error': 'Message cannot be empty'}), 400
        
        if len(user_message) > 2000:
            return jsonify({'error': 'Message too long (max 2000 characters)'}), 400
        
        # 不正な文字のチェック
        if any(ord(char) < 32 and char not in '\n\r\t' for char in user_message):
            return jsonify({'error': 'Invalid characters in message'}), 400
        
        # Dify API呼び出し
        logger.info(f"Processing chat request from user {user_id}")
        dify_result = call_dify_api(user_message, user_id, conversation_id)
        
        if not dify_result['success']:
            logger.error(f"Dify API failed: {dify_result.get('error')}")
            return jsonify({
                'error': 'Failed to get AI response',
                'details': dify_result.get('error', 'Unknown error')
            }), 500
        
        # データベースに保存
        conn = get_db_connection()
        record_id = None
        
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO chat_conversations 
                    (user_id, user_message, ai_response, conversation_id, response_time_ms, tokens_used, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    user_id,
                    user_message,
                    dify_result['response'],
                    dify_result.get('conversation_id'),
                    dify_result.get('response_time_ms', 0),
                    dify_result.get('tokens_used', 0),
                    datetime.now()
                ))
                
                record_id = cur.fetchone()[0]
                conn.commit()
                cur.close()
                conn.close()
                
                logger.info(f"Chat saved with ID: {record_id}")
                
            except Exception as e:
                logger.error(f"Database save error: {e}")
                if conn:
                    conn.rollback()
                    cur.close()
                    conn.close()
        
        # レスポンス構築
        response_data = {
            'success': True,
            'response': dify_result['response'],
            'conversation_id': dify_result.get('conversation_id'),
            'response_time_ms': dify_result.get('response_time_ms'),
            'tokens_used': dify_result.get('tokens_used')
        }
        
        if record_id:
            response_data['id'] = record_id
        else:
            response_data['warning'] = 'Response generated but not saved to database'
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/history/<user_id>')
def get_history(user_id):
    """チャット履歴取得エンドポイント"""
    try:
        # パラメータ取得とバリデーション
        limit = min(request.args.get('limit', 50, type=int), 100)
        offset = max(request.args.get('offset', 0, type=int), 0)
        
        if not user_id or len(user_id) > 255:
            return jsonify({'error': 'Invalid user_id'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
        
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 履歴取得
            cur.execute("""
                SELECT id, user_message, ai_response, conversation_id, response_time_ms, 
                       tokens_used, created_at, feedback_rating
                FROM chat_conversations 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
            
            history = cur.fetchall()
            
            # 総数取得
            cur.execute("SELECT COUNT(*) FROM chat_conversations WHERE user_id = %s", (user_id,))
            total_count = cur.fetchone()[0]
            
            cur.close()
            conn.close()
            
            return jsonify({
                'history': [dict(row) for row in history],
                'total': total_count,
                'limit': limit,
                'offset': offset,
                'has_more': offset + len(history) < total_count
            })
            
        except Exception as e:
            logger.error(f"History retrieval error: {e}")
            return jsonify({'error': 'Failed to retrieve history'}), 500
        
    except Exception as e:
        logger.error(f"History endpoint error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/search/<user_id>')
def search_conversations(user_id):
    """会話検索エンドポイント"""
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'error': 'Search query is required'}), 400
        
        if len(query) > 100:
            return jsonify({'error': 'Search query too long'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
        
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT id, user_message, ai_response, conversation_id, created_at,
                       ts_rank(to_tsvector('english', user_message || ' ' || ai_response), 
                              plainto_tsquery('english', %s)) as rank
                FROM chat_conversations 
                WHERE user_id = %s 
                AND to_tsvector('english', user_message || ' ' || ai_response) @@ plainto_tsquery('english', %s)
                ORDER BY rank DESC, created_at DESC 
                LIMIT 20
            """, (query, user_id, query))
            
            results = cur.fetchall()
            cur.close()
            conn.close()
            
            return jsonify({
                'results': [dict(row) for row in results],
                'query': query,
                'count': len(results)
            })
            
        except Exception as e:
            logger.error(f"Search error: {e}")
            return jsonify({'error': 'Search failed'}), 500
        
    except Exception as e:
        logger.error(f"Search endpoint error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/stats')
def get_stats():
    """システム統計情報取得"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 基本統計（過去30日）
        cur.execute("""
            SELECT 
                COUNT(*) as total_conversations,
                COUNT(DISTINCT user_id) as unique_users,
                ROUND(AVG(response_time_ms)) as avg_response_time,
                SUM(tokens_used) as total_tokens,
                COUNT(CASE WHEN feedback_rating >= 4 THEN 1 END) as positive_feedback,
                COUNT(CASE WHEN feedback_rating IS NOT NULL THEN 1 END) as total_feedback
            FROM chat_conversations 
            WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
        """)
        basic_stats = cur.fetchone()
        
        # 日別統計（過去7日）
        cur.execute("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as conversations,
                ROUND(AVG(response_time_ms)) as avg_response_time,
                COUNT(DISTINCT user_id) as unique_users
            FROM chat_conversations 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """)
        daily_stats = cur.fetchall()
        
        # 時間別統計（過去24時間）
        cur.execute("""
            SELECT 
                EXTRACT(HOUR FROM created_at) as hour,
                COUNT(*) as conversations
            FROM chat_conversations 
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY EXTRACT(HOUR FROM created_at)
            ORDER BY hour
        """)
        hourly_stats = cur.fetchall()
        
        cur.close()
        conn.close()
        
        # 計算された統計
        stats = dict(basic_stats)
        if stats['total_feedback'] > 0:
            stats['satisfaction_rate'] = round((stats['positive_feedback'] / stats['total_feedback']) * 100, 1)
        else:
            stats['satisfaction_rate'] = None
        
        return jsonify({
            'basic_stats': stats,
            'daily_stats': [dict(row) for row in daily_stats],
            'hourly_stats': [dict(row) for row in hourly_stats],
            'generated_at': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({'error': 'Failed to get statistics'}), 500

@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    """フィードバック送信エンドポイント"""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        record_id = data.get('id')
        rating = data.get('rating')
        
        # バリデーション
        if not record_id or rating is None:
            return jsonify({'error': 'id and rating are required'}), 400
        
        if rating not in [1, 2, 3, 4, 5]:
            return jsonify({'error': 'Rating must be between 1 and 5'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
        
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE chat_conversations 
                SET feedback_rating = %s, updated_at = %s
                WHERE id = %s
                RETURNING user_id
            """, (rating, datetime.now(), record_id))
            
            result = cur.fetchone()
            if not result:
                return jsonify({'error': 'Record not found'}), 404
            
            conn.commit()
            cur.close()
            conn.close()
            
            logger.info(f"Feedback submitted: record_id={record_id}, rating={rating}")
            
            return jsonify({
                'success': True, 
                'message': 'Feedback submitted successfully',
                'rating': rating
            })
            
        except Exception as e:
            logger.error(f"Feedback save error: {e}")
            return jsonify({'error': 'Failed to save feedback'}), 500
        
    except Exception as e:
        logger.error(f"Feedback endpoint error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    print("🚀 Dify Chat API Server Starting...")
    print(f"📊 Health Check: http://localhost:5000/health")
    print(f"💬 Chat Interface: http://localhost:5000/")
    print(f"📈 Dashboard: http://localhost:5000/dashboard")
    print(f"🔧 Environment: {os.getenv('FLASK_ENV', 'development')}")
    
    app.run(
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true', 
        host='0.0.0.0', 
        port=int(os.getenv('PORT', 5000))
    )