import os
from pathlib import Path


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BOOKRELAY_DATA", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("BOOKRELAY_DB", DATA_DIR / "bookrelay.db"))
UPLOAD_DIR = Path(BASE_DIR / "app" / "static" / "uploads")
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

ENV = os.getenv("BOOKRELAY_ENV", "development").strip().lower()
IS_PRODUCTION = ENV in ("production", "prod")

SECRET_KEY = os.getenv("BOOKRELAY_SECRET", "").strip()
SECRET_KEY_IS_DEFAULT = not bool(SECRET_KEY)
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("生产环境必须设置 BOOKRELAY_SECRET（至少 32 字节随机值），拒绝使用开发密钥启动")
    SECRET_KEY = "bookrelay-local-dev-secret-change-in-production"

SESSION_COOKIE = "br_session"
SESSION_MAX_AGE = _env_int("BOOKRELAY_SESSION_HOURS", 24 if IS_PRODUCTION else 168) * 3600
COOKIE_SECURE = os.getenv("BOOKRELAY_COOKIE_SECURE", "1" if IS_PRODUCTION else "0") == "1"

PAY_WINDOW_MINUTES = 30
REFUND_WINDOW_HOURS = 48
AUTO_CLOSE_DAYS = 30
IDEMPOTENCY_TTL_HOURS = 24
HANDOVER_CODE_LENGTH = 4
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = _env_int("BOOKRELAY_MAX_REQUEST_BYTES", 3 * 1024 * 1024, 1024)
MAX_ACTIVE_UNPAID_ORDERS = _env_int("BOOKRELAY_MAX_ACTIVE_UNPAID_ORDERS", 3)

ADMIN_EMAIL = os.getenv("BOOKRELAY_ADMIN_EMAIL", "admin@bookrelay.local").strip().lower()
ADMIN_PASSWORD = os.getenv("BOOKRELAY_ADMIN_PASSWORD", "Admin@135")
STUDENT_PASSWORD = os.getenv("BOOKRELAY_STUDENT_PASSWORD", "Student@123")
SEED_DEMO = os.getenv("BOOKRELAY_SEED_DEMO", "0" if IS_PRODUCTION else "1") == "1"
DOCS_ENABLED = os.getenv("BOOKRELAY_ENABLE_DOCS", "0" if IS_PRODUCTION else "1") == "1"
ALLOWED_EMAIL_DOMAIN = os.getenv("BOOKRELAY_EMAIL_DOMAIN", "").strip().lower().lstrip("@")
TRUSTED_ORIGINS = tuple(
    item.strip().rstrip("/")
    for item in os.getenv("BOOKRELAY_TRUSTED_ORIGINS", "").split(",")
    if item.strip()
)

if IS_PRODUCTION and ADMIN_PASSWORD == "Admin@135":
    raise RuntimeError("生产环境必须通过 BOOKRELAY_ADMIN_PASSWORD 设置管理员强密码，禁止使用默认口令")
