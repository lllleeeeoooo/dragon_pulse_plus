import sqlite3
from datetime import datetime
from config import DB_PATH

class Database:
    """SQLite 数据库管理类"""
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """初始化数据表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # 1. 信号推送历史表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS signal_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    push_time TEXT,
                    stock_code TEXT,
                    stock_name TEXT,
                    signal_type TEXT,
                    trigger_price REAL,
                    trigger_pct REAL,
                    volume_ratio REAL,
                    close_price REAL DEFAULT NULL,
                    is_limit_up INTEGER DEFAULT NULL
                )
            ''')
            # 2. 每日市场情绪表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_sentiment (
                    date TEXT PRIMARY KEY,
                    limit_up_count INTEGER,
                    broken_limit_count INTEGER,
                    max_height INTEGER,
                    sentiment_score REAL
                )
            ''')
            conn.commit()

    def record_signal(self, code: str, name: str, signal_type: str, price: float, pct: float, vr: float = 0.0):
        """记录推送信号"""
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO signal_history (push_time, stock_code, stock_name, signal_type, trigger_price, trigger_pct, volume_ratio)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (now_str, code, name, signal_type, price, pct, vr))
            conn.commit()

    def update_daily_close(self, date_prefix: str, code: str, close_price: float, is_limit_up: int):
        """盘后回算更新收盘价与封板状态"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE signal_history
                SET close_price = ?, is_limit_up = ?
                WHERE stock_code = ? AND push_time LIKE ?
            ''', (close_price, is_limit_up, code, f"{date_prefix}%"))
            conn.commit()
