from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db, domain, services
from .errors import AppError
from .security import allow_rate, make_session, read_session, verify_password

logger = logging.getLogger("bookrelay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

templates = Jinja2Templates(directory=str(config.TEMPLATE_DIR))
templates.env.globals.update(
    ORDER_LABELS=domain.ORDER_LABELS, BOOK_LABELS=domain.BOOK_LABELS,
    HANDOVER_LABELS=services.HANDOVER_LABELS, yuan=domain.yuan,
)


async def _periodic_sweep():
    while True:
        await asyncio.sleep(60)
        try:
            result = await asyncio.to_thread(services.sweep_timeouts)
            if result["cancelled"] or result["closed"]:
                logger.info("timeout sweep: cancelled=%s closed=%s", result["cancelled"], result["closed"])
        except Exception:
            logger.exception("timeout sweep failed")


def _audit_credentials() -> None:
    """生产启动时拒绝已知默认管理员口令，并提示仍在使用演示学生口令的数据。"""
    if not config.IS_PRODUCTION:
        return
    with db.transaction(write=False) as conn:
        admin = db.one(conn, "SELECT password_hash FROM users WHERE role='admin' ORDER BY id LIMIT 1")
        if admin and verify_password("Admin@135", admin["password_hash"]):
            raise RuntimeError("检测到管理员仍在使用默认口令 Admin@135，请在数据库中先修改密码再启动")
        demo_rows = db.all_rows(
            conn, "SELECT email,password_hash FROM users WHERE role='student' AND email LIKE '%@bookrelay.local' LIMIT 100")
        demo_users = [row["email"] for row in demo_rows if verify_password("Student@123", row["password_hash"])]
        if demo_users:
            logger.warning("检测到演示学生口令仍在使用：%s；生产环境请删除或重置这些账号", ", ".join(demo_users))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if config.SECRET_KEY_IS_DEFAULT:
        logger.warning("BOOKRELAY_SECRET 未设置，当前使用开发密钥；生产环境必须通过环境变量注入")
    if config.IS_PRODUCTION and not config.COOKIE_SECURE:
        logger.warning("生产环境未开启 Secure Cookie；请设置 BOOKRELAY_COOKIE_SECURE=1 并启用 HTTPS")
    if config.IS_PRODUCTION and config.SEED_DEMO:
        logger.warning("生产环境仍开启演示数据（BOOKRELAY_SEED_DEMO=1），请确认不是误配置")
    _audit_credentials()
    services.sweep_timeouts()
    task = asyncio.create_task(_periodic_sweep())
    yield
    task.cancel()


app = FastAPI(
    title="书接力 BookRelay API", version="1.0.0",
    description="校园二手教材流转平台 · SQLite 模块化单体",
    lifespan=lifespan,
    docs_url="/docs" if config.DOCS_ENABLED else None,
    redoc_url="/redoc" if config.DOCS_ENABLED else None,
    openapi_url="/openapi.json" if config.DOCS_ENABLED else None,
)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin(request: Request) -> bool:
    """浏览器跨站写请求防护；curl/脚本不带 Origin 时保持可用。"""
    origin = request.headers.get("origin")
    if origin:
        if origin == "null" or origin in config.TRUSTED_ORIGINS:
            return origin in config.TRUSTED_ORIGINS
        try:
            parsed_origin = urlsplit(origin)
            origin_host = parsed_origin.hostname
        except ValueError:
            return False
        # 只信任 Host；反向代理必须 proxy_set_header Host $host，特殊域名用
        # BOOKRELAY_TRUSTED_ORIGINS 显式放行，避免伪造 X-Forwarded-Host 绕过。
        host = (request.headers.get("host") or "").split(":")[0]
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip().lower()
        return bool(origin_host and host and origin_host.lower() == host.lower()
                    and parsed_origin.scheme.lower() == proto)
    fetch_site = request.headers.get("sec-fetch-site")
    return not fetch_site or fetch_site in ("same-origin", "none")


@app.middleware("http")
async def security_guard(request: Request, call_next):
    if request.method in UNSAFE_METHODS:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > config.MAX_REQUEST_BYTES:
                    logger.warning("rejected oversized request: %s %s", request.method, request.url.path)
                    return JSONResponse(status_code=413, content={
                        "code": "PAYLOAD_TOO_LARGE", "message": "请求体超过大小限制",
                        "details": [], "trace_id": "size",
                    })
            except ValueError:
                pass
        if not _same_origin(request):
            logger.warning("rejected cross-site request: %s %s origin=%s",
                           request.method, request.url.path, request.headers.get("origin"))
            return JSONResponse(status_code=403, content={
                "code": "CSRF_REJECTED", "message": "请求来源校验失败，请从本站页面发起操作",
                "details": [], "trace_id": "csrf",
            })
    return await call_next(request)


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        logger.info("%s %s -> %s %.1fms", request.method, request.url.path,
                    response.status_code, (time.perf_counter() - started) * 1000)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    if (config.COOKIE_SECURE or request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path not in ("/docs", "/redoc", "/openapi.json"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'",
        )
    return response


# ---------------------------------------------------------------- 公共辅助

def _session_user(request: Request) -> dict | None:
    token = request.cookies.get(config.SESSION_COOKIE)
    parsed = read_session(token) if token else None
    if not parsed:
        return None
    uid, version = parsed
    try:
        user = services.get_user(uid)
    except AppError:
        return None
    if int(user.get("session_version", 0)) != version:
        return None
    return user


def optional_user(request: Request) -> dict | None:
    """页面展示用；封禁用户按未登录处理，避免旧 Cookie 继续渲染已登录态。"""
    user = _session_user(request)
    if not user or user["status"] == "banned":
        return None
    return user


def require_user(request: Request) -> dict:
    user = _session_user(request)
    if not user:
        raise AppError("UNAUTHORIZED", "请先登录")
    if user["status"] == "banned":
        raise AppError("ACCOUNT_BANNED", "账号已被封禁，请联系管理员")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise AppError("FORBIDDEN", "仅管理员可访问")
    return user


def page_context(request: Request, **kwargs) -> dict:
    user = optional_user(request)
    return {
        "request": request,
        "user": user,
        "unread": services.unread_count(user) if user else 0,
        "categories": services.list_categories(),
        **kwargs,
    }


def _client_ip(request: Request) -> str:
    # uvicorn 在受信代理下会把 X-Forwarded-For 解析到 client.host；
    # 直接暴露时该值就是 TCP 对端地址。
    return request.client.host if request.client else "unknown"


def _limit(key: str, *, limit: int, window_seconds: int) -> None:
    if not allow_rate(key, limit=limit, window_seconds=window_seconds):
        raise AppError("RATE_LIMITED", f"操作过于频繁，请 {window_seconds // 60 or 1} 分钟后再试")


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status, content=exc.as_dict())
    if exc.status == 401:
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)
    return templates.TemplateResponse(request, "error.html", page_context(
        request, error=exc, title="操作无法完成"), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "code": "VALIDATION_ERROR", "message": "请求参数不合法",
        "details": [{"field": ".".join(str(p) for p in e["loc"][1:]), "reason": e["msg"]} for e in exc.errors()],
        "trace_id": "validation",
    })


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={
            "code": "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
            "message": str(exc.detail), "details": [], "trace_id": "http",
        })
    err = AppError("NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR", str(exc.detail), exc.status_code)
    return templates.TemplateResponse(request, "error.html", page_context(request, error=err, title="页面未找到"),
                                      status_code=exc.status_code)


# ---------------------------------------------------------------- 请求模型

class RegisterIn(BaseModel):
    email: str
    nickname: str
    password: str
    student_id: str = ""
    security_question: str = "入学年份？"
    security_answer: str


class LoginIn(BaseModel):
    email: str
    password: str


class ResetIn(BaseModel):
    email: str
    security_answer: str
    new_password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class ProfileIn(BaseModel):
    nickname: str
    student_id: str = ""


class BookIn(BaseModel):
    title: str
    author: str = ""
    isbn: str = ""
    edition: str = ""
    category_id: int
    condition_text: str
    price_cents: int = Field(gt=0)
    description: str = ""


class BookActionIn(BaseModel):
    reason: str = ""


class OrderIn(BaseModel):
    book_id: int
    handover_method: str
    remark: str = ""


class PayIn(BaseModel):
    channel: str
    amount_cents: int | None = None


class CancelIn(BaseModel):
    note: str = ""


class ShipIn(BaseModel):
    handover_point: str


class RefundIn(BaseModel):
    reason: str
    evidence: str = ""


class ConfirmIn(BaseModel):
    code: str


class ReviewIn(BaseModel):
    rating: int
    content: str


class ReportIn(BaseModel):
    target_type: str
    target_id: int
    reason: str
    detail: str = ""


class UserStatusIn(BaseModel):
    status: str


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


def with_cookie(payload: dict, user: dict, status_code: int = 200) -> JSONResponse:
    resp = JSONResponse(content=payload, status_code=status_code)
    resp.set_cookie(config.SESSION_COOKIE, make_session(user["id"], user.get("session_version", 0)),
                    max_age=config.SESSION_MAX_AGE, path="/",
                    httponly=True, samesite="lax", secure=config.COOKIE_SECURE)
    return resp


# ---------------------------------------------------------------- REST API

@app.get("/api/v1/health")
def health():
    return {"status": "ok", "db": "sqlite"}


@app.get("/api/v1/categories")
def api_categories():
    return services.list_categories()


@app.post("/api/v1/auth/register")
def api_register(request: Request, payload: RegisterIn):
    _limit(f"register:{_client_ip(request)}", limit=30, window_seconds=3600)
    user = services.register(**payload.model_dump())
    return with_cookie({"user": user}, user, 201)


@app.post("/api/v1/auth/login")
def api_login(request: Request, payload: LoginIn):
    email = payload.email.strip().lower()
    _limit(f"login:{_client_ip(request)}:{email}", limit=30, window_seconds=300)
    user = services.login(**payload.model_dump())
    return with_cookie({"user": user}, user)


@app.post("/api/v1/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


@app.get("/api/v1/auth/me")
def api_me(request: Request):
    return require_user(request)


@app.post("/api/v1/auth/reset-password")
def api_reset(request: Request, payload: ResetIn):
    email = payload.email.strip().lower()
    _limit(f"reset:{_client_ip(request)}:{email}", limit=5, window_seconds=600)
    return services.reset_password(**payload.model_dump())


@app.post("/api/v1/auth/password")
def api_password(request: Request, payload: PasswordIn):
    return services.change_password(require_user(request), **payload.model_dump())


@app.patch("/api/v1/auth/profile")
def api_profile(request: Request, payload: ProfileIn):
    return services.update_profile(require_user(request), **payload.model_dump())


@app.get("/api/v1/books")
def api_books(request: Request, keyword: str = "", category_id: int = 0, sort: str = "latest",
              seller_id: int | None = None, status: str | None = None):
    if status and status != "ON_SALE":
        require_admin(request)
    return services.list_books(keyword=keyword, category_id=category_id, sort=sort,
                               seller_id=seller_id, status=status)


@app.post("/api/v1/books", status_code=201)
def api_create_book(request: Request, payload: BookIn):
    return services.create_book(require_user(request), **payload.model_dump())


@app.get("/api/v1/books/{book_id}")
def api_get_book(book_id: int, request: Request):
    return services.get_book(book_id, optional_user(request))


@app.patch("/api/v1/books/{book_id}")
def api_update_book(book_id: int, request: Request, payload: BookIn):
    return services.update_book(require_user(request), book_id, **payload.model_dump())


@app.post("/api/v1/books/{book_id}/actions/{event}")
def api_book_action(book_id: int, event: str, request: Request, payload: BookActionIn = Body(default=BookActionIn())):
    return services.book_action(require_user(request), book_id, event, reason=payload.reason)


@app.post("/api/v1/books/{book_id}/cover")
async def api_upload_cover(book_id: int, request: Request, file: UploadFile = File(...)):
    content = await file.read()
    return services.upload_cover(require_user(request), book_id, file.filename or "cover", content)


@app.post("/api/v1/orders", status_code=201)
def api_create_order(request: Request, payload: OrderIn,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return services.create_order(require_user(request), **payload.model_dump(),
                                 idempotency_key=idempotency_key)


@app.get("/api/v1/orders")
def api_orders(request: Request, scope: str = "all"):
    return services.list_orders(require_user(request), scope)


@app.get("/api/v1/orders/{order_id}")
def api_get_order(order_id: int, request: Request):
    return services.get_order(order_id, require_user(request))


@app.post("/api/v1/orders/{order_id}/pay")
def api_pay(order_id: int, request: Request, payload: PayIn):
    return services.pay_order(require_user(request), order_id, **payload.model_dump())


@app.post("/api/v1/orders/{order_id}/cancel")
def api_cancel(order_id: int, request: Request, payload: CancelIn = Body(default=CancelIn())):
    return services.cancel_order(require_user(request), order_id, note=payload.note)


@app.post("/api/v1/orders/{order_id}/ship")
def api_ship(order_id: int, request: Request, payload: ShipIn):
    return services.ship_order(require_user(request), order_id, handover_point=payload.handover_point)


@app.post("/api/v1/orders/{order_id}/refund")
def api_refund(order_id: int, request: Request, payload: RefundIn,
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return services.apply_refund(require_user(request), order_id, **payload.model_dump(),
                                 idempotency_key=idempotency_key)


@app.post("/api/v1/orders/{order_id}/confirm")
def api_confirm(order_id: int, request: Request, payload: ConfirmIn,
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return services.confirm_receipt(require_user(request), order_id, code=payload.code,
                                    idempotency_key=idempotency_key)


@app.get("/api/v1/orders/{order_id}/reviews")
def api_order_reviews(order_id: int):
    return services.list_reviews_for_order(order_id)


@app.post("/api/v1/orders/{order_id}/reviews", status_code=201)
def api_create_review(order_id: int, request: Request, payload: ReviewIn):
    return services.create_review(require_user(request), order_id, **payload.model_dump())


@app.get("/api/v1/users/{user_id}/reviews")
def api_user_reviews(user_id: int):
    return services.list_reviews_for_user(user_id)


@app.post("/api/v1/reports", status_code=201)
def api_report(request: Request, payload: ReportIn):
    user = require_user(request)
    _limit(f"report:{user['id']}", limit=20, window_seconds=3600)
    return services.report_target(user, **payload.model_dump())


@app.get("/api/v1/notifications")
def api_notifications(request: Request, unread_only: bool = False):
    return services.list_notifications(require_user(request), unread_only)


@app.get("/api/v1/notifications/unread-count")
def api_unread(request: Request):
    return {"count": services.unread_count(require_user(request))}


@app.post("/api/v1/notifications/{notification_id}/read")
def api_read_notification(notification_id: int, request: Request):
    return services.mark_notification_read(require_user(request), notification_id)


@app.post("/api/v1/notifications/read-all")
def api_read_all(request: Request):
    return services.mark_all_read(require_user(request))


@app.get("/api/v1/admin/dashboard")
def api_dashboard(request: Request):
    require_admin(request)
    return services.admin_dashboard()


@app.get("/api/v1/admin/books")
def api_admin_books(request: Request):
    require_admin(request)
    return services.pending_books()


@app.get("/api/v1/admin/refunds")
def api_admin_refunds(request: Request):
    require_admin(request)
    return services.list_refunds()


@app.post("/api/v1/admin/refunds/{refund_id}")
def api_handle_refund(refund_id: int, request: Request, payload: DecisionIn):
    return services.handle_refund(require_admin(request), refund_id, approve=payload.approve, note=payload.note)


@app.get("/api/v1/admin/reports")
def api_admin_reports(request: Request):
    require_admin(request)
    return services.list_reports()


@app.post("/api/v1/admin/reports/{report_id}")
def api_handle_report(report_id: int, request: Request, payload: DecisionIn):
    return services.handle_report(require_admin(request), report_id, resolve=payload.approve, note=payload.note)


@app.get("/api/v1/admin/users")
def api_admin_users(request: Request):
    require_admin(request)
    return services.list_users()


@app.post("/api/v1/admin/users/{user_id}/status")
def api_admin_user_status(user_id: int, request: Request, payload: UserStatusIn):
    return services.admin_set_user_status(require_admin(request), user_id, payload.status)


# ---------------------------------------------------------------- 页面路由

@app.get("/")
def home(request: Request, keyword: str = "", category: int = 0, sort: str = "latest"):
    books = services.list_books(keyword=keyword, category_id=category, sort=sort)
    return templates.TemplateResponse(request, "home.html", page_context(
        request, books=books, keyword=keyword, category=category, sort=sort, title="淘教材"))


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", page_context(request, next_url=next, title="登录"))


@app.get("/register")
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", page_context(request, title="注册"))


@app.get("/forgot")
def forgot_page(request: Request):
    return templates.TemplateResponse(request, "forgot.html", page_context(request, title="找回密码"))


@app.get("/books/new")
def new_book_page(request: Request):
    require_user(request)
    return templates.TemplateResponse(request, "book_form.html", page_context(request, book=None, title="发布教材"))


@app.get("/books/{book_id}/edit")
def edit_book_page(book_id: int, request: Request):
    user = require_user(request)
    book = services.get_book(book_id, user)
    return templates.TemplateResponse(request, "book_form.html", page_context(request, book=book, title="编辑教材"))


@app.get("/books/{book_id}")
def book_detail_page(book_id: int, request: Request):
    book = services.get_book(book_id, optional_user(request))
    return templates.TemplateResponse(request, "book_detail.html", page_context(request, book=book, title=book["title"]))


@app.get("/checkout/{book_id}")
def checkout_page(book_id: int, request: Request):
    user = require_user(request)
    book = services.get_book(book_id, user)
    return templates.TemplateResponse(request, "checkout.html", page_context(request, book=book, title="确认订单"))


@app.get("/pay/{order_id}")
def pay_page(order_id: int, request: Request):
    order = services.get_order(order_id, require_user(request))
    if order["status"] != "CREATED":
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    return templates.TemplateResponse(request, "pay.html", page_context(request, order=order, title="完成支付"))


@app.get("/orders")
def orders_page(request: Request, scope: str = "all"):
    orders = services.list_orders(require_user(request), scope)
    return templates.TemplateResponse(request, "orders.html", page_context(request, orders=orders, scope=scope, title="我的订单"))


@app.get("/orders/{order_id}")
def order_detail_page(order_id: int, request: Request):
    order = services.get_order(order_id, require_user(request))
    return templates.TemplateResponse(request, "order_detail.html", page_context(request, order=order, title="订单详情"))


@app.get("/reviews/{order_id}")
def review_page(order_id: int, request: Request):
    user = require_user(request)
    order = services.get_order(order_id, user)
    if order["status"] != "COMPLETED":
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    return templates.TemplateResponse(request, "review.html", page_context(request, order=order, title="评价交易"))


@app.get("/notifications")
def notifications_page(request: Request):
    items = services.list_notifications(require_user(request))
    return templates.TemplateResponse(request, "notifications.html", page_context(request, items=items, title="消息中心"))


@app.get("/profile")
def profile_page(request: Request):
    user = require_user(request)
    reviews = services.list_reviews_for_user(user["id"])
    return templates.TemplateResponse(request, "profile.html", page_context(request, reviews=reviews, title="个人中心"))


@app.get("/admin")
def admin_page(request: Request, tab: str = "overview"):
    require_admin(request)
    return templates.TemplateResponse(request, "admin.html", page_context(request, tab=tab, title="后台运营"))
