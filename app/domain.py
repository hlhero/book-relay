"""纯业务规则层：状态机、守卫与常量。禁止依赖 FastAPI / sqlite3。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import AUTO_CLOSE_DAYS, PAY_WINDOW_MINUTES, REFUND_WINDOW_HOURS
from .errors import AppError

ORDER_STATES = (
    "CREATED", "PAID", "SHIPPED", "COMPLETED", "CANCELLED", "REFUNDING", "REFUNDED", "CLOSED",
)
BOOK_STATES = ("DRAFT", "PENDING_REVIEW", "ON_SALE", "LOCKED", "SOLD", "OFF_SHELF", "BANNED")
ORDER_TERMINAL = ("CANCELLED", "REFUNDED", "CLOSED")

ORDER_LABELS = {
    "CREATED": "待支付", "PAID": "已支付", "SHIPPED": "待交接", "COMPLETED": "已完成",
    "CANCELLED": "已取消", "REFUNDING": "退款中", "REFUNDED": "已退款", "CLOSED": "已关闭",
}
BOOK_LABELS = {
    "DRAFT": "草稿", "PENDING_REVIEW": "审核中", "ON_SALE": "在售", "LOCKED": "已锁定",
    "SOLD": "已售", "OFF_SHELF": "已下架", "BANNED": "已封禁",
}
EVENT_LABELS = {
    "pay": "完成支付", "cancel": "取消订单", "ship": "确认交接方式", "apply_refund": "申请退款",
    "confirm": "确认收货", "approve": "同意退款", "reject": "驳回退款", "auto_close": "自动关闭",
}


@dataclass(frozen=True)
class Transition:
    target: str
    action: str
    guard: str | None = None


ORDER_TRANSITIONS = {
    ("CREATED", "pay"): Transition("PAID", "write_payment_record", "amount_matches"),
    ("CREATED", "cancel"): Transition("CANCELLED", "release_stock_lock", "buyer_or_timeout"),
    ("PAID", "ship"): Transition("SHIPPED", "generate_handover_code", "seller_only"),
    ("PAID", "apply_refund"): Transition("REFUNDING", "create_refund_order", "refund_before_ship"),
    ("SHIPPED", "confirm"): Transition("COMPLETED", "settle_seller_amount", "handover_code_matches"),
    ("SHIPPED", "apply_refund"): Transition("REFUNDING", "freeze_settlement", "refund_after_ship"),
    ("REFUNDING", "approve"): Transition("REFUNDED", "refund_to_user", "admin_only"),
    ("REFUNDING", "reject"): Transition("PAID", "notify_applicant", "admin_only"),
    ("COMPLETED", "auto_close"): Transition("CLOSED", None, "close_window_elapsed"),
}

BOOK_TRANSITIONS = {
    ("DRAFT", "submit"): Transition("PENDING_REVIEW", None, None),
    ("PENDING_REVIEW", "approve"): Transition("ON_SALE", None, "admin_only"),
    ("PENDING_REVIEW", "reject"): Transition("OFF_SHELF", None, "admin_only"),
    ("ON_SALE", "lock"): Transition("LOCKED", "notify_seller", None),
    ("LOCKED", "unlock"): Transition("ON_SALE", "notify_seller", None),
    ("LOCKED", "withdraw"): Transition("OFF_SHELF", None, "admin_only"),
    ("LOCKED", "mark_sold"): Transition("SOLD", None, None),
    ("ON_SALE", "off_shelf"): Transition("OFF_SHELF", None, "seller_only"),
    ("OFF_SHELF", "list"): Transition("ON_SALE", None, "seller_only"),
    ("SOLD", "relist"): Transition("ON_SALE", None, "seller_only"),
}
for _state in BOOK_STATES:
    if _state != "BANNED":
        BOOK_TRANSITIONS[(_state, "ban")] = Transition("BANNED", None, "admin_only")
BOOK_TRANSITIONS[("BANNED", "unban")] = Transition("OFF_SHELF", None, "admin_only")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def pay_deadline(created_at: datetime) -> datetime:
    return created_at + timedelta(minutes=PAY_WINDOW_MINUTES)


def refund_deadline(order: dict, now: datetime) -> datetime | None:
    base = parse_dt(order.get("paid_at")) if order.get("status") == "PAID" else parse_dt(order.get("shipped_at"))
    return base + timedelta(hours=REFUND_WINDOW_HOURS) if base else None


def check_order_guard(guard: str | None, order: dict, now: datetime, **ctx) -> None:
    if not guard:
        return
    if guard == "amount_matches":
        deadline = parse_dt(order.get("expires_at"))
        if deadline and now > deadline:
            raise AppError("ORDER_STATE_CONFLICT", "订单已超过 30 分钟支付时限，请重新下单")
        amount = ctx.get("amount_cents")
        if amount is not None and int(amount) != int(order["total_cents"]):
            raise AppError("PAYMENT_FAILED", "支付金额与订单金额不一致", details=[{"field": "amount_cents"}])
    elif guard == "buyer_or_timeout":
        expired_at = parse_dt(order.get("expires_at"))
        expired = bool(expired_at and now >= expired_at)
        if ctx.get("actor_id") not in (order["buyer_id"], None) and ctx.get("role") != "admin" and not expired:
            raise AppError("FORBIDDEN", "只有买家或管理员可以取消该订单")
    elif guard == "seller_only":
        if ctx.get("actor_id") != order.get("seller_id") and ctx.get("role") != "admin":
            raise AppError("FORBIDDEN", "只有卖家可以确认交接方式")
    elif guard == "refund_before_ship":
        deadline = refund_deadline(order, now)
        if deadline is None or now > deadline:
            raise AppError("REFUND_WINDOW_CLOSED", "支付已超过 48 小时，无法申请退款")
    elif guard == "refund_after_ship":
        deadline = refund_deadline(order, now)
        if deadline is None or now > deadline:
            raise AppError("REFUND_WINDOW_CLOSED", "交接已超过 48 小时，无法申请退款")
        if not (ctx.get("evidence") or "").strip():
            raise AppError("VALIDATION_ERROR", "交接后申请退款必须填写纠纷说明", details=[{"field": "evidence"}])
    elif guard == "handover_code_matches":
        if ctx.get("actor_id") != order.get("buyer_id") and ctx.get("role") != "admin":
            raise AppError("FORBIDDEN", "只有买家可以确认收货")
        expected = str(order.get("handover_code") or "")
        if not expected or str(ctx.get("code") or "").strip() != expected:
            raise AppError("HANDOVER_CODE_MISMATCH", "交接码不正确，请向卖家核对后再试", details=[{"field": "code"}])
    elif guard == "admin_only":
        if ctx.get("role") != "admin":
            raise AppError("FORBIDDEN", "该操作仅管理员可执行")
    elif guard == "close_window_elapsed":
        completed = parse_dt(order.get("completed_at"))
        if completed and now < completed + timedelta(days=AUTO_CLOSE_DAYS):
            raise AppError("ORDER_STATE_CONFLICT", "订单尚未满足自动关闭条件")
    else:
        raise AppError("VALIDATION_ERROR", f"未知守卫：{guard}")


def transit_order(order: dict, event: str, now: datetime, **ctx) -> Transition:
    rule = ORDER_TRANSITIONS.get((order["status"], event))
    if rule is None:
        raise AppError(
            "ORDER_STATE_CONFLICT",
            f"当前状态「{ORDER_LABELS.get(order['status'], order['status'])}」不允许执行「{EVENT_LABELS.get(event, event)}」",
            details=[{"field": "event", "reason": f"{order['status']}+{event} 不在状态转移表中"}],
        )
    check_order_guard(rule.guard, order, now, **ctx)
    return rule


def check_book_guard(guard: str | None, book: dict, **ctx) -> None:
    if not guard:
        return
    if guard == "admin_only":
        if ctx.get("role") != "admin":
            raise AppError("FORBIDDEN", "该操作仅管理员可执行")
    elif guard == "seller_only":
        if ctx.get("actor_id") != book["seller_id"] and ctx.get("role") != "admin":
            raise AppError("FORBIDDEN", "只有卖家可以管理这本书")
    else:
        raise AppError("VALIDATION_ERROR", f"未知守卫：{guard}")


def transit_book(book: dict, event: str, **ctx) -> Transition:
    rule = BOOK_TRANSITIONS.get((book["status"], event))
    if rule is None:
        raise AppError(
            "BOOK_STATE_CONFLICT",
            f"书籍当前「{BOOK_LABELS.get(book['status'], book['status'])}」状态不允许执行该操作",
        )
    check_book_guard(rule.guard, book, **ctx)
    return rule


def yuan(cents: int) -> str:
    return f"{cents / 100:.2f}"


def reputation_after(ratings: list[int], old_avg: float, old_count: int) -> float:
    if not ratings:
        return round(old_avg, 2)
    total = old_avg * old_count + sum(ratings)
    return round(min(5.0, max(0.0, total / (old_count + len(ratings)))), 2)
