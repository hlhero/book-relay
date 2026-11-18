# 书接力 BookRelay

校园二手教材流转平台。学生发布闲置教材，买家检索下单，线下凭交接码交付，双方互评；管理员负责内容审核、退款纠纷和举报处理。

项目依据 `docs/index.html` 的技术设计实现，数据库使用 **SQLite**，不依赖 MySQL、Redis、Node 构建或外部支付服务，克隆后可直接运行。

## 功能一览

| 模块       | 已实现能力                                                                           |
| ---------- | ------------------------------------------------------------------------------------ |
| 身份与账号 | 注册、登录、登出、找回密码、修改密码、个人资料、管理员封禁/解封                      |
| 书籍目录   | 发布、编辑、上下架、重新出售、分类、关键词搜索、价格排序、详情、封面图片、管理员审核 |
| 订单       | 创建订单、30 分钟锁书、超时自动取消、取消订单、状态机、幂等键、状态流水              |
| 支付       | 模拟校内卡 / 第三方支付、支付流水、退款原路标记                                      |
| 交接与物流 | 交接点选择、4 位交接码、买家确认收货、交接码仅卖家/管理员可见                        |
| 评价与信誉 | 交易完成后双向评价、平均信誉分、交易次数统计                                         |
| 消息通知   | 订单、支付、交接、退款、评价、审核结果站内信，未读计数与已读操作                     |
| 后台运营   | 数据看板、书籍审核、退款审批、举报处理、用户封禁/解封                                |

页面共 10 个核心场景：首页/书籍列表、书籍详情、下单确认、支付、我的订单、订单详情、发布书籍、消息中心、评价页、后台审核。主要列表和后台页面包含加载/空态/错误提示，详情页与操作结果都有明确反馈。

## 技术栈

- **后端**：Python 3.10+ / FastAPI 0.118 / Uvicorn
- **数据库**：SQLite（Python 标准库 `sqlite3`，WAL 模式，`BEGIN IMMEDIATE` 写事务）
- **模板**：Jinja2 服务端渲染
- **前端**：原生 CSS + 原生 JavaScript（无构建步骤）
- **安全**：bcrypt 密码哈希、itsdangerous 签名 Cookie、参数化 SQL、上传文件魔数校验
- **测试**：pytest + FastAPI TestClient

## 快速开始

```powershell
# 1. 安装依赖（建议使用虚拟环境）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 启动
python run.py

# 3. 打开浏览器
# 应用：http://127.0.0.1:8000
# 自动生成的 OpenAPI 文档：http://127.0.0.1:8000/docs
```

首次启动会自动创建 `data/bookrelay.db` 并写入演示数据。重置数据：停止服务后删除 `data/bookrelay.db`、`data/bookrelay.db-wal`、`data/bookrelay.db-shm`，再启动即可。

### 在 GitHub Codespaces 中运行

仓库已内置 `.devcontainer/`：新建或重建 Codespace 时自动创建 `.venv`、安装依赖，并以 `0.0.0.0:8000` 启动，转发后的 8000 端口设为公开。

已有的 Codespace 里手动启动：

```bash
cd /workspaces/book-relay
git pull origin master
bash .devcontainer/start.sh
```

脚本是幂等的，重复执行不会起第二个进程；运行日志在 `/tmp/bookrelay.log`。启动后访问 `https://<codespace 名称>-8000.app.github.dev/`（端口页签里也会显示该链接）。

### 演示账号

| 角色           | 邮箱                      | 密码                |
| -------------- | ------------------------- | ------------------- |
| 管理员         | `admin@bookrelay.local` | 本地默认 `Admin@135`，部署时由 `BOOKRELAY_ADMIN_PASSWORD` 指定 |
| 学生（小林）   | `lin@bookrelay.local`   | `Student@123`     |
| 学生（阿陈）   | `chen@bookrelay.local`  | `Student@123`     |
| 学生（王同学） | `wang@bookrelay.local`  | `Student@123`     |
| 学生（赵学姐） | `zhao@bookrelay.local`  | `Student@123`     |
| 学生（小孙）   | `sun@bookrelay.local`   | `Student@123`     |
| 学生（周周）   | `zhou@bookrelay.local`  | `Student@123`     |

种子数据包含 5 个分类、12 本在售教材和 1 本待审核教材，可直接体验完整交易流程。生产环境设置 `BOOKRELAY_SEED_DEMO=0` 后不会创建演示学生和教材，只创建分类与管理员。

## 业务规则

### 订单状态机

```
CREATED --pay--> PAID --ship--> SHIPPED --confirm--> COMPLETED --auto_close--> CLOSED
   |                 |               |
   | cancel          | apply_refund  | apply_refund（需纠纷说明）
   v                 v               v
CANCELLED         REFUNDING <--------+
                     | approve      | reject
                     v              v
                  REFUNDED         PAID
```

- 待支付订单 **30 分钟**超时自动取消，书籍锁释放。
- 支付后 **48 小时内**且未交接可申请退款；交接后 **48 小时内**可凭纠纷说明申请退款。
- 交接码为 4 位数字，仅卖家和管理员可读取；买家必须线下获取。
- 完成满 **30 天**自动关闭，关闭后评价入口关闭。
- 非法状态转移返回 `409 ORDER_STATE_CONFLICT`，不会静默失败。

### 书籍状态机

`PENDING_REVIEW`（待审核）→ `ON_SALE`（在售）→ `LOCKED`（被下单锁定）→ `SOLD`（已售）；另含 `OFF_SHELF`（下架）、`BANNED`（封禁）、`DRAFT`（预留草稿态）。卖家只能操作自己的书籍，审核/封禁只能由管理员执行。

### 一致性与幂等

- 金额一律使用整数分，数据库与 API 均不出现浮点金额。
- 订单项价格、成色、书名、买家昵称均为下单快照，后续改价不影响历史订单。
- 创建订单、申请退款、确认收货要求 `Idempotency-Key` 请求头，24 小时内重复请求返回首次结果。
- 每次订单状态转移写入 `order_events` 审计日志，并产生站内通知。

### 权限与安全边界

- 封禁用户的旧会话立即失效：页面按未登录处理，接口返回 `403 ACCOUNT_BANNED`。
- 交接码只对卖家和管理员可见；买家调用订单 API 只能拿到 `null`，必须线下获取。
- 订单、评价、通知接口都校验资源归属；书籍状态筛选（非 `ON_SALE`）仅管理员可用。
- 密码使用 bcrypt 哈希；会话使用 HttpOnly 签名 Cookie；生产环境可开启 `Secure` Cookie。
- 上传封面只接受 JPG/PNG/GIF/WEBP 魔数且限制 2MB；SQL 全部参数化。

## 项目结构

```text
BookRelay/
├─ AGENTS.md                 # AI 编码约定与设计裁决
├─ README.md
├─ requirements.txt
├─ run.py                    # 启动入口
├─ app/
│  ├─ config.py              # 配置与业务常量
│  ├─ errors.py              # 统一错误码与错误结构
│  ├─ domain.py              # 纯领域层：状态机、守卫、金额规则
│  ├─ db.py                  # SQLite DDL、连接、事务、种子数据
│  ├─ security.py            # bcrypt 与签名会话
│  ├─ services.py            # 8 个业务模块的用例编排
│  ├─ main.py                # FastAPI 路由、鉴权、定时任务
│  ├─ templates/             # Jinja2 页面
│  └─ static/                # CSS、JS、上传封面
├─ tests/test_bookrelay.py   # 端到端与回归测试
├─ docs/index.html           # 原始技术设计教案
├─ docs/需求文档.md           # 功能需求与验收基线
└─ data/bookrelay.db         # 运行时生成（不提交版本库）
```

## API 概览

所有接口前缀为 `/api/v1`，完整定义见 `/docs` 或 `/openapi.json`。

| 分组 | 代表接口                                                                                                                                                                                                            |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 认证 | `POST /auth/register`、`POST /auth/login`、`POST /auth/reset-password`、`GET /auth/me`                                                                                                                      |
| 书籍 | `GET /books`、`POST /books`、`GET /books/{id}`、`PATCH /books/{id}`、`POST /books/{id}/actions/{event}`                                                                                                   |
| 订单 | `POST /orders`、`GET /orders`、`GET /orders/{id}`、`POST /orders/{id}/pay`、`.../cancel`、`.../ship`、`.../refund`、`.../confirm`                                                                   |
| 评价 | `POST /orders/{id}/reviews`、`GET /orders/{id}/reviews`、`GET /users/{id}/reviews`                                                                                                                            |
| 通知 | `GET /notifications`、`GET /notifications/unread-count`、`POST /notifications/{id}/read`、`POST /notifications/read-all`                                                                                    |
| 举报 | `POST /reports`                                                                                                                                                                                                   |
| 后台 | `GET /admin/dashboard`、`GET /admin/books`、`GET /admin/refunds`、`POST /admin/refunds/{id}`、`GET /admin/reports`、`POST /admin/reports/{id}`、`GET /admin/users`、`POST /admin/users/{id}/status` |

统一错误结构：

```json
{
  "code": "ORDER_STATE_CONFLICT",
  "message": "当前状态「已支付」不允许执行「取消订单」",
  "details": [{"field": "event", "reason": "PAID+cancel 不在状态转移表中"}],
  "trace_id": "a1b2c3d4"
}
```

状态码约定：`200` 成功、`201` 已创建、`400` 参数错误、`401` 未登录、`403` 无权限/账号封禁、`404` 不存在、`409` 状态冲突、`422` 语义校验失败。

示例（创建订单）：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 0b7d3c2e-6f8a-4b1c-9d2e-1234567890ab" \
  -H "Cookie: br_session=<登录后的会话 Cookie>" \
  -d '{"book_id": 1, "handover_method": "LIBRARY_WEST", "remark": "今晚八点图书馆西门"}'
```

> 非管理员调用 `GET /api/v1/books?status=...` 只允许 `ON_SALE`；查询待审核、封禁等状态需要管理员身份。

## 测试

```powershell
pytest -q
pytest -q --cov=app --cov-report=term-missing
```

当前包含 11 个测试，覆盖率约 85%，覆盖：

- 注册、登录、找回密码、封禁会话失效
- 发布 → 审核 → 下单 → 支付 → 交接 → 收货 → 双向评价完整闭环
- 幂等键重放、非法状态转移、交接码错误
- 退款同意 / 驳回、防超卖、超时取消、自动关闭
- 权限隔离、后台审核、举报处理、页面渲染
- 后台脚本常量、模板脚本标签配对、交接码脱敏等回归项
- 改密/找回密码后旧会话失效、跨站写请求拦截、超大请求体拒绝

## 配置

| 环境变量                               | 默认值                    | 说明                                                                                                  |
| -------------------------------------- | ------------------------- | ----------------------------------------------------------------------------------------------------- |
| `BOOKRELAY_ENV`                      | `development`           | 设为 `production` 开启生产保护（强制密钥/管理员口令、关闭演示数据和 `/docs`、默认 Secure Cookie） |
| `BOOKRELAY_SECRET`                   | 开发占位密钥              | 会话签名密钥；生产必须设置为至少 32 字节随机值，否则拒绝启动                                          |
| `BOOKRELAY_DB`                       | `data/bookrelay.db`     | SQLite 数据库路径                                                                                     |
| `BOOKRELAY_DATA`                     | `data/`                 | 数据目录                                                                                              |
| `BOOKRELAY_COOKIE_SECURE`            | 生产默认 `1`            | HTTPS 部署必须为 `1`，Cookie 加 `Secure`                                                          |
| `BOOKRELAY_SESSION_HOURS`            | `168`                   | 会话有效期（小时），公网可缩短到 12–24                                                               |
| `BOOKRELAY_ADMIN_EMAIL`              | `admin@bookrelay.local` | 首次种子管理员邮箱                                                                                    |
| `BOOKRELAY_ADMIN_PASSWORD`           | `Admin@135`（仅开发）   | 生产必须设置强口令，否则拒绝启动                                                                      |
| `BOOKRELAY_ADMIN_SECURITY_ANSWER`    | 生产随机                  | 管理员找回密码答案；生产不设则生成随机值                                                              |
| `BOOKRELAY_SEED_DEMO`                | 生产默认 `0`            | 是否写入演示学生/教材；生产应保持 `0`                                                               |
| `BOOKRELAY_EMAIL_DOMAIN`             | 空                        | 如 `campus.edu`，限制注册邮箱域名                                                                   |
| `BOOKRELAY_TRUSTED_ORIGINS`          | 空                        | 反向代理/多域名场景的可信 Origin，逗号分隔                                                            |
| `BOOKRELAY_MAX_REQUEST_BYTES`        | `3145728`               | 请求体上限（约 3MB），上传封面另限 2MB                                                                |
| `BOOKRELAY_MAX_ACTIVE_UNPAID_ORDERS` | `3`                     | 单个买家最多同时持有的待支付订单数，防止恶意锁书                                                      |
| `BOOKRELAY_ENABLE_DOCS`              | 生产默认 `0`            | 是否开放 `/docs`、`/redoc`、`/openapi.json`                                                     |

### 生产启动保护

- `BOOKRELAY_ENV=production` 且未设置 `BOOKRELAY_SECRET`：启动失败。
- 生产环境仍使用默认管理员口令 `Admin@135`：启动失败。
- 生产环境检测到数据库里的管理员仍是默认口令：启动失败，需先重置数据库或修改密码。
- 生产环境默认不写入演示学生/教材，并默认关闭 API 文档。
- 封禁、改密、找回密码都会递增 `session_version`，旧 Cookie 立即失效。
- 写请求会校验 `Origin` / `Sec-Fetch-Site`，跨站写请求返回 `403 CSRF_REJECTED`；curl/脚本不带 Origin 时仍可用。
- 登录、注册、找回密码、举报有进程内滑动窗口限流；超大请求体在路由前返回 `413`。
- 上传封面只接受 JPG/PNG/GIF/WEBP 魔数且限制 2MB；SQL 全部参数化，模板默认转义。

### 阿里云上线安全清单

1. **网络边界**：ECS 安全组只放行 443（以及 80 跳转）给 SLB/WAF；SSH 22 只放行办公网/堡垒机 IP；不要对公网暴露 8000 端口。
2. **TLS**：Nginx 启用 TLS 1.2/1.3，HTTP 跳转 HTTPS；应用已输出 HSTS、CSP、`X-Frame-Options: DENY`、`nosniff`。
3. **反向代理**：设置 `client_max_body_size 3m`、`server_tokens off`、`autoindex off`，并传递 `Host`、`X-Real-IP`、`X-Forwarded-For`、`X-Forwarded-Proto`；禁止访问 `/.git`、`/data`、`/docs`。
4. **WAF/限流**：在阿里云 WAF 对 `/api/v1/auth/*`、`/api/v1/reports`、`/api/v1/orders` 配置频率限制和 Bot 防护；应用内限流是单进程内存实现，不能替代 WAF。
5. **运行身份**：使用非 root 的专用用户运行，`UMask=0077`；SQLite 单 worker；数据库目录权限 700、数据库文件 600。
6. **密钥管理**：`BOOKRELAY_SECRET`、管理员口令通过环境变量/密钥管理注入，不写入代码、镜像或 Git；定期轮换密钥会使现有会话失效。
7. **数据备份**：备份到私有 OSS Bucket（开启服务端加密），限制 RAM 权限；验证恢复流程；不要在 Web 根目录提供数据库文件。
8. **监控与日志**：接入 CloudMonitor/日志服务，监控 5xx、401/403/429 突增、磁盘和备份失败；日志中不要打印密码、Cookie、支付凭证。
9. **依赖与主机**：CI 中执行 `pip-audit`，定期更新依赖；ECS 开启快照、云安全中心与 Anti-DDoS 基础防护。
10. **首次上线**：不要直接复用开发库；在干净数据库上设置强管理员口令并关闭演示数据；确认默认账号无法登录。

### 安全边界与残余风险

- 会话是无状态签名 Cookie；改密/封禁会使旧会话失效，但“仅退出当前设备”不会让被盗 Cookie 立刻失效，建议缩短会话有效期。
- 找回密码依赖安全问题，没有邮件/短信二次验证；生产建议接入校园邮箱验证码或 SSO。
- 未实现 MFA、登录验证码、设备管理；公网部署建议配合 WAF 和强口令。
- SQLite 写事务串行，适合单实例；高并发/多 worker 需要迁移到云数据库。
- 邮件通知、申诉流程、评价/用户举报页面仍为后续迭代。

## 与教案的刻意差异

| 教案选择          | 本项目                                | 原因                         |
| ----------------- | ------------------------------------- | ---------------------------- |
| MySQL 8           | SQLite                                | 用户明确要求使用 SQLite      |
| Redis 缓存/库存锁 | SQLite `BEGIN IMMEDIATE` + 条件更新 | 单机部署，减少外部依赖       |
| 云对象存储        | 本地 `app/static/uploads/`          | 零预算、可直接运行           |
| 真实支付网关      | 模拟支付 + 支付流水                   | 课程项目不对接真实资金       |
| Vue 3 + Vite      | Jinja2 + 原生 JS                      | 零构建、离线可运行，便于验收 |
| 邮件通知          | 仅站内信                              | 不依赖 SMTP 配置             |

## 已知限制

- 未实现邮件通知、申诉流程、评价/用户的举报页面入口（API 支持 `BOOK` / `REVIEW` / `USER` 三种举报目标，页面目前只提供书籍举报）。
- 未实现真实支付回调、物流轨迹、跨校交易、分账。
- 列表暂无分页；书籍搜索使用 `LIKE`，大数据量下需要全文索引。
- 未做浏览器级 E2E 自动化和并发压测；当前质量目标是单机课程项目，不是生产级高并发。
- 管理员审核列表目前聚焦待审核书籍；全量书籍管理仍可通过详情页操作。

## 设计来源

需求、状态机、页面清单和数据模型来自仓库内 `docs/index.html`。实现约定与设计冲突裁决见 `AGENTS.md`。
