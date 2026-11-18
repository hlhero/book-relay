"""用例编排层：8 个业务模块的服务函数。每个公开函数独占一个 SQLite 写事务。"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from . import config, domain
from .db import all_rows, now_iso, one, transaction
from .errors import AppError, fail
from .security import hash_password, verify_password

SYSTEM = {"id": None, "role": "admin", "nickname": "系统"}
# 用户不存在时也执行一次 bcrypt 校验，降低通过响应耗时枚举账号的风险。
_DUMMY_PASSWORD_HASH = hash_password("bookrelay-dummy-password")
HANDOVER_METHODS = ("LIBRARY_WEST", "DORM_LOBBY", "COURIER")
HANDOVER_LABELS = {
    "LIBRARY_WEST": "图书馆西门", "DORM_LOBBY": "宿舍楼下", "COURIER": "校园快递点",
}
NOTICE_LABELS = {
    "SYSTEM": "系统", "ORDER": "订单", "PAYMENT": "支付", "HANDOVER": "交接",
    "REFUND": "退款", "REVIEW": "评价", "BOOK": "书籍", "REPORT": "举报",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(row):
    return dict(row) if row is not None else None


def _user(conn, uid: int) -> dict:
    row = one(conn, "SELECT * FROM users WHERE id=?", (uid,))
    if not row:
        fail("NOT_FOUND", "用户不存在")
    return _row(row)


def _book(conn, book_id: int) -> dict:
    row = one(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    if not row:
        fail("NOT_FOUND", "书籍不存在")
    return _row(row)


def _order(conn, order_id: int) -> dict:
    row = one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        fail("NOT_FOUND", "订单不存在")
    return _row(row)


def _public_user(row: dict) -> dict:
    data = dict(row)
    data.pop("password_hash", None)
    data.pop("security_answer_hash", None)
    return data


def _notify(conn, user_id: int, ntype: str, title: str, content: str, link: str = "") -> None:
    conn.execute(
        "INSERT INTO notifications(user_id,type,title,content,link,created_at) VALUES (?,?,?,?,?,?)",
        (user_id, ntype, title, content, link, now_iso()),
    )


def _notify_admins(conn, title: str, content: str, link: str = "") -> None:
    for admin in all_rows(conn, "SELECT id FROM users WHERE role='admin' AND status='active'"):
        _notify(conn, admin["id"], "SYSTEM", title, content, link)


def _book_transition(conn, book: dict, event: str, actor: dict, reason: str = "") -> dict:
    rule = domain.transit_book(book, event, actor_id=actor.get("id"), role=actor.get("role"))
    conn.execute(
        "UPDATE books SET status=?, reject_reason=?, updated_at=? WHERE id=?",
        (rule.target, reason if event in ("reject", "ban") else "", now_iso(), book["id"]),
    )
    if event in ("approve", "reject", "ban", "unban"):
        text = {
            "approve": "你发布的《%s》已通过审核并上架" % book["title"],
            "reject": "你发布的《%s》未通过审核：%s" % (book["title"], reason),
            "ban": "你发布的《%s》因违规已被封禁：%s" % (book["title"], reason),
            "unban": "你发布的《%s》已解除封禁，可重新上架" % book["title"],
        }[event]
        _notify(conn, book["seller_id"], "BOOK", "书籍审核结果", text, f"/books/{book['id']}")
    return _book(conn, book["id"])


def _log_order(conn, order: dict, event: str, to_status: str, actor: dict, note: str = "") -> None:
    conn.execute(
        """INSERT INTO order_events(order_id,from_status,to_status,event,actor_id,note,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (order["id"], order["status"], to_status, event, actor.get("id"), note, now_iso()),
    )


def _notify_order_event(conn, order: dict, event: str, actor: dict) -> None:
    no = order["order_no"]
    link = f"/orders/{order['id']}"
    title = domain.ORDER_LABELS[order["status"]]
    if event == "pay":
        _notify(conn, order["seller_id"], "PAYMENT", "买家已支付", f"订单 {no} 已支付，请确认交接方式。", link)
    elif event == "ship":
        _notify(conn, order["buyer_id"], "HANDOVER", "卖家已生成交接码", f"订单 {no} 待交接，请线下核对交接码。", link)
    elif event == "confirm":
        for uid in (order["buyer_id"], order["seller_id"]):
            _notify(conn, uid, "REVIEW", "交易已完成", f"订单 {no} 已完成，现在可以为对方评价。", f"/reviews/{order['id']}")
    elif event == "cancel":
        for uid in {order["buyer_id"], order["seller_id"]}:
            if actor.get("id") != uid:
                _notify(conn, uid, "ORDER", "订单已取消", f"订单 {no} 已取消，书籍已重新上架。", link)
    elif event == "apply_refund":
        _notify(conn, actor.get("id") or order["buyer_id"], "REFUND", "退款申请已提交", f"订单 {no} 的退款申请待管理员处理。", link)
        _notify_admins(conn, "待处理退款申请", f"订单 {no} 有新的退款申请。", "/admin")
    elif event == "approve":
        for uid in (order["buyer_id"], order["seller_id"]):
            _notify(conn, uid, "REFUND", "退款已同意", f"订单 {no} 退款已原路退回。", link)
    elif event == "reject":
        applicant = one(conn, "SELECT applicant_id FROM refunds WHERE order_id=? ORDER BY id DESC LIMIT 1", (order["id"],))
        if applicant:
            _notify(conn, applicant["applicant_id"], "REFUND", "退款申请被驳回", f"订单 {no} 的退款申请被驳回，交易继续。", link)
    elif event == "auto_close":
        for uid in (order["buyer_id"], order["seller_id"]):
            _notify(conn, uid, "ORDER", "订单已关闭", f"订单 {no} 已关闭，评价入口已过期。", link)


def _transition(conn, order: dict, event: str, actor: dict, note: str = "", **ctx) -> dict:
    now = utcnow()
    ctx = dict(ctx)
    ctx.setdefault("actor_id", actor.get("id"))
    ctx.setdefault("role", actor.get("role"))
    rule = domain.transit_order(order, event, now, **ctx)
    stamp = now.isoformat(timespec="seconds")
    fields = {"status": rule.target, "updated_at": stamp}
    if event == "pay":
        fields["paid_at"] = stamp
    elif event == "ship":
        fields["shipped_at"] = stamp
        fields["handover_code"] = str(ctx["code"])
        fields["handover_point"] = ctx.get("handover_point")
        if ctx.get("handover_method"):
            fields["handover_method"] = ctx["handover_method"]
    elif event == "confirm":
        fields["completed_at"] = stamp
    elif event in ("cancel", "auto_close"):
        fields["closed_at"] = stamp

    sets = ",".join(f"{key}=?" for key in fields)
    conn.execute(f"UPDATE orders SET {sets} WHERE id=?", (*fields.values(), order["id"]))
    book = _book(conn, order["book_id"])
    action = rule.action

    if action == "release_stock_lock":
        if book["status"] == "LOCKED":
            _book_transition(conn, book, "unlock", SYSTEM)
    elif action == "settle_seller_amount":
        if book["status"] == "LOCKED":
            _book_transition(conn, book, "mark_sold", SYSTEM)
        for uid in (order["buyer_id"], order["seller_id"]):
            conn.execute("UPDATE users SET deal_count=deal_count+1 WHERE id=?", (uid,))
    elif action == "refund_to_user":
        if book["status"] == "LOCKED":
            _book_transition(conn, book, "withdraw", actor, "订单退款下架")
        conn.execute(
            "UPDATE payments SET status='REFUNDED', updated_at=? WHERE order_id=? AND status='SUCCEEDED'",
            (stamp, order["id"]),
        )
        conn.execute(
            """UPDATE refunds SET status='APPROVED', handled_by=?, handled_at=?, updated_at=?
               WHERE order_id=? AND status='PENDING'""",
            (actor.get("id"), stamp, stamp, order["id"]),
        )
    elif action == "notify_applicant":
        conn.execute(
            """UPDATE refunds SET status='REJECTED', handled_by=?, handled_at=?, updated_at=?, handle_note=?
               WHERE order_id=? AND status='PENDING'""",
            (actor.get("id"), stamp, stamp, note, order["id"]),
        )
    elif action == "auto_close":
        pass

    updated = _order(conn, order["id"])
    _log_order(conn, order, event, rule.target, actor, note)
    _notify_order_event(conn, updated, event, actor)
    return updated


def _idem(conn, user: dict, key: str | None, endpoint: str, producer):
    if not key or len(key) < 8:
        fail("VALIDATION_ERROR", "缺少有效的幂等键（Idempotency-Key）", details=[{"field": "idempotency_key"}])
    cached = one(conn, "SELECT * FROM idempotency_keys WHERE user_id=? AND key=?", (user["id"], key))
    if cached:
        if cached["endpoint"] != endpoint:
            fail("IDEMPOTENCY_KEY_REUSED", "幂等键已被其它接口使用")
        created = domain.parse_dt(cached["created_at"])
        if created and utcnow() - created < timedelta(hours=config.IDEMPOTENCY_TTL_HOURS):
            return json.loads(cached["response_json"])
    result = producer()
    conn.execute(
        """INSERT INTO idempotency_keys(user_id,key,endpoint,response_json,created_at) VALUES (?,?,?,?,?)
           ON CONFLICT(user_id,key) DO UPDATE SET endpoint=excluded.endpoint,response_json=excluded.response_json,created_at=excluded.created_at""",
        (user["id"], key, endpoint, json.dumps(result, ensure_ascii=False), now_iso()),
    )
    return result


def _require_party(order: dict, user: dict) -> None:
    if user["id"] not in (order["buyer_id"], order["seller_id"]) and user["role"] != "admin":
        fail("NOT_YOUR_RESOURCE", "你无权查看或操作这笔订单")


def _validate_password(password: str) -> None:
    password = password or ""
    if len(password) < 8:
        fail("VALIDATION_ERROR", "密码至少 8 位", details=[{"field": "password"}])
    if len(password.encode("utf-8")) > 72:
        fail("VALIDATION_ERROR", "密码过长（最多 72 字节）", details=[{"field": "password"}])
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        fail("VALIDATION_ERROR", "密码需同时包含字母和数字", details=[{"field": "password"}])


# ---------------------------------------------------------------- 身份与账号

def register(*, email: str, nickname: str, password: str, student_id: str = "",
             security_question: str, security_answer: str) -> dict:
    email = (email or "").strip().lower()
    nickname = (nickname or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        fail("VALIDATION_ERROR", "邮箱格式不正确", details=[{"field": "email"}])
    if config.ALLOWED_EMAIL_DOMAIN and not email.endswith("@" + config.ALLOWED_EMAIL_DOMAIN):
        fail("VALIDATION_ERROR", f"仅允许使用 @{config.ALLOWED_EMAIL_DOMAIN} 邮箱注册",
             details=[{"field": "email"}])
    if len(nickname) < 2 or len(nickname) > 20:
        fail("VALIDATION_ERROR", "昵称长度需为 2–20 字", details=[{"field": "nickname"}])
    _validate_password(password)
    if not security_question.strip() or not security_answer.strip():
        fail("VALIDATION_ERROR", "请填写找回密码问题与答案", details=[{"field": "security_answer"}])
    stamp = now_iso()
    with transaction() as conn:
        if one(conn, "SELECT id FROM users WHERE email=?", (email,)):
            fail("EMAIL_TAKEN", "该邮箱已注册")
        cur = conn.execute(
            """INSERT INTO users(email,nickname,password_hash,role,student_id,security_question,security_answer_hash,created_at,updated_at)
               VALUES (?,?,?,'student',?,?,?,?,?)""",
            (email, nickname, hash_password(password), student_id.strip(), security_question.strip(),
             hash_password(security_answer.strip().lower()), stamp, stamp),
        )
        user = _user(conn, cur.lastrowid)
        _notify(conn, user["id"], "SYSTEM", "欢迎来到书接力", "完善个人信息后即可发布教材或开始淘书。", "/")
        return _public_user(user)


def login(*, email: str, password: str) -> dict:
    with transaction(write=False) as conn:
        row = one(conn, "SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))
        candidate_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        valid = verify_password(password or "", candidate_hash)
        if not row or not valid:
            fail("INVALID_CREDENTIALS", "邮箱或密码错误")
        if row["status"] == "banned":
            fail("ACCOUNT_BANNED", "账号已被封禁，请联系管理员")
        return _public_user(_row(row))


def get_user(user_id: int) -> dict:
    with transaction(write=False) as conn:
        return _public_user(_user(conn, user_id))


def reset_password(*, email: str, security_answer: str, new_password: str) -> dict:
    _validate_password(new_password)
    with transaction() as conn:
        row = one(conn, "SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))
        candidate_hash = row["security_answer_hash"] if row else _DUMMY_PASSWORD_HASH
        valid = verify_password((security_answer or "").strip().lower(), candidate_hash)
        if not row or not valid:
            fail("INVALID_SECURITY_ANSWER", "邮箱或安全问题答案不正确")
        conn.execute("UPDATE users SET password_hash=?, session_version=session_version+1, updated_at=? WHERE id=?",
                     (hash_password(new_password), now_iso(), row["id"]))
        return {"ok": True}


def change_password(user: dict, *, old_password: str, new_password: str) -> dict:
    _validate_password(new_password)
    with transaction() as conn:
        current = _user(conn, user["id"])
        if not verify_password(old_password, current["password_hash"]):
            fail("INVALID_CREDENTIALS", "原密码不正确")
        conn.execute("UPDATE users SET password_hash=?, session_version=session_version+1, updated_at=? WHERE id=?",
                     (hash_password(new_password), now_iso(), user["id"]))
        return {"ok": True}


def update_profile(user: dict, *, nickname: str, student_id: str) -> dict:
    nickname = (nickname or "").strip()
    if len(nickname) < 2 or len(nickname) > 20:
        fail("VALIDATION_ERROR", "昵称长度需为 2–20 字")
    with transaction() as conn:
        conn.execute("UPDATE users SET nickname=?, student_id=?, updated_at=? WHERE id=?",
                     (nickname, (student_id or "").strip(), now_iso(), user["id"]))
        return _public_user(_user(conn, user["id"]))


def admin_set_user_status(admin: dict, user_id: int, status: str) -> dict:
    if status not in ("active", "banned"):
        fail("VALIDATION_ERROR", "无效的用户状态")
    with transaction() as conn:
        target = _user(conn, user_id)
        if target["role"] == "admin":
            fail("FORBIDDEN", "不能封禁管理员账号")
        conn.execute(
            "UPDATE users SET status=?, session_version=session_version+1, updated_at=? WHERE id=?",
            (status, now_iso(), user_id),
        )
        _notify(conn, user_id, "SYSTEM", "账号状态变更",
                "你的账号已被封禁" if status == "banned" else "你的账号已恢复正常", "/")
        return _public_user(_user(conn, user_id))


# ---------------------------------------------------------------- 书籍目录

def list_categories() -> list[dict]:
    with transaction(write=False) as conn:
        return [_row(r) for r in all_rows(conn, "SELECT * FROM categories ORDER BY sort_order,id")]


def book_view(conn, book: dict) -> dict:
    data = dict(book)
    seller = _user(conn, book["seller_id"])
    cat = one(conn, "SELECT name FROM categories WHERE id=?", (book["category_id"],))
    data["status_label"] = domain.BOOK_LABELS[book["status"]]
    data["seller_nickname"] = seller["nickname"]
    data["seller_reputation"] = seller["reputation"]
    data["seller_deals"] = seller["deal_count"]
    data["category_name"] = cat["name"] if cat else "未分类"
    data["price_text"] = domain.yuan(book["price_cents"])
    data["cover_url"] = "/static/uploads/" + book["cover_path"] if book["cover_path"] else ""
    return data


def list_books(*, keyword: str = "", category_id: int = 0, sort: str = "latest",
               seller_id: int | None = None, status: str | None = None) -> list[dict]:
    if status and status not in domain.BOOK_STATES:
        fail("VALIDATION_ERROR", "无效的书籍状态筛选")
    conditions, params = [], []
    if status:
        conditions.append("b.status=?")
        params.append(status)
    else:
        conditions.append("b.status='ON_SALE'")
    if keyword:
        conditions.append("(b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)
    if category_id:
        conditions.append("b.category_id=?")
        params.append(category_id)
    if seller_id:
        conditions.append("b.seller_id=?")
        params.append(seller_id)
    order_by = {
        "latest": "b.id DESC", "price_asc": "b.price_cents ASC", "price_desc": "b.price_cents DESC",
    }.get(sort, "b.id DESC")
    sql = "SELECT b.* FROM books b WHERE " + " AND ".join(conditions) + f" ORDER BY {order_by} LIMIT 200"
    with transaction(write=False) as conn:
        return [book_view(conn, _row(r)) for r in all_rows(conn, sql, params)]


def get_book(book_id: int, viewer: dict | None = None) -> dict:
    with transaction(write=False) as conn:
        book = _book(conn, book_id)
        allowed = book["status"] in ("ON_SALE", "SOLD") or (
            viewer and (viewer["id"] == book["seller_id"] or viewer["role"] == "admin"))
        if not allowed and viewer:
            # 买家应能回看自己买过/正在交易的书籍详情，即使它已锁定或下架
            allowed = bool(one(conn, "SELECT 1 FROM orders WHERE book_id=? AND buyer_id=?", (book_id, viewer["id"])))
        if not allowed:
            fail("NOT_FOUND", "书籍不存在或已下架")
        return book_view(conn, book)


def create_book(user: dict, *, title: str, author: str, isbn: str, edition: str,
                category_id: int, condition_text: str, price_cents: int, description: str) -> dict:
    title = (title or "").strip()
    if not title or len(title) > 100:
        fail("VALIDATION_ERROR", "书名不能为空且不超过 100 字", details=[{"field": "title"}])
    if not isinstance(price_cents, int) or price_cents <= 0 or price_cents > 1000000:
        fail("VALIDATION_ERROR", "价格必须是 1–10000 元之间的整数分", details=[{"field": "price_cents"}])
    if not (condition_text or "").strip():
        fail("VALIDATION_ERROR", "请填写成色说明", details=[{"field": "condition_text"}])
    stamp = now_iso()
    with transaction() as conn:
        if not one(conn, "SELECT id FROM categories WHERE id=?", (category_id,)):
            fail("VALIDATION_ERROR", "请选择有效的分类", details=[{"field": "category_id"}])
        cur = conn.execute(
            """INSERT INTO books(seller_id,category_id,title,author,isbn,edition,condition_text,price_cents,description,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'PENDING_REVIEW',?,?)""",
            (user["id"], category_id, title, (author or "").strip(), (isbn or "").strip(), (edition or "").strip(),
             condition_text.strip(), price_cents, (description or "").strip(), stamp, stamp),
        )
        _notify_admins(conn, "待审核新书", f"《{title}》等待内容审核。", "/admin")
        return book_view(conn, _book(conn, cur.lastrowid))


def update_book(user: dict, book_id: int, *, title: str, author: str, isbn: str,
                edition: str, category_id: int, condition_text: str, price_cents: int, description: str) -> dict:
    title = (title or "").strip()
    if not title or len(title) > 100:
        fail("VALIDATION_ERROR", "书名不能为空且不超过 100 字", details=[{"field": "title"}])
    if not (condition_text or "").strip():
        fail("VALIDATION_ERROR", "请填写成色说明", details=[{"field": "condition_text"}])
    if not isinstance(price_cents, int) or price_cents <= 0 or price_cents > 1000000:
        fail("VALIDATION_ERROR", "价格必须是 1–10000 元之间的整数分", details=[{"field": "price_cents"}])
    with transaction() as conn:
        book = _book(conn, book_id)
        if book["status"] == "BANNED":
            fail("BOOK_STATE_CONFLICT", "被封禁的书籍不能编辑")
        if user["id"] != book["seller_id"] and user["role"] != "admin":
            fail("NOT_YOUR_RESOURCE", "只能编辑自己发布的书籍")
        if not one(conn, "SELECT id FROM categories WHERE id=?", (category_id,)):
            fail("VALIDATION_ERROR", "请选择有效的分类", details=[{"field": "category_id"}])
        conn.execute(
            """UPDATE books SET title=?,author=?,isbn=?,edition=?,category_id=?,condition_text=?,price_cents=?,description=?,updated_at=?
               WHERE id=?""",
            (title, (author or "").strip(), (isbn or "").strip(), (edition or "").strip(),
             category_id, condition_text.strip(), price_cents, (description or "").strip(), now_iso(), book_id),
        )
        return book_view(conn, _book(conn, book_id))


def book_action(user: dict, book_id: int, event: str, *, reason: str = "", internal: bool = False) -> dict:
    actor = SYSTEM if internal else user
    allowed_internal = {"lock", "unlock", "mark_sold", "withdraw"}
    if event in allowed_internal and not internal and user["role"] != "admin":
        fail("FORBIDDEN", "该状态转移由系统自动触发")
    with transaction() as conn:
        book = _book(conn, book_id)
        _book_transition(conn, book, event, actor, reason)
        return book_view(conn, _book(conn, book_id))


def upload_cover(user: dict, book_id: int, filename: str, content: bytes) -> dict:
    if len(content) > config.MAX_UPLOAD_BYTES:
        fail("UPLOAD_REJECTED", "图片大小不能超过 2MB")
    signatures = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"),
                  (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"RIFF", ".webp"))
    suffix = next((s for sig, s in signatures if content.startswith(sig)), None)
    if not suffix:
        fail("UPLOAD_REJECTED", "仅支持 JPG / PNG / GIF / WEBP 图片")
    with transaction() as conn:
        book = _book(conn, book_id)
        if book["seller_id"] != user["id"] and user["role"] != "admin":
            fail("NOT_YOUR_RESOURCE", "只能上传自己书籍的封面")
        name = f"cover-{book_id}-{uuid.uuid4().hex[:10]}{suffix}"
        path = config.UPLOAD_DIR / name
        path.write_bytes(content)
        try:
            if book["cover_path"]:
                old = config.UPLOAD_DIR / book["cover_path"]
                if old.exists() and old.is_file():
                    old.unlink()
            conn.execute("UPDATE books SET cover_path=?, updated_at=? WHERE id=?", (name, now_iso(), book_id))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return book_view(conn, _book(conn, book_id))


# ---------------------------------------------------------------- 订单与支付

def _new_order_no(conn) -> str:
    prefix = "BR" + utcnow().strftime("%Y%m%d")
    for _ in range(50):
        candidate = prefix + f"{secrets.randbelow(100000):05d}"
        if not one(conn, "SELECT id FROM orders WHERE order_no=?", (candidate,)):
            return candidate
    fail("VALIDATION_ERROR", "订单号生成失败，请稍后重试")


def create_order(user: dict, *, book_id: int, handover_method: str, remark: str = "",
                 idempotency_key: str | None = None) -> dict:
    if handover_method not in HANDOVER_METHODS:
        fail("VALIDATION_ERROR", "请选择有效的交接方式", details=[{"field": "handover_method"}])
    with transaction() as conn:
        def producer():
            book = _book(conn, book_id)
            if book["seller_id"] == user["id"]:
                fail("VALIDATION_ERROR", "不能购买自己发布的书籍")
            if book["status"] != "ON_SALE":
                fail("BOOK_ALREADY_LOCKED", "这本书已被锁定或下架，看看相似教材吧")
            active = one(conn, "SELECT COUNT(*) AS n FROM orders WHERE buyer_id=? AND status='CREATED'",
                         (user["id"],))["n"]
            if active >= config.MAX_ACTIVE_UNPAID_ORDERS:
                fail("RATE_LIMITED",
                     f"你已有 {active} 笔待支付订单，请先支付或取消后再下单",
                     details=[{"field": "orders"}])
            now = utcnow()
            stamp = now.isoformat(timespec="seconds")
            order_no = _new_order_no(conn)
            cur = conn.execute(
                """INSERT INTO orders(order_no,buyer_id,seller_id,book_id,status,total_cents,unit_price_snapshot,condition_snapshot,
                   book_title_snapshot,buyer_nickname_snapshot,handover_method,remark,expires_at,created_at,updated_at)
                   VALUES (?,?,?,?,'CREATED',?,?,?,?,?,?,?,?,?,?)""",
                (order_no, user["id"], book["seller_id"], book_id, book["price_cents"], book["price_cents"],
                 book["condition_text"], book["title"], user["nickname"], handover_method,
                 (remark or "").strip()[:200], domain.pay_deadline(now).isoformat(timespec="seconds"), stamp, stamp),
            )
            order = _order(conn, cur.lastrowid)
            _log_order(conn, order, "create", "CREATED", user, "创建订单，锁书 30 分钟")
            _book_transition(conn, book, "lock", SYSTEM)
            _notify(conn, book["seller_id"], "ORDER", "新订单待支付",
                    f"买家 {user['nickname']} 下单了《{book['title']}》。", f"/orders/{order['id']}")
            return order_view(conn, _order(conn, order["id"]), user)

        return _idem(conn, user, idempotency_key, "create_order", producer)


def pay_order(user: dict, order_id: int, *, channel: str, amount_cents: int | None = None) -> dict:
    if channel not in ("CAMPUS_CARD", "MOCK_THIRD_PARTY"):
        fail("VALIDATION_ERROR", "请选择有效的支付方式", details=[{"field": "channel"}])
    with transaction() as conn:
        order = _order(conn, order_id)
        if order["buyer_id"] != user["id"] and user["role"] != "admin":
            fail("NOT_YOUR_RESOURCE", "只能支付自己的订单")
        amount = int(amount_cents) if amount_cents is not None else order["total_cents"]
        domain.transit_order(order, "pay", utcnow(), amount_cents=amount,
                             actor_id=user["id"], role=user["role"])
        stamp = now_iso()
        txn_id = "MOCK" + uuid.uuid4().hex[:18].upper()
        conn.execute(
            """INSERT INTO payments(order_id,order_no,channel,amount_cents,status,provider_txn_id,created_at,updated_at)
               VALUES (?,?,?,?,'SUCCEEDED',?,?,?)""",
            (order_id, order["order_no"], channel, amount, txn_id, stamp, stamp),
        )
        updated = _transition(conn, order, "pay", user, amount_cents=amount)
        payment = _row(one(conn, "SELECT * FROM payments WHERE order_id=?", (order_id,)))
        return {"order": order_view(conn, updated, user), "payment": payment}


def cancel_order(user: dict, order_id: int, *, note: str = "") -> dict:
    with transaction() as conn:
        order = _order(conn, order_id)
        _require_party(order, user)
        updated = _transition(conn, order, "cancel", user, note)
        return order_view(conn, updated, user)


def ship_order(user: dict, order_id: int, *, handover_point: str) -> dict:
    if handover_point not in HANDOVER_METHODS:
        fail("VALIDATION_ERROR", "请选择有效的交接点", details=[{"field": "handover_point"}])
    with transaction() as conn:
        order = _order(conn, order_id)
        _require_party(order, user)
        code = f"{secrets.randbelow(10000):04d}"
        updated = _transition(conn, order, "ship", user, code=code, handover_point=handover_point)
        result = order_view(conn, updated, user)
        result["handover_code"] = code
        return result


def apply_refund(user: dict, order_id: int, *, reason: str, evidence: str = "",
                 idempotency_key: str | None = None) -> dict:
    if not (reason or "").strip():
        fail("VALIDATION_ERROR", "请填写退款原因", details=[{"field": "reason"}])
    with transaction() as conn:
        def producer():
            order = _order(conn, order_id)
            _require_party(order, user)
            now = utcnow()
            domain.transit_order(order, "apply_refund", now, actor_id=user["id"], role=user["role"],
                                 evidence=evidence)
            stamp = now.isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO refunds(order_id,applicant_id,amount_cents,reason,evidence,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,'PENDING',?,?)""",
                (order_id, user["id"], order["total_cents"], reason.strip(), evidence.strip(), stamp, stamp),
            )
            updated = _transition(conn, order, "apply_refund", user, reason, evidence=evidence)
            return order_view(conn, updated, user)

        return _idem(conn, user, idempotency_key, "apply_refund", producer)


def handle_refund(admin: dict, refund_id: int, *, approve: bool, note: str = "") -> dict:
    if admin["role"] != "admin":
        fail("FORBIDDEN", "仅管理员可处理退款")
    with transaction() as conn:
        refund = one(conn, "SELECT * FROM refunds WHERE id=?", (refund_id,))
        if not refund:
            fail("NOT_FOUND", "退款单不存在")
        if refund["status"] != "PENDING":
            fail("REPORT_ALREADY_HANDLED", "该退款申请已处理")
        order = _order(conn, refund["order_id"])
        if order["status"] != "REFUNDING":
            fail("ORDER_STATE_CONFLICT", "订单当前状态无法处理退款")
        updated = _transition(conn, order, "approve" if approve else "reject", admin, note)
        return {"order": order_view(conn, updated, admin), "refund": _row(one(conn, "SELECT * FROM refunds WHERE id=?", (refund_id,)))}


def confirm_receipt(user: dict, order_id: int, *, code: str,
                    idempotency_key: str | None = None) -> dict:
    with transaction() as conn:
        def producer():
            order = _order(conn, order_id)
            updated = _transition(conn, order, "confirm", user, code=code)
            return order_view(conn, updated, user)

        return _idem(conn, user, idempotency_key, "confirm_receipt", producer)


def get_order(order_id: int, viewer: dict) -> dict:
    with transaction(write=False) as conn:
        order = _order(conn, order_id)
        _require_party(order, viewer)
        return order_view(conn, order, viewer)


def list_orders(user: dict, scope: str = "all") -> list[dict]:
    conditions, params = [], []
    if scope == "buying":
        conditions.append("buyer_id=?")
        params.append(user["id"])
    elif scope == "selling":
        conditions.append("seller_id=?")
        params.append(user["id"])
    elif user["role"] != "admin":
        conditions.append("(buyer_id=? OR seller_id=?)")
        params.extend([user["id"], user["id"]])
    sql = "SELECT * FROM orders"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC LIMIT 200"
    with transaction(write=False) as conn:
        return [order_view(conn, _row(r), user) for r in all_rows(conn, sql, params)]


def order_view(conn, order: dict, viewer: dict | None = None) -> dict:
    data = dict(order)
    # 交接码只对卖家和管理员可见；买家必须线下向卖家索取，不能从 API 直接读取。
    if not viewer or (viewer["id"] != order["seller_id"] and viewer["role"] != "admin"):
        data["handover_code"] = None
    data["status_label"] = domain.ORDER_LABELS[order["status"]]
    data["amount_text"] = domain.yuan(order["total_cents"])
    data["handover_label"] = HANDOVER_LABELS.get(order.get("handover_method") or "", "")
    data["handover_point_label"] = HANDOVER_LABELS.get(order.get("handover_point") or "", "")
    buyer = _user(conn, order["buyer_id"])
    seller = _user(conn, order["seller_id"])
    data["buyer_nickname"] = buyer["nickname"]
    data["seller_nickname"] = seller["nickname"]
    book = _book(conn, order["book_id"])
    data["book_cover_url"] = "/static/uploads/" + book["cover_path"] if book["cover_path"] else ""
    data["payment"] = _row(one(conn, "SELECT * FROM payments WHERE order_id=? AND status!='FAILED' ORDER BY id DESC LIMIT 1", (order["id"],)))
    data["refund"] = _row(one(conn, "SELECT * FROM refunds WHERE order_id=? ORDER BY id DESC LIMIT 1", (order["id"],)))
    data["events"] = [_row(r) for r in all_rows(conn, "SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order["id"],))]
    data["reviews"] = [_row(r) for r in all_rows(conn, "SELECT * FROM reviews WHERE order_id=? ORDER BY id", (order["id"],))]
    data["actions"] = available_actions(order, viewer)
    data["can_review"] = order["status"] == "COMPLETED" and bool(
        viewer and viewer["id"] in (order["buyer_id"], order["seller_id"]) and
        all(r["reviewer_id"] != viewer["id"] for r in data["reviews"]))
    if order["status"] == "CREATED":
        deadline = domain.parse_dt(order["expires_at"])
        data["seconds_left"] = max(0, int((deadline - utcnow()).total_seconds())) if deadline else 0
    else:
        data["seconds_left"] = 0
    return data


def available_actions(order: dict, viewer: dict | None) -> list[dict]:
    if not viewer:
        return []
    uid, role = viewer["id"], viewer["role"]
    actions = []
    if order["status"] == "CREATED":
        if uid == order["buyer_id"] or role == "admin":
            actions.extend([
                {"event": "pay", "label": "完成支付"},
                {"event": "cancel", "label": "取消订单"},
            ])
    elif order["status"] == "PAID":
        if uid == order["seller_id"] or role == "admin":
            actions.append({"event": "ship", "label": "确认交接方式"})
        if uid == order["buyer_id"] or role == "admin":
            actions.append({"event": "apply_refund", "label": "申请退款"})
    elif order["status"] == "SHIPPED":
        if uid == order["buyer_id"] or role == "admin":
            actions.extend([
                {"event": "confirm", "label": "我已收到书，确认收货"},
                {"event": "apply_refund", "label": "申请退款"},
            ])
    elif order["status"] == "REFUNDING" and role == "admin":
        actions.extend([
            {"event": "approve", "label": "同意退款"},
            {"event": "reject", "label": "驳回退款"},
        ])
    return actions


def sweep_timeouts() -> dict:
    cancelled = closed = 0
    with transaction() as conn:
        now = utcnow()
        expired = all_rows(conn, "SELECT * FROM orders WHERE status='CREATED' AND expires_at<=?",
                           (now.isoformat(timespec="seconds"),))
        for order in expired:
            try:
                _transition(conn, _row(order), "cancel", SYSTEM, "支付超时 30 分钟，系统自动取消")
                cancelled += 1
            except AppError:
                continue
        threshold = (now - timedelta(days=config.AUTO_CLOSE_DAYS)).isoformat(timespec="seconds")
        finished = all_rows(conn, "SELECT * FROM orders WHERE status='COMPLETED' AND completed_at<=?", (threshold,))
        for order in finished:
            try:
                _transition(conn, _row(order), "auto_close", SYSTEM, "完成满 30 天，自动关闭")
                closed += 1
            except AppError:
                continue
    return {"cancelled": cancelled, "closed": closed}


# ---------------------------------------------------------------- 评价与信誉

def create_review(user: dict, order_id: int, *, rating: int, content: str) -> dict:
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        fail("VALIDATION_ERROR", "评分必须是 1–5 的整数", details=[{"field": "rating"}])
    if not (content or "").strip():
        fail("VALIDATION_ERROR", "请填写评价内容", details=[{"field": "content"}])
    with transaction() as conn:
        order = _order(conn, order_id)
        if order["status"] != "COMPLETED":
            fail("ORDER_STATE_CONFLICT", "订单完成后、关闭前才能评价")
        if user["id"] not in (order["buyer_id"], order["seller_id"]):
            fail("NOT_YOUR_RESOURCE", "只有交易双方可以评价")
        if one(conn, "SELECT id FROM reviews WHERE order_id=? AND reviewer_id=?", (order_id, user["id"])):
            fail("DUPLICATE_REVIEW", "你已经评价过这笔订单")
        reviewee_id = order["seller_id"] if user["id"] == order["buyer_id"] else order["buyer_id"]
        stamp = now_iso()
        cur = conn.execute(
            """INSERT INTO reviews(order_id,reviewer_id,reviewee_id,rating,content,created_at)
               VALUES (?,?,?,?,?,?)""",
            (order_id, user["id"], reviewee_id, rating, content.strip(), stamp),
        )
        stats = one(conn, "SELECT AVG(rating) AS avg_rating, COUNT(*) AS n FROM reviews WHERE reviewee_id=?", (reviewee_id,))
        conn.execute("UPDATE users SET reputation=?, updated_at=? WHERE id=?",
                     (round(stats["avg_rating"], 2), stamp, reviewee_id))
        _notify(conn, reviewee_id, "REVIEW", "收到一条评价",
                f"你在订单 {order['order_no']} 中收到 {rating} 星评价。", f"/orders/{order_id}")
        return _row(one(conn, "SELECT * FROM reviews WHERE id=?", (cur.lastrowid,)))


def list_reviews_for_user(user_id: int) -> list[dict]:
    with transaction(write=False) as conn:
        rows = all_rows(
            conn,
            """SELECT r.*, u.nickname AS reviewer_nickname, o.order_no FROM reviews r
               JOIN users u ON u.id=r.reviewer_id JOIN orders o ON o.id=r.order_id
               WHERE r.reviewee_id=? ORDER BY r.id DESC""",
            (user_id,),
        )
        return [_row(r) for r in rows]


def list_reviews_for_order(order_id: int) -> list[dict]:
    with transaction(write=False) as conn:
        return [_row(r) for r in all_rows(conn, "SELECT * FROM reviews WHERE order_id=? ORDER BY id", (order_id,))]


def report_target(user: dict, *, target_type: str, target_id: int, reason: str, detail: str = "") -> dict:
    if target_type not in ("BOOK", "REVIEW", "USER"):
        fail("VALIDATION_ERROR", "无效的举报类型")
    if not (reason or "").strip():
        fail("VALIDATION_ERROR", "请填写举报原因", details=[{"field": "reason"}])
    table = {"BOOK": "books", "REVIEW": "reviews", "USER": "users"}[target_type]
    with transaction() as conn:
        if not one(conn, f"SELECT id FROM {table} WHERE id=?", (target_id,)):
            fail("NOT_FOUND", "举报目标不存在")
        stamp = now_iso()
        cur = conn.execute(
            """INSERT INTO reports(reporter_id,target_type,target_id,reason,detail,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (user["id"], target_type, target_id, reason.strip(), detail.strip(), stamp, stamp),
        )
        _notify_admins(conn, "待处理举报", f"有新的{target_type}举报，请及时处理。", "/admin")
        return _row(one(conn, "SELECT * FROM reports WHERE id=?", (cur.lastrowid,)))


def handle_report(admin: dict, report_id: int, *, resolve: bool, note: str = "") -> dict:
    if admin["role"] != "admin":
        fail("FORBIDDEN", "仅管理员可处理举报")
    with transaction() as conn:
        report = one(conn, "SELECT * FROM reports WHERE id=?", (report_id,))
        if not report:
            fail("NOT_FOUND", "举报不存在")
        if report["status"] != "PENDING":
            fail("REPORT_ALREADY_HANDLED", "该举报已处理")
        status = "RESOLVED" if resolve else "REJECTED"
        conn.execute("UPDATE reports SET status=?, handled_by=?, handle_note=?, updated_at=? WHERE id=?",
                     (status, admin["id"], note, now_iso(), report_id))
        if resolve and report["target_type"] == "BOOK":
            book = one(conn, "SELECT * FROM books WHERE id=?", (report["target_id"],))
            if book and book["status"] != "BANNED":
                _book_transition(conn, _row(book), "ban", admin, note or "举报成立")
        _notify(conn, report["reporter_id"], "REPORT", "举报处理结果",
                "你提交的举报已处理：" + ("举报成立" if resolve else "未发现违规"), "/notifications")
        return _row(one(conn, "SELECT * FROM reports WHERE id=?", (report_id,)))


# ---------------------------------------------------------------- 消息通知

def list_notifications(user: dict, unread_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM notifications WHERE user_id=?"
    params = [user["id"]]
    if unread_only:
        sql += " AND is_read=0"
    sql += " ORDER BY id DESC LIMIT 100"
    with transaction(write=False) as conn:
        rows = [_row(r) for r in all_rows(conn, sql, params)]
        for row in rows:
            row["type_label"] = NOTICE_LABELS.get(row["type"], "通知")
        return rows


def unread_count(user: dict) -> int:
    with transaction(write=False) as conn:
        return one(conn, "SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND is_read=0", (user["id"],))["n"]


def mark_notification_read(user: dict, notification_id: int) -> dict:
    with transaction() as conn:
        row = one(conn, "SELECT * FROM notifications WHERE id=? AND user_id=?", (notification_id, user["id"]))
        if not row:
            fail("NOT_FOUND", "通知不存在")
        conn.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notification_id,))
        return {"ok": True}


def mark_all_read(user: dict) -> dict:
    with transaction() as conn:
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
        return {"ok": True}


# ---------------------------------------------------------------- 后台运营

def admin_dashboard() -> dict:
    with transaction(write=False) as conn:
        count = lambda sql, params=(): one(conn, sql, params)["n"]
        revenue = one(conn, "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payments WHERE status='SUCCEEDED'")["s"]
        statuses = {row["status"]: row["n"] for row in all_rows(conn, "SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}
        return {
            "users": count("SELECT COUNT(*) AS n FROM users"),
            "books_on_sale": count("SELECT COUNT(*) AS n FROM books WHERE status='ON_SALE'"),
            "pending_books": count("SELECT COUNT(*) AS n FROM books WHERE status='PENDING_REVIEW'"),
            "pending_reports": count("SELECT COUNT(*) AS n FROM reports WHERE status='PENDING'"),
            "pending_refunds": count("SELECT COUNT(*) AS n FROM refunds WHERE status='PENDING'"),
            "orders": count("SELECT COUNT(*) AS n FROM orders"),
            "revenue_cents": revenue,
            "statuses": statuses,
            "recent_orders": [_row(r) for r in all_rows(
                conn, "SELECT o.*, u.nickname AS buyer_nickname FROM orders o JOIN users u ON u.id=o.buyer_id ORDER BY o.id DESC LIMIT 8")],
        }


def list_users() -> list[dict]:
    with transaction(write=False) as conn:
        return [_public_user(_row(r)) for r in all_rows(conn, "SELECT * FROM users ORDER BY id")]


def pending_books() -> list[dict]:
    with transaction(write=False) as conn:
        return [book_view(conn, _row(r)) for r in all_rows(conn, "SELECT * FROM books WHERE status='PENDING_REVIEW' ORDER BY id")]


def list_refunds() -> list[dict]:
    with transaction(write=False) as conn:
        rows = all_rows(
            conn,
            """SELECT r.*, o.order_no, o.status AS order_status, b.nickname AS applicant_nickname
               FROM refunds r JOIN orders o ON o.id=r.order_id JOIN users b ON b.id=r.applicant_id ORDER BY r.id DESC""",
        )
        return [_row(r) for r in rows]


def list_reports() -> list[dict]:
    with transaction(write=False) as conn:
        rows = all_rows(
            conn,
            """SELECT rp.*, u.nickname AS reporter_nickname FROM reports rp
               JOIN users u ON u.id=rp.reporter_id ORDER BY rp.id DESC""",
        )
        return [_row(r) for r in rows]
