import os
import psycopg2
from datetime import datetime, timedelta
import pytz
from linebot import LineBotApi
from linebot.models import TextSendMessage
import logging

DATABASE_URL = os.getenv('DATABASE_URL')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def send_reminders():
    if not DATABASE_URL or not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("環境変数が設定されていません。")
        return

    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    conn = None
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        now = datetime.now(pytz.timezone('Asia/Tokyo'))
        
        # 期限が来ていてアクティブなリマインダーを取得（繰り返し情報も）
        cur.execute(
            "SELECT id, target_id, reminder_content, due_at, is_recurring, recurrence_rule FROM reminders WHERE due_at <= %s AND status = 'active'",
            (now,)
        )
        due_reminders = cur.fetchall()
        
        if not due_reminders:
            logger.info("送信対象のリマインダーはありません。")
            return

        for rem_id, target_id, content, due_at, is_recurring, rule in due_reminders:
            try:
                # LINEで通知を送信
                clean_target_id = target_id.replace('line_', '', 1)
                message = f"【リマインダー】\n{content}"
                line_bot_api.push_message(clean_target_id, TextSendMessage(text=message))
                
                # --- ▼ 繰り返し処理のロジック ▼ ---
                if is_recurring:
                    # 繰り返し設定なら、次の通知時間を計算して更新
                    next_due_at = None
                    if rule == 'daily':
                        next_due_at = due_at + timedelta(days=1)
                    elif rule and rule.startswith('weekly'):
                        next_due_at = due_at + timedelta(weeks=1)
                    
                    if next_due_at:
                        cur.execute(
                            "UPDATE reminders SET due_at = %s WHERE id = %s",
                            (next_due_at, rem_id)
                        )
                        logger.info(f"繰り返しリマインダー(ID: {rem_id})の次回通知を {next_due_at} に設定しました。")
                    else:
                        # 不明なルールなら停止
                        cur.execute("UPDATE reminders SET status = 'error' WHERE id = %s", (rem_id,))
                else:
                    # 繰り返しでなければ、送信済みにする
                    cur.execute("UPDATE reminders SET status = 'sent' WHERE id = %s", (rem_id,))
                
                conn.commit()
                logger.info(f"リマインダー (ID: {rem_id}) を {target_id} に送信しました。")
                
            except Exception as send_error:
                logger.error(f"リマインダー (ID: {rem_id}) の送信に失敗しました: {send_error}")
                cur.execute("UPDATE reminders SET status = 'error' WHERE id = %s", (rem_id,))
                conn.commit()

        cur.close()

    except Exception as e:
        logger.error(f"リマインダー処理中にエラーが発生しました: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    logger.info("リマインダースケジューラを開始します。")
    send_reminders()
    logger.info("リマインダースケジューラを終了します。")
