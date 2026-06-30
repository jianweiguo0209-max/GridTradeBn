# 图表加刻度 / 类目标签 / 图例 / 数值标注 — 设计文档

> 日期：2026-06-30
> 状态：设计已确认，待转 writing-plans
> 背景：P3 `/analytics` 的 charts.py（line/bar/stacked）与 P1 实时图 `gridchart.render` 当前只画几何（折线/条形/点），无任何轴刻度、类目标签、图例、数值标注。根因：当初为守住「SVG 内无 DB/用户文本插值」的 |safe 安全线，最省事地不画任何文本，连传进 bar_chart/stacked_bar 的 label 都丢弃（`_label`/`_seg`）。
> 相关记忆：`dashboard-project`

## 1. 目标

给**所有图表**补上：① Y 轴数值刻度；② X 轴刻度（时间图 HH:MM）/类目标签（条形图小时·line_index·买卖）；③ 图例 legend；④ 数值标注（折线末端值、条形顶值）。涉及 `charts.py`（line_chart/bar_chart/stacked_bar）与 `gridchart.render`。tag 归因、退出原因在 /analytics 里是**表格不是图**，不涉及。

## 2. 安全约束（守住既有 |safe 边界）

SVG 由服务端拼接、以 `| safe` 渲染。新增文本来源**只允许**：数值（`%.1f` / `%d`）、时间（`HH:MM`，从 ts 数值算）、固定字面量（图例词、买/卖）。这些图的类目本就是数值或固定词（小时桶/line_index 整数、买卖固定词），无任意 DB 文本。**另加 `svg_escape(s)`**（转义 `< > & " '`）对任何字符串标签兜底防御——即便将来类目带特殊字符也不破。终审仍以「SVG 无未转义 DB/用户文本」为安全线。

## 3. 架构

新增 `gridtrade/dashboard/svgaxes.py`——**纯函数共享层**（charts.py 与 gridchart 共用，DRY、可单测）：
- `svg_escape(s: str) -> str`：转义 `& < > " '`。
- `nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]`：在 [lo,hi] 等分约 n 个刻度值（含端点；lo==hi 退化为单值）。
- `y_axis(ticks, sy, x_left, x_right, *, fmt) -> str`：每个 tick 一条淡水平网格线（x_left→x_right）+ 左侧数值标签。
- `x_time_axis(xmin, xmax, sx, y_base) -> str`：起/中/现 3 个 `HH:MM`（UTC）刻度标签 + 短竖线。
- `x_cat_axis(labels, centers, y_base) -> str`：每个类目在其条形中心下方一个标签（labels 经 svg_escape）。
- `legend(items: List[Tuple[str,str]], x, y) -> str`：一行 `(color, text)` 色块+文字（text 经 svg_escape）。
- `value_label(x, y, text) -> str`：在 (x,y) 标一个数值文本（右/上对齐）。

`charts.py` 三函数与 `gridchart.render` 调用这些 helper 绘制 chrome；几何绘制逻辑不变。

## 4. charts.py 改造（新增可选参，向后兼容）

- `line_chart(series, *, width=720, height=240, pad=10, x_is_time=False, series_labels=None, value_labels=False)`：
  - 画布加大下/左/上留白（轴/图例空间）；多序列每条用不同色（取色板）。
  - Y 轴：`nice_ticks(ymin,ymax)` → `y_axis`（数值标签）。
  - X 轴：`x_is_time` → `x_time_axis`（HH:MM）；否则数值刻度（起/中/末）。
  - 图例：`series_labels` 给定时 → `legend`（每序列色+名）。
  - 数值标注：`value_labels` → 每条折线末点标当前值。
- `bar_chart(bars, *, width=720, height=240, pad=10, value_labels=False)`：
  - **不再丢弃 label**：`x_cat_axis` 把每根的 label 画在 x 轴下。
  - Y 轴：`nice_ticks(0, vmax)` → `y_axis`。
  - 数值标注：`value_labels` → 每根顶标值。
- `stacked_bar(groups, *, width=720, height=240, pad=10, seg_labels=None)`：
  - X 轴：组 label（`x_cat_axis`）。Y 轴：`nice_ticks(0, vmax)`。
  - 图例：`seg_labels`（如 `['买','卖']`）→ `legend`（段色+名）。

空数据仍返回占位 SVG（不变）。

## 5. /analytics 路由接线（app.py）

把图表调用传上元数据：
- `equity_svg = line_chart([realized, equity], x_is_time=True, series_labels=[('#6cf','已实现'),('#fb0','真权益')], value_labels=True)`（两序列分色）。
- `fee_cum_svg = line_chart([dist.fee_cum], x_is_time=True, series_labels=[('#6cf','累计手续费')], value_labels=True)`。
- `by_hour_svg = bar_chart([(str(h), n) for ...], value_labels=True)`（x 轴显示小时桶）。
- `by_side_svg = stacked_bar([('成交', dist.by_side)], seg_labels=[('#4caf50','买'),('#e53935','卖')])`。
- `by_line_svg = bar_chart([(str(li), n) for ...], value_labels=True)`（x 轴显示 line_index）。

## 6. gridchart.render 改造（内联加 chrome）

`render` 内联调 svgaxes：
- 把 `pad` 调大（左留价格刻度、下留时间刻度、上留图例）。
- **Y 价格刻度**：`nice_ticks(ymin,ymax)` → `y_axis`（数值）。
- **X 时间刻度**：`x_time_axis(start_ms,end_ms,sx,y_base)`（HH:MM）。
- **图例**（固定词）：`legend([('#6cf','走势'),('#4caf50','买单'),('#e53935','卖单'),('#999','入场'),('#e53935','止损'),('#fb0','现价')], ...)`。
- 几何层（挂点线/入场止损/折线/成交点/现价）逻辑不变；降级/占位不变。
- 成交点不逐点标值（太密）；图例说明颜色即可。

## 7. 测试（双后端无关，纯函数为主）

- `svgaxes.py`：`svg_escape`（`<script>`→转义）；`nice_ticks`（已知区间分档、lo==hi 退化）；`y_axis`/`x_time_axis`/`x_cat_axis`/`legend`/`value_label` 对已知输入断言含预期 `<text>`/`<line>`/数值/HH:MM。
- `charts.py`：line/bar/stacked 加参后输出含 Y 刻度数值、X 标签/时间、图例文字、(value_labels 时)数值标注；空数据仍占位；几何（polyline/rect）仍在。
- `gridchart.render`：输出含 HH:MM 时间刻度 + 价格刻度数值 + 固定图例词（走势/买单/…），几何层仍在；降级路径仍「行情暂不可用」。
- `/analytics` 与 `/grid/{id}/chart` 端点 TestClient：页面/片段含图例文字（已实现/真权益/买/卖）与轴标签；既有 dashboard 测试不回归。
- 安全：断言任何字符串标签经 `svg_escape`（构造一个含 `<` 的类目，断言输出已转义、无裸 `<script`）。

## 8. 风险与开放项

- 画布增高/留白后，移动端窄屏 `@media` 下 SVG 仍 `overflow-x:auto`（P2 已加），无需额外改。
- 刻度数量默认 4，点/类目极多时 X 标签可能拥挤 → 时间图固定 3 档、类目图过多时可后续抽样（本期不做）。
- `nice_ticks` 用等分（非「整数美化」），首版够用；要 1/2/5 美化档可后续。
