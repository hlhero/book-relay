import os
import re
import tempfile
import uuid
from pathlib import Path

TMP_DIR = tempfile.mkdtemp(prefix="bookrelay-test-")
os.environ["BOOKRELAY_DB"] = os.path.join(TMP_DIR, "test.db")
os.environ["BOOKRELAY_SECRET"] = "bookrelay-test-secret"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import config, db, services


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def book_status(book_id):
    return services.get_book(book_id, {"id": 1, "role": "admin"})["status"]


def login(client, email="lin@bookrelay.local", password="Student@123"):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def new_client(email="lin@bookrelay.local", password="Student@123"):
    c = TestClient(app)
    login(c, email, password)
    return c


def admin_client():
    return new_client("admin@bookrelay.local", "Admin@135")


def make_book(seller, title=None):
    payload = {
        "title": title or f"测试教材-{uuid.uuid4().hex[:8]}", "author": "测试作者", "isbn": "1234567890",
        "edition": "第1版", "category_id": 1, "condition_text": "9成新", "price_cents": 1800,
        "description": "端到端测试用书",
    }
    r = seller.post("/api/v1/books", json=payload)
    assert r.status_code == 201, r.text
    book_id = r.json()["id"]
    resp = admin_client().post(f"/api/v1/books/{book_id}/actions/approve", json={})
    assert resp.status_code == 200, resp.text
    return book_id


def make_order(buyer, book_id):
    key = uuid.uuid4().hex
    r = buyer.post("/api/v1/orders", json={"book_id": book_id, "handover_method": "LIBRARY_WEST"},
                   headers={"Idempotency-Key": key})
    assert r.status_code == 201, r.text
    return r.json(), key


def test_health_and_seed(client):
    assert client.get("/api/v1/health").json() == {"status": "ok", "db": "sqlite"}
    books = client.get("/api/v1/books").json()
    assert len(books) >= 10
    assert client.get("/").status_code == 200
    assert "让闲置教材" in client.get("/").text


def test_register_login_and_auth(client):
    email = f"user-{uuid.uuid4().hex[:8]}@campus.edu"
    r = client.post("/api/v1/auth/register", json={
        "email": email, "nickname": "新同学", "password": "Password@123",
        "security_question": "入学年份？", "security_answer": "2024"})
    assert r.status_code == 201, r.text
    assert "password_hash" not in r.json()
    assert client.get("/api/v1/auth/me").json()["email"] == email
    assert client.post("/api/v1/auth/logout", json={}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/reset-password", json={
        "email": email, "security_answer": "2024", "new_password": "NewPass@123"}).status_code == 200
    login(client, email, "NewPass@123")


def test_full_trade_closed_loop(client):
    seller = new_client("chen@bookrelay.local")
    buyer = new_client("lin@bookrelay.local")
    book_id = make_book(seller)
    order, key = make_order(buyer, book_id)

    # 幂等：相同 key 返回同一订单
    again = buyer.post("/api/v1/orders", json={"book_id": book_id, "handover_method": "LIBRARY_WEST"},
                       headers={"Idempotency-Key": key})
    assert again.status_code == 201 and again.json()["id"] == order["id"]

    assert book_status(book_id) == "LOCKED"
    pay = buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "CAMPUS_CARD"})
    assert pay.status_code == 200 and pay.json()["payment"]["status"] == "SUCCEEDED"
    assert services.get_order(order["id"], login(buyer))["status"] == "PAID"

    # 非法转移：已支付订单不能取消
    conflict = buyer.post(f"/api/v1/orders/{order['id']}/cancel", json={})
    assert conflict.status_code == 409 and conflict.json()["code"] == "ORDER_STATE_CONFLICT"

    ship = seller.post(f"/api/v1/orders/{order['id']}/ship", json={"handover_point": "LIBRARY_WEST"})
    assert ship.status_code == 200
    code = ship.json()["handover_code"]
    assert len(code) == 4

    wrong = buyer.post(f"/api/v1/orders/{order['id']}/confirm",
                       json={"code": "0000"}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert wrong.status_code == 422 and wrong.json()["code"] == "HANDOVER_CODE_MISMATCH"

    ok = buyer.post(f"/api/v1/orders/{order['id']}/confirm",
                    json={"code": code}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert ok.status_code == 200 and ok.json()["status"] == "COMPLETED"
    assert book_status(book_id) == "SOLD"

    for c in (buyer, seller):
        r = c.post(f"/api/v1/orders/{order['id']}/reviews", json={"rating": 5, "content": "非常满意"})
        assert r.status_code == 201, r.text
    assert services.get_user(2)["reputation"] > 5.0 or services.get_user(2)["deal_count"] >= 1
    assert len(services.list_reviews_for_order(order["id"])) == 2

    notices = buyer.get("/api/v1/notifications").json()
    assert any(n["type"] == "REVIEW" for n in notices)


def test_refund_flow(client):
    seller = new_client("chen@bookrelay.local")
    buyer = new_client("wang@bookrelay.local")
    book_id = make_book(seller)
    order, _ = make_order(buyer, book_id)
    assert buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "MOCK_THIRD_PARTY"}).status_code == 200
    r = buyer.post(f"/api/v1/orders/{order['id']}/refund",
                   json={"reason": "教材有缺页"}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "REFUNDING"
    refunds = admin_client().get("/api/v1/admin/refunds").json()
    target = next(rf for rf in refunds if rf["order_id"] == order["id"])
    done = admin_client().post(f"/api/v1/admin/refunds/{target['id']}", json={"approve": True, "note": "已核实"})
    assert done.status_code == 200
    final = services.get_order(order["id"], login(buyer, "wang@bookrelay.local"))
    assert final["status"] == "REFUNDED"
    assert final["payment"]["status"] == "REFUNDED"
    assert book_status(book_id) == "OFF_SHELF"


def test_refund_reject_restores_order(client):
    seller = new_client("zhao@bookrelay.local")
    buyer = new_client("sun@bookrelay.local")
    book_id = make_book(seller)
    order, _ = make_order(buyer, book_id)
    buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "CAMPUS_CARD"})
    buyer.post(f"/api/v1/orders/{order['id']}/refund",
               json={"reason": "想转给同学"}, headers={"Idempotency-Key": uuid.uuid4().hex})
    target = next(rf for rf in admin_client().get("/api/v1/admin/refunds").json() if rf["order_id"] == order["id"])
    r = admin_client().post(f"/api/v1/admin/refunds/{target['id']}", json={"approve": False, "note": "不满足条件"})
    assert r.status_code == 200
    assert services.get_order(order["id"], login(buyer, "sun@bookrelay.local"))["status"] == "PAID"
    assert book_status(book_id) == "LOCKED"


def test_oversell_and_timeout(client):
    seller = new_client("zhou@bookrelay.local")
    buyer1 = new_client("lin@bookrelay.local")
    buyer2 = new_client("wang@bookrelay.local")
    book_id = make_book(seller)
    first, _ = make_order(buyer1, book_id)
    conflict = buyer2.post("/api/v1/orders", json={"book_id": book_id, "handover_method": "DORM_LOBBY"},
                           headers={"Idempotency-Key": uuid.uuid4().hex})
    assert conflict.status_code == 409 and conflict.json()["code"] == "BOOK_ALREADY_LOCKED"

    with db.transaction() as conn:
        conn.execute("UPDATE orders SET expires_at='2000-01-01T00:00:00' WHERE id=?", (first["id"],))
    result = services.sweep_timeouts()
    assert result["cancelled"] >= 1
    assert services.get_order(first["id"], login(buyer1))["status"] == "CANCELLED"
    assert book_status(book_id) == "ON_SALE"


def test_permissions_admin_and_pages(client):
    buyer = new_client("lin@bookrelay.local")
    seller = new_client("chen@bookrelay.local")
    book_id = make_book(seller)
    order, _ = make_order(buyer, book_id)
    buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "CAMPUS_CARD"})

    assert buyer.post(f"/api/v1/orders/{order['id']}/ship", json={"handover_point": "LIBRARY_WEST"}).status_code in (403, 409)
    assert seller.get(f"/api/v1/orders/{order['id']}").status_code == 200

    other = new_client("sun@bookrelay.local")
    assert other.get(f"/api/v1/orders/{order['id']}").status_code == 403

    adm = admin_client()
    assert adm.get("/api/v1/admin/dashboard").status_code == 200
    users = adm.get("/api/v1/admin/users").json()
    victim = next(u for u in users if u["email"] == "sun@bookrelay.local")
    assert adm.post(f"/api/v1/admin/users/{victim['id']}/status", json={"status": "banned"}).status_code == 200
    assert TestClient(app).post("/api/v1/auth/login",
                                json={"email": "sun@bookrelay.local", "password": "Student@123"}).status_code == 403
    adm.post(f"/api/v1/admin/users/{victim['id']}/status", json={"status": "active"})
    other = new_client("sun@bookrelay.local")  # 封禁会递增 session_version，旧会话必须重新登录

    report = other.post("/api/v1/reports", json={"target_type": "BOOK", "target_id": book_id, "reason": "测试举报"})
    assert report.status_code == 201, report.text
    pending = adm.get("/api/v1/admin/reports").json()
    target = next(r for r in pending if r["id"] == report.json()["id"])
    assert adm.post(f"/api/v1/admin/reports/{target['id']}", json={"approve": True, "note": "核实违规"}).status_code == 200
    assert book_status(book_id) == "BANNED"


def test_all_pages_render(client):
    buyer = new_client("lin@bookrelay.local")
    adm = admin_client()
    book_id = make_book(buyer)
    order, _ = make_order(adm, book_id)  # admin 作为买家，后面页面用它验证订单页
    paths = ["/", "/books/new", f"/books/{book_id}", "/checkout/" + str(make_book(buyer)),
             "/orders", f"/orders/{order['id']}", "/notifications", "/profile"]
    for path in paths:
        r = buyer.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"
    seller = new_client("chen@bookrelay.local")
    for path in ["/login", "/register", "/forgot"]:
        assert TestClient(app).get(path).status_code == 200
    assert adm.get(f"/pay/{order['id']}").status_code == 200
    assert adm.get("/admin?tab=overview").status_code == 200
    assert adm.get("/admin?tab=users").status_code == 200


def test_expired_pay_guard_and_review_page(client):
    seller = new_client("chen@bookrelay.local")
    buyer = new_client("lin@bookrelay.local")
    book_id = make_book(seller)
    order, _ = make_order(buyer, book_id)
    with db.transaction() as conn:
        conn.execute("UPDATE orders SET expires_at='2000-01-01T00:00:00' WHERE id=?", (order["id"],))
    expired = buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "CAMPUS_CARD"})
    assert expired.status_code == 409 and expired.json()["code"] == "ORDER_STATE_CONFLICT"

    book2 = make_book(seller)
    order2, _ = make_order(buyer, book2)
    buyer.post(f"/api/v1/orders/{order2['id']}/pay", json={"channel": "CAMPUS_CARD"})
    ship = seller.post(f"/api/v1/orders/{order2['id']}/ship", json={"handover_point": "DORM_LOBBY"})
    buyer.post(f"/api/v1/orders/{order2['id']}/confirm",
               json={"code": ship.json()["handover_code"]}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert buyer.get(f"/reviews/{order2['id']}").status_code == 200


def test_security_and_consistency_regressions(client):
    admin = admin_client()
    seller = new_client("chen@bookrelay.local")
    buyer = new_client("lin@bookrelay.local")

    # 1) 后台脚本必须注入浏览器端常量
    page = admin.get("/admin?tab=overview").text
    assert "const ORDER_LABELS =" in page
    assert "const yuan =" in page
    for tpl in Path("app/templates").glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert len(re.findall(r"<script", text)) == len(re.findall(r"</script>", text)), tpl.name

    # 2) 封禁后旧会话立刻失效
    victim = new_client("sun@bookrelay.local")
    victim_id = victim.get("/api/v1/auth/me").json()["id"]
    admin.post(f"/api/v1/admin/users/{victim_id}/status", json={"status": "banned"})
    assert victim.get("/api/v1/auth/me").status_code == 401
    assert victim.get("/orders", follow_redirects=False).status_code == 303
    assert TestClient(app).post("/api/v1/auth/login",
                                json={"email": "sun@bookrelay.local", "password": "Student@123"}).status_code == 403
    admin.post(f"/api/v1/admin/users/{victim_id}/status", json={"status": "active"})

    # 3) 非管理员不能按状态枚举待审核/封禁书籍
    assert TestClient(app).get("/api/v1/books?status=PENDING_REVIEW").status_code == 401
    assert new_client("wang@bookrelay.local").get("/api/v1/books?status=BANNED").status_code == 403
    assert admin.get("/api/v1/books?status=PENDING_REVIEW").status_code == 200

    # 4) 交接码只对卖家/管理员可见，买家 API 读到 None
    book_id = make_book(seller)
    order, _ = make_order(buyer, book_id)
    buyer.post(f"/api/v1/orders/{order['id']}/pay", json={"channel": "CAMPUS_CARD"})
    ship = seller.post(f"/api/v1/orders/{order['id']}/ship", json={"handover_point": "LIBRARY_WEST"})
    code = ship.json()["handover_code"]
    buyer_view = buyer.get(f"/api/v1/orders/{order['id']}").json()
    seller_view = seller.get(f"/api/v1/orders/{order['id']}").json()
    assert buyer_view["handover_code"] is None
    assert seller_view["handover_code"] == code
    assert buyer.get(f"/books/{book_id}").status_code == 200

    # 5) 订单关闭后不能再评价
    buyer.post(f"/api/v1/orders/{order['id']}/confirm",
               json={"code": code}, headers={"Idempotency-Key": uuid.uuid4().hex})
    with db.transaction() as conn:
        conn.execute("UPDATE orders SET completed_at='2000-01-01T00:00:00' WHERE id=?", (order["id"],))
    services.sweep_timeouts()
    closed = buyer.get(f"/api/v1/orders/{order['id']}").json()
    assert closed["status"] == "CLOSED" and closed["can_review"] is False
    late_review = buyer.post(f"/api/v1/orders/{order['id']}/reviews",
                             json={"rating": 5, "content": "关闭后评价"})
    assert late_review.status_code == 409
    assert buyer.get(f"/reviews/{order['id']}", follow_redirects=False).status_code == 303

    # 6) 编辑书籍沿用创建时的校验，非法分类不允许 500
    edit_book = make_book(seller)
    payload = {"title": "", "author": "", "isbn": "", "edition": "", "category_id": 999,
               "condition_text": "", "price_cents": 0, "description": ""}
    assert seller.patch(f"/api/v1/books/{edit_book}", json=payload).status_code == 422
    payload["title"] = "合法标题"
    payload["category_id"] = 1
    payload["condition_text"] = "9成新"
    payload["price_cents"] = 1000
    assert seller.patch(f"/api/v1/books/{edit_book}", json=payload).status_code == 200


def test_security_hardening_sessions_csrf_and_payload(client):
    email = f"security-{uuid.uuid4().hex[:8]}@campus.edu"
    user = TestClient(app)
    registered = user.post("/api/v1/auth/register", json={
        "email": email, "nickname": "安全测试", "password": "Password@123",
        "security_question": "入学年份？", "security_answer": "2024"})
    assert registered.status_code == 201, registered.text
    assert user.get("/api/v1/auth/me").status_code == 200

    # 改密后旧会话立即失效
    changed = user.post("/api/v1/auth/password",
                        json={"old_password": "Password@123", "new_password": "NewPass@123"})
    assert changed.status_code == 200
    assert user.get("/api/v1/auth/me").status_code == 401
    login(user, email, "NewPass@123")

    # 找回密码后旧会话立即失效
    reset = TestClient(app).post("/api/v1/auth/reset-password", json={
        "email": email, "security_answer": "2024", "new_password": "ResetPass@123"})
    assert reset.status_code == 200
    assert user.get("/api/v1/auth/me").status_code == 401
    login(user, email, "ResetPass@123")

    # 跨站写请求被 Origin / Sec-Fetch-Site 拦截；同源请求正常
    evil = user.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json()["code"] == "CSRF_REJECTED"
    fetch_cross = user.post("/api/v1/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert fetch_cross.status_code == 403
    same_origin = user.post("/api/v1/auth/logout", headers={"Origin": "http://testserver"})
    assert same_origin.status_code == 200

    # 超大请求体在路由前被拒绝
    oversized = TestClient(app).post(
        "/api/v1/auth/login", content=b"x" * (config.MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413
