# eff1 选币 + tickSize 过滤上实盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把回测已验证的 eff1 选币器(p12_eff = cross1/(1+100·mae) 全池 top-1)与 tickSize 票池过滤(MIN_TICKS=3)移植到实盘,与回测 stage_L / tick_filter **逐位同源**。

**Architecture:** 三块新件:①`gridtrade/core/p12_labels.py` 纯标签数学(与 `holdout_gate._label_one` 同公式);②`gridtrade/runtime/label_feed.py` 每币滚动 13h 1m 缓冲(增量拉取,weight 1/币/轮);③`triggers.build_eff1_select_fn` 作为 `ScheduledSelectionTrigger(select_fn=...)` 的可插拔选币函数。tick 过滤在因子表 → 排名截断**之前**做(名次递补免费获得),对 rank_sum 与 eff1 两条路都生效。配置开关 `SELECTION_RANKER=eff1|rank`(rank=即时回滚,改 secrets 不用重部署)。

**Tech Stack:** pandas/numpy、ccxt(binanceusdm)、现有 TriggerEngine/GateChain、fly.io secrets。

## Global Constraints

- **同源性**:cross1 = `floor(ln(close)/ln(1.01))` 阶梯的**收盘价**穿越计数,dstep 在整段序列上 `np.diff(prepend=首值)` 后再切窗(窗首根带窗前过渡);o = 窗内**第一根 1m 的 close**;mae = `max(|maxH/o−1|, |minL/o−1|)`;窗 = 选币时刻 rt 的 **[rt−12h, rt)**;窗内 bar<**600** ⇒ 该币该轮**缺标签不参选**(全部照抄 `data/score_research_2026-07-21/holdout_gate.py::_label_one`)。
- **eff1 票池口径**:布网列(close/Atr_5/middle_5)有限即可,**不走** rank_sum 的 filter v1.0 / 因子 dropna(用户令 2026-07-25)。
- **tick 过滤语义**:spacing=(high−low)/grid_count < MIN_TICKS×tickSize ⇒ 剔;缺 tick / 算不出 ⇒ **fail-open 保留**(与 `eff1_scan_v2.tick_filter` 同)。
- **API 权重硬约束**(实测 2026-07-27):基线最差分钟 1436/2400;eff1 稳态 +≤200/min(pace 300ms)、冷启动 +≤375/min(pace 800ms);任何新增拉取必须过 `report_weight` 遥测。
- **排序决定论**:eff1 排名 = `sort_values(['time','p12_eff','symbol'], ascending=[True,False,True])` 后组内序号(与回测 `make_picks` 同 tiebreak)。
- 部署硬规则:prod 只走 production 分支 CD;先 testnet;verify-ledger 前置门。

## 文件结构

```
gridtrade/core/p12_labels.py        新 纯标签数学(无 I/O)
gridtrade/core/tick_fit.py          新 tick 适配度过滤(用 calc_grid_params_v2)
gridtrade/runtime/label_feed.py     新 1m 滚动缓冲(增量拉取+权重遥测)
gridtrade/exchanges/ccxt_adapter.py 改 +fetch_tick_sizes()(读缓存 markets,零权重)
gridtrade/execution/triggers.py     改 _default_select_fn 加 tick 过滤;+build_eff1_select_fn
gridtrade/config.py                 改 +selection_ranker/selection_min_ticks/label_fetch_pace_ms
gridtrade/runtime/factory.py        改 按开关装配 select_fn/LabelFeed;Runtime +label_feed
gridtrade/runtime/scheduler.py      改 选币轮 candles 后 feed.update() 钩子
tests/core/test_p12_labels.py       新
tests/core/test_p12_labels_golden.py 新(本地数据 skip 守卫)
tests/core/test_tick_fit.py         新
tests/runtime/test_label_feed.py    新
tests/execution/test_eff1_select.py 新
tests/test_config.py                改 +3 个新旋钮断言
```

---

### Task 1: `gridtrade/core/p12_labels.py` 纯标签数学

**Files:**
- Create: `gridtrade/core/p12_labels.py`
- Test: `tests/core/test_p12_labels.py`

**Interfaces:**
- Produces: `ladder_dstep(close: np.ndarray) -> np.ndarray`;`window_label(bars: pd.DataFrame, w0, w1) -> tuple[float,float] | None`(返回 `(cross1, mae)`,窗内 bar<600 返 None;bars 需列 `candle_begin_time/close/high/low` 升序);`p12_eff(cross1, mae) -> float`;常量 `MIN_WINDOW_BARS = 600`。

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_p12_labels.py
import numpy as np
import pandas as pd
import pytest

from gridtrade.core.p12_labels import (MIN_WINDOW_BARS, ladder_dstep, p12_eff,
                                       window_label)


def _bars(closes, start='2026-07-01', highs=None, lows=None):
    n = len(closes)
    t = pd.date_range(start, periods=n, freq='1min')
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({'candle_begin_time': t, 'close': c,
                         'high': np.asarray(highs, float) if highs is not None else c,
                         'low': np.asarray(lows, float) if lows is not None else c})


def test_ladder_dstep_counts_level_crossings():
    # 100 → 101.1 跨过 100×1.01=101 一级;首根 prepend ⇒ dstep[0]=0
    d = ladder_dstep(np.array([100.0, 100.5, 101.1, 100.5]))
    assert d[0] == 0 and d.sum() == 2          # 上穿一次+回落一次


def test_window_label_includes_boundary_transition():
    # 窗前最后一根 100.0 → 窗首根 101.1:过渡发生在窗首根,必须计入(同 stage_L 整段 diff 再切窗)
    n = 720
    closes = [100.0] * 61 + [101.1] * (n)      # 61 根窗前(其中最后一根前也全平)
    bars = _bars(closes)
    w0 = bars['candle_begin_time'].iloc[61]
    r = window_label(bars, w0, w0 + pd.Timedelta(hours=12))
    assert r is not None
    cross1, mae = r
    assert cross1 == 1.0                        # 唯一过渡在窗首根
    assert mae == 0.0                           # o=窗首收盘 101.1,窗内无偏离


def test_window_label_o_is_first_close_and_mae_uses_high_low():
    n = 720
    closes = [100.0] * n
    highs = list(closes); lows = list(closes)
    highs[300] = 108.0                          # 窗内最大上偏 8%
    lows[500] = 95.0                            # 窗内最大下偏 5%
    bars = _bars(closes, highs=highs, lows=lows)
    w0 = bars['candle_begin_time'].iloc[0]
    cross1, mae = window_label(bars, w0, w0 + pd.Timedelta(hours=12))
    assert mae == pytest.approx(0.08)


def test_window_label_returns_none_below_600_bars():
    bars = _bars([100.0] * 599)
    w0 = bars['candle_begin_time'].iloc[0]
    assert window_label(bars, w0, w0 + pd.Timedelta(hours=12)) is None
    assert MIN_WINDOW_BARS == 600


def test_p12_eff_formula():
    assert p12_eff(10.0, 0.05) == pytest.approx(10.0 / (1.0 + 100.0 * 0.05))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/core/test_p12_labels.py -q`
Expected: FAIL(ModuleNotFoundError: p12_labels)

- [ ] **Step 3: 最小实现**

```python
# gridtrade/core/p12_labels.py
"""p12(过去12h)标签:cross1/mae/eff —— 与回测 stage_L(holdout_gate._label_one)逐位同源。

⚠ 同源性红线(动任何一行先对 golden 测试):
  - cross1 = floor(ln(close)/ln(1.01)) 阶梯的收盘价穿越计数;dstep 在**整段传入序列**上
    diff(prepend=首值)后再按时间切窗——窗首根带着窗前最后一根的过渡,调用方须多给窗前 bar
    (LabelFeed 缓冲 13h、窗 12h,留 1h 余量即为此)。
  - o = 窗内第一根 1m 的 **close**(不是 open);mae = max(|maxH/o−1|, |minL/o−1|)。
  - 窗内 bar < 600(应 720)⇒ 返 None ⇒ 该币该轮缺标签不参选(回测 inner join 同款)。
"""
import numpy as np
import pandas as pd

LADDER = 1.01
MIN_WINDOW_BARS = 600


def ladder_dstep(close):
    step = np.floor(np.log(np.clip(np.asarray(close, dtype=float), 1e-18, None))
                    / np.log(LADDER))
    return np.abs(np.diff(step, prepend=step[0]))


def window_label(bars, w0, w1):
    t = bars['candle_begin_time'].values
    m = (t >= np.datetime64(pd.Timestamp(w0))) & (t < np.datetime64(pd.Timestamp(w1)))
    if int(m.sum()) < MIN_WINDOW_BARS:
        return None
    c = bars['close'].to_numpy(dtype=float)
    sd = ladder_dstep(c)
    cw = c[m]
    o = cw[0]
    hi = bars['high'].to_numpy(dtype=float)[m].max()
    lo = bars['low'].to_numpy(dtype=float)[m].min()
    return float(sd[m].sum()), max(abs(float(hi / o - 1.0)), abs(float(lo / o - 1.0)))


def p12_eff(cross1, mae):
    return cross1 / (1.0 + 100.0 * mae)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/core/test_p12_labels.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add gridtrade/core/p12_labels.py tests/core/test_p12_labels.py
git commit -m "feat(selection): p12 标签纯数学模块(与回测 stage_L 逐位同源)"
```

---

### Task 2: 黄金向量测试(对回测存档逐位)

**Files:**
- Create: `tests/core/test_p12_labels_golden.py`

**Interfaces:**
- Consumes: Task 1 的 `window_label`/`p12_eff`。
- Produces: 无(纯守卫)。CI 无 data/ ⇒ skip 守卫照抄 test_cf_patrol 风格("能否真加载")。

- [ ] **Step 1: 写测试(本地即失败=不一致,CI=skip)**

```python
# tests/core/test_p12_labels_golden.py
"""黄金向量:hold_labels_JUL26.parquet 抽样 50 行,从本地 Vision 1m 重算,逐位一致。

标签表口径:rt = 窗**起点**,窗=[rt, rt+12h)(选币用时再 +12h 平移,与本测试无关)。
CI/无数据机器:文件缺失 → skip(研究资产 gitignore,先例 test_cf_patrol)。
"""
import os

import pandas as pd
import pytest

LAB = 'data/score_research_2026-07-21/ablation/hold_labels_JUL26.parquet'


@pytest.mark.skipif(not os.path.exists(LAB), reason='研究资产不在此机器(gitignore)')
def test_golden_jul26_sample_bitwise():
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.core.p12_labels import window_label

    lab = pd.read_parquet(LAB)
    smp = lab.sample(50, random_state=7)
    cache = ParquetCache(V.default_cache_root())
    checked = 0
    for r in smp.itertuples(index=False):
        m1 = cache.read_all_days('1m', r.symbol)
        if m1 is None or m1.empty:
            continue
        w0, w1 = pd.Timestamp(r.rt), pd.Timestamp(r.rt) + pd.Timedelta(hours=12)
        seg = m1[(m1['candle_begin_time'] >= w0 - pd.Timedelta(hours=1))
                 & (m1['candle_begin_time'] < w1)].sort_values('candle_begin_time')
        # ⚠ 窗前必须给到与存档相同的 positional 前驱:取窗前 1h 足够(无 >1h 断档时等价)
        got = window_label(seg, w0, w1)
        if got is None:
            continue
        assert got[0] == pytest.approx(r.cross1, abs=1e-9), (r.symbol, r.rt)
        assert got[1] == pytest.approx(r.mae, abs=1e-12), (r.symbol, r.rt)
        checked += 1
    assert checked >= 30, '有效样本过少(%d),黄金测试没咬到' % checked
```

- [ ] **Step 2: 本地跑**

Run: `.venv/bin/python -m pytest tests/core/test_p12_labels_golden.py -q`
Expected: 1 passed(本地有数据)。若 FAIL = 同源性破裂,**停下修 Task 1,不得放宽容差**。
⚠ 已知可接受差异源:窗前 >1h 断档的币,positional 前驱不同(存档从全史取,本测从 1h 余量取)——若某样本仅 cross1 差 1 以内且该币窗前有断档,换样本种子验证,并在模块 docstring 记录此边界。

- [ ] **Step 3: Commit**

```bash
git add tests/core/test_p12_labels_golden.py
git commit -m "test(selection): p12 标签黄金向量(对 JUL26 存档逐位,CI skip 守卫)"
```

---

### Task 3: `gridtrade/core/tick_fit.py` tick 适配度过滤

**Files:**
- Create: `gridtrade/core/tick_fit.py`
- Test: `tests/core/test_tick_fit.py`

**Interfaces:**
- Consumes: `gridtrade.core.grid_params.calc_grid_params_v2`(已有)。
- Produces: `filter_tick_fit(df, tick_map, strategy_config, min_ticks, log=None) -> (df, dropped:list)`,df 须含 `symbol/close/Atr_5/middle_5`。

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_tick_fit.py
import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG
from gridtrade.core.tick_fit import filter_tick_fit


def _df():
    # close=100/Atr_5=0.05/middle_5=100 ⇒ band3:range=15%,spacing_ratio=0.025,
    # calc_grid_params_v2: spacing=(30)/grid_count;grid_count=round(30/2.5)=12→cmin16 ⇒ 16 格
    # spacing = 30/16 = 1.875
    rows = [dict(symbol='FINE/USDT', close=100.0, Atr_5=0.05, middle_5=100.0),
            dict(symbol='COARSE/USDT', close=100.0, Atr_5=0.05, middle_5=100.0),
            dict(symbol='NOTICK/USDT', close=100.0, Atr_5=0.05, middle_5=100.0)]
    return pd.DataFrame(rows)


def test_coarse_tick_dropped_fine_kept_missing_failopen():
    ticks = {'FINE/USDT': 0.01, 'COARSE/USDT': 1.0}   # 1.875/1.0 < 3 ⇒ 剔;NOTICK 缺 ⇒ 留
    out, dropped = filter_tick_fit(_df(), ticks, DEFAULT_STRATEGY_CONFIG, 3.0)
    assert list(out['symbol']) == ['FINE/USDT', 'NOTICK/USDT']
    assert dropped == ['COARSE/USDT']


def test_min_ticks_zero_disables():
    out, dropped = filter_tick_fit(_df(), {'COARSE/USDT': 1.0}, DEFAULT_STRATEGY_CONFIG, 0.0)
    assert len(out) == 3 and dropped == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/core/test_tick_fit.py -q`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 最小实现**

```python
# gridtrade/core/tick_fit.py
"""tickSize 票池过滤 —— 与回测 eff1_scan_v2.tick_filter 同语义。

背景(memory: tick-blindspot-is-eff1-edge):spacing<3×tick 的密格币回测虚高 3.9×、
实盘 41% 挂不上单;eff1 的全部回测读数只在 MIN_TICKS=3 下有效。缺 tick / 布网算不出
⇒ fail-open 保留(生产 rank 人群实测最小 23.2 tick,过滤对它是空转保险)。
在排名/截断**之前**调用 ⇒ 名次递补免费获得。
"""
from gridtrade.core.grid_params import calc_grid_params_v2


def _unfit(row, tick, cfg, min_ticks):
    if not tick or tick != tick:
        return False                                   # fail-open
    try:
        p = calc_grid_params_v2(row=row, price_limit=cfg['price_limit'],
                                stop_limit=cfg['stop_limit'],
                                v2_config=cfg.get('grid_v2_config', {}))
    except Exception:
        return False                                   # fail-open
    return (p['high_price'] - p['low_price']) / p['grid_count'] < min_ticks * tick


def filter_tick_fit(df, tick_map, strategy_config, min_ticks, log=None):
    """返回 (保留df, 剔除symbol列表)。min_ticks<=0 或空表 ⇒ 原样返回。"""
    if min_ticks <= 0 or df is None or df.empty or not tick_map:
        return df, []
    bad = [i for i, row in df.iterrows()
           if _unfit(row, tick_map.get(row['symbol']), strategy_config, min_ticks)]
    if not bad:
        return df, []
    dropped = sorted(df.loc[bad, 'symbol'].unique().tolist())
    if log:
        log('[tick-filter] -%d 币 spacing<%g×tick (e.g. %s)'
            % (len(dropped), min_ticks, dropped[:5]))
    return df.drop(index=bad), dropped
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/core/test_tick_fit.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add gridtrade/core/tick_fit.py tests/core/test_tick_fit.py
git commit -m "feat(selection): tickSize 票池过滤(MIN_TICKS,与回测 tick_filter 同语义)"
```

---

### Task 4: 适配器 `fetch_tick_sizes()`

**Files:**
- Modify: `gridtrade/exchanges/ccxt_adapter.py`(挨着 `fetch_max_leverages`,~line 168)
- Test: `tests/exchanges/test_ccxt_adapter.py`(追加;若该文件不存在则建 `tests/exchanges/test_tick_sizes.py`)

**Interfaces:**
- Produces: `CCXTAdapter.fetch_tick_sizes() -> dict[symbol, float]`,读 ccxt 缓存 markets(**零 REST 权重**);解析失败的币不入表(下游 fail-open)。

- [ ] **Step 1: 写失败测试**

```python
# tests/exchanges/test_tick_sizes.py
from unittest.mock import MagicMock

from gridtrade.exchanges.ccxt_adapter import CCXTAdapter


def _adapter_with_markets(markets):
    a = CCXTAdapter.__new__(CCXTAdapter)          # 不走 __init__(不建真 client)
    a.client = MagicMock()
    a.client.markets = markets
    a.client.load_markets = MagicMock()
    return a


def test_tick_from_price_filter_and_skip_bad():
    mk = {
        'BTC/USDT:USDT': {'symbol': 'BTC/USDT:USDT', 'swap': True, 'quote': 'USDT',
                          'info': {'filters': [{'filterType': 'PRICE_FILTER',
                                                'tickSize': '0.10'}]},
                          'precision': {'price': 0.1}},
        'XYZ/USDT:USDT': {'symbol': 'XYZ/USDT:USDT', 'swap': True, 'quote': 'USDT',
                          'info': {}, 'precision': {}},   # 双缺 ⇒ 不入表
    }
    a = _adapter_with_markets(mk)
    out = a.fetch_tick_sizes()
    assert out.get('BTC/USDT') == 0.10
    assert 'XYZ/USDT' not in out
```

⚠ 统一 symbol 映射:先读 `to_native`/`list_instruments` 现行写法,测试断言键与**现行统一口径**一致(上面按 `BTC/USDT` 假设,若现行是别的口径,改测试跟随,勿改适配器口径)。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_tick_sizes.py -q`
Expected: FAIL(AttributeError: fetch_tick_sizes)

- [ ] **Step 3: 实现(镜像 fetch_max_leverages 的映射写法)**

```python
    def fetch_tick_sizes(self) -> dict:
        """{统一symbol: tickSize} —— 读 ccxt 缓存 markets(零 REST 权重)。

        选币 tick 过滤用(core.tick_fit);解析不出的币不入表 ⇒ 下游 fail-open 保留。
        优先 info.filters PRICE_FILTER.tickSize(币安权威),缺则回退 precision.price
        (ccxt binanceusdm 为 TICK_SIZE 模式,该值即 tick)。
        """
        self.client.load_markets()
        out = {}
        for m in self.client.markets.values():
            if not m.get('swap') or m.get('quote') != 'USDT':
                continue
            tick = None
            for f in ((m.get('info') or {}).get('filters') or []):
                if f.get('filterType') == 'PRICE_FILTER':
                    try:
                        tick = float(f.get('tickSize') or 0) or None
                    except (TypeError, ValueError):
                        tick = None
            if tick is None:
                p = (m.get('precision') or {}).get('price')
                try:
                    tick = float(p) if p else None
                except (TypeError, ValueError):
                    tick = None
            if tick:
                out[self.from_native(m['symbol'])] = tick     # ← 用现行统一映射函数名
        return out
```

⚠ `from_native` 是占位名——**照抄 `fetch_max_leverages`/`list_instruments` 里 native→统一 的现行写法**(那两处已在生产验证过映射)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_tick_sizes.py -q`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_tick_sizes.py
git commit -m "feat(adapter): fetch_tick_sizes(缓存 markets 零权重,PRICE_FILTER 优先)"
```

---

### Task 5: 配置旋钮 ×3

**Files:**
- Modify: `gridtrade/config.py`(load_deploy_config,挨着 `scheduler_fetch_pace_ms`)
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `config.selection_ranker`(env `SELECTION_RANKER`,default `'rank'`);`config.selection_min_ticks`(env `SELECTION_MIN_TICKS`,default `3.0`,`0`=关);`config.label_fetch_pace_ms`(env `LABEL_FETCH_PACE_MS`,default `300.0`)。

- [ ] **Step 1: 写失败测试(追加到 test_config.py)**

```python
def test_selection_ranker_and_tick_knobs():
    from gridtrade.config import load_deploy_config
    c = load_deploy_config(env={'EXCHANGE': 'fake'})
    assert c.selection_ranker == 'rank'          # 默认生产 rank(eff1 须显式开)
    assert c.selection_min_ticks == 3.0          # tick 过滤默认开(rank 人群空转保险)
    assert c.label_fetch_pace_ms == 300.0
    c2 = load_deploy_config(env={'EXCHANGE': 'fake', 'SELECTION_RANKER': 'eff1',
                                 'SELECTION_MIN_TICKS': '0', 'LABEL_FETCH_PACE_MS': '500'})
    assert (c2.selection_ranker, c2.selection_min_ticks, c2.label_fetch_pace_ms) \
        == ('eff1', 0.0, 500.0)
```

- [ ] **Step 2: 跑确认失败** → `.venv/bin/python -m pytest tests/test_config.py -q` Expected: FAIL
- [ ] **Step 3: 实现**(照 `scheduler_fetch_pace_ms` 现行读法加三个字段;`selection_ranker` 取值校验 `in ('rank','eff1')`,非法值 raise——fail-fast 同 HL_* 守卫风格)
- [ ] **Step 4: 跑确认通过**
- [ ] **Step 5: Commit** `git commit -m "feat(config): SELECTION_RANKER/SELECTION_MIN_TICKS/LABEL_FETCH_PACE_MS"`

---

### Task 6: `gridtrade/runtime/label_feed.py` 1m 滚动缓冲

**Files:**
- Create: `gridtrade/runtime/label_feed.py`
- Test: `tests/runtime/test_label_feed.py`

**Interfaces:**
- Consumes: `adapter.fetch_ohlcv(sym,'1m',start_ms,end_ms) -> df[ts/open/high/low/close/vol/candle_begin_time/symbol]`(已有);Task 1 `window_label/p12_eff`。
- Produces: `LabelFeed(adapter, *, pace_ms=300.0, cold_pace_ms=800.0, log=print, sleep=time.sleep)`;`feed.update(symbols, run_time)`;`feed.labels(run_time) -> dict[sym, {'p12_cross1','p12_mae','p12_eff'}]`。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_label_feed.py
import numpy as np
import pandas as pd

from gridtrade.runtime.label_feed import LabelFeed


class FakeAdapter:
    """合成 1m 行情:恒价 100,仅 GOOD/USDT 在窗内某分钟拉高 8%。记录取数区间。"""
    def __init__(self):
        self.calls = []

    def fetch_ohlcv(self, sym, tf, start_ms, end_ms):
        assert tf == '1m'
        self.calls.append((sym, start_ms, end_ms))
        t = pd.date_range(pd.Timestamp(start_ms, unit='ms'),
                          pd.Timestamp(end_ms, unit='ms'), freq='1min')
        t = t[t <= pd.Timestamp(end_ms, unit='ms')]
        df = pd.DataFrame({'ts': (t.view('int64') // 10**6),
                           'candle_begin_time': t, 'symbol': sym,
                           'open': 100.0, 'high': 100.0, 'low': 100.0,
                           'close': 100.0, 'vol': 1.0})
        if sym == 'GOOD/USDT' and len(df) > 400:
            df.loc[400, 'high'] = 108.0
        return df


def test_cold_then_incremental_and_labels():
    ad = FakeAdapter()
    feed = LabelFeed(ad, pace_ms=0, cold_pace_ms=0, sleep=lambda s: None)
    rt = pd.Timestamp('2026-07-27 03:00:00')
    feed.update(['GOOD/USDT', 'FLAT/USDT'], rt)
    n_cold = len(ad.calls)
    assert n_cold == 2
    lab = feed.labels(rt)
    assert set(lab) == {'GOOD/USDT', 'FLAT/USDT'}
    assert lab['GOOD/USDT']['p12_mae'] > 0.07          # 窗内 8% 上偏被 mae 捕获
    assert lab['FLAT/USDT']['p12_cross1'] == 0.0
    # 下一小时增量:since 从缓冲尾回退 ≤2min,不再全量
    rt2 = rt + pd.Timedelta(hours=1)
    feed.update(['GOOD/USDT', 'FLAT/USDT'], rt2)
    sym, start_ms, _ = ad.calls[n_cold]
    gap_min = (rt2 - pd.Timestamp(start_ms, unit='ms')).total_seconds() / 60
    assert gap_min <= 63                                # 增量而非 13h 全量


def test_missing_coin_excluded_and_pool_trim():
    class DeadAdapter(FakeAdapter):
        def fetch_ohlcv(self, sym, tf, s, e):
            if sym == 'DEAD/USDT':
                raise RuntimeError('boom')
            return super().fetch_ohlcv(sym, tf, s, e)
    ad = DeadAdapter()
    feed = LabelFeed(ad, pace_ms=0, cold_pace_ms=0, sleep=lambda s: None)
    rt = pd.Timestamp('2026-07-27 03:00:00')
    feed.update(['DEAD/USDT', 'FLAT/USDT'], rt)
    assert set(feed.labels(rt)) == {'FLAT/USDT'}       # fail-open:缺数据币缺标签不参选
    feed.update(['FLAT/USDT'], rt + pd.Timedelta(hours=1))
    assert set(feed._buf) == {'FLAT/USDT'}             # 掉出票池的缓冲被修剪
```

- [ ] **Step 2: 跑确认失败** Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
# gridtrade/runtime/label_feed.py
"""每币滚动 13h 1m 缓冲 → p12 标签(eff1 选币数据源)。

权重账(实测 2026-07-27,上限 2400/min,基线最差分钟 1436):
  稳态:每轮每币增量 limit≈65 ⇒ weight 1;282 币 × pace 300ms ≈ +200/min。
  冷启动(进程首轮):limit≈780 ⇒ weight 5/币;pace 800ms 摊 ~226s ≈ +375/min。
每次取数前调 adapter.report_weight()(遥测归因,同 scheduler._fetch_pass)。
缓冲 13h、标签窗 12h:1h 余量保证窗首根的 positional 前驱在场(p12_labels docstring)。
fail-open:单币取数失败只跳过 ⇒ 该币本轮缺标签不参选,绝不阻塞选币轮。
"""
import time

import pandas as pd

from gridtrade.core.p12_labels import p12_eff, window_label

BUFFER_HOURS = 13
REFETCH_TAIL_MS = 120_000        # 尾部回拉 2min:治「上轮末根未定型」的陈旧半根


class LabelFeed:
    def __init__(self, adapter, *, pace_ms=300.0, cold_pace_ms=800.0,
                 log=print, sleep=time.sleep):
        self.adapter = adapter
        self.pace_ms = float(pace_ms)
        self.cold_pace_ms = float(cold_pace_ms)
        self.log = log
        self.sleep = sleep
        self._buf = {}

    def update(self, symbols, run_time):
        rt = pd.Timestamp(run_time)
        end_ms = int(rt.value // 1_000_000) - 1          # 排除 begin==rt 的成型中 bar
        lo_ms = int((rt - pd.Timedelta(hours=BUFFER_HOURS)).value // 1_000_000)
        rw = getattr(self.adapter, 'report_weight', None)
        t0, n_cold, n_fail = time.time(), 0, 0
        for i, sym in enumerate(symbols):
            prev = self._buf.get(sym)
            cold = prev is None or prev.empty
            n_cold += int(cold)
            since = lo_ms if cold else max(
                lo_ms, int(prev['ts'].iloc[-1]) - REFETCH_TAIL_MS)
            if i:
                self.sleep((self.cold_pace_ms if cold else self.pace_ms) / 1000.0)
            if rw is not None:
                rw()
            try:
                df = self.adapter.fetch_ohlcv(sym, '1m', since, end_ms)
            except Exception:
                n_fail += 1
                continue                                  # fail-open:缺标签不参选
            if df is None or df.empty:
                continue
            cur = df if cold else pd.concat([prev, df], ignore_index=True)
            cur = (cur.drop_duplicates(subset=['ts'], keep='last')
                      .sort_values('ts'))
            self._buf[sym] = cur[cur['ts'] >= lo_ms].reset_index(drop=True)
        keep = set(symbols)                               # 掉出票池的缓冲修剪防缓涨
        for s in [s for s in self._buf if s not in keep]:
            del self._buf[s]
        self.log('[label-feed] %d 币(冷%d 失败%d) %.1fs'
                 % (len(symbols), n_cold, n_fail, time.time() - t0))

    def labels(self, run_time):
        w1 = pd.Timestamp(run_time)
        w0 = w1 - pd.Timedelta(hours=12)
        out = {}
        for sym, df in self._buf.items():
            r = window_label(df, w0, w1)
            if r is not None:
                out[sym] = {'p12_cross1': r[0], 'p12_mae': r[1],
                            'p12_eff': p12_eff(r[0], r[1])}
        return out
```

- [ ] **Step 4: 跑确认通过** → `.venv/bin/python -m pytest tests/runtime/test_label_feed.py -q` Expected: 2 passed
- [ ] **Step 5: Commit** `git commit -m "feat(runtime): LabelFeed 1m 滚动缓冲(增量拉取+权重遥测+fail-open)"`

---

### Task 7: triggers 接入(tick 过滤 + eff1 select_fn + 快照)

**Files:**
- Modify: `gridtrade/execution/triggers.py`
- Test: `tests/execution/test_eff1_select.py`

**Interfaces:**
- Consumes: Task 1/3/6 全部;`proceed_calc_symbol_factor(..., needed=, batch=)`(已有);`GRID_ROW_FACTORS`(grid_params)。
- Produces: `_default_select_fn(strategy_config, factors, weight_list, *, tick_map_fn=None, min_ticks=0.0, log=print)`(向后兼容:新参有默认);`build_eff1_select_fn(strategy_config, label_feed, *, tick_map_fn=None, min_ticks=0.0, log=print)`。两者返回 `_fn(symbol_candle_data, run_time, offset) -> df[symbol/time/close/Atr_5/middle_5/rank(+p12_*)]`,行数 ≤ choose_symbols。

- [ ] **Step 1: 写失败测试**

```python
# tests/execution/test_eff1_select.py
import numpy as np
import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG
from gridtrade.execution.triggers import build_eff1_select_fn


class FeedStub:
    def __init__(self, lab):
        self._lab = lab

    def labels(self, run_time):
        return self._lab


def _candles(syms, rt, n=200):
    t = pd.date_range(rt - pd.Timedelta(hours=n), periods=n, freq='1h')
    out = {}
    for s in syms:
        out[s] = pd.DataFrame({'candle_begin_time': t, 'symbol': s,
                               'open': 100.0, 'high': 101.0, 'low': 99.0,
                               'close': 100.0, 'vol': 5.0, 'quote_volume': 500.0})
    return out


def test_eff1_ranks_by_eff_desc_symbol_tiebreak_and_truncates():
    rt = pd.Timestamp('2026-07-27 03:00:00')
    lab = {'AAA/USDT': dict(p12_cross1=8.0, p12_mae=0.02, p12_eff=8.0 / 3.0),
           'BBB/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5),
           'CCC/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5)}
    fn = build_eff1_select_fn(DEFAULT_STRATEGY_CONFIG, FeedStub(lab))
    out = fn(_candles(['AAA/USDT', 'BBB/USDT', 'CCC/USDT', 'NOLAB/USDT'], rt), rt, 3)
    assert len(out) == DEFAULT_STRATEGY_CONFIG['choose_symbols'] == 1
    assert out.iloc[0]['symbol'] == 'BBB/USDT'      # eff 同分 ⇒ symbol 升序,B 在 C 前
    assert 'NOLAB/USDT' not in set(out['symbol'])   # 缺标签不参选
    assert {'p12_eff', 'p12_cross1', 'p12_mae', 'rank'} <= set(out.columns)


def test_eff1_tick_filter_promotes_next():
    rt = pd.Timestamp('2026-07-27 03:00:00')
    lab = {'TOP/USDT': dict(p12_cross1=9.0, p12_mae=0.01, p12_eff=4.5),
           'SECOND/USDT': dict(p12_cross1=5.0, p12_mae=0.01, p12_eff=2.5)}
    fn = build_eff1_select_fn(DEFAULT_STRATEGY_CONFIG, FeedStub(lab),
                              tick_map_fn=lambda: {'TOP/USDT': 50.0},  # 粗 tick ⇒ 剔
                              min_ticks=3.0)
    out = fn(_candles(['TOP/USDT', 'SECOND/USDT'], rt), rt, 3)
    assert list(out['symbol']) == ['SECOND/USDT']   # 递补
```

- [ ] **Step 2: 跑确认失败** Expected: ImportError(build_eff1_select_fn)

- [ ] **Step 3: 实现(triggers.py 追加/修改)**

```python
# imports 区追加:
import numpy as np
from gridtrade.core.grid_params import GRID_ROW_FACTORS
from gridtrade.core.tick_fit import filter_tick_fit


def _default_select_fn(strategy_config, factors, weight_list, *,
                       tick_map_fn=None, min_ticks=0.0, log=print):
    period = strategy_config['period']
    choose_symbols = strategy_config['choose_symbols']

    def _fn(symbol_candle_data, run_time, offset):
        all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time,
                                            period, offset)
        if all_df is None or all_df.empty:
            return all_df
        if tick_map_fn is not None and min_ticks > 0:
            # 排名前过滤 ⇒ 名次递补免费获得(与回测 pick_first_allowed 语义等价)
            all_df, _ = filter_tick_fit(all_df, tick_map_fn(), strategy_config,
                                        min_ticks, log=log)
            if all_df.empty:
                return all_df
        return select_grid_coin(all_df, factors, weight_list, choose_symbols,
                                run_time)

    return _fn


def build_eff1_select_fn(strategy_config, label_feed, *,
                         tick_map_fn=None, min_ticks=0.0, log=print):
    """eff1 选币(与回测 eff1_scan.make_picks 同口径):
    票池 = 布网列有限(**不走** rank_sum filter v1.0/因子 dropna,用户令 2026-07-25);
    p12_eff 降序、symbol 升序 tiebreak;缺标签不参选(inner merge 同款)。"""
    period = strategy_config['period']
    choose_symbols = strategy_config['choose_symbols']

    def _fn(symbol_candle_data, run_time, offset):
        all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period,
                                            offset, needed=set(GRID_ROW_FACTORS),
                                            batch=True)
        if all_df is None or all_df.empty:
            return all_df
        all_df = all_df[np.isfinite(all_df['close']) & np.isfinite(all_df['Atr_5'])
                        & np.isfinite(all_df['middle_5'])]
        if tick_map_fn is not None and min_ticks > 0:
            all_df, _ = filter_tick_fit(all_df, tick_map_fn(), strategy_config,
                                        min_ticks, log=log)
        lab = label_feed.labels(run_time)
        if all_df.empty or not lab:
            log('[eff1] 本轮无候选(候选=%d 标签=%d)' % (len(all_df), len(lab)))
            return all_df.iloc[0:0]
        ldf = pd.DataFrame([dict(symbol=s, **v) for s, v in lab.items()])
        d = all_df.merge(ldf, on='symbol', how='inner')
        d = d.sort_values(['time', 'p12_eff', 'symbol'],
                          ascending=[True, False, True])
        d['rank'] = d.groupby('time', sort=False).cumcount() + 1.0
        return d[d['rank'] <= choose_symbols]

    return _fn
```

⚠ 快照兼容:`ScheduledSelectionTrigger` 的 `_fcols` 从 `self.factors` 取——factory(Task 8)给 eff1 触发器传 `factors=('p12_eff','p12_cross1','p12_mae')`、`weight_list=()`,快照自动带上三列;`rank_sum` 缺列走既有 `0.0` 默认,零改动。

- [ ] **Step 4: 跑确认通过** + 回归:`.venv/bin/python -m pytest tests/execution/ -q` Expected: 全绿(_default_select_fn 新参有默认,旧调用不破)
- [ ] **Step 5: Commit** `git commit -m "feat(selection): eff1 select_fn + tick 过滤接入触发器(排名前过滤=免费递补)"`

---

### Task 8: factory 装配 + scheduler 钩子

**Files:**
- Modify: `gridtrade/runtime/factory.py`(~line 106 触发器构造区;Runtime 构造加 `label_feed`)
- Modify: `gridtrade/runtime/scheduler.py`(candles 取完之后、TriggerContext 之前,~line 236)
- Test: `tests/runtime/test_scheduler_label_hook.py`

**Interfaces:**
- Consumes: Task 5 配置字段、Task 6 LabelFeed、Task 7 两个 select_fn builder、Task 4 fetch_tick_sizes。
- Produces: `Runtime.label_feed`(eff1 时为 LabelFeed,否则 None);scheduler 在选币轮调 `feed.update(list(candles.keys()), run_time)`,降级不阻塞。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_scheduler_label_hook.py
"""scheduler 选币轮:label_feed 存在则先 update 再进 cycle;update 崩溃只降级。
复用本目录现有 scheduler 测试的 runtime stub 写法(读 tests/runtime/ 里
run_scheduler_once 既有测试,抄它的最小 runtime 构造,加 label_feed 属性)。"""
```

(具体 stub 依既有 `tests/runtime/` 的 scheduler 测试样式填——断言两点:①`feed.update` 被调且参数=candles 键集;②update raise 时轮次照常完成、结果无异常键。)

- [ ] **Step 2: 跑确认失败**

- [ ] **Step 3: 实现**

factory(替换 line 106-108 一带):

```python
    sc = DEFAULT_STRATEGY_CONFIG
    label_feed = None
    _tick_fn = adapter.fetch_tick_sizes          # 缓存 markets,零权重,每轮惰性调用
    if getattr(config, 'selection_ranker', 'rank') == 'eff1':
        from gridtrade.runtime.label_feed import LabelFeed
        from gridtrade.execution.triggers import build_eff1_select_fn
        label_feed = LabelFeed(adapter, pace_ms=config.label_fetch_pace_ms,
                               log=_flush_log)
        _sel = build_eff1_select_fn(sc, label_feed, tick_map_fn=_tick_fn,
                                    min_ticks=config.selection_min_ticks,
                                    log=_flush_log)
        trigger = ScheduledSelectionTrigger(
            sc, ('p12_eff', 'p12_cross1', 'p12_mae'), (), select_fn=_sel)
    else:
        from gridtrade.execution.triggers import _default_select_fn
        _sel = _default_select_fn(sc, sc['factors'], sc['weight_list'],
                                  tick_map_fn=_tick_fn,
                                  min_ticks=config.selection_min_ticks,
                                  log=_flush_log)
        trigger = ScheduledSelectionTrigger(sc, sc['factors'], sc['weight_list'],
                                            select_fn=_sel)
    trigger_engine = TriggerEngine([trigger])
```

Runtime 构造(文件尾 `return Runtime(...)`)加 `label_feed=label_feed`;`Runtime` dataclass 加字段 `label_feed: object = None`。

scheduler(line ~236 `ctx = TriggerContext(...)` 之前):

```python
    # eff1 标签供给:1m 增量缓冲先于选币更新(冷启动首轮 ~4min,12H 周期晚几分钟无影响,
    # 先例 SALVAGE_COOLDOWN)。降级=本轮部分币缺标签不参选,绝不阻塞。
    feed = getattr(rt, 'label_feed', None)
    if feed is not None and open_enabled:
        try:
            feed.update(list(candles.keys()), run_time)
        except Exception as exc:
            print('[scheduler] label-feed degraded: %r' % exc, flush=True)
```

⚠ `open_enabled` 条件:pool-guard/shock 拦开仓的轮次不烧 1m 权重(反正不开仓)。

- [ ] **Step 4: 全量回归** `.venv/bin/python -m pytest tests/ -q` Expected: 全绿
- [ ] **Step 5: Commit** `git commit -m "feat(runtime): SELECTION_RANKER 装配 eff1 选币线(LabelFeed+tick 过滤+快照)"`

---

### Task 9: 上线(testnet → prod)+ 记档

**Files:**
- Modify: memory `binance-param-resweep.md`(部署状态)、新 memory(eff1 上线记录)
- Ops only(无代码)

- [ ] **Step 1: testnet 部署**

```bash
git checkout main && git merge <feature-branch> && git push origin main
fly secrets set --app gridtrade-bi-test SELECTION_RANKER=eff1
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile --remote-only --app gridtrade-bi-test
```

- [ ] **Step 2: testnet 验收(下一个整点选币轮)**

```bash
fly logs -a gridtrade-bi-test | grep -E "label-feed|eff1|tick-filter|\[weight\]"
```
验收单:①`[label-feed] N 币(冷N …)` 出现且耗时 <5min;②选币快照带 p12_eff 三列(`flyctl ssh` 查 selection_snapshots 最新行);③`[weight]` 选币分钟 <2000 且无 429/CircuitOpen;④若有剔币,`[tick-filter]` 行出现;⑤开格 tag/币与 eff1 榜首一致。

- [ ] **Step 3: 影子对齐(测得起就测)**:testnet 选币快照的 (symbol, p12_eff) vs 本地 `select_gr…`/hold_labels 管道重算同 rt——Vision 滞后 ~2 天,T+2 后跑;差 >1e-6 记 issue 不阻塞 prod(黄金测试已锁数学,此步锁**数据源差**)。

- [ ] **Step 4: prod 上线(硬规则链)**

```bash
flyctl ssh console -a gridtrade-bi-prod -C "python -m gridtrade.runtime.dbadmin verify-ledger"   # 必须 clean
git checkout production && git pull && git merge main --no-edit && git push origin production      # CD
fly secrets set --app gridtrade-bi-prod SELECTION_RANKER=eff1    # secrets 即环境,machines 自动重启
gh run watch --exit-status <run-id>
```
验收同 Step 2 + 次日 verify-ledger 复核。**回滚 = `fly secrets set SELECTION_RANKER=rank`(不用回代码)。**

- [ ] **Step 5: 记档**:更新 memory(部署=S2G0+eff1+tick3;`eff1_edge_is_six_coins` 集中度与容量提醒挂到部署记录);prereg §10 补一行"整包后续已上"。

---

## Self-Review 备忘

- 同源性三锁:单元测试(Task 1)锁公式、黄金向量(Task 2)锁与存档逐位、影子对齐(Task 9.3)锁数据源差。
- 递补语义:tick 过滤在排名前 ⇒ 两 ranker 都免费递补;cap/杠杆递补沿用 scheduler 现有 pre-filter(与回测 allocate_with_tiers 等价性已在 §8 记档)。
- 风险披露不变:eff1 选币器线 9/9 判死在案(prereg §8.3);本次上线是用户决定,eff1_edge=6币 集中度、容量闸门照旧有效。
