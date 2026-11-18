import threading
import time
from collections import defaultdict, deque

import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import SECRET_KEY, SESSION_MAX_AGE

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="bookrelay-session")

# 进程内滑动窗口限流。单 worker 部署时有效；多实例/多 worker 应同时使用
# 阿里云 WAF 或 Nginx limit_req 做边界防护。
_buckets: dict[str, deque[float]] = defaultdict(deque)
_bucket_lock = threading.Lock()


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def make_session(user_id: int, version: int = 0) -> str:
    return _serializer.dumps({"uid": int(user_id), "v": int(version)})


def read_session(token: str) -> tuple[int, int] | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return int(data["uid"]), int(data.get("v", 0))
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


def allow_rate(key: str, *, limit: int, window_seconds: int) -> bool:
    """记录一次请求；超过窗口内上限时返回 False。"""
    now = time.monotonic()
    with _bucket_lock:
        bucket = _buckets[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(_buckets) > 10000:
            for name, queue in list(_buckets.items()):
                while queue and now - queue[0] > 3600:
                    queue.popleft()
                if not queue:
                    _buckets.pop(name, None)
        return True


def clear_rate(key: str) -> None:
    with _bucket_lock:
        _buckets.pop(key, None)
