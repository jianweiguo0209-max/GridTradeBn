# 图表加刻度/类目/图例/数值标注 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给所有 dashboard 图表（/analytics 的 line/bar/stacked + 实时图 gridchart）补上 Y 轴刻度、X 轴刻度/类目标签、图例、数值标注。

**Architecture:** 新增 `gridtrade/dashboard/svgaxes.py` 纯函数共享层（刻度/坐标轴/图例/标注/转义），`charts.py` 三函数与 `gridchart.render` 调它绘制 chrome；/analytics 路由传序列名/类目/图例元数据。文本只用数值+时间(HH:MM)+固定词，`svg_escape` 对任何字符串标签兜底，守住「SVG 无未转义 DB/用户文本」的 |safe 边界。

**Tech Stack:** Python 3.9 / 内联 SVG / FastAPI / Jinja2 / pytest。

## Global Constraints

- Python 3.9；测试命令 `TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- SVG 以 `| safe` 渲染：新增文本来源只允许数值（`%.1f`/`%d`）、时间（`HH:MM` 从 ts UTC 算）、固定字面量；任何字符串标签经 `svg_escape` 转义（`& < > " '`）。
- 新增参数全部可选、默认保持旧行为 → 既有 dashboard 测试不回归。
- 几何绘制（polyline/rect/circle/挂点线）逻辑不变；空数据仍返回占位 SVG。
- charts.py 与 gridchart 共用 `svgaxes.py`（DRY）。

---

### Task 1: svgaxes.py 共享纯函数（转义/刻度/坐标轴/图例/标注）

**Files:**
- Create: `gridtrade/dashboard/svgaxes.py`
- Test: `tests/dashboard/test_svgaxes.py`

**Interfaces:**
- Produces（纯函数，无 I/O）：
  - `def svg_escape(s) -> str`：`str(s)` 后转义 `&`→`&amp;`、`<`→`&lt;`、`>`→`&gt;`、`"`→`&quot;`、`'`→`&#39;`（先转 `&`）。
  - `def nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]`：返回 n+1 个等分刻度值 `[lo, lo+step, ..., hi]`；`lo==hi` 返回 `[lo]`。
  - `def y_axis(ticks, sy, x_left, x_right, *, digits=2) -> str`：每个 tick 一条淡线（`stroke="#222"`）从 x_left 到 `sy(tick)` 横到 x_right，外加左侧数值 `<text>`（`%.{digits}f`，`text-anchor="end"`，x=x_left-2）。
  - `def x_time_axis(xmin, xmax, sx, y_base) -> str`：在 xmin、(xmin+xmax)//2、xmax 三处画 `HH:MM`（UTC，`datetime.utcfromtimestamp(ms/1000)`）`<text>`（`text-anchor="middle"`，y=y_base+10）。
  - `def x_cat_axis(labels, centers, y_base) -> str`：`labels[i]` 经 `svg_escape` 画在 `centers[i]`（`text-anchor="middle"`，y=y_base+10）。
  - `def legend(items, x, y) -> str`：`items=[(color, text), ...]`，每项一个 8x8 `<rect fill=color>` + `svg_escape(text)` `<text>`，横向排开（每项约 60px 宽）。
  - `def value_label(x, y, text) -> str`：`<text x y text-anchor="middle" font-size="9" fill="#ccc">svg_escape(text)</text>`。
  - 全部 `<text>` 加 `font-size="9"`、`fill="#999"`（轴）/ `#ccc`（标注），避免过大。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_svgaxes.py
from gridtrade.dashboard.svgaxes import (svg_escape, nice_ticks, y_axis,
                                         x_time_axis, x_cat_axis, legend, value_label)


def test_svg_escape():
    assert svg_escape('<script>&"\'') == '&lt;script&gt;&amp;&quot;&#39;'
    assert svg_escape(5) == '5'


def test_nice_ticks():
    assert nice_ticks(0.0, 100.0, 4) == [0.0, 25.0, 50.0, 75.0, 100.0]
    assert nice_ticks(5.0, 5.0) == [5.0]              # lo==hi 退化


def test_y_axis_has_lines_and_number_labels():
    svg = y_axis([0.0, 50.0, 100.0], sy=lambda v: 100 - v, x_left=20, x_right=200, digits=1)
    assert svg.count('<line') == 3
    assert '0.0' in svg and '50.0' in svg and '100.0' in svg


def test_x_time_axis_hhmm():
    # 0 ms = 1970-01-01 00:00 UTC
    svg = x_time_axis(0, 3600_000, sx=lambda t: t / 3600_000 * 100, y_base=120)
    assert '00:00' in svg and '01:00' in svg          # 起/现
    assert '<text' in svg


def test_x_cat_axis_escapes():
    svg = x_cat_axis(['<b>', 'sell'], [10.0, 50.0], y_base=120)
    assert '&lt;b&gt;' in svg and 'sell' in svg and '<b>' not in svg


def test_legend_swatches_and_text():
    svg = legend([('#4caf50', '买'), ('#e53935', '卖')], x=10, y=8)
    assert svg.count('<rect') == 2 and '买' in svg and '卖' in svg


def test_value_label_escapes():
    assert '<text' in value_label(10, 10, '1.5') and '1.5' in value_label(10, 10, '1.5')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_svgaxes.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.svgaxes`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/svgaxes.py
"""SVG 图表 chrome 共享纯函数：转义 / 刻度 / 坐标轴 / 图例 / 数值标注。
文本只用数值+时间+固定词；svg_escape 对字符串标签兜底，守 |safe 边界。"""
from datetime import datetime, timezone
from typing import List, Tuple


def svg_escape(s) -> str:
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]:
    if hi == lo:
        return [float(lo)]
    step = (hi - lo) / n
    return [round(lo + i * step, 10) for i in range(n + 1)]


def y_axis(ticks, sy, x_left, x_right, *, digits: int = 2) -> str:
    out = []
    for t in ticks:
        y = sy(t)
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#222" '
                   'stroke-width="0.5"/>' % (x_left, y, x_right, y))
        out.append('<text x="%.1f" y="%.1f" text-anchor="end" font-size="9" '
                   'fill="#999">%s</text>' % (x_left - 2, y + 3, ('%%.%df' % digits) % t))
    return ''.join(out)


def _hhmm(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).strftime('%H:%M')


def x_time_axis(xmin, xmax, sx, y_base) -> str:
    mid = (int(xmin) + int(xmax)) // 2
    out = []
    for t in (xmin, mid, xmax):
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" '
                   'fill="#999">%s</text>' % (sx(t), y_base + 10, _hhmm(t)))
    return ''.join(out)


def x_cat_axis(labels, centers, y_base) -> str:
    out = []
    for lab, cx in zip(labels, centers):
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" '
                   'fill="#999">%s</text>' % (cx, y_base + 10, svg_escape(lab)))
    return ''.join(out)


def legend(items: List[Tuple[str, str]], x, y) -> str:
    out = []
    cx = x
    for color, text in items:
        out.append('<rect x="%.1f" y="%.1f" width="8" height="8" fill="%s"/>'
                   % (cx, y, color))
        out.append('<text x="%.1f" y="%.1f" font-size="9" fill="#ccc">%s</text>'
                   % (cx + 10, y + 8, svg_escape(text)))
        cx += 60
    return ''.join(out)


def value_label(x, y, text) -> str:
    return ('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" fill="#ccc">%s</text>'
            % (x, y, svg_escape(text)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_svgaxes.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/svgaxes.py tests/dashboard/test_svgaxes.py
git commit -m "feat(charts): svgaxes 共享纯函数（转义/刻度/坐标轴/图例/标注）"
```

---

### Task 2: charts.py 集成 chrome（line/bar/stacked）

**Files:**
- Modify: `gridtrade/dashboard/charts.py`
- Test: `tests/dashboard/test_charts.py`（既有，追加断言 + 改默认 height 相关计数）

**Interfaces:**
- Consumes: `svgaxes`（`nice_ticks`, `y_axis`, `x_time_axis`, `x_cat_axis`, `legend`, `value_label`）。
- Produces（新增可选参，默认旧行为；几何坐标用「内绘图区」算，留出轴/图例边距）：
  - `line_chart(series, *, width=720, height=240, pad=10, x_is_time=False, series_labels=None, value_labels=False)`——`series_labels: Optional[List[Tuple[str,str]]]`（(color,name)，也决定每序列描边色）。
  - `bar_chart(bars, *, width=720, height=240, pad=10, value_labels=False)`——`bars=[(label, value)]`，label 画到 x 轴下。
  - `stacked_bar(groups, *, width=720, height=240, pad=10, seg_labels=None)`——`seg_labels: Optional[List[Tuple[str,str]]]`（(color,name) 决定段色 + 图例）。
- **绘图区边距常量**：`L=34`（左，y 标签）、`R=10`（右）、`T=16`（上，图例）、`B=16`（下，x 标签）。`plot_left=L`、`plot_right=width-R`、`plot_top=T`、`plot_bottom=height-B`。`sx/sy` 映射到该区。

- [ ] **Step 1: Write the failing test**（追加到 `tests/dashboard/test_charts.py` 末尾）

```python
# --- 追加到 tests/dashboard/test_charts.py ---
from gridtrade.dashboard.charts import line_chart, bar_chart, stacked_bar


def test_line_chart_has_axes_legend_value():
    svg = line_chart([[(0, 0.0), (3600_000, 10.0)]], x_is_time=True,
                     series_labels=[('#6cf', '权益')], value_labels=True)
    assert '<polyline' in svg                 # 几何仍在
    assert '00:00' in svg                      # x 时间刻度
    assert '权益' in svg                        # 图例
    assert '10.0' in svg or '10.00' in svg     # y 刻度/末值标注（数值出现）


def test_bar_chart_shows_category_labels_and_values():
    svg = bar_chart([('0', 5.0), ('1', 10.0)], value_labels=True)
    assert svg.count('<rect') >= 2             # 几何仍在
    assert '>0<' in svg or '>0</text>' in svg  # 类目标签 0
    assert '10' in svg                          # 顶值标注 / y 刻度


def test_stacked_bar_legend():
    svg = stacked_bar([('成交', [('buy', 3.0), ('sell', 1.0)])],
                      seg_labels=[('#4caf50', '买'), ('#e53935', '卖')])
    assert svg.count('<rect') >= 2 + 2          # 段 + 图例色块
    assert '买' in svg and '卖' in svg


def test_charts_empty_still_placeholder():
    assert '暂无数据' in line_chart([])
    assert '暂无数据' in bar_chart([])
    assert '暂无数据' in stacked_bar([])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_charts.py -v`
Expected: FAIL — `line_chart() got an unexpected keyword argument 'x_is_time'`

- [ ] **Step 3: Write minimal implementation**

把 `gridtrade/dashboard/charts.py` 整体替换为：

```python
# gridtrade/dashboard/charts.py
"""服务端内联 SVG 图表：纯函数，确定坐标映射，可单测。空数据返回占位。
chrome（刻度/类目/图例/标注）由 svgaxes 提供；文本只用数值+时间+固定词。"""
from typing import List, Tuple

from gridtrade.dashboard import svgaxes as ax

_L, _R, _T, _B = 34, 10, 16, 16     # 绘图区边距：左(y标签)/右/上(图例)/下(x标签)
_SERIES_COLORS = ['#6cf', '#fb0', '#4caf50', '#e53935']


def _placeholder(width: int, height: int) -> str:
    return ('<svg viewBox="0 0 %d %d" class="chart">'
            '<text x="%d" y="%d" text-anchor="middle" fill="#999">暂无数据</text>'
            '</svg>' % (width, height, width // 2, height // 2))


def _frame(width, height):
    return _L, width - _R, _T, height - _B     # plot_left, plot_right, plot_top, plot_bottom


def line_chart(series, *, width: int = 720, height: int = 240, pad: int = 10,
               x_is_time: bool = False, series_labels=None, value_labels: bool = False) -> str:
    pts = [p for s in series for p in s]
    if not pts:
        return _placeholder(width, height)
    pl, pr, pt, pb = _frame(width, height)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    dx = (xmax - xmin) or 1.0
    dy = (ymax - ymin) or 1.0

    def sx(x): return pl + (x - xmin) / dx * (pr - pl)
    def sy(y): return pt + (ymax - y) / dy * (pb - pt)

    parts = [ax.y_axis(ax.nice_ticks(ymin, ymax), sy, pl, pr)]
    if x_is_time:
        parts.append(ax.x_time_axis(xmin, xmax, sx, pb))
    else:
        cs = [sx(v) for v in (xmin, (xmin + xmax) / 2, xmax)]
        parts.append(ax.x_cat_axis(['%.0f' % v for v in (xmin, (xmin + xmax) / 2, xmax)], cs, pb))
    for i, s in enumerate(series):
        if not s:
            continue
        color = (series_labels[i][0] if series_labels and i < len(series_labels)
                 else _SERIES_COLORS[i % len(_SERIES_COLORS)])
        coords = ' '.join('%.1f,%.1f' % (sx(x), sy(y)) for x, y in s)
        parts.append('<polyline fill="none" stroke="%s" stroke-width="1.5" points="%s"/>'
                     % (color, coords))
        if value_labels and s:
            lx, ly = sx(s[-1][0]), sy(s[-1][1])
            parts.append(ax.value_label(lx, ly - 3, '%.2f' % s[-1][1]))
    if series_labels:
        parts.append(ax.legend(series_labels, pl, 8))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))


def bar_chart(bars, *, width: int = 720, height: int = 240, pad: int = 10,
              value_labels: bool = False) -> str:
    if not bars:
        return _placeholder(width, height)
    pl, pr, pt, pb = _frame(width, height)
    vmax = max(abs(v) for _, v in bars) or 1.0
    iw, ih = pr - pl, pb - pt
    n = len(bars)
    bw = iw / n * 0.7
    gap = iw / n

    def sy(v): return pt + (1 - v / vmax) * ih

    parts = [ax.y_axis(ax.nice_ticks(0.0, vmax), sy, pl, pr)]
    centers = []
    for i, (label, v) in enumerate(bars):
        h = abs(v) / vmax * ih
        x = pl + i * gap + (gap - bw) / 2
        y = pt + (ih - h)
        centers.append(x + bw / 2)
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#4caf50"/>'
                     % (x, y, bw, h))
        if value_labels:
            parts.append(ax.value_label(x + bw / 2, y - 2, '%g' % v))
    parts.append(ax.x_cat_axis([lab for lab, _ in bars], centers, pb))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))


def stacked_bar(groups, *, width: int = 720, height: int = 240, pad: int = 10,
                seg_labels=None) -> str:
    if not groups:
        return _placeholder(width, height)
    pl, pr, pt, pb = _frame(width, height)
    totals = [sum(abs(v) for _, v in segs) for _, segs in groups]
    vmax = max(totals) or 1.0
    iw, ih = pr - pl, pb - pt
    n = len(groups)
    bw = iw / n * 0.7
    gap = iw / n
    colors = [c for c, _ in seg_labels] if seg_labels else ['#4caf50', '#e53935', '#6cf', '#fb0']

    def sy(v): return pt + (1 - v / vmax) * ih

    parts = [ax.y_axis(ax.nice_ticks(0.0, vmax), sy, pl, pr)]
    centers = []
    for i, (label, segs) in enumerate(groups):
        x = pl + i * gap + (gap - bw) / 2
        centers.append(x + bw / 2)
        y_bottom = pt + ih
        for j, (_seg, v) in enumerate(segs):
            h = abs(v) / vmax * ih
            y_bottom -= h
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                         % (x, y_bottom, bw, h, colors[j % len(colors)]))
    parts.append(ax.x_cat_axis([lab for lab, _ in groups], centers, pb))
    if seg_labels:
        parts.append(ax.legend(seg_labels, pl, 8))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_charts.py -v`
Expected: PASS（既有 + 新增；既有 5 测可能断言旧坐标——若 `test_line_chart_maps_points`/`test_bar_chart_rects` 断言具体坐标失败，按新绘图区坐标更新这些断言值，并在 commit message 注明。先跑确认哪些需更新。）

> 实现注意：既有 `test_charts.py` 有断言具体坐标的测试（如 `'10.0,90.0'`、`height="80.0"`）。新绘图区边距改变了坐标，这些断言需按新公式更新为实际值（运行测试看 AssertionError 的 actual 值填入）。这是 chrome 引入边距的必然结果，非缺陷。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/charts.py tests/dashboard/test_charts.py
git commit -m "feat(charts): line/bar/stacked 加 Y刻度/X类目时间/图例/数值标注"
```

---

### Task 3: /analytics 路由传图表元数据

**Files:**
- Modify: `gridtrade/dashboard/app.py`（`/analytics` 路由的 5 处图表调用）
- Test: `tests/dashboard/test_app_analytics.py`（追加断言）

**Interfaces:**
- Consumes: `charts.line_chart/bar_chart/stacked_bar`（Task 2 的新参）。
- Produces: 渲染的 /analytics 页含图例与轴文本。

- [ ] **Step 1: Write the failing test**（追加到 `tests/dashboard/test_app_analytics.py`）

```python
# --- 追加到 tests/dashboard/test_app_analytics.py ---
def test_analytics_charts_have_legend(store):
    RecordRepository(store).add(Record(id='r9', exchange='x', symbol='BTC', tag='gt0',
                                       total_pnl=5.0, exit_reason='take_profit', closed_at=1000))
    r = _client(store).get('/analytics')
    assert r.status_code == 200
    assert '已实现' in r.text and '真权益' in r.text     # 权益图图例
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_analytics.py::test_analytics_charts_have_legend -v`
Expected: FAIL（图例文本未出现）

- [ ] **Step 3: Write minimal implementation**

`app.py` `/analytics` 路由内，把 5 处图表调用改为带元数据（保持原变量名）：

```python
            'equity_svg': ch.line_chart([realized, equity], x_is_time=True,
                                        series_labels=[('#6cf', '已实现'), ('#fb0', '真权益')],
                                        value_labels=True),
            'tags': an.tag_attribution(store, start_ms=start_ms),
            'by_hour_svg': ch.bar_chart([(str(h), n) for h, n in dist.by_hour], value_labels=True),
            'by_side_svg': (ch.stacked_bar([('成交', dist.by_side)],
                                           seg_labels=[('#4caf50', '买'), ('#e53935', '卖')])
                            if dist.by_side else ch.bar_chart([])),
            'by_line_svg': ch.bar_chart([(str(li), n) for li, n in dist.by_line], value_labels=True),
            'fee_cum_svg': ch.line_chart([dist.fee_cum], x_is_time=True,
                                         series_labels=[('#6cf', '累计手续费')], value_labels=True),
```

（仅改这 5 个 `ch.*` 调用的参数；路由其余逻辑不动。）

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_analytics.py -v`
然后全 dashboard 套件：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q`
Expected: PASS（新测过；既有 dashboard 不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/app.py tests/dashboard/test_app_analytics.py
git commit -m "feat(charts): /analytics 图表传图例/时间轴/数值标注元数据"
```

---

### Task 4: gridchart.render 内联加 chrome

**Files:**
- Modify: `gridtrade/dashboard/gridchart.py`（`render`）
- Test: `tests/dashboard/test_gridchart_render.py`（追加断言）

**Interfaces:**
- Consumes: `svgaxes`（`nice_ticks`, `y_axis`, `x_time_axis`, `legend`）。
- Produces: `render` 输出含 Y 价格刻度数值 + X 时间刻度 HH:MM + 固定图例；几何层/降级不变。

- [ ] **Step 1: Write the failing test**（追加到 `tests/dashboard/test_gridchart_render.py`）

```python
# --- 追加到 tests/dashboard/test_gridchart_render.py ---
def test_render_has_time_axis_and_legend():
    svg = render(_dto(start_ms=0, end_ms=3600_000))   # _dto 见本文件既有 helper
    assert '00:00' in svg                              # X 时间刻度
    assert '买单' in svg and '卖单' in svg              # 固定图例
    assert '<polyline' in svg                          # 几何仍在
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_render.py::test_render_has_time_axis_and_legend -v`
Expected: FAIL（无 HH:MM/图例）

- [ ] **Step 3: Write minimal implementation**

`gridchart.py` 顶部加 `from gridtrade.dashboard import svgaxes as ax`。把 `render` 的绘图区改为留边距并加 chrome。将 `render` 内 `iw, ih = ...` 与 `sx/sy` 段及收尾改为：

```python
    _L, _R, _T, _B = 40, 12, 18, 16
    pl, pr, pt, pb = _L, width - _R, _T, height - _B

    def sx(t): return pl + (t - xmin) / dx * (pr - pl)
    def sy(p): return pt + (ymax - p) / dy * (pb - pt)
```

（即把原来基于 `pad`/`iw`/`ih` 的 `sx/sy` 换成基于绘图区 `pl/pr/pt/pb`；原 `iw, ih = width-2*pad, height-2*pad` 删除。其余几何绘制里出现的 `pad`、`width - pad` 改为 `pl`、`pr`。）

在 `parts = []` 之后、画几何之前，先放轴：

```python
    parts = []
    parts.append(ax.y_axis(ax.nice_ticks(ymin, ymax), sy, pl, pr))
    parts.append(ax.x_time_axis(xmin, xmax, sx, pb))
```

在 `return` 之前追加图例：

```python
    parts.append(ax.legend([('#6cf', '走势'), ('#4caf50', '买单'), ('#e53935', '卖单'),
                            ('#fb0', '成交/现价'), ('#999', '入场'), ('#e53935', '止损')], pl, 8))
```

降级文案 `行情暂不可用` 的 y 仍放在绘图区内（`pt + 12`）。所有原先 `pad` / `width - pad` 在挂点线/入场止损/当前价绘制里替换为 `pl` / `pr`。

> 实现注意：`render` 既有测试 `test_render_full_chart` 断言 `<line` 计数 `>= 2+1+2`、`<circle' >= 1` 等用 `>=`，加 chrome（y_axis 多 `<line>`）只会增加计数，不破 `>=` 断言。逐条确认既有 render 测试仍过；个别用 `==` 的计数断言（若有）按实际更新。

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_gridchart_render.py -v`
然后 `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q`
Expected: PASS（新测过；既有 render/dashboard 不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/gridchart.py tests/dashboard/test_gridchart_render.py
git commit -m "feat(charts): gridchart.render 加价格刻度/时间刻度/图例"
```

---

### Task 5: 文档同步

**Files:**
- Modify: `docs/STATUS.md`（§5 web 行补「图表带刻度/类目/图例/数值标注」）

**Interfaces:** 无代码。

- [ ] **Step 1: 更新 STATUS.md §5 web 行**

在 dashboard 图表相关描述补一句：

```markdown
  所有服务端 SVG 图表（/analytics 与实时网格图）均带 Y 轴刻度 + X 轴时间(HH:MM)/类目标签 + 图例 + 数值标注（svgaxes 共享纯函数；文本仅数值/时间/固定词 + svg_escape 兜底，守 |safe 边界）。
```

- [ ] **Step 2: Commit**

```bash
git add docs/STATUS.md
git commit -m "docs(charts): STATUS 同步图表刻度/图例"
```

---

## Self-Review

**Spec 覆盖（核对 2026-06-30-chart-axes-legend-design.md）：**
- §1 四样（Y刻度/X刻度类目/图例/数值标注）→ T1(helpers) + T2(charts) + T4(gridchart)。✅
- §2 安全（数值+时间+固定词 + svg_escape 兜底）→ T1 svg_escape + 各 helper 用它；测试断言转义（T1 test_x_cat_axis_escapes）。✅
- §3 svgaxes 共享层 → T1。✅
- §4 charts.py 改造（新参/绘图区/不丢 label）→ T2。✅
- §5 /analytics 接线 → T3。✅
- §6 gridchart 改造 → T4。✅
- §7 测试（svgaxes/charts/gridchart/端点/安全）→ 各任务 TDD + T1 转义测试 + T3 端点图例。✅
- §8 风险（窄屏横滚已有、刻度数、nice_ticks 等分）→ 实现注意/保留。✅

**Placeholder 扫描：** 无 TBD/TODO；每 code step 给完整代码。T2/T4 明确告知既有坐标断言需按新绘图区更新（带「跑测看 actual 值填入」的具体指引，非占位）。✅

**类型一致：** `svg_escape/nice_ticks/y_axis/x_time_axis/x_cat_axis/legend/value_label`(T1) 在 T2/T4 一致调用；`line_chart(...,x_is_time,series_labels,value_labels)`/`bar_chart(...,value_labels)`/`stacked_bar(...,seg_labels)`(T2) 在 T3 /analytics 一致传参；`series_labels`/`seg_labels` 均为 `[(color,name)]` 形状，一致。✅
