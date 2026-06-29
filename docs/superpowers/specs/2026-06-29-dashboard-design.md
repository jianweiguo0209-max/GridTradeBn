# GridTradeGP Dashboard — 设计文档

> 日期：2026-06-29
> 状态：设计已确认，待转 writing-plans
> 相关：总设计 `docs/superpowers/specs/2026-06-28-exchange-decoupling-design.md`、状态 `docs/STATUS.md`

## 1. 背景与目标

当前 GridTradeGP 部署在 fly.io（app `gridtrade-hl`，region nrt），两个常驻 process group：`monitor`（~5s 对账补单+记账+止损）与 `scheduler`（整点关旧→选币→准入→开新），状态外部托管在 Fly Postgres 的 6 张表（`grids` / `grid_orders` / `grid_accounting` / `order_records` / `grid_fills` / `heartbeats`）。

今天唯一的观测窗口是 `fly logs` 和 `fly console` 里手敲 SQL。目标：建一个 Web Dashboard，让单人运维在 testnet→mainnet 过程中一眼看清系统是否健康、网格在做什么、盈亏与战绩如何。

**终态**是「监控 + 控制 + 复盘」三合一；**本设计聚焦第一期：只读监控**，控制与复盘分阶段加（见 §8）。

## 2. 核心决策（已确认）

| 维度 | 决策 |
|---|---|
| 核心用途 | 全都要，分阶段；**第一期只读监控** |
| 部署形态 | fly.io 上的**第三个 process group `web`**，同镜像，读同一个 Fly Postgres |
| 技术栈 | **FastAPI + Jinja2 + HTMX**（服务端渲染，轻轮询；零前端构建链） |
| 首期视图 | ① 系统健康顶栏 ② 活跃网格总览 ③ 单网格明细 ④ 历史战绩/成交流（四个全要） |
| 现价来源 | web 进程**持一个只读行情 adapter**，实时算未实现盈亏 / 止损线距现价 |
| 鉴权 | **登录式**：用户名+密码登录建会话；登录失败计数，达上限**封禁 ≥ 1 小时**（防爆破） |
| 视图④体量 | 已平网格列表 + **按 tag 聚合小结**（总盈亏/胜率/笔数） |

## 3. 架构定位

```
fly app gridtrade-hl (同一镜像)
├── monitor     (常驻 ~5s)   ─┐
├── scheduler   (常驻整点)    ├─→  Fly Postgres ←──┐
└── web   ★新增 (uvicorn)  ──────────────────────┘ 只读 bot 状态
        FastAPI + Jinja2 + HTMX
        + 只读行情 adapter（仅 fetch_ticker/fetch_balance，绝不下单）
        公网 URL + 登录鉴权
```

**关键不变量（第一期）**：web 进程**不改任何网格状态、不下任何单**。它只
1. 通过新的只读查询层读 Postgres 的 6 张表；
2. 通过只读行情 adapter 读现价/余额。

把它独立成第三进程（而非塞进 monitor）的核心理由：dashboard 即使崩溃或被攻破，也碰不到真金白银的执行路径。

## 4. 模块设计（落到现有包结构）

```
gridtrade/
  dashboard/                  ★新增子包
    __init__.py
    app.py            # FastAPI 应用工厂 create_app(store, market_adapter, auth)
    queries.py        # 只读查询层：吃 StateStore + market_adapter，出 dashboard DTO
    auth.py           # 登录会话 + 失败计数锁定
    formatting.py     # ms→人类时间、盈亏着色、数字格式（Jinja2 filter）
    templates/        # base.html + login.html + 4 个视图模板/片段
    static/           # 一点 CSS + HTMX 单文件（vendored，离线可用）
  runtime/
    web.py            # ★ `python -m gridtrade.runtime.web` 入口；复用 factory 组装 store + 只读 adapter，起 uvicorn
```

### 4.1 `queries.py`（核心新增）

只读查询/聚合层。**复用现有 6 个仓储模块**（`state/grids.py` 等）做底层取数，只新增 dashboard 需要的聚合 DTO，例如把「一个活跃网格 + 它的 `grid_accounting` + 现价算出的未实现盈亏」合成一行。**现有仓储一行不改。**

输出 dataclass DTO（非 ORM 行），供模板渲染：
- `HealthDTO`：两进程心跳距今秒数、endpoint、余额、DB 连通。
- `GridOverviewRow`：币种/状态/方向/价区间/挂单数/净持仓/已实现PnL/未实现PnL/止损线距现价。
- `GridDetailDTO`：26 挂单（价/量/状态）、该网格成交流水、记账明细。
- `RecordsDTO`：已平网格列表 + 按 tag 聚合小结 + 全局最近成交流水。

未实现盈亏 = f(net_position, avg_price, 现价)；现价取自只读 adapter 的 ticker 缓存（带短 TTL）。adapter 行情拉取失败时**优雅退化**为「最后记账价」并在 UI 标注「价过期」，绝不让 dashboard 因行情抖动而 500。

### 4.2 `auth.py`（登录 + 锁定）

- 凭据：用户名 + 密码哈希放 fly secret（`DASHBOARD_USER` / `DASHBOARD_PASSWORD_HASH`）。比对用恒定时间 + 密码哈希（如 `hashlib.pbkdf2_hmac` / `hmac.compare_digest`，避免引第三方依赖；具体算法实现期定）。
- 会话：登录成功下发签名会话 cookie（HttpOnly + Secure + SameSite=Lax），含过期时间。
- 防爆破：按来源（IP / 用户名）记失败次数；达阈值 N（默认 5）后**锁定 ≥ 1 小时**，锁定期内即使密码正确也拒。计数器与锁定状态存内存即可（单 web 实例；多实例留待 P2/P3，届时落 DB 或 Redis）。
- 所有业务路由依赖会话中间件；未登录跳登录页，API/HTMX 片段返 401。

### 4.3 `runtime/web.py`（进程入口）

复用 `runtime/factory.py` 的组装逻辑取 `StateStore`（`from_url(DATABASE_URL)`）与一个**只读行情 adapter**（registry 按 `EXCHANGE`/`HL_TESTNET` 构造，但 dashboard 只调 `fetch_ticker`/`fetch_balance`）。起 uvicorn 监听 fly 注入的端口。启动时打印 endpoint（复用 `runtime/introspect.adapter_endpoint`），让 fly logs 铁证「连 testnet 还是 mainnet」。

### 4.4 新依赖

`fastapi`、`uvicorn[standard]`、`jinja2`（均兼容 py3.9）。HTMX 单文件 vendored 进 `static/`，不走 CDN（离线/可审计）。

### 4.5 fly 配置

`deploy/fly.toml` 增加 `web` process group，复用同镜像；给 web 配 `[http_service]`（公网 URL + 健康检查），`monitor`/`scheduler` 保持无公网。fly secret 增 `DASHBOARD_USER` / `DASHBOARD_PASSWORD_HASH`。

**Scale-to-zero（确认采用）**：web 进程是 scale-to-zero 的理想对象——单人、访问稀疏。`[http_service]` 配 `auto_stop_machines = "stop"` / `auto_start_machines = true` / `min_machines_running = 0`：有 HTTP 请求时 fly 自动唤醒机器，空闲一段后自动停。代价是首次访问的冷启动延迟（uvicorn 起 + 首查，数秒量级），对监控看板可接受。

与轮询的关系（关键）：HTMX 每 5–10s 轮询只在**浏览器标签开着时**发请求——开着 = 机器保持温热实时刷新；关掉 = 没有请求 → 机器空闲后自动停 → 真正零成本。这正是单人运维想要的形态，与 bot 进程现有「5s+hourly scale-to-zero」决策一致（见记忆 `p4-deploy-decisions`）。

注意：scale-to-zero 下 web 是单实例按需起，§4.2 的内存态失败计数器/锁定**会随机器停止而清空**——第一期单人可接受（攻击者也要先穿过 fly + HTTPS）；要让锁定跨重启持久，需落 DB（留待 P2，见 §9）。

## 5. 第一期四视图（全部只读）

| 视图 | 数据来源 | 关键字段 |
|---|---|---|
| **① 系统健康顶栏**（常驻每页顶部） | `heartbeats` + introspect + `fetch_balance` | monitor/scheduler 上次心跳距今秒数（超阈值标红）、连接 endpoint（testnet/mainnet）、账户余额、DB 连通 |
| **② 活跃网格总览**（首页主表） | `grids`(ACTIVE_STATES) ⋈ `grid_accounting` + 现价 | 每行：币种/状态/方向/价区间/挂单数/净持仓/已实现PnL/未实现PnL/止损线距现价 |
| **③ 单网格明细**（点击进入） | `grid_orders` + `grid_fills` + `grid_accounting` | 该网格全部挂单的价/量/状态、成交流水、记账明细（fee/funding/avg_price/pnl_ratio_max） |
| **④ 历史战绩 / 成交流**（独立页） | `order_records` + `grid_fills` | 已平网格列表（总盈亏/pnl_ratio/exit_reason）、**按 tag 聚合小结**（总盈亏/胜率/笔数）、全局最近成交流水 |

**刷新**：HTMX 每 5–10s 局部轮询（对齐 monitor 周期），无 websocket。心跳阈值（判定进程「掉线」）默认 monitor 30s / scheduler 视整点节奏放宽（实现期定具体值）。

## 6. 鉴权与安全

公网 URL 必须挡一道：
- 登录页（用户名+密码）→ 会话 cookie → 才放行业务页。
- 失败计数 + ≥1 小时锁定（§4.2）。
- 第一期只读、不暴露私钥/不动执行路径，强度够单人内部用。
- 会话 cookie HttpOnly+Secure+SameSite；强制 HTTPS（fly 默认）。

## 7. 测试（沿用双后端 TDD）

- `queries.py`：内存 SQLite + 现有 `store` fixture 喂数据，断言聚合 DTO 正确（含未实现盈亏算式、tag 聚合、优雅退化）；行情用 FakeExchange 注入，不碰真交易所。
- 路由：FastAPI `TestClient` 断状态码、鉴权（未登录 401/跳登录）、锁定逻辑（N 次失败后即使正确也拒）、关键字段渲染。
- `auth.py`：失败计数与锁定窗口单测（含时间推进）。
- 不引入真网络/真交易所；与现有 CI（py3.9 + pytest）一致。

## 8. 分阶段路线

- **P1（本设计）**：只读四视图 + 健康顶栏 + 登录鉴权，部署成第三 `web` 进程。
- **P2**：控制台——手动开/关网格、kill switch、暂停 scheduler。走 form POST → 复用 `GridManager`/runtime，**带二次确认 + 审计日志**。会写状态/动交易所，**单独 spec**；鉴权升级为 per-action 二次确认 + 操作审计；锁定计数器落持久层。
- **P3**：复盘分析——权益曲线、tag 盈亏归因、成交分布图（届时可能引入图表库）。

## 9. 风险与开放项（实现期定）

- 心跳「掉线」阈值具体秒数（monitor/scheduler 节奏不同）。
- 密码哈希算法落地（优先标准库，避免新依赖）。
- 只读 adapter 的 ticker 缓存 TTL 与失败退化文案。
- web VM 资源（512mb 共享 CPU 是否够 uvicorn + 模板渲染；预计够）。
- 多 web 实例时锁定计数器需落 DB/Redis——第一期单实例规避。
- scale-to-zero 冷启动延迟（首次访问数秒）；内存态锁定计数器随机器停止清空（持久化留待 P2）。
