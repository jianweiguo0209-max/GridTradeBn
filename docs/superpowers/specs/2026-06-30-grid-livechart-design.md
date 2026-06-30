# P1 实时网格价格图（live chart）— 设计文档

> 日期：2026-06-30
> 状态：设计已确认，待转 writing-plans
> 前序：P1 只读监控 / P2 控制台 / P3 复盘分析（均已上线 testnet）。复用 P3 服务端内联 SVG 路线（`charts.py`）。
> 相关记忆：`dashboard-project`

## 1. 背景与目标

在 P1 单网格明细页（`/grid/{id}`）加一张**实时价格走势图**：显示该持仓币在网格周期内的价格走势，叠加网格挂点线、当前买/卖挂单、已成交点位、入场价与止盈/止损线、当前价标记。图表**每 5s 异步局部刷新**（不整页重载）。延续「服务端内联 SVG、零新前端框架、可单测」的栈；刷新用原生 JS fetch 轮询（dashboard 首个局部异步刷新）。

**核心约束（延续 P1/P2/P3）**：web 进程**只读**——图表端点只调只读行情（`fetch_ohlcv`/`fetch_price`）+ 读 DB + 纯函数重算网格线；零写。

## 2. 范围（已确认）

- **叠加层（4 类全要）**：① 网格挂点线 + 买/卖挂单标记；② 已成交点位；③ 入场价 + 止盈/止损线；④ 当前价标记。外加价格走势主干（K 线收盘折线）。
- **数据源**：价格序列按需 `fetch_ohlcv`；不新增存储。
- **刷新**：原生 JS fetch 轮询，每 5s 换图表片段；标签页隐藏时暂停。
- **时间窗口**：默认「网格生命周期」（opened_at→现在/closed_at），可切换 1h/6h/24h。
- **落点**：仅单网格明细页 `/grid/{id}`；只刷图表，页面其余表格不动。

**不在本期（YAGNI）**：蜡烛图（用折线）；总览页放图；缩放/平移；自定义窗口；K 线缓存策略之外的预存。

## 3. 架构与数据流

```
浏览器 /grid/{id} 明细页
  <div id="livechart"> ← 内联 JS：load + setInterval(5s) → fetch 片段 → 换 innerHTML
        │  GET /grid/{id}/chart?window=life|1h|6h|24h（登录门控，只读，返回 SVG 片段）
        ▼
  web：build_grid_chart(store, adapter, grid_id, window) -> ChartDTO
        ├─ ohlcv：adapter.fetch_ohlcv(symbol, timeframe, start_ms, end_ms)  ← 只读行情
        ├─ grid_lines：grid_order_info(grid 行参数)['价格序列']               ← 纯函数重算
        ├─ open_orders：grid_orders status='open'（line/side/price）          ← 读 DB
        ├─ fills：grid_fills（ts/price/side）                                  ← 读 DB
        ├─ entry/stop_low/stop_high：grid 行字段
        └─ current_price：adapter.fetch_price(symbol)                          ← 只读行情
        ▼  gridchart.render(dto) -> 内联 SVG 片段
```

**不变量**：图表端点只读（fetch_ohlcv/fetch_price 是只读行情，与 P1 health/overview 同级；DB 只读；网格线纯函数重算）。**零写**。

**timeframe 自适应**（让点数 ~150–300，避免拉太多）：窗口跨度 ≤2h→`1m`、≤12h→`5m`、≤2d→`15m`、否则 `1h`。`start_ms`/`end_ms` 按窗口算：life→[opened_at, now（或 closed_at）]；1h/6h/24h→[now-N, now]。

## 4. 模块设计

```
gridtrade/dashboard/
  gridchart.py   # ★ build_grid_chart(采集→ChartDTO) + render(ChartDTO→svg 纯函数) + 窗口/timeframe helper
  app.py         # 改：GET /grid/{id}/chart 片段端点（登录门控）
  templates/detail.html  # 改：嵌 <div id="livechart"> + 窗口按钮 + 轮询 JS
  charts.py      # 复用其坐标/格式思路（必要时抽公共 scale helper；不强制）
```

### 4.1 数据采集 `build_grid_chart(store, adapter, grid_id, window, *, now_ms_fn=now_ms) -> ChartDTO`
- 读 `grid = GridRepository.get(grid_id)`；不存在 → 返回 `None`（端点据此 404）。
- 算窗口 `[start_ms, end_ms]` 与 `timeframe`（§3 规则；life 用 `grid.opened_at`/`created_at` 起，已平用 `closed_at` 止，否则 now）。
- **价格序列**：`try: df = adapter.fetch_ohlcv(symbol, timeframe, start_ms, end_ms)` 取 `[(candle_begin_time_ms, close), ...]`；`except Exception: price_series = []`（**降级标志** `ohlcv_ok=False`，不抛）。
- **网格线**：`gi = grid_order_info(grid.cap, grid.leverage, grid.low_price, grid.high_price, int(grid.grid_count), grid.stop_low_price, grid.stop_high_price)`；`grid_lines = [float(p) for p in gi['价格序列']]`（`gi is None` → `[]`）。
- **挂单**：`OrderRepository.list_open_by_grid(grid_id)` → `[(price, side), ...]`。
- **成交**：`FillRepository.list_by_grid(grid_id)`（窗口内 ts）→ `[(ts, price, side), ...]`。
- **当前价**：`try: current_price = adapter.fetch_price(symbol) except: current_price = None`。
- 返回 `ChartDTO(symbol, window, timeframe, start_ms, end_ms, price_series, ohlcv_ok, grid_lines, open_orders, fills, entry_price, stop_low, stop_high, current_price)`。

### 4.2 渲染 `render(dto: ChartDTO, *, width=720, height=320, pad=28) -> str`（纯函数，可单测）
- x：ts→[pad, width-pad]（按 `start_ms..end_ms`）；y：price→[height-pad, pad]（高价在上）。y 范围 = {price_series 高低、grid_lines、stop_low/high、entry、current_price} 并集（各自存在才计）。
- 层序（下→上）：① grid_lines 横细线，有 open 买单的线染绿/卖单染红、其余灰；② entry 中性虚线 + stop_low/high 红虚线；③ price_series 折线（`<polyline>`）；④ fills 小圆点（买绿卖红）；⑤ current_price 右缘标记 + 横虚线。
- 轴：起/中/现 3 个时间刻度（HH:MM）+ 若干价格刻度；一行图例。
- **降级**：`ohlcv_ok=False` 或 `price_series=[]` → 不画价格折线，仍画 DB 层（grid_lines/orders/fills/entry/stops，按 grid_lines∪stops 定 y 范围）+ 文案「行情暂不可用」。永不抛异常。
- 安全：SVG 全由数值/固定文案拼接，**无 symbol/用户/DB 文本插值**（symbol 只进端点查询、不进 SVG）→ 模板可 `| safe`。

## 5. 路由与刷新

- `GET /grid/{id}/chart?window=life|1h|6h|24h`：登录门控（`_user`，匿名 302 /login）；`build_grid_chart` → None 则 404；否则 `render` 返回 `HTMLResponse(svg)`。`window` 非法值回退 `life`。
- `detail.html`：在现有内容里插入：
  - 窗口按钮（life/1h/6h/24h），点击设 `window` 并立即重拉。
  - `<div id="livechart">`（首屏可服务端先渲染一次，或留空由 JS 首拉）。
  - 内联 JS（~20 行）：`load → fetch('/grid/{id}/chart?window='+cur) → #livechart.innerHTML`；`setInterval(5000)` 轮询；`document.hidden` 时跳过该次轮询（隐藏标签不拉）。
- 仅刷新图表；明细页其余表格保持现状（不改其刷新方式）。

### 5.1 行情调用去重（可选但建议）
图表端点对 `fetch_ohlcv` 加 **~4s TTL 进程内缓存**（键 `(symbol, timeframe, start_bucket, end_bucket)`），让多标签/快刷共享一次交易所调用，降限频风险。单实例内存缓存即可（web 单进程）。

## 6. 鉴权 / 安全

- 端点在 P1 登录会话之后（沿用 `_user`）；纯 GET、只读。
- web 仍**零写**：只 `fetch_ohlcv`/`fetch_price`（只读行情）+ 读 DB + 纯函数重算。
- SVG 由服务端从数值生成、无文本插值；`| safe` 仅用于该 SVG（同 P3 边界）。

## 7. 测试（双后端 + FakeExchange）

- `render(dto)`（纯函数）：喂已知 ChartDTO → 断言价格 `<polyline>` 点、grid_lines 横线（买绿卖红着色）、fills 圆点、entry/stop 虚线、current 标记；`ohlcv_ok=False`/空序列 DTO → 无价格折线但 DB 层在 + 「行情暂不可用」。
- `build_grid_chart`：FakeExchange（`fetch_ohlcv` 返 DataFrame、`fetch_price`）+ store 喂 grid/orders/fills → 断言 DTO 字段（grid_lines 重算、open_orders、fills、entry/stops、current_price）；`fetch_ohlcv` 抛错 → `ohlcv_ok=False` 且不抛；缺网格 → None。
- 窗口→timeframe/start_ms 映射单测（life 用 opened_at；1h/6h/24h 用 now-N；timeframe 分档）。
- 端点：TestClient 鉴权门控（匿名 302）/ 登录 200 返 SVG 片段 / `window` 参数 / 缺网格 404。
- detail 页：含 `#livechart` + 轮询 JS（`setInterval`/`fetch`/`document.hidden`）+ 窗口按钮；既有 dashboard 测试不回归。

## 8. 风险与开放项（实现期定）

- `fetch_ohlcv` 每 5s/每查看者一次；单运维可接受，TTL 缓存进一步去重。限频时降级到 DB 层。
- `grid.opened_at` 可能为空（早期记录）→ life 起点回退 `grid.created_at`。
- CANDLE_COLS 实际列名（candle_begin_time / close）实现期按 `gridtrade` 既有常量取；DataFrame→序列在 build 层完成，render 只吃数值。
- y 轴刻度/降采样（点多时）实现期按真实数据微调；折线点数已由 timeframe 自适应控制。
- 已平网格（CLOSED）仍可看图：window=life 用 [opened_at, closed_at]，current_price 仍取（或省略标记）。
