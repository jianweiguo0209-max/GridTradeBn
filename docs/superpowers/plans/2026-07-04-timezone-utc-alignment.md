# 时区统一（内部 UTC + 显示可配）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把系统所有时间语义统一——内部处理/存储/策略对齐全用 UTC（无机器 TZ 依赖、铲平 `utc_offset`），外部显示时区由 `DISPLAY_TZ`（IANA，默认 UTC）配置。

**Architecture:** 分三层——存储/计算（UTC epoch）、策略对齐（换仓 offset + 因子截断纯 UTC）、显示（`DISPLAY_TZ` 仅面板层）。策略侧删除 `utc_offset` 全链路 + `proceed_calc_symbol_factor` 的 `tm_gmtoff`，金标在 `TZ=UTC` 下重生成；显示侧新增 `to_display_dt` helper 并透传到 `ms_to_human`/SVG 时间轴。

**Tech Stack:** Python 3.9（stdlib `zoneinfo`）、pandas 1.3.5、numpy 1.22.4、TA-Lib 0.6.8、pytest、FastAPI/Jinja2、fly.io。

## Global Constraints

- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`；**本改动额外要求同一套测试在 `TZ=UTC` 下也全绿**（证明摆脱机器 TZ 依赖）。
- Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / TA-Lib 0.6.8 / ccxt 4.5.61 / SQLAlchemy 2.0（勿升级）。
- `gridtrade/core/` 不得 import 任何交易所库。
- **不改因子数学、网格参数、选币因子集与阈值**——只改时区对齐。
- 在分支 `tz-utc-alignment` 上工作；**绝不 push `production`**。
- 策略路径不得再出现 `time.localtime()` / `tm_gmtoff`。
- 金标比对是 parity 铁律：重生成后须核对**仅 `time` 列变化**、symbols/rank/factor 值不变。

---

### Task 1: 铲平 `utc_offset`（`compute_offset` 纯 UTC + 全链路移除）

**Files:**
- Modify: `gridtrade/core/selection.py:154-157`
- Modify: `gridtrade/execution/triggers.py:65-79`
- Modify: `gridtrade/runtime/scheduler.py:56`
- Modify: `gridtrade/runtime/factory.py:82-83`
- Modify: `gridtrade/config.py:56,97`
- Modify: `gridtrade/backtest/selection_replay.py:30-49`
- Modify: `gridtrade/backtest/backtest_run.py:33-37,95-96,111,122,156-158,165-166,275`
- Modify: `deploy/fly.toml`（删 `UTC_OFFSET` 行）、`deploy/fly.prod.toml`（删 `UTC_OFFSET` 行）
- Test: `tests/core/test_selection_parity.py:40-45`、`tests/execution/test_triggers.py:74-93`、`tests/backtest/test_selection_replay.py:52`、`tests/test_config.py:30,48,58`

**Interfaces:**
- Produces: `compute_offset(run_time, period) -> int`（去掉第三参数 `utc_offset`）；`ScheduledSelectionTrigger.__init__(strategy_config, factors, weight_list, *, select_fn=None, source=...)`（去掉 `utc_offset`）；`replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *, timeframe='1h', log=print)`（去掉位置参 `utc_offset`）；`holding_bars(series_df, run_time, period)`、`build_grid_tasks(...)`、`run_backtest(...)` 去掉 `utc_offset`；`DeployConfig` 去掉 `utc_offset` 字段。

- [ ] **Step 1: 改写 compute_offset 金标测试为新签名（先 RED）**

`tests/core/test_selection_parity.py` 末尾的 `test_compute_offset_matches_legacy_formula` 整体替换为：

```python
def test_compute_offset_is_pure_utc():
    from gridtrade.core.selection import compute_offset
    run_time = pd.Timestamp('2024-01-09 05:00:00')
    # 纯 UTC：utc_run_time == run_time（不再 −8）
    expected = int(((run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % 12)
    assert compute_offset(run_time, '12H') == expected
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_selection_parity.py::test_compute_offset_is_pure_utc -q`
Expected: FAIL（`compute_offset()` 仍要求 3 个参数 → TypeError）

- [ ] **Step 3: `compute_offset` 去掉 `utc_offset`**

`gridtrade/core/selection.py` 152-157 行替换为：

```python
def compute_offset(run_time, period):
    """换仓 offset 相位（纯 UTC）。run_time 恒为 UTC 墙钟（由 epoch 构造）。"""
    return int(((run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % int(period[:-1]))
```

- [ ] **Step 4: 触发器去掉 `utc_offset`**

`gridtrade/execution/triggers.py` 中 `ScheduledSelectionTrigger.__init__` 与 `propose` 改为：

```python
    def __init__(self, strategy_config, factors, weight_list, *,
                 select_fn=None, source='ScheduledSelectionTrigger'):
        self.strategy_config = strategy_config
        self.factors = factors
        self.weight_list = weight_list
        self.source = source
        self.select_fn = select_fn or _default_select_fn(
            strategy_config, factors, weight_list)
```

`propose` 内 79 行 `offset = compute_offset(ctx.run_time, period, self.utc_offset)` 改为：

```python
        offset = compute_offset(ctx.run_time, period)
```

- [ ] **Step 5: scheduler / factory 去掉 `utc_offset`**

`gridtrade/runtime/scheduler.py:56`：

```python
    offset = compute_offset(run_time, period)
```

`gridtrade/runtime/factory.py:82-83`：

```python
    trigger = ScheduledSelectionTrigger(sc, sc['factors'], sc['weight_list'])
```

- [ ] **Step 6: config 删除 `utc_offset` 字段与解析**

`gridtrade/config.py`：删掉 `DeployConfig` 里 `utc_offset: int` 字段（56 行），并删掉 `load_deploy_config` 里 `utc_offset=_i(env, 'UTC_OFFSET', 8),`（97 行）。

- [ ] **Step 7: 回测链路去掉 `utc_offset`**

`gridtrade/backtest/selection_replay.py`：函数签名改 `def replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *, timeframe='1h', log=print):`；删掉函数体内对 `utc_offset` 的引用——46 行 `offset = compute_offset(run_time, period)`；49 行 `mask = df['candle_begin_time'] < run_time`。

`gridtrade/backtest/backtest_run.py`：
- `holding_bars`（33-37）：

```python
def holding_bars(series_df, run_time, period):
    td = pd.to_timedelta(period)
    cbt = series_df['candle_begin_time']
    sub = series_df[(cbt >= run_time) & (cbt < run_time + td)]
    return sub.sort_values('candle_begin_time')
```

- `build_grid_tasks`（95-96）去掉 `utc_offset` 形参；111 行调用改 `SR.replay_selection(cache, universe, run_times, strategy_config, factors, lambda rt, off, row: grids.append((rt, off, row.copy())), timeframe=timeframe, log=log)`；122 行改 `bars_df = holding_bars(series[sym], rt, period)`。
- `run_backtest`（156-158）去掉 `utc_offset` 形参；165-166 行调用 `build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors, timeframe=timeframe, sim_timeframe=sim_timeframe, log=log)`。
- `main`（275）调用改 `df = run_backtest(cache, HL_UNIVERSE, win_start, win_end, HL_STRATEGY, HL_FACTORS, timeframe='1h', sim_timeframe=(None if sim_tf == '1h' else sim_tf), workers=workers)`。

- [ ] **Step 8: 删除部署 env `UTC_OFFSET`**

`deploy/fly.toml` 删除 `  UTC_OFFSET = "8"` 行；`deploy/fly.prod.toml` 删除 `  UTC_OFFSET = "8"` 行。

- [ ] **Step 9: 更新受影响的调用方测试**

`tests/execution/test_triggers.py`：
- 74-76 行 `ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True, 'Sgcz_2': True}, [1, 1], utc_offset=8, select_fn=lambda scd, rt, off: rows)` → 删 `utc_offset=8,`。
- 90-93 行断言块改为：

```python
        from gridtrade.core.selection import compute_offset
        off = compute_offset(run_time, '12H')
        assert out[0].offset == off and out[0].tag == 'acc1at%d' % off
```

`tests/backtest/test_selection_replay.py:52`：`replay_selection(cache, syms, run_times, STRAT, FACTORS, 8, lambda rt, off, row: picks.append((rt, off, row['symbol'])), timeframe='1h')` → 删掉位置参 `8,`（`... FACTORS, lambda rt, off, row: ...`）。

`tests/test_config.py`：删 30 行 `assert cfg.utc_offset == 8`；删 48 行 `'UTC_OFFSET': '0',`；58 行 `assert cfg.default_cap == 200.0 and cfg.utc_offset == 0` → `assert cfg.default_cap == 200.0`。

- [ ] **Step 10: 全量测试（两个 TZ 均绿）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q` 然后 `TZ=UTC .venv/bin/python -m pytest -q`
Expected: 两次都 PASS（注意：本 Task 未改 `proceed_calc_symbol_factor`，`test_selection_matches_golden` 仍靠机器 TZ，故此步须在 `TZ=Asia/Shanghai` 下先跑；`TZ=UTC` 下 `test_selection_matches_golden` 会因旧 +8 金标而 FAIL——**允许该单测在此 Task 于 UTC 下暂红，Task 2 修复**。除它以外其余全绿。）

> 说明：为避免 Step 10 的 UTC 暂红干扰，运行 `TZ=UTC` 时排除该测试：
> `TZ=UTC .venv/bin/python -m pytest -q --deselect tests/core/test_selection_parity.py::test_selection_matches_golden`

- [ ] **Step 11: 提交**

```bash
git add gridtrade/core/selection.py gridtrade/execution/triggers.py \
        gridtrade/runtime/scheduler.py gridtrade/runtime/factory.py \
        gridtrade/config.py gridtrade/backtest/selection_replay.py \
        gridtrade/backtest/backtest_run.py deploy/fly.toml deploy/fly.prod.toml \
        tests/core/test_selection_parity.py tests/execution/test_triggers.py \
        tests/backtest/test_selection_replay.py tests/test_config.py
git commit -m "refactor(tz): 铲平 utc_offset，换仓 offset 相位改纯 UTC"
```

---

### Task 2: 因子截断去 `tm_gmtoff` + 金标重基线

**Files:**
- Modify: `gridtrade/core/selection.py:67-72`
- Regenerate: `tests/golden/cross_select_golden.parquet`
- Test: `tests/core/test_selection_tz_independence.py`（新建）、`tests/core/test_selection_parity.py`（`_run_new` 沿用，验证匹配新金标）

**Interfaces:**
- Consumes: `compute_offset(run_time, period)`（Task 1）。
- Produces: `proceed_calc_symbol_factor` 的 `time` 列 = 纯 UTC（不随机器 TZ 平移）。

- [ ] **Step 1: 写机器 TZ 独立性测试（先 RED）**

新建 `tests/core/test_selection_tz_independence.py`：

```python
import os
import time

import pandas as pd

from tests.golden.gen_golden import make_symbol_df


def _select():
    from gridtrade.core.selection import (proceed_calc_symbol_factor,
                                          select_grid_coin)
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    all_df = proceed_calc_symbol_factor(scd, run_time, '12H', 0)
    sel = select_grid_coin(all_df.copy(),
                           {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True},
                           [1, 1, 1], 2, run_time)
    return sel.sort_values(['time', 'symbol']).reset_index(drop=True)


def _run_under(tz):
    old = os.environ.get('TZ')
    os.environ['TZ'] = tz
    time.tzset()
    try:
        return _select()
    finally:
        if old is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old
        time.tzset()


def test_selection_independent_of_machine_tz():
    a = _run_under('UTC')
    b = _run_under('Asia/Shanghai')
    pd.testing.assert_frame_equal(a, b)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/core/test_selection_tz_independence.py -q`
Expected: FAIL（`time` 列在两 TZ 下相差 8h，`assert_frame_equal` 报 time 列不等）

- [ ] **Step 3: 去掉 `tm_gmtoff` 平移**

`gridtrade/core/selection.py` 67-72 行（`# 兼容时区` 起到删 runtime 行止）替换为：

```python
    # 时间恒 UTC：candle_begin_time 由 epoch 构造(tz-naive UTC)，不做机器 TZ 平移
    all_data_df['time'] = pd.to_datetime(all_data_df['time'], unit='ms')
    # 删除runtime那行的数据，如果有的话
    all_data_df = all_data_df[all_data_df['time'] < run_time]
```

同时删除文件顶部不再使用的 `import time`（确认 `selection.py` 无其它 `time.` 引用后再删；若 Task 内仍有引用则保留）。

- [ ] **Step 4: 运行确认 TZ 独立性通过**

Run: `.venv/bin/python -m pytest tests/core/test_selection_tz_independence.py -q`
Expected: PASS

- [ ] **Step 5: 确认旧金标现在失配（预期）**

Run: `.venv/bin/python -m pytest tests/core/test_selection_parity.py::test_selection_matches_golden -q`
Expected: FAIL（新 UTC 输出 vs 旧 +8 金标 `time` 列不符）

- [ ] **Step 6: 在 `TZ=UTC` 下从 legacy 重生成金标**

```bash
mkdir -p legacy/data
TZ=UTC .venv/bin/python tests/golden/gen_golden.py
# 只保留 cross_select 的重基线，丢弃 factors/grid 金标的无意义重写
git checkout -- tests/golden/factors_golden.parquet tests/golden/grid_params_golden.json
rm -rf legacy/data
```

Expected: 打印 `golden fixtures written to ...`；`git status` 仅 `tests/golden/cross_select_golden.parquet` 有改动。

- [ ] **Step 7: 核对重基线只动了 `time` 列（parity 铁律）**

用一次性脚本比对旧（git HEAD 版本）与新 parquet：

```bash
.venv/bin/python - <<'PY'
import subprocess, io, pandas as pd
new = pd.read_parquet('tests/golden/cross_select_golden.parquet')
old = pd.read_parquet(io.BytesIO(
    subprocess.check_output(['git', 'show', 'HEAD:tests/golden/cross_select_golden.parquet'])))
cols = [c for c in old.columns if c != 'time']
assert list(new['symbol']) == list(old['symbol']), 'symbols changed!'
pd.testing.assert_frame_equal(new[cols].reset_index(drop=True),
                              old[cols].reset_index(drop=True),
                              check_exact=False, rtol=1e-9)
shift = (pd.to_datetime(old['time']).reset_index(drop=True)
         - pd.to_datetime(new['time']).reset_index(drop=True))
print('non-time cols identical ✓ ; time shift(uniques):', shift.unique())
PY
```

Expected: 打印 `non-time cols identical ✓`，且 time shift 全为 `8 hours`（旧比新晚 8h）。若非 → 停下排查，说明重基线改变了选币、违反 parity。

- [ ] **Step 8: 全量测试（两个 TZ 均绿）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q` 然后 `TZ=UTC .venv/bin/python -m pytest -q`
Expected: 两次都全 PASS（含 `test_selection_matches_golden` 与新 TZ 独立性测试）。

- [ ] **Step 9: 提交**

```bash
git add gridtrade/core/selection.py tests/core/test_selection_tz_independence.py \
        tests/golden/cross_select_golden.parquet
git commit -m "refactor(tz): 因子截断去 tm_gmtoff（纯 UTC）+ 金标 UTC 重基线"
```

---

### Task 3: `DISPLAY_TZ` 显示时区（config + helper + 面板透传）

**Files:**
- Modify: `gridtrade/config.py`（加 `display_tz`）
- Modify: `gridtrade/dashboard/formatting.py`（`to_display_dt` + `ms_to_human` tz 参）
- Modify: `gridtrade/dashboard/svgaxes.py:43-53`（`_hhmm`/`x_time_axis` tz 参）
- Modify: `gridtrade/dashboard/charts.py:22-39`（`line_chart` tz 参转发）
- Modify: `gridtrade/dashboard/gridchart.py:117,137`（`render` tz 参转发）
- Modify: `gridtrade/dashboard/app.py:21-38,97,208,217`（`create_app` 收 `display_tz` + 绑定/透传）
- Modify: `gridtrade/runtime/web.py:16`（透传 `config.display_tz`）
- Modify: `deploy/Dockerfile`（装 `tzdata`）、`requirements.txt`（加 `tzdata`）
- Test: `tests/dashboard/test_formatting.py`（追加 `to_display_dt` 用例）、`tests/test_config.py`（追加 `DISPLAY_TZ` 解析）

**Interfaces:**
- Produces: `to_display_dt(ts_ms, tz_name='UTC') -> datetime`；`ms_to_human(ts, tz_name='UTC')`；`svgaxes._hhmm(ms, tz_name='UTC')`、`x_time_axis(xmin, xmax, sx, y_base, tz_name='UTC')`；`charts.line_chart(..., tz_name='UTC')`；`gridchart.render(dto, *, width=720, height=320, tz_name='UTC')`；`create_app(..., display_tz='UTC')`；`DeployConfig.display_tz: str`。

- [ ] **Step 1: 写 `to_display_dt` 测试（先 RED）**

`tests/dashboard/test_formatting.py` 追加：

```python
def test_to_display_dt_utc_default_and_iana_and_fallback():
    from gridtrade.dashboard.formatting import to_display_dt
    ts = 1704067200000  # 2024-01-01 00:00:00 UTC
    assert to_display_dt(ts).strftime('%Y-%m-%d %H:%M') == '2024-01-01 00:00'
    assert to_display_dt(ts, 'Asia/Shanghai').strftime('%Y-%m-%d %H:%M') == '2024-01-01 08:00'
    # 非法时区回退 UTC、不抛
    assert to_display_dt(ts, 'Nowhere/Nope').strftime('%Y-%m-%d %H:%M') == '2024-01-01 00:00'


def test_ms_to_human_respects_tz():
    from gridtrade.dashboard.formatting import ms_to_human
    ts = 1704067200000
    assert ms_to_human(ts) == '2024-01-01 00:00:00'
    assert ms_to_human(ts, 'Asia/Shanghai') == '2024-01-01 08:00:00'
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/dashboard/test_formatting.py -q`
Expected: FAIL（`to_display_dt` 未定义；`ms_to_human` 不接受第二参）

- [ ] **Step 3: 实现 `to_display_dt` 并让 `ms_to_human` 用它**

`gridtrade/dashboard/formatting.py` 顶部 import 改为：

```python
import math
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
```

在 `ms_to_human` 之前插入 helper，并替换 `ms_to_human`：

```python
def to_display_dt(ts_ms, tz_name: str = 'UTC') -> datetime:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    if not tz_name or tz_name == 'UTC':
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:        # 非法/缺 tzdata → 回退 UTC，绝不崩
        return dt


def ms_to_human(ts: Optional[int], tz_name: str = 'UTC') -> str:
    if ts is None:
        return '-'
    return to_display_dt(ts, tz_name).strftime('%Y-%m-%d %H:%M:%S')
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/dashboard/test_formatting.py -q`
Expected: PASS

- [ ] **Step 5: svgaxes 时间轴接 tz**

`gridtrade/dashboard/svgaxes.py` 顶部加 `from gridtrade.dashboard.formatting import to_display_dt`；`_hhmm` 与 `x_time_axis`（43-53 行）替换为：

```python
def _hhmm(ms, tz_name: str = 'UTC') -> str:
    return to_display_dt(int(ms), tz_name).strftime('%H:%M')


def x_time_axis(xmin, xmax, sx, y_base, tz_name: str = 'UTC') -> str:
    mid = (int(xmin) + int(xmax)) // 2
    out = []
    for t in (xmin, mid, xmax):
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" '
                   'fill="#999">%s</text>' % (sx(t), y_base + 10, _hhmm(t, tz_name)))
    return ''.join(out)
```

- [ ] **Step 6: charts / gridchart 转发 tz**

`gridtrade/dashboard/charts.py`：`line_chart` 签名 23 行末加 `tz_name: str = 'UTC'`；39 行 `parts.append(ax.x_time_axis(xmin, xmax, sx, pb, tz_name))`。

`gridtrade/dashboard/gridchart.py`：`render` 签名 117 行改 `def render(dto, *, width: int = 720, height: int = 320, tz_name: str = 'UTC') -> str:`；137 行 `parts.append(ax.x_time_axis(xmin, xmax, sx, pb, tz_name))`。

- [ ] **Step 7: config 加 `display_tz`**

`gridtrade/config.py`：`DeployConfig` 追加字段 `display_tz: str = 'UTC'`（放在 `dashboard_port` 附近的带默认值区）；`load_deploy_config` 追加 `display_tz=_s(env, 'DISPLAY_TZ', 'UTC'),`。

`tests/test_config.py` 追加：

```python
def test_display_tz_defaults_and_parsing():
    assert load_deploy_config(env={}).display_tz == 'UTC'
    assert load_deploy_config(env={'DISPLAY_TZ': 'Asia/Shanghai'}).display_tz == 'Asia/Shanghai'
```

- [ ] **Step 8: app / web 透传 `display_tz`**

`gridtrade/dashboard/app.py`：
- `create_app` 签名（21-25 行）加关键字参 `display_tz: str = 'UTC'`。
- 顶部 import 加 `import functools`。
- 34-38 行 filter 注册：把 `('ms_to_human', fmt.ms_to_human)` 换成 `('ms_to_human', functools.partial(fmt.ms_to_human, tz_name=display_tz))`。
- 97 行：`return HTMLResponse(gc.render(dto, tz_name=display_tz))`。
- 208-210 行 equity_svg 的 `ch.line_chart(...)` 调用末尾加 `tz_name=display_tz`；217-218 行 fee_cum_svg 的 `ch.line_chart(...)` 调用末尾加 `tz_name=display_tz`。

`gridtrade/runtime/web.py:16-19` 的 `create_app(...)` 调用加 `display_tz=config.display_tz,`。

- [ ] **Step 9: Dockerfile / requirements 加 tzdata**

`requirements.txt` 追加一行 `tzdata`（zoneinfo 数据源，slim 镜像必需）。
`deploy/Dockerfile`：确认 `pip install -r requirements.txt` 会带上 `tzdata`（若 Dockerfile 单独列依赖则补 `tzdata`）。

- [ ] **Step 10: 全量测试（两个 TZ）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q` 然后 `TZ=UTC .venv/bin/python -m pytest -q`
Expected: 两次全 PASS（含 charts/gridchart/svgaxes 既有测试仍绿——默认 `tz_name='UTC'` 保证向后兼容）。

- [ ] **Step 11: 提交**

```bash
git add gridtrade/config.py gridtrade/dashboard/formatting.py \
        gridtrade/dashboard/svgaxes.py gridtrade/dashboard/charts.py \
        gridtrade/dashboard/gridchart.py gridtrade/dashboard/app.py \
        gridtrade/runtime/web.py requirements.txt deploy/Dockerfile \
        tests/dashboard/test_formatting.py tests/test_config.py
git commit -m "feat(tz): DISPLAY_TZ 可配显示时区（默认 UTC）+ to_display_dt"
```

---

### Task 4: 文档 & 记忆同步

**Files:**
- Modify: `docs/STATUS.md`（§8 或 §9 加时区统一条目）
- Modify: `docs/superpowers/specs/2026-07-04-timezone-utc-alignment-design.md`（状态改「已实现」）
- Create: `/Users/thomaschang/.claude/projects/-Users-thomaschang-Projects-GridTradeGP/memory/timezone-utc-alignment.md` + `MEMORY.md` 追加一行

- [ ] **Step 1: 更新 STATUS.md**

在 `docs/STATUS.md` §8 gotchas 追加一条：

```markdown
- **时区**：内部全 UTC（无机器 TZ 依赖，已铲平 `utc_offset`/`tm_gmtoff`）；换仓 offset 相位现为纯 UTC（与回测 `utc_offset=0` 同口径）；显示时区由 `DISPLAY_TZ`（IANA，默认 UTC）控制，仅面板层。**注**：本次上线令 live 换仓 12H 边界相位相较旧 +8 平移 8h（有意变更，与回测一致）。
```

- [ ] **Step 2: 更新设计文档状态**

`docs/superpowers/specs/2026-07-04-timezone-utc-alignment-design.md` 顶部 `状态：设计已确认，待写实现计划` 改为 `状态：已实现（testnet 验证中）`。

- [ ] **Step 3: 写记忆并索引**

新建记忆文件 `timezone-utc-alignment.md`（frontmatter type: project），正文记：铲平 utc_offset + tm_gmtoff、换仓相位改纯 UTC、金标 UTC 重基线（仅 time 列变）、DISPLAY_TZ 显示层、live 相位平移 8h 为有意变更、testnet 先行。并在 `MEMORY.md` 追加一行指针。

- [ ] **Step 4: 提交**

```bash
git add docs/STATUS.md docs/superpowers/specs/2026-07-04-timezone-utc-alignment-design.md
git commit -m "docs(tz): STATUS/spec 同步时区统一"
```

---

### Task 5: testnet 部署验证（ops，非代码）

**前置：** Task 1-4 已合入、`TZ=UTC` 与 `TZ=Asia/Shanghai` 全测绿。

- [ ] **Step 1: 合并分支到 main 并触发 testnet CD**

```bash
git checkout main && git merge --no-ff tz-utc-alignment
git push origin main            # CI 每 push 跑
gh workflow run deploy.yml      # 手动 CD 到 gridtrade-hl（testnet）
```

- [ ] **Step 2: 核验部署健康**

Run: `bash scripts/testnet_status.sh`（fly 机器状态 + 心跳 + 活跃网格 + 余额）
Expected: monitor/scheduler/web 三进程 healthy、testnet=True、无报错。

- [ ] **Step 3: 观察首个换仓周期（关键）**

Run: `fly logs -a gridtrade-hl`
观察点：
- scheduler 整点跑出的 `tag`（`gt0{offset}`）offset 值反映**纯 UTC 相位**（不再是旧 +8）；
- 旧 tag 网格关闭 / 新 tag 网格开出的衔接无异常、无反复 churn；
- 无 `[gate] rejected` 异常、无 `degraded`。

- [ ] **Step 4: 核验面板显示时区（可选）**

在 testnet 设 `DISPLAY_TZ=Asia/Shanghai`（`fly secrets set` 或 env）重启 web，确认面板时间戳与图表 X 轴显示为北京时间、非 UTC；设回 `UTC` 恢复。

- [ ] **Step 5: 确认后再上 mainnet**

testnet 一个完整换仓周期无异常后，按 `deploy/DEPLOY.md`：`main` → merge 进 `production` → `git push origin production`（= 真钱部署，谨慎）。

---

## 自查（写完计划后对照 spec）

- **Spec 覆盖**：①策略→UTC=Task1+2；②显示可配=Task3；③金标重基线=Task2 Step6-7；④测试(机器TZ独立/to_display_dt/双TZ全测)=Task2 Step1、Task3 Step1、各 Step10；⑤testnet 先行=Task5；文档=Task4。回测缓存"不受影响"已由 Task1 保持（无缓存改动）。✅ 无遗漏。
- **占位符扫描**：无 TBD/TODO；每个改码步骤含完整代码。✅
- **类型一致**：`compute_offset(run_time, period)`、`ScheduledSelectionTrigger(...)` 去 utc_offset、`replay_selection(... , on_select, *, ...)`、`to_display_dt(ts_ms, tz_name='UTC')`、`ms_to_human(ts, tz_name='UTC')`、`x_time_axis(..., tz_name='UTC')`、`render(dto, *, ..., tz_name='UTC')`、`create_app(..., display_tz='UTC')` —— 定义与调用处签名一致。✅
