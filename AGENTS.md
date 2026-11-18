# AGENTS.md — 书接力 BookRelay

本文件是 AI 编码助手在本仓库工作的首要约定。任何实现决策都必须先满足本文件，其次参考 `docs/index.html`。

## 1. 项目是什么

书接力是校园二手教材流转平台：学生发布闲置教材，买家检索下单并线下交接，双方互评；管理员审核内容、处理纠纷。设计事实源是 `docs/index.html`（一份完整的技术设计教案，案例贯穿全文）。

用户明确要求：**用 SQLite 实现完整功能，不要用 MySQL。**

## 2. 已裁决的设计冲突（不要再改）

| 项 | 文档原文冲突 | 本项目裁决 |
|---|---|---|
| 待支付超时 | §2.1 写「锁书 15 分钟」，§2.4/§5.4 写「30 分钟」 | 统一为 **30 分钟**（与状态机、DDL 注释一致） |
| 数据库 | §4.6 选 MySQL 8 | 改为 **SQLite**（用户指令）；WAL + `BEGIN IMMEDIATE` 保证写事务串行化 |
| 缓存 | §4.6 选 Redis | 不使用 Redis；幂等键、库存锁均落 SQLite，单表单机足够 |
| 对象存储 | §4.6 选云对象存储 | 本地 `static/uploads/` 存书影，接口仍只返回相对 URL |
| 支付 | 渠道待定 | 模拟支付：校内卡 / 模拟第三方两种渠道，均写支付流水，无外部网络调用 |
| 前端 | §4.6 选 Vue3 + Vite | FastAPI + Jinja2 服务端渲染 + 原生 JS `fetch`，零构建步骤，离线可跑；UI 规范仍遵循 §3.5–§3.7 |

## 3. 固定词汇（全仓统一，禁止自造同义词）

- 订单状态：`CREATED` `PAID` `SHIPPED` `COMPLETED` `CANCELLED` `REFUNDING` `REFUNDED` `CLOSED`
- 订单事件：`pay` `cancel` `ship` `apply_refund` `confirm` `approve` `reject` `auto_close`
- 书籍状态：`DRAFT` `PENDING_REVIEW` `ON_SALE` `LOCKED` `SOLD` `OFF_SHELF` `BANNED`
- 支付方式：`CAMPUS_CARD` `MOCK_THIRD_PARTY`
- 交接点：`LIBRARY_WEST` `DORM_LOBBY` `COURIER`
- 角色：`student` `admin`；通知类型、错误码统一写在 `app/errors.py` / `app/domain.py`

## 4. 关键业务常量

- 待支付 30 分钟超时自动取消，释放书籍锁
- 已支付未交接 48 小时内可申请退款；已交接 48 小时内可凭纠纷说明申请退款
- 交接码为 4 位数字，由卖家发货时生成；**仅卖家和管理员可读取**，买家必须线下向卖家索取，API 对买家返回 `null`
- 评价仅在 `COMPLETED` 状态开放；`CLOSED` 后评价入口关闭，后端也必须拒绝
- 已完成满 30 天自动关闭
- 幂等键 24 小时内有效，用于创建订单、申请退款、确认收货
- 金额一律用**整数分**存储与计算，禁止 float
- 订单项 `unit_price` / `condition`、订单 `buyer_nickname_snapshot` / `book_title_snapshot` 是快照字段，永不随主数据变化
- 封禁用户的旧会话必须立即失效（`session_version` 递增，旧 Cookie 返回 401）；重新登录被封禁账号返回 403 `ACCOUNT_BANNED`
- 改密、找回密码同样递增 `session_version`，所有旧会话失效
- 书籍状态筛选（`status` 非 `ON_SALE`）仅管理员可用，避免泄露待审核/封禁书籍
- 单买家最多同时持有 3 笔待支付订单，防止恶意锁书
- 生产环境必须提供 `BOOKRELAY_SECRET` 与 `BOOKRELAY_ADMIN_PASSWORD`；默认关闭演示数据与 API 文档

## 5. 代码分层与边界

```
app/
  config.py     # 配置与常量；密钥从环境变量 BOOKRELAY_SECRET 读取，缺省仅用于本地开发
  errors.py     # 统一业务错误码 + 统一错误结构 {code,message,details,trace_id}
  db.py         # SQLite 连接、DDL、迁移、种子数据；所有写事务用 BEGIN IMMEDIATE
  domain.py     # 纯业务规则：订单/书籍状态机、守卫、金额、信誉分；不依赖 FastAPI 或 sqlite3
  services.py   # 用例编排：8 个业务模块的服务函数；事务、审计、领域事件、通知
  main.py       # FastAPI 应用、REST API（/api/v1）、页面路由、鉴权依赖、后台定时任务
  templates/    # Jinja2 页面（主要列表/后台页含 loading / empty / error 反馈）
  static/       # CSS、JS、上传的书影
tests/          # pytest：状态机、业务闭环、权限、幂等、防重复锁书
```

规则：
1. 领域层不 import FastAPI / sqlite3；状态转移只能走转移表，非法转移显式抛错（409 `ORDER_STATE_CONFLICT` / `BOOK_STATE_CONFLICT`）。
2. 每次状态转移必须写 `order_events` 审计日志，并触发通知；通知是单向事件，业务模块不反向依赖通知实现。
3. 所有写操作在单个 SQLite 事务内完成；并发下单用 `BEGIN IMMEDIATE` + `UPDATE ... WHERE status='ON_SALE'` 防超卖。
4. 所有页面按钮 = 状态机当前状态的出口事件；不要在 UI 里自创状态机之外的操作。
5. 所有用户输入转义由 Jinja2 处理；上传仅图片且限制大小；密码用 bcrypt；会话用签名 Cookie；订单/评价/通知接口必须校验归属。
6. 模板中的内联 JS 不得直接引用 Jinja 全局变量名；必须用 `{{ NAME|tojson }}` 注入为 JS 常量。每个 `{% block scripts %}` 必须有配对的 `<script>...</script>`。
7. 页面脚本只通过 `/api/v1` 调用后端；不要在模板里直接访问数据库或复制业务规则。
8. 写请求必须经过 `security_guard` 的 Origin / `Sec-Fetch-Site` 校验；请求体默认不超过 3MB；跨站请求返回 403 `CSRF_REJECTED`。
9. 会话 Cookie 必须 HttpOnly + SameSite=Lax；生产必须 Secure。登录、注册、找回密码、举报必须经过限流；`session_version` 校验不可绕过。
10. 生产默认 `BOOKRELAY_SEED_DEMO=0`、`BOOKRELAY_ENABLE_DOCS=0`；禁止把演示账号、默认管理员口令或开发数据库带上公网。

## 6. 功能范围（DoD）

必须全部可用：注册/登录/找回/封禁；书籍发布/编辑/上下架/分类/搜索/详情/审核；下单/支付/取消/超时/退款申请与审批；交接点与交接码确认收货；双向评价、信誉分、举报处理；站内通知已读未读；后台审核/用户管理/纠纷处理/统计看板。10 个页面：首页书籍列表、书籍详情、下单确认、支付、我的订单、订单详情、发布书籍、消息中心、评价页、后台审核。

明确不做：跨校交易、真实支付网关、快递物流、邮件发送、多卖家分账、微服务/容器编排；申诉流程、评价/用户举报的页面入口列为后续迭代（API 已支持 `BOOK` / `REVIEW` / `USER` 三种举报目标）。

## 7. 运行与验证

```powershell
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8000，API 文档 /docs
pytest -q                # 全部测试必须通过
pytest -q --cov=app      # 覆盖率参考
```

配置：生产必须设置 `BOOKRELAY_ENV=production`、`BOOKRELAY_SECRET`、`BOOKRELAY_ADMIN_PASSWORD`、`BOOKRELAY_COOKIE_SECURE=1`；默认 `BOOKRELAY_SEED_DEMO=0`、`BOOKRELAY_ENABLE_DOCS=0`。可选 `BOOKRELAY_TRUSTED_ORIGINS`、`BOOKRELAY_EMAIL_DOMAIN`、`BOOKRELAY_SESSION_HOURS`、`BOOKRELAY_MAX_REQUEST_BYTES`、`BOOKRELAY_MAX_ACTIVE_UNPAID_ORDERS`。

开发/演示种子数据含管理员 `admin@bookrelay.local` / `Admin@135`，学生密码统一 `Student@123`；生产不得复用这些账号或开发数据库。阿里云上线清单见 `README.md`。

面向使用者的功能、API、限制与部署说明见 `README.md`。修改设计或代码后必须同步更新 `README.md`、本文件与测试，保持产物一致（C1–C10）。
