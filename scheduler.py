# データベース改造のための一時的なコード
from main import init_database
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("データベースの改造タスクを開始します。")
# 以前作った、改造機能付きのinit_databaseを実行
init_database() 
logger.info("データベースの改造タスクが完了しました。")
