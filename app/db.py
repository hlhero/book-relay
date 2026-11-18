import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config
from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  nickname TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student','admin')),
  student_id TEXT,
  security_question TEXT NOT NULL,
  security_answer_hash TEXT NOT NULL,
  reputation REAL NOT NULL DEFAULT 5.0,
  deal_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','banned')),
  session_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  icon TEXT NOT NULL DEFAULT 'book',
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seller_id INTEGER NOT NULL REFERENCES users(id),
  category_id INTEGER NOT NULL REFERENCES categories(id),
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  isbn TEXT NOT NULL DEFAULT '',
  edition TEXT NOT NULL DEFAULT '',
  condition_text TEXT NOT NULL,
  price_cents INTEGER NOT NULL CHECK (price_cents > 0),
  description TEXT NOT NULL DEFAULT '',
  cover_path TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
    CHECK (status IN ('DRAFT','PENDING_REVIEW','ON_SALE','LOCKED','SOLD','OFF_SHELF','BANNED')),
  reject_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL UNIQUE,
  buyer_id INTEGER NOT NULL REFERENCES users(id),
  seller_id INTEGER NOT NULL REFERENCES users(id),
  book_id INTEGER NOT NULL REFERENCES books(id),
  status TEXT NOT NULL DEFAULT 'CREATED'
    CHECK (status IN ('CREATED','PAID','SHIPPED','COMPLETED','CANCELLED','REFUNDING','REFUNDED','CLOSED')),
  total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
  unit_price_snapshot INTEGER NOT NULL,
  condition_snapshot TEXT NOT NULL,
  book_title_snapshot TEXT NOT NULL,
  buyer_nickname_snapshot TEXT NOT NULL,
  handover_method TEXT,
  handover_point TEXT,
  handover_code TEXT,
  remark TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  paid_at TEXT,
  shipped_at TEXT,
  completed_at TEXT,
  closed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  event TEXT NOT NULL,
  actor_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  order_no TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('CAMPUS_CARD','MOCK_THIRD_PARTY')),
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('SUCCEEDED','REFUNDED','FAILED')),
  provider_txn_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  applicant_id INTEGER NOT NULL REFERENCES users(id),
  amount_cents INTEGER NOT NULL,
  reason TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
  handled_by INTEGER REFERENCES users(id),
  handled_at TEXT,
  handle_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  reviewee_id INTEGER NOT NULL REFERENCES users(id),
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (order_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reporter_id INTEGER NOT NULL REFERENCES users(id),
  target_type TEXT NOT NULL CHECK (target_type IN ('BOOK','REVIEW','USER')),
  target_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RESOLVED','REJECTED')),
  handled_by INTEGER REFERENCES users(id),
  handle_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  link TEXT NOT NULL DEFAULT '',
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  user_id INTEGER NOT NULL REFERENCES users(id),
  key TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_books_status_category ON books(status, category_id, created_at);
CREATE INDEX IF NOT EXISTS idx_books_seller ON books(seller_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_seller ON orders(seller_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status_expire ON orders(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_orders_completed ON orders(status, completed_at);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);
"""

CATEGORIES = [
    ("计算机教材", "📚"), ("数学与统计", "∑"), ("外语学习", "🌍"),
    ("专业课程", "⚗"), ("通识与课外", "✨"),
]

SEED_BOOKS = [
    ("数据结构（第2版）", "严蔚敏", "9787302023685", "第2版", "9成新，少量笔记", 1800, "清华大学出版社，经典教材，封面完好，无缺页。", "计算机教材"),
    ("操作系统概念精要", "Silberschatz", "9781119318560", "第9版", "8成新", 2500, "英文影印版，前几章有划重点。", "计算机教材"),
    ("计算机网络：自顶向下方法", "Kurose", "9787115540793", "第7版", "9成新", 2200, "中文版，书角轻微磨损。", "计算机教材"),
    ("高等数学（上册）", "同济大学", "9787040396638", "第7版", "8成新", 1200, "课后习题册一起出。", "数学与统计"),
    ("线性代数", "同济大学", "9787040396621", "第6版", "全新", 1500, "未使用，塑封已拆。", "数学与统计"),
    ("概率论与数理统计", "浙江大学", "9787040406643", "第4版", "9成新", 1000, "有少量铅笔笔记，可擦。", "数学与统计"),
    ("新概念英语（第三册）", "路易·亚历山大", "9787560012223", "新版", "8成新", 900, "附带光盘一张。", "外语学习"),
    ("大学英语精读（第三册）", "上海外语教育出版社", "9787544634635", "第3版", "9成新", 800, "课堂用书，干净整洁。", "外语学习"),
    ("编译原理", "Alfred Aho", "9787115271460", "第2版", "8成新", 2600, "龙书中文版，经典收藏。", "专业课程"),
    ("数据库系统概念", "Abraham Silberschatz", "9787111544937", "第6版", "9成新", 2800, "有笔记但不影响阅读。", "专业课程"),
    ("算法导论", "Thomas Cormen", "9787111407010", "第3版", "8成新", 3500, "大部头，校园内可图书馆西门自提。", "专业课程"),
    ("乡土中国", "费孝通", "9787108063106", "新版", "全新", 600, "通识课指定阅读，几乎全新。", "通识与课外"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def transaction(write: bool = True):
    conn = connect()
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def one(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchone()


def all_rows(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchall()


def _secure_runtime_paths() -> None:
    """Linux 上收紧数据库权限，避免同机其他用户读取密码哈希与用户数据。"""
    if os.name != "posix":
        return
    try:
        os.chmod(config.DB_PATH.parent, 0o700)
        if config.DB_PATH.exists():
            os.chmod(config.DB_PATH, 0o600)
    except OSError:
        pass


def init_db(seed: bool = True) -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        refund_columns = {row["name"] for row in conn.execute("PRAGMA table_info(refunds)").fetchall()}
        if "handle_note" not in refund_columns:
            conn.execute("ALTER TABLE refunds ADD COLUMN handle_note TEXT NOT NULL DEFAULT ''")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "session_version" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()
    _secure_runtime_paths()
    if seed:
        seed_data()
        _secure_runtime_paths()


def seed_data() -> None:
    stamp = now_iso()
    with transaction() as conn:
        if one(conn, "SELECT COUNT(*) AS n FROM users")["n"]:
            return
        for name, icon in CATEGORIES:
            conn.execute(
                "INSERT INTO categories(name, icon, sort_order) VALUES (?,?,?)",
                (name, icon, CATEGORIES.index((name, icon)) + 1),
            )
        admin_hash = hash_password(config.ADMIN_PASSWORD)
        admin_answer = os.getenv("BOOKRELAY_ADMIN_SECURITY_ANSWER") or (
            "2024" if not config.IS_PRODUCTION else secrets.token_urlsafe(32)
        )
        admin_question = "入学年份？" if not config.IS_PRODUCTION else "请联系运维重置管理员密码"
        conn.execute(
            """INSERT INTO users(email,nickname,password_hash,role,student_id,security_question,security_answer_hash,reputation,deal_count,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (config.ADMIN_EMAIL, "书接力管理员", admin_hash, "admin", "ADMIN",
             admin_question, hash_password(admin_answer), 5.0, 0, stamp, stamp),
        )
        if not config.SEED_DEMO:
            return
        student_hash = hash_password(config.STUDENT_PASSWORD)
        answer_hash = hash_password("2024")
        students = [
            ("lin@bookrelay.local", "小林", "20240101"),
            ("chen@bookrelay.local", "阿陈", "20240102"),
            ("wang@bookrelay.local", "王同学", "20240103"),
            ("zhao@bookrelay.local", "赵学姐", "20230201"),
            ("sun@bookrelay.local", "小孙", "20250101"),
            ("zhou@bookrelay.local", "周周", "20240202"),
        ]
        for email, nickname, sid in students:
            conn.execute(
                """INSERT INTO users(email,nickname,password_hash,role,student_id,security_question,security_answer_hash,reputation,deal_count,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (email, nickname, student_hash, "student", sid, "入学年份？", answer_hash, 5.0, 0, stamp, stamp),
            )
        cat_ids = {row["name"]: row["id"] for row in all_rows(conn, "SELECT id,name FROM categories")}
        for idx, (title, author, isbn, edition, condition, price, desc, category) in enumerate(SEED_BOOKS):
            seller_id = 2 + (idx % 6)
            conn.execute(
                """INSERT INTO books(seller_id,category_id,title,author,isbn,edition,condition_text,price_cents,description,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'ON_SALE',?,?)""",
                (seller_id, cat_ids[category], title, author, isbn, edition, condition, price, desc, stamp, stamp),
            )
        conn.execute(
            """INSERT INTO books(seller_id,category_id,title,author,isbn,edition,condition_text,price_cents,description,status,created_at,updated_at)
               VALUES (3,?,'数据挖掘导论','Pang-Ning Tan','9787115246878','第1版','9成新',2900,'等待管理员审核的示例书籍。','PENDING_REVIEW',?,?)""",
            (cat_ids["计算机教材"], stamp, stamp),
        )
        conn.execute(
            """INSERT INTO notifications(user_id,type,title,content,link,created_at)
               VALUES (2,'SYSTEM','欢迎来到书接力','发布教材、下单交易、线下交接，教材流转就这么简单。','/',?)""",
            (stamp,),
        )
