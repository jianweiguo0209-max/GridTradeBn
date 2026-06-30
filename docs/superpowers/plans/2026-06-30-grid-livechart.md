# P1 实时网格价格图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 单网格明细页 `/grid/{id}` 加一张实时价格图——K 线走势叠加网格挂点线/买卖挂单/已成交点/入场止损线/当前价，原生 JS 每 5s 异步局部刷新。

**Architecture:** 新模块 `gridtrade/dashboard/gridchart.py`：`build_grid_chart`（采集——只读拉 K 线 + 纯函数重算挂点 + 读 DB 单/成交 + 现价 → ChartDTO）与 `render`（纯函数 ChartDTO→内联 SVG）。新端点 `GET /grid/{id}/chart` 返 SVG 片段；明细页嵌 `<div id="livechart">` + 轮询 JS。web 仍零写；`fetch_ohlcv` 失败优雅降级到纯 DB 层。

**Tech Stack:** Python 3.9 / FastAPI / Jinja2 / 原生 JS / 内联 SVG / pandas / SQLAlchemy 2.0 Core / pytest（双后端 + FakeExchange）。

## Global Constraints

- Python 3.9；测试命令 `TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- DB 测试用 `tests/conftest.py` 的 `store` fixture（默认内存 SQLite；`TEST_DATABASE_URL` 走 PG）。
- 时间戳一律 UTC 毫秒整数，用 `gridtrade.state.models.now_ms`。
- **web 进程零写**：图表端点 + `build_grid_chart` 只调只读行情（`fetch_ohlcv`/`fetch_price`）+ 读 DB + 纯函数 `grid_order_info` 重算挂点；无写、无下单。
- SVG 全服务端从**数值/固定文案**生成，**无 symbol/用户/DB 文本插值**；模板 `| safe` 仅用于该 SVG（同 P3 边界）。
- `fetch_ohlcv`/`fetch_price` 失败 try/except 降级（`ohlcv_ok=False` / `current_price=None`），**绝不抛**；端点永远 200 返 SVG（缺网格才 404）。
- 图表端点登录门控（沿用 app.py 的 `_user`，匿名 302 /login）。
- 新参数/字段默认 off/None → 既有 dashboard 测试不回归。
- OHLCV DataFrame 列见 `gridtrade.exchanges.base.CANDLE_COLS`：`candle_begin_time`（datetime64）、`close`（float）。FakeExchange 用 `seed_ohlcv(symbol, df)` 喂、`set_price(symbol, p)` 设价。

---

### Task 1: ChartDTO + 窗口/timeframe helper

**Files:**
- Create: `gridtrade/dashboard/gridchart.py`
- Test: `tests/dashboard/test_gridchart_window.py`

**Interfaces:**
- Consumes: `gridtrade.state.models.now_ms`；`Grid` 数据类字段（`opened_at`/`created_at`/`closed_at`/`status`）。
- Produces:
  - `@dataclass ChartDTO(symbol, window, timeframe, start_ms, end_ms, price_series, ohlcv_ok, grid_lines, open_orders, fills, entry_price, stop_low, stop_high, current_price)`——`price_series: List[Tuple[int,float]]`、`grid_lines: List[float]`、`open_orders: List[Tuple[float,str]]`(price,side)、`fills: List[Tuple[int,float,str]]`(ts,price,side)、`ohlcv_ok: bool`、`entry_price/stop_low/stop_high/current_price: Optional[float]`。
  - `def window_bounds(grid, window: str, *, now_ms_fn=now_ms) -> Tuple[int, int, str]`——返回 `(start_ms, end_ms, timeframe)`。`life`：start=`grid.opened_at or grid.created_at`，end=`grid.closed_at or now`；`1h/6h/24h`：start=`now - N*3600_000`，end=`now`；非法 window 当 `life`。timeframe 按 `end-start` 跨度：≤2h→`1m`、≤12h→`5m`、≤2d→`15m`、否则 `1h`。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_gridchart_window.py
from gridtrade.dashboard.gridchart import window_bounds, ChartDTO
from gridtrade.state.models import Grid, ACTIVE, CLOSED


def _grid(**kw):
    base = dict(id='g1', exchange='x', symbol='BTC/USDT:USDT', status=ACTIVE,
                created_at=1_000_000, opened_at=1_000_000)
    base.update(kw)
    return Grid(**base)


def test_window_bounds_life_active():
    g = _grid(opened_at=1_000_000)
    start, end, tf = window_bounds(g, 'life', now_ms_fn=lambda: 1_000_000 + 3600_000)
    assert start == 1_000_000 and end == 1_000_000 + 3600_000
    assert tf == '1m'                       # 1h 跨度 ≤2h → 1m


def test_window_bounds_life_closed_uses_closed_at():
    g = _grid(status=CLOSED, opened_at=1_000_000, closed_at=1_000_000 + 6 * 3600_000)
    start, end, tf = window_bounds(g, 'life', now_ms_fn=lambda: 9_999_999_999)
    assert end == 1_000_000 + 6 * 3600_000  # 已平用 closed_at，不用 now
    assert tf == '5m'                        # 6h 跨度 ≤12h → 5m


def test_window_bounds_fixed_24h():
    g = _grid()
    now = 100_000_000
    start, end, tf = window_bounds(g, '24h', now_ms_fn=lambda: now)
    assert end == now and start == now - 24 * 3600_000
    assert tf == '15m'                       # 24h ≤2d → 15m


def test_window_bounds_bad_value_falls_back_to_life():
    g = _grid(opened_at=5_000_000)
    start, _end, _tf = window_bounds(g, 'nonsense', now_ms_fn=lambda: 5_000_000 + 1000)
    assert start == 5_000_000                # 回退 life


def test_chart_dto_defaults():
    d = ChartDTO(symbol='BTC', window='life', timeframe='1m', start_ms=0, end_ms=1,
                 price_series=[], ohlcv_ok=False, grid_lines=[], open_orders=[],
                 fills=[], entry_price=None, stop_low=None, stop_high=None,
                 current_price=None)
    assert d.ohlcv_ok is False and d.grid_lines == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_window.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.gridchart`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/gridchart.py
"""单网格实时价格图：build_grid_chart(采集只读) + render(纯函数 SVG)。web 零写。"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from gridtrade.state.models import now_ms

_HOUR = 3600_000


@dataclass
class ChartDTO:
    symbol: str
    window: str
    timeframe: str
    start_ms: int
    end_ms: int
    price_series: List[Tuple[int, float]]
    ohlcv_ok: bool
    grid_lines: List[float]
    open_orders: List[Tuple[float, str]]
    fills: List[Tuple[int, float, str]]
    entry_price: Optional[float]
    stop_low: Optional[float]
    stop_high: Optional[float]
    current_price: Optional[float]


def _timeframe_for(span_ms: int) -> str:
    if span_ms <= 2 * _HOUR:
        return '1m'
    if span_ms <= 12 * _HOUR:
        return '5m'
    if span_ms <= 2 * 24 * _HOUR:
        return '15m'
    return '1h'


def window_bounds(grid, window: str, *, now_ms_fn=now_ms) -> Tuple[int, int, str]:
    now = int(now_ms_fn())
    if window in ('1h', '6h', '24h'):
        hours = {'1h': 1, '6h': 6, '24h': 24}[window]
        start, end = now - hours * _HOUR, now
    else:                                    # life（含非法回退）
        start = int(grid.opened_at or grid.created_at or now)
        end = int(grid.closed_at) if getattr(grid, 'closed_at', None) else now
    return start, end, _timeframe_for(end - start)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_window.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/gridchart.py tests/dashboard/test_gridchart_window.py
git commit -m "feat(livechart): ChartDTO + window_bounds（生命周期/1h/6h/24h + timeframe 自适应）"
```

---

### Task 2: build_grid_chart（采集 → ChartDTO，只读 + 降级）

**Files:**
- Modify: `gridtrade/dashboard/gridchart.py`
- Test: `tests/dashboard/test_gridchart_build.py`

**Interfaces:**
- Consumes: `window_bounds`/`ChartDTO`(T1)；`GridRepository.get`/`OrderRepository.list_open_by_grid`/`FillRepository.list_by_grid`；`gridtrade.core.grid_engine.grid_order_info`；adapter `fetch_ohlcv(symbol, timeframe, start_ms, end_ms)->DataFrame`、`fetch_price(symbol)->float`。
- Produces:
  - `def build_grid_chart(store, adapter, grid_id, window, *, now_ms_fn=now_ms) -> Optional[ChartDTO]`——grid 不存在返回 `None`。`grid_lines` 由 `grid_order_info(cap, leverage, low, high, int(grid_count), stop_low, stop_high)['价格序列']`（`None`→`[]`）；`price_series` 由 `fetch_ohlcv` 的 `candle_begin_time`(datetime→ms)+`close`（失败→`[]`, `ohlcv_ok=False`）；`open_orders` 取 open 单 `(price, side)`；`fills` 取窗口内 `(ts, price, side)`；`current_price` 由 `fetch_price`（失败→None）。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_gridchart_build.py
import pandas as pd
from gridtrade.dashboard.gridchart import build_grid_chart
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Grid, GridOrder, Fill, ACTIVE


def _seed(store):
    GridRepository(store).create(Grid(
        id='g1', exchange='fake', symbol='BTC/USDT:USDT', status=ACTIVE,
        created_at=1_000_000, opened_at=1_000_000, entry_price=100.0,
        low_price=90.0, high_price=110.0, grid_count=10,
        stop_low_price=80.0, stop_high_price=120.0, cap=100.0, leverage=5.0))
    OrderRepository(store).upsert(GridOrder(client_oid='o1', grid_id='g1', line_index=1,
                                            side='buy', price=95.0, size=1.0, status='open'))
    FillRepository(store).add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=2,
                                          side='sell', price=105.0, size=1.0, fee=0.1, ts=1_500_000))


def _candles():
    return pd.DataFrame({
        'symbol': ['BTC/USDT:USDT', 'BTC/USDT:USDT'],
        'candle_begin_time': pd.to_datetime([1_000_000, 1_060_000], unit='ms'),
        'open': [100.0, 101.0], 'high': [102.0, 103.0], 'low': [99.0, 100.0],
        'close': [101.0, 102.5], 'vol': [1.0, 1.0], 'volCcy': [1.0, 1.0],
        'quote_volume': [1.0, 1.0],
    })


def test_build_populates_all_layers(store):
    _seed(store)
    fake = FakeExchange(); fake.seed_ohlcv('BTC/USDT:USDT', _candles()); fake.set_price('BTC/USDT:USDT', 102.0)
    dto = build_grid_chart(store, fake, 'g1', 'life', now_ms_fn=lambda: 2_000_000)
    assert dto is not None
    assert dto.ohlcv_ok is True
    assert dto.price_series == [(1_000_000, 101.0), (1_060_000, 102.5)]
    assert len(dto.grid_lines) >= 2 and min(dto.grid_lines) >= 90.0 - 1e-9
    assert dto.open_orders == [(95.0, 'buy')]
    assert dto.fills == [(1_500_000, 105.0, 'sell')]
    assert dto.entry_price == 100.0 and dto.stop_low == 80.0 and dto.stop_high == 120.0
    assert dto.current_price == 102.0


def test_build_degrades_on_ohlcv_error(store):
    _seed(store)

    class _BadOhlcv(FakeExchange):
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError('rate limited')
    fake = _BadOhlcv(); fake.set_price('BTC/USDT:USDT', 102.0)
    dto = build_grid_chart(store, fake, 'g1', 'life', now_ms_fn=lambda: 2_000_000)
    assert dto.ohlcv_ok is False and dto.price_series == []
    assert len(dto.grid_lines) >= 2                # DB/纯函数层仍在
    assert dto.open_orders == [(95.0, 'buy')]


def test_build_missing_grid_returns_none(store):
    assert build_grid_chart(store, FakeExchange(), 'nope', 'life') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_build.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_grid_chart'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/dashboard/gridchart.py`)

```python
# --- 追加到 gridtrade/dashboard/gridchart.py ---
import pandas as pd

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository


def _grid_lines(grid) -> List[float]:
    try:
        gi = grid_order_info(grid.cap, grid.leverage, grid.low_price, grid.high_price,
                             int(grid.grid_count), grid.stop_low_price, grid.stop_high_price)
    except Exception:
        return []
    if gi is None:
        return []
    return [float(p) for p in gi['价格序列']]


def build_grid_chart(store, adapter, grid_id, window, *, now_ms_fn=now_ms):
    grid = GridRepository(store).get(grid_id)
    if grid is None:
        return None
    start_ms, end_ms, timeframe = window_bounds(grid, window, now_ms_fn=now_ms_fn)

    price_series: List[Tuple[int, float]] = []
    ohlcv_ok = True
    try:
        df = adapter.fetch_ohlcv(grid.symbol, timeframe, start_ms, end_ms)
        if df is not None and not df.empty:
            ts_ms = (pd.to_datetime(df['candle_begin_time']).astype('int64') // 1_000_000)
            price_series = [(int(t), float(c)) for t, c in zip(ts_ms, df['close'])]
    except Exception:
        ohlcv_ok = False

    grid_lines = _grid_lines(grid)
    open_orders = [(float(o.price), o.side)
                   for o in OrderRepository(store).list_open_by_grid(grid_id)]
    fills = [(int(f.ts), float(f.price), f.side)
             for f in FillRepository(store).list_by_grid(grid_id)
             if start_ms <= f.ts <= end_ms]

    current_price = None
    try:
        current_price = float(adapter.fetch_price(grid.symbol))
    except Exception:
        current_price = None

    return ChartDTO(
        symbol=grid.symbol, window=window, timeframe=timeframe,
        start_ms=start_ms, end_ms=end_ms, price_series=price_series, ohlcv_ok=ohlcv_ok,
        grid_lines=grid_lines, open_orders=open_orders, fills=fills,
        entry_price=grid.entry_price, stop_low=grid.stop_low_price,
        stop_high=grid.stop_high_price, current_price=current_price)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_build.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/gridchart.py tests/dashboard/test_gridchart_build.py
git commit -m "feat(livechart): build_grid_chart（只读采集 + ohlcv 降级 + 纯函数重算挂点）"
```

---

### Task 3: render（ChartDTO → 内联 SVG，纯函数 + 降级）

**Files:**
- Modify: `gridtrade/dashboard/gridchart.py`
- Test: `tests/dashboard/test_gridchart_render.py`

**Interfaces:**
- Consumes: `ChartDTO`(T1)。
- Produces:
  - `def render(dto: ChartDTO, *, width: int = 720, height: int = 320, pad: int = 28) -> str`——返回 `<svg>` 串。x：ts→[pad,width-pad]（按 start_ms..end_ms）；y：price→[height-pad,pad]（高价在上），y 范围 = price_series∪grid_lines∪{entry,stop_low,stop_high,current_price 非 None}。层：grid_lines 横线（open 买单价着绿/卖单价着红/其余灰）→ entry 中性虚线 + stop 红虚线 → price 折线 `<polyline>` → fills `<circle>`（买绿卖红）→ current 横虚线 + 右缘点。`ohlcv_ok=False` 或 `price_series=[]`：不画折线、加 `<text>行情暂不可用</text>`，仍画 DB 层（y 范围用 grid_lines∪stops）。全空 → 占位 `<text>无数据</text>`。**无 symbol/文本插值**。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_gridchart_render.py
from gridtrade.dashboard.gridchart import render, ChartDTO


def _dto(**kw):
    base = dict(symbol='BTC', window='life', timeframe='1m', start_ms=1000, end_ms=2000,
                price_series=[(1000, 100.0), (2000, 110.0)], ohlcv_ok=True,
                grid_lines=[95.0, 105.0], open_orders=[(95.0, 'buy'), (105.0, 'sell')],
                fills=[(1500, 102.0, 'buy')], entry_price=100.0, stop_low=80.0,
                stop_high=120.0, current_price=108.0)
    base.update(kw)
    return ChartDTO(**base)


def test_render_full_chart():
    svg = render(_dto(), width=200, height=200, pad=20)
    assert svg.startswith('<svg') and svg.endswith('</svg>')
    assert '<polyline' in svg                      # 价格走势
    assert svg.count('<line') >= 2 + 1 + 2         # 2 网格线 + entry + 2 stop（至少）
    assert '#4caf50' in svg and '#e53935' in svg   # 买绿（grid 95/ fill）、卖红（grid 105）
    assert svg.count('<circle') >= 1               # 至少 1 个 fill 点（current 也可能是 circle）


def test_render_degrades_without_ohlcv():
    svg = render(_dto(price_series=[], ohlcv_ok=False), width=200, height=200, pad=20)
    assert '<polyline' not in svg                  # 无价格折线
    assert '行情暂不可用' in svg
    assert svg.count('<line') >= 2                 # 网格线仍在


def test_render_all_empty_placeholder():
    svg = render(_dto(price_series=[], ohlcv_ok=False, grid_lines=[], open_orders=[],
                      fills=[], entry_price=None, stop_low=None, stop_high=None,
                      current_price=None))
    assert '无数据' in svg and '<polyline' not in svg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_render.py -v`
Expected: FAIL — `ImportError: cannot import name 'render'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/dashboard/gridchart.py`)

```python
# --- 追加到 gridtrade/dashboard/gridchart.py ---
def _yvals(dto) -> List[float]:
    vs = [p for _, p in dto.price_series]
    vs += list(dto.grid_lines)
    for v in (dto.entry_price, dto.stop_low, dto.stop_high, dto.current_price):
        if v is not None:
            vs.append(float(v))
    return vs


def render(dto, *, width: int = 720, height: int = 320, pad: int = 28) -> str:
    yvals = _yvals(dto)
    if not yvals:
        return ('<svg viewBox="0 0 %d %d" class="chart"><text x="%d" y="%d" '
                'text-anchor="middle" fill="#999">无数据</text></svg>'
                % (width, height, width // 2, height // 2))
    ymin, ymax = min(yvals), max(yvals)
    dy = (ymax - ymin) or 1.0
    xmin, xmax = dto.start_ms, dto.end_ms
    dx = (xmax - xmin) or 1.0
    iw, ih = width - 2 * pad, height - 2 * pad

    def sx(t): return pad + (t - xmin) / dx * iw
    def sy(p): return pad + (ymax - p) / dy * ih

    buy = {round(pr, 8) for pr, sd in dto.open_orders if sd == 'buy'}
    sell = {round(pr, 8) for pr, sd in dto.open_orders if sd == 'sell'}
    parts = []
    # 网格挂点线（买绿/卖红/其余灰）
    for gl in dto.grid_lines:
        key = round(gl, 8)
        color = '#4caf50' if key in buy else ('#e53935' if key in sell else '#333')
        y = sy(gl)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="0.8"/>' % (pad, y, width - pad, y, color))
    # 入场（中性虚线）+ 止盈/止损（红虚线）
    if dto.entry_price is not None:
        y = sy(dto.entry_price)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#999" '
                     'stroke-dasharray="4" stroke-width="0.8"/>' % (pad, y, width - pad, y))
    for stop in (dto.stop_low, dto.stop_high):
        if stop is not None:
            y = sy(stop)
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e53935" '
                         'stroke-dasharray="4" stroke-width="0.8"/>' % (pad, y, width - pad, y))
    # 价格走势 / 降级文案
    if dto.ohlcv_ok and dto.price_series:
        coords = ' '.join('%.1f,%.1f' % (sx(t), sy(p)) for t, p in dto.price_series)
        parts.append('<polyline fill="none" stroke="#6cf" stroke-width="1.5" points="%s"/>'
                     % coords)
    else:
        parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#e53935">行情暂不可用</text>'
                     % (width // 2, pad + 12))
    # 已成交点（买绿卖红）
    for ts, price, side in dto.fills:
        if not (xmin <= ts <= xmax):
            continue
        c = '#4caf50' if side == 'buy' else '#e53935'
        parts.append('<circle cx="%.1f" cy="%.1f" r="2.5" fill="%s"/>' % (sx(ts), sy(price), c))
    # 当前价（横虚线 + 右缘点）
    if dto.current_price is not None:
        y = sy(dto.current_price)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#fb0" '
                     'stroke-dasharray="2" stroke-width="0.8"/>' % (pad, y, width - pad, y))
        parts.append('<circle cx="%.1f" cy="%.1f" r="3" fill="#fb0"/>' % (width - pad, y))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_render.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/gridchart.py tests/dashboard/test_gridchart_render.py
git commit -m "feat(livechart): render 纯函数 SVG（挂点/挂单/成交/入场止损/现价 + ohlcv 降级）"
```

---

### Task 4: /grid/{id}/chart 端点 + 明细页轮询 JS

**Files:**
- Modify: `gridtrade/dashboard/app.py`（`GET /grid/{id}/chart` 路由）
- Modify: `gridtrade/dashboard/templates/detail.html`（嵌 `#livechart` + 窗口按钮 + 轮询 JS）
- Test: `tests/dashboard/test_app_livechart.py`

**Interfaces:**
- Consumes: `gridtrade.dashboard.gridchart`（`build_grid_chart`, `render`）；`_user`；`store`/`adapter`（create_app 闭包内已有）。
- Produces: `GET /grid/{grid_id}/chart?window=...`——未登录 302 /login；`build_grid_chart` 返 None → 404；否则 `HTMLResponse(render(dto))`（200 SVG 片段）。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_app_livechart.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid, ACTIVE
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0
    def fetch_ohlcv(self, s, tf, a, b):
        import pandas as pd
        return pd.DataFrame()                      # 空 K 线 → 降级，仍 200


def _app(store):
    return create_app(store, _Adapter(), username='admin',
                      password_hash=hash_password('pw', iterations=1000), session_secret='sek')


def _seed(store):
    GridRepository(store).create(Grid(id='g1', exchange='x', symbol='BTC/USDT:USDT',
                                      status=ACTIVE, created_at=1000, opened_at=1000,
                                      low_price=90.0, high_price=110.0, grid_count=10,
                                      stop_low_price=80.0, stop_high_price=120.0,
                                      cap=100.0, leverage=5.0, entry_price=100.0))


def test_chart_requires_login(store):
    _seed(store)
    anon = TestClient(_app(store), base_url='https://testserver')
    r = anon.get('/grid/g1/chart', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'].endswith('/login')


def test_chart_returns_svg_fragment(store):
    _seed(store)
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    r = c.get('/grid/g1/chart?window=6h')
    assert r.status_code == 200 and '<svg' in r.text


def test_chart_missing_grid_404(store):
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert c.get('/grid/nope/chart').status_code == 404


def test_detail_page_has_livechart_and_poll(store):
    _seed(store)
    c = TestClient(_app(store), base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    html = c.get('/grid/g1').text
    assert 'id="livechart"' in html
    assert '/grid/g1/chart' in html and 'setInterval' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_livechart.py -v`
Expected: FAIL — chart 路由 404 / detail 无 livechart

- [ ] **Step 3: Write minimal implementation**

`app.py`：加路由（放在已有 `/grid/{grid_id}` 路由附近、`return app` 前）：

```python
    @app.get('/grid/{grid_id}/chart', response_class=HTMLResponse)
    def grid_chart(request: Request, grid_id: str, window: str = 'life'):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        from gridtrade.dashboard import gridchart as gc
        dto = gc.build_grid_chart(store, adapter, grid_id, window)
        if dto is None:
            return HTMLResponse('grid not found', status_code=404)
        return HTMLResponse(gc.render(dto))
```

`detail.html`：在 `{% block content %}` 内、`<h1>` 之后插入图表区块（用实际 grid id `d.grid.id`）：

```html
<section class="livechart-wrap">
  <div class="rangesel">
    走势：<a href="#" data-w="life">生命周期</a>
    <a href="#" data-w="1h">1h</a> <a href="#" data-w="6h">6h</a> <a href="#" data-w="24h">24h</a>
  </div>
  <div id="livechart">加载中…</div>
</section>
<script>
(function(){
  var gid = {{ d.grid.id | tojson }};
  var cur = 'life';
  var box = document.getElementById('livechart');
  function load(){
    if (document.hidden) return;               // 隐藏标签不拉
    fetch('/grid/' + encodeURIComponent(gid) + '/chart?window=' + cur)
      .then(function(r){ return r.text(); })
      .then(function(h){ box.innerHTML = h; })
      .catch(function(){});
  }
  document.querySelectorAll('.rangesel a').forEach(function(a){
    a.addEventListener('click', function(e){ e.preventDefault(); cur = a.getAttribute('data-w'); load(); });
  });
  load();
  setInterval(load, 5000);
})();
</script>
```

> 注：`{{ d.grid.id | tojson }}` 由 Jinja2 安全序列化为 JS 字符串字面量（autoescape 下 `tojson` 是注入安全的）；其余 JS 为静态文本，无 DB 插值。SVG 片段由 `render` 纯数值生成。

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_livechart.py -v`
然后全 dashboard 套件：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q`
Expected: PASS（新测 4 passed；既有 dashboard 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/app.py gridtrade/dashboard/templates/detail.html tests/dashboard/test_app_livechart.py
git commit -m "feat(livechart): /grid/{id}/chart 片段端点 + 明细页 5s 轮询 JS + 窗口切换"
```

---

### Task 5: 文档同步（STATUS / DEPLOY）

**Files:**
- Modify: `docs/STATUS.md`（§5 web 行补 live chart）
- Modify: `deploy/DEPLOY.md`（dashboard 段补 live chart）

**Interfaces:** 无代码；文档同步。

- [ ] **Step 1: 更新 STATUS.md §5 web 行**

补一句：

```markdown
  P1 明细页实时网格价格图：/grid/{id}/chart 片段端点（K 线走势 + 网格挂点/买卖挂单/已成交点/入场止损/当前价，服务端 SVG），原生 JS 每 5s 异步局部刷新（隐藏标签暂停），窗口 生命周期/1h/6h/24h；行情失败降级到 DB 层不崩。
```

- [ ] **Step 2: 更新 DEPLOY.md dashboard 段**

追加：

```markdown
### 实时网格价格图（P1 明细页）
- /grid/{id}/chart 返回 SVG 片段；明细页内联 JS 每 5s fetch 局部刷新（document.hidden 暂停）。
- 走势 fetch_ohlcv 按需拉（timeframe 按窗口自适应 1m/5m/15m/1h）；网格挂点由 grid_order_info 纯函数重算；挂单/成交读 DB；当前价 fetch_price。
- web 零写；fetch_ohlcv/fetch_price 失败 try/except 降级（画 DB 层 + 「行情暂不可用」），端点永不 500。
```

- [ ] **Step 3: Commit**

```bash
git add docs/STATUS.md deploy/DEPLOY.md
git commit -m "docs(livechart): STATUS/DEPLOY 同步 P1 实时网格价格图"
```

---

## Self-Review

**Spec 覆盖（逐节核对 2026-06-30-grid-livechart-design.md）：**
- §2 叠加层（挂点+买卖挂单/成交点/入场止损/当前价 + 走势）→ T3 render 各层；数据 T2 build。✅
- §3 架构/数据流/timeframe 自适应 → T1 window_bounds(timeframe) / T2 build(只读采集) / T4 端点。✅
- §4.1 build_grid_chart（None/降级/重算挂点）→ T2。§4.2 render（层序/降级/无文本插值）→ T3。✅
- §5 路由 + 轮询 JS（窗口按钮/document.hidden/5s）→ T4。§5.1 TTL 缓存 → 列为可选，未建任务（YAGNI，spec 标「可选」；如需在 T2 加，留实现注意）。⚠️ 见下。
- §6 鉴权/只读/`|safe` 仅 SVG → T4 端点 gate；render 无文本插值（T3）；detail 用 `tojson` 安全注入 gid。✅
- §7 测试（render/build/window/端点/detail 页）→ T1–T4 各 TDD。✅
- §8 开放项（opened_at 回退 created_at / CANDLE 列名 / 已平网格）→ T1 回退、T2 列名（candle_begin_time/close）、window_bounds 已平用 closed_at。✅

**§5.1 TTL 缓存取舍：** spec 标为「可选但建议」。本计划**未**单列任务以保持最小可用面（YAGNI）；首版每次刷新一次 fetch_ohlcv，单运维可接受。若上线后限频再加：在 `build_grid_chart` 的 fetch_ohlcv 外包一个 `(symbol,timeframe,start//4000,end//4000)→(df,ts)` 的 ~4s 进程内字典缓存。此为有意延后，非计划缺口。

**Placeholder 扫描：** 无 TBD/TODO；每 code step 给完整代码 + 命令。render 坐标公式确定、测试断言元素存在/计数/着色（含一处 polyline 生成）。✅

**类型一致：** `ChartDTO` 字段(T1) 被 T2 构造、T3 消费一致；`window_bounds(grid,window,*,now_ms_fn)->(start,end,tf)`(T1) 被 T2 调用一致；`build_grid_chart(store,adapter,grid_id,window,*,now_ms_fn)->Optional[ChartDTO]`(T2) 被 T4 端点调用一致；`render(dto,*,width,height,pad)->str`(T3) 被 T4 调用一致；OHLCV 列 `candle_begin_time`/`close`、FakeExchange `seed_ohlcv`/`set_price` 与真实接口一致。✅
```
