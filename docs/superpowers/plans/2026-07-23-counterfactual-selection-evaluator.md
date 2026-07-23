# 反事实选币评价器（cf_eval）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一个回顾性测量工具：对每个选币轮的全票池逐币跑双口径反事实引擎模拟（E0 纯网格=主分数、s030 全链=副刻度），产出选币 alpha/捕获率/遗憾/止损链税/池体温成绩单；先研究六窗验证（锚 diff==0），再接实盘巡检（对账本）。

**Architecture:** 复用已锚验的标定引擎协议（`s030_calib.py`/`geo_sweep_run.py` 的逐格块），抽成 `cf_eval.eval_grid()` 纯函数；P1 驱动用战役 byte 精确生产格 `pdetail_*.parquet` 当"实际选中"、`build_pit_candidates` 生产语义当票池、`sc_factors/hold_factors` 面板查 Atr_5；P2 用容器 dump 的 selection_snapshots + grids + fapi 公开行情重建。**不新写引擎、不重写选币**（choose_n 教训）。

**Tech Stack:** Python（repo `.venv`）、pandas/numpy、`gridtrade.core.grid_engine.simulate_grid_engine`、`gridtrade.backtest`（cache/vision/sweep/backtest_run/selection_replay）、pytest、fapi 公开 REST（P2）。

**Spec:** `docs/superpowers/specs/2026-07-23-counterfactual-selection-evaluator-design.md`

## Global Constraints

- 一律用 `.venv/bin/python`，从 repo 根 `/Users/thomaschang/Projects/GridTradeBi` 运行。
- 研究脚本头部固定两行（锁 OpenBLAS 线程，防死机）：`sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')` 后紧跟 `import gridtrade.backtest`。
- 多进程必守 `__main__`，全机并发 workers ≤4（本计划全部串行脚本，无进程池）。
- 策略参数单源：从 `gridtrade.backtest.sweep` import `_S/_STOP/_V2/FEE_MAKER/FEE_TAKER/MAX_RATE/GEARING`，**禁止硬编码几何/止损值**。
- 锚纪律：diff==0 用严格 `!=` 判（同代码路径同输入应逐位相等）；锚不平 → 查保真度修 bug，**禁止放宽成 atol**。
- 禁回喂（spec §6.2）：所有产物只许回看，不得作为选币输入。
- 六窗日期：W1 2025-08-15~10-14 / W2 2025-10-15~12-14 / OOS 2026-01-01~02-28 / IS 2026-03-01~06-30 / HOLD-A 2025-02-01~03-31 / HOLD-B 2024-10-01~11-30。
- 票池生产语义：`build_pit_candidates(series_1h, rt, max_candle_num=160, min_quote_volume=0.0, top_volume_pct=0.55, blacklist=())`，universe = `V.list_archive_symbols()` 减 `effective_blacklist((), DEFAULT_TIER_POLICY)`（0.55=生产 env `UNIVERSE_TOP_VOLUME_PCT` 现值）。
- 研究资产目录 `RD = data/score_research_2026-07-21`（下文相对路径均基于 repo 根）。依赖资产测试一律 `pytest.mark.skipif` 资产缺失。
- 提交在当前分支 `snapshot-reads-ttl`，消息风格 `feat(research): 中文说明` / `test(research): ...`。

## 已核实的事实（实现者不需再查）

- 引擎调用协议（全链）= `s030_calib.py:68-73`；E0 = `geo_sweep_run.py:87-91`（`stop_cfg=None, pv_spike_df=None, active_stop_mode='none'`）。返回 `res['pnl_ratio']`、`res.get('exit_reason')`。
- `calc_grid_params_v2(gr, _S['price_limit'], _S['stop_limit'], _V2)`，`gr={'Atr_5','close','middle_5'}`，`middle_5≈close`（Stage E 备案）。
- geo 的 m30_c16 gp 公式（绕 V2 clamp）：`r=clip(3·Atr_5, 0.02, 0.5)`，±1% stop buffer，固定 16 格——**只作锚**，生产口径用 V2。
- 锚文件：`RD/ablation/s030_calib_{W1,W2,OOS,IS}.parquet`（cols: rt/symbol/pnl/reason/cross1/drift/mae/eff，~699/窗）；`RD/ablation/geo_{win}.parquet`（cols 含 rt/symbol/Atr_5/pnl_m30_c16/reason_m30_c16）。
- 选中记录：`RD/ablation/pdetail_{六窗}.parquet`（cols: run_time/offset/symbol/pnl_ratio/exit_reason/window）。⚠其 pnl_ratio 是组合战役口径（真 cap/lot 取整/shock 剔轮），与本工具 standalone 模拟（cap=1000 无取整）**不应也不必相等**——pdetail 只取"选了谁"，禁止拿它当 pnl 锚。
- 因子面板：`RD/sc_factors_{W1,W2,OOS,IS}.parquet` 与 `RD/ablation/hold_factors_{HOLD-A,HOLD-B}.parquet`（cols: rt/symbol/offset/Reg_v2_5/Sgcz_5/Er_2/S_shape_5/Atr_5，全 universe 528 币）。
- 标签：`RD/sc_labels_{win}.parquet` 与 `RD/ablation/hold_labels_{HOLD-x}.parquet`（cols: rt/symbol/cross1/drift/mae）。
- 权重：`RD/ablation/score_eval_weights.json` → 键 `weights_bp_per_sigma = {'cross1': 32.82, 'drift': -60.67}`（json 里 `form` 文案数字是旧的，以 `weights_bp_per_sigma` 为准）。
- funding 缓存 schema：`ts(ms)/symbol/fundingRate/realizedRate`；引擎消费 `ts/fundingRate`。
- `selection_snapshots.ranked` **只存选中币**（`triggers.py:83` `factor_data=select_fn(...)` 即 `select_grid_coin` 输出；models.py 注释"名次升序全池"有误导）→ P2 票池按 spec fallback 重建。
- P2 从本机跑，fapi 权重按 IP 计，与 prod 容器预算隔离；仍按 250ms pace 自律。
- 容器 dump 模式：`flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/xxx.py > out.json`（`dump_live_grids.py` 先例，sqlalchemy + `DATABASE_URL`）。

## File Structure

| 文件 | 职责 |
|---|---|
| Create `RD/cf_eval.py` | 核心纯函数：一格双口径模拟（无 IO 副作用） |
| Create `RD/cf_anchor.py` | P1 锚门：2,796 格双口径 diff==0 + E0@V2−E0@geo 分布 |
| Create `RD/cf_run.py` | P1 驱动：逐轮全池反事实 → `RD/ablation/cf_<win>.parquet` |
| Create `RD/cf_report.py` | 指标纯函数 + 六窗成绩单 → `RD/ablation/cf_results.txt` |
| Create `scripts/dump_selection_snapshots.py` | 容器内 dump snapshots → JSON |
| Create `scripts/cf_patrol.py` | P2：fapi 取数 + 重建池 + 模拟 + 账本对照 |
| Create `tests/research/__init__.py` | 空包文件 |
| Create `tests/research/test_cf_eval.py` | 单元锚（3 已知格×双口径） |
| Create `tests/research/test_cf_report.py` | 指标数学合成数据测试 |
| Create `tests/research/test_cf_patrol.py` | fapi 行→df 构造器纯函数测试（无网络） |

---

### Task 1: cf_eval 核心 + 单元锚

**Files:**
- Create: `data/score_research_2026-07-21/cf_eval.py`
- Create: `tests/research/__init__.py`（空文件）
- Test: `tests/research/test_cf_eval.py`

**Interfaces:**
- Consumes: `simulate_grid_engine`、`calc_grid_params_v2`、`pv_spike_for_window`、`holding_bars`、sweep 常量（全部现存）。
- Produces: `cf_eval.eval_grid(m1: DataFrame|None, fd_all: DataFrame|None, rt: Timestamp, atr5: float, geometry: str='v2') -> dict|None`，返回键 `pnl_e0/reason_e0/pnl_s030/reason_s030`；`geometry ∈ {'v2','geo'}`。数据不足（m1 空/bars<600/atr 非有限）返回 None。后续所有任务只用这一个入口。

- [ ] **Step 1: 写失败测试**

```python
# tests/research/test_cf_eval.py
"""cf_eval 单元锚(spec §4):已知格逐位复现 s030_calib / geo 产物。
依赖本机研究资产(缺失即 skip);读 1m 缓存,运行 ~1min。"""
import importlib.util
import os

import pandas as pd
import pytest

RD = 'data/score_research_2026-07-21'

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(RD, 'ablation', 's030_calib_W1.parquet')),
    reason='research assets not on this machine')


@pytest.fixture(scope='module')
def ctx():
    spec = importlib.util.spec_from_file_location(
        'cf_eval', os.path.join(RD, 'cf_eval.py'))
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.cache import ParquetCache
    return cf, ParquetCache(V.default_cache_root())


def test_s030_caliber_reproduces_anchor(ctx):
    cf, cache = ctx
    s030 = pd.read_parquet(os.path.join(RD, 'ablation', 's030_calib_W1.parquet')).head(3)
    geo = pd.read_parquet(os.path.join(RD, 'ablation', 'geo_W1.parquet'))[
        ['rt', 'symbol', 'Atr_5']]
    s030 = s030.merge(geo, on=['rt', 'symbol'], how='left')
    for _, r in s030.iterrows():
        m1 = cache.read_all_days('1m', r['symbol'])
        fd = cache.read_all_days('funding', r['symbol'])
        out = cf.eval_grid(m1, fd, pd.Timestamp(r['rt']), r['Atr_5'], geometry='v2')
        assert out is not None
        assert out['pnl_s030'] == r['pnl']          # 逐位相等,禁 atol


def test_e0_geo_caliber_reproduces_anchor(ctx):
    cf, cache = ctx
    geo = pd.read_parquet(os.path.join(RD, 'ablation', 'geo_W1.parquet')).head(3)
    for _, r in geo.iterrows():
        m1 = cache.read_all_days('1m', r['symbol'])
        fd = cache.read_all_days('funding', r['symbol'])
        out = cf.eval_grid(m1, fd, pd.Timestamp(r['rt']), r['Atr_5'], geometry='geo')
        assert out is not None
        assert out['pnl_e0'] == r['pnl_m30_c16']


def test_insufficient_data_returns_none(ctx):
    cf, _ = ctx
    assert cf.eval_grid(None, None, pd.Timestamp('2026-01-01'), 0.02) is None
    assert cf.eval_grid(pd.DataFrame(), None, pd.Timestamp('2026-01-01'), 0.02) is None
    assert cf.eval_grid(None, None, pd.Timestamp('2026-01-01'), float('nan')) is None
```

先建空包：`touch tests/research/__init__.py`

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/research/test_cf_eval.py -x -q`
Expected: FAIL/ERROR（`cf_eval.py` 不存在，spec_from_file_location 报 FileNotFoundError）

- [ ] **Step 3: 写实现**

```python
# data/score_research_2026-07-21/cf_eval.py
"""反事实选币评价器核心(spec docs/superpowers/specs/2026-07-23-counterfactual-selection-evaluator-design.md):
对 (rt, symbol) 在已实现 [rt, rt+12h) 上跑双口径标定引擎——
  pnl_e0   纯网格无退出链(主分数=走势固有网格适配度)
  pnl_s030 全止损链(副刻度=系统实收)
geometry='v2' 生产几何(calc_grid_params_v2, config 现值);'geo' 锚模式(geo_sweep_run
的 m30_c16 公式,只用于对 geo_* 复现)。协议与 s030_calib/geo_sweep_run 逐位同构。
"""
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest.backtest_run import (_FUNDING_BACK_MS, holding_bars,
                                             pv_spike_for_window)
from gridtrade.backtest.sweep import (FEE_MAKER, FEE_TAKER, GEARING, MAX_RATE,
                                      _S, _STOP, _V2)
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v2

STOP_CFG = {'stop_loss': _STOP['stop_loss'], 'trailing_k': _STOP['trailing_k'],
            'trailing_floor': _STOP['trailing_floor'],
            'fundingRate_stop_loss': _STOP['fundingRate_stop_loss']}
PV_CFG = {'mult': _STOP['pv_mult'], 'n': _STOP['pv_n'], 'period': _STOP['pv_period']}


def prep_window(m1, rt):
    """12h 持仓窗 bars;数据不足(<600 根)返回 None——与 s030_calib/geo 同判。"""
    if m1 is None or m1.empty:
        return None
    bars = holding_bars(m1, pd.Timestamp(rt), _S['period'])
    if len(bars) < 600:
        return None
    return bars


def slice_funding(fd, bars):
    if fd is None or fd.empty:
        return fd
    lo = int(bars['candle_begin_time'].min().value // 1_000_000)
    hi = int(bars['candle_begin_time'].max().value // 1_000_000)
    return fd[(fd['ts'] >= lo - _FUNDING_BACK_MS) & (fd['ts'] <= hi)]


def gp_v2(atr5, close):
    """生产几何:V2 + config 现值,middle_5≈close(Stage E 备案)。"""
    gr = {'Atr_5': float(atr5), 'close': close, 'middle_5': close}
    return calc_grid_params_v2(gr, _S['price_limit'], _S['stop_limit'], _V2)


def gp_geo(atr5, close):
    """锚模式:geo_sweep_run.make_gp 的 m30_c16(clip 0.02~0.5,±1% buffer,固定16格)。
    只用于对 geo_* 复现;生产口径一律 gp_v2。"""
    r = min(max(3.0 * float(atr5), 0.02), 0.5)
    return {'high_price': close * (1 + r), 'low_price': close * (1 - r),
            'stop_high_price': close * (1 + r) * 1.01,
            'stop_low_price': close * (1 - r) * 0.99, 'grid_count': 16}


def run_engine(m1, bars, gp, fd, full_chain):
    """单口径引擎调用 → (pnl, reason)。True=s030 全链(逐参同 s030_calib);
    False=E0(逐参同 geo_sweep_run)。"""
    if full_chain:
        pv_df = pv_spike_for_window(m1, bars, PV_CFG)
        res = simulate_grid_engine(
            bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
            fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
            stop_cfg=STOP_CFG, funding_df=fd, pv_spike_df=pv_df,
            neutral_init=False, active_stop_mode='pv',
            pv_pnl_thr=_STOP['pv_pnl_thr'])
    else:
        res = simulate_grid_engine(
            bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
            fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
            stop_cfg=None, funding_df=fd, pv_spike_df=None,
            neutral_init=False, active_stop_mode='none')
    return float(res['pnl_ratio']), res.get('exit_reason', '?')


def eval_grid(m1, fd_all, rt, atr5, geometry='v2'):
    """一格双口径。返回 {'pnl_e0','reason_e0','pnl_s030','reason_s030'} 或 None。"""
    if atr5 is None or not np.isfinite(atr5):
        return None
    bars = prep_window(m1, rt)
    if bars is None:
        return None
    fd = slice_funding(fd_all, bars)
    close = float(bars['open'].iloc[0])
    gp = gp_v2(atr5, close) if geometry == 'v2' else gp_geo(atr5, close)
    p0, r0 = run_engine(m1, bars, gp, fd, full_chain=False)
    p1, r1 = run_engine(m1, bars, gp, fd, full_chain=True)
    return {'pnl_e0': p0, 'reason_e0': r0, 'pnl_s030': p1, 'reason_s030': r1}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/research/test_cf_eval.py -x -q`
Expected: `3 passed`（~1min，读 1m 缓存）。若锚测试 FAIL：这是保真度 bug（对照 `s030_calib.py`/`geo_sweep_run.py` 逐参 diff），**不许改判据**。

- [ ] **Step 5: Commit**

```bash
git add data/score_research_2026-07-21/cf_eval.py tests/research/__init__.py tests/research/test_cf_eval.py
git commit -m "feat(research): cf_eval双口径反事实核心——E0固有适配度+s030实收,单元锚逐位复现"
```

---

### Task 2: cf_anchor 全量锚门（2,796 格）

**Files:**
- Create: `data/score_research_2026-07-21/cf_anchor.py`

**Interfaces:**
- Consumes: `cf_eval.eval_grid`（Task 1）。
- Produces: 命令行门（exit 0=PASS）；`RD/ablation/cf_anchor_results.txt` 追加各窗结果行。这是 spec §7 复选框 1 的验收物。

- [ ] **Step 1: 写脚本**

```python
# data/score_research_2026-07-21/cf_anchor.py
"""锚门(P1 验收,spec §4/§7):同 2,796 标准格双口径逐位复现。
  锚A: eval_grid(geometry='v2').pnl_s030 == s030_calib_<win>.pnl
  锚B: eval_grid(geometry='geo').pnl_e0  == geo_<win>.pnl_m30_c16
附报 E0@V2−E0@geo 分布(V2 clamp 角落量化,不设门)。锚不平→查保真度,禁放宽。
用法: cf_anchor.py <W1|W2|OOS|IS> [limit]
"""
import importlib.util
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf_eval)


def main(wn, limit=None):
    s030 = pd.read_parquet('%s/ablation/s030_calib_%s.parquet' % (RD, wn))
    geo = pd.read_parquet('%s/ablation/geo_%s.parquet' % (RD, wn))
    pairs = s030[['rt', 'symbol', 'pnl']].merge(
        geo[['rt', 'symbol', 'Atr_5', 'pnl_m30_c16']], on=['rt', 'symbol'], how='inner')
    print('[%s] pairs=%d (s030=%d geo=%d)' % (wn, len(pairs), len(s030), len(geo)),
          flush=True)
    if limit is not None:
        pairs = pairs.head(limit)
    cache = ParquetCache(V.default_cache_root())
    m1_map, fd_map = {}, {}
    bad_a, bad_b, e0diff, n = [], [], [], 0
    for i, r in pairs.reset_index(drop=True).iterrows():
        sym, rt = r['symbol'], pd.Timestamp(r['rt'])
        m1 = m1_map.get(sym)
        if m1 is None:
            m1 = cache.read_all_days('1m', sym)
            m1_map[sym] = m1
        fd = fd_map.get(sym)
        if fd is None:
            fd = cache.read_all_days('funding', sym)
            fd_map[sym] = fd
        rv = cf_eval.eval_grid(m1, fd, rt, r['Atr_5'], geometry='v2')
        rg = cf_eval.eval_grid(m1, fd, rt, r['Atr_5'], geometry='geo')
        if rv is None:
            bad_a.append((r['rt'], sym, 'NONE', r['pnl']))
        elif rv['pnl_s030'] != r['pnl']:
            bad_a.append((r['rt'], sym, rv['pnl_s030'], r['pnl']))
        if rg is None:
            bad_b.append((r['rt'], sym, 'NONE', r['pnl_m30_c16']))
        elif rg['pnl_e0'] != r['pnl_m30_c16']:
            bad_b.append((r['rt'], sym, rg['pnl_e0'], r['pnl_m30_c16']))
        if rv is not None and rg is not None:
            n += 1
            e0diff.append(rv['pnl_e0'] - rg['pnl_e0'])
        if len(m1_map) > 120:
            m1_map.clear()
            fd_map.clear()
        if (i + 1) % 100 == 0:
            print('[%s] %d/%d badA=%d badB=%d' % (wn, i + 1, len(pairs),
                  len(bad_a), len(bad_b)), flush=True)
    d = pd.Series(e0diff, dtype=float) * 1e4
    line = ('[%s] n=%d | 锚A(s030) mismatch=%d | 锚B(geoE0) mismatch=%d | '
            'E0@V2−E0@geo bp: 中位 %+.2f p5 %+.1f p95 %+.1f 非零占比 %.2f'
            % (wn, n, len(bad_a), len(bad_b),
               d.median(), d.quantile(0.05), d.quantile(0.95), float((d != 0).mean())))
    print(line, flush=True)
    with open('%s/ablation/cf_anchor_results.txt' % RD, 'a') as f:
        f.write(line + '\n')
    for t in (bad_a[:5] + bad_b[:5]):
        print('  MISMATCH', t, flush=True)
    if bad_a or bad_b:
        sys.exit(1)
    print('[%s] ANCHOR PASS' % wn, flush=True)


if __name__ == '__main__':
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
```

- [ ] **Step 2: 冒烟（limit=20）**

Run: `.venv/bin/python data/score_research_2026-07-21/cf_anchor.py W1 20`
Expected: `[W1] ANCHOR PASS`，mismatch=0。记录单格耗时（printed 进度推算）。

- [ ] **Step 3: 四窗全量**

Run（串行即可，每窗数分钟）:
```bash
for w in W1 W2 OOS IS; do .venv/bin/python data/score_research_2026-07-21/cf_anchor.py $w || break; done
```
Expected: 四行 `ANCHOR PASS`；`cf_anchor_results.txt` 四行。任何 mismatch → 停下修保真度（先 diff 引擎参数与 `s030_calib.py`/`geo_sweep_run.py`），修完全量重跑。

- [ ] **Step 4: 勾 spec §7 复选框 1 并 Commit**

把 spec 文件 §7 第一项 `- [ ]` 改 `- [x]`。

```bash
git add data/score_research_2026-07-21/cf_anchor.py data/score_research_2026-07-21/ablation/cf_anchor_results.txt docs/superpowers/specs/2026-07-23-counterfactual-selection-evaluator-design.md
git commit -m "feat(research): cf_anchor锚门四窗PASS——2796格双口径逐位复现s030_calib/geo_m30_c16"
```

---

### Task 3: cf_run P1 驱动（逐轮全池反事实）

**Files:**
- Create: `data/score_research_2026-07-21/cf_run.py`

**Interfaces:**
- Consumes: `cf_eval.eval_grid`；`selection_replay.build_pit_candidates/load_full_series`；`pdetail_*.parquet`（选中）；因子面板（Atr_5）。
- Produces: `RD/ablation/cf_<win>.parquet`，cols：`run_time(Timestamp)/offset(int)/symbol/in_pool(bool)/picked(bool)/Atr_5(float)/pnl_e0/reason_e0/pnl_s030/reason_s030`。Task 4 只依赖这个 schema。

- [ ] **Step 1: 写脚本**

```python
# data/score_research_2026-07-21/cf_run.py
"""P1 驱动(spec §4 Phase1):逐选币轮全票池反事实双口径。
轮=(run_time,offset) 取自 pdetail_<win>(战役 byte 精确生产格=实际选中记录);
池=build_pit_candidates 生产语义(top55%+PIT,universe 已剔黑名单);
Atr_5 查 sc_factors/hold_factors 面板;选中币恒评(池外 in_pool=False 标记)。
产物 ablation/cf_<win>.parquet。用法: cf_run.py <WIN> [stride] [limit]
stride=每N轮取1(算力降采样,配对设计统计无损;正式跑按冒烟耗时定)。
"""
import importlib.util
import os
import sys
import time

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates, load_full_series
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf_eval)

WD = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
      'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
      'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30')}
FAC = {w: '%s/sc_factors_%s.parquet' % (RD, w) for w in ('W1', 'W2', 'OOS', 'IS')}
FAC.update({w: '%s/ablation/hold_factors_%s.parquet' % (RD, w)
            for w in ('HOLD-A', 'HOLD-B')})
TOP_VOLUME_PCT = 0.55        # 生产 env UNIVERSE_TOP_VOLUME_PCT 现值(spec §1)
M1_CAP = 320                 # 1m LRU 上限:须>单轮池币数(~260),否则轮内清缓存=IO 爆炸


def main(wn, stride=1, limit=None):
    out_p = '%s/ablation/cf_%s.parquet' % (RD, wn)
    if os.path.exists(out_p) and limit is None:
        print('[%s] SKIP(已有产物)' % wn, flush=True)
        return
    w0, w1 = WD[wn]
    pdet = pd.read_parquet('%s/ablation/pdetail_%s.parquet' % (RD, wn))
    fac = pd.read_parquet(FAC[wn])[['rt', 'symbol', 'Atr_5']]
    atr = {(pd.Timestamp(r.rt), r.symbol): float(r.Atr_5) for r in fac.itertuples()}
    rounds = pdet[['run_time', 'offset']].drop_duplicates().sort_values('run_time')
    rounds = rounds.iloc[::max(1, int(stride))]
    if limit is not None:
        rounds = rounds.head(limit)
    picks_by_rt = pdet.groupby('run_time')['symbol'].apply(set).to_dict()
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    syms = sorted(set(V.list_archive_symbols()) - set(bl))
    cache = ParquetCache(V.default_cache_root())
    lo1h = pd.Timestamp(w0) - pd.Timedelta(days=10)
    hi1h = pd.Timestamp(w1) + pd.Timedelta(days=2)
    series = load_full_series(cache, syms, '1h')
    for s_ in list(series):                       # 裁窗省内存
        df = series[s_]
        df = df[(df['candle_begin_time'] >= lo1h) & (df['candle_begin_time'] < hi1h)]
        if len(df) < 24:
            del series[s_]
        else:
            series[s_] = df.reset_index(drop=True)
    m1lo = pd.Timestamp(w0) - pd.Timedelta(days=2)
    m1hi = pd.Timestamp(w1) + pd.Timedelta(days=2)
    m1_map, fd_map = {}, {}
    rows, n_skip, t0 = [], 0, time.time()
    for i, rr in enumerate(rounds.itertuples()):
        rt = pd.Timestamp(rr.run_time)
        pool = set(build_pit_candidates(
            series, rt, max_candle_num=160, min_quote_volume=0.0,
            top_volume_pct=TOP_VOLUME_PCT, blacklist=()).keys())
        picks = picks_by_rt.get(rr.run_time, set())
        for sym in sorted(pool | picks):
            a5 = atr.get((rt, sym))
            if a5 is None or not np.isfinite(a5):
                n_skip += 1
                continue
            m1 = m1_map.get(sym)
            if m1 is None:
                m1 = cache.read_all_days('1m', sym)
                if m1 is not None and not m1.empty:
                    m1 = m1[(m1['candle_begin_time'] >= m1lo)
                            & (m1['candle_begin_time'] < m1hi)].reset_index(drop=True)
                m1_map[sym] = m1
            fd = fd_map.get(sym)
            if fd is None:
                fd = cache.read_all_days('funding', sym)
                fd_map[sym] = fd
            try:
                out = cf_eval.eval_grid(m1, fd, rt, a5, geometry='v2')
            except Exception:
                n_skip += 1
                continue
            if out is None:
                n_skip += 1
                continue
            rows.append({'run_time': rt, 'offset': int(rr.offset), 'symbol': sym,
                         'in_pool': sym in pool, 'picked': sym in picks,
                         'Atr_5': a5, **out})
        if len(m1_map) > M1_CAP:
            m1_map.clear()
            fd_map.clear()
        if (i + 1) % 10 == 0:
            print('[%s] 轮 %d/%d 行=%d skip=%d %.1fs/轮'
                  % (wn, i + 1, len(rounds), len(rows), n_skip,
                     (time.time() - t0) / (i + 1)), flush=True)
    df = pd.DataFrame(rows)
    if limit is None:
        df.to_parquet(out_p)
    n_out = int((df['picked'] & ~df['in_pool']).sum()) if len(df) else 0
    print('[%s] DONE 轮=%d 行=%d skip=%d 池外选中=%d' % (wn, len(rounds), len(df),
          n_skip, n_out), flush=True)


if __name__ == '__main__':
    main(sys.argv[1],
         int(sys.argv[2]) if len(sys.argv) > 2 else 1,
         int(sys.argv[3]) if len(sys.argv) > 3 else None)
```

- [ ] **Step 2: 冒烟（3 轮）验 schema 与选中覆盖**

Run: `.venv/bin/python data/score_research_2026-07-21/cf_run.py W1 1 3`
Expected: 打印 `DONE 轮=3`，行数 ≈ 3×(池币数~200-280)，`池外选中` 打印（应为 0 或个位数）。记录 `s/轮`（首轮含缓存冷启动会偏慢，看第 2-3 轮）。若 s/轮 > 30，先检查是否轮内在反复清 m1_map（M1_CAP 调大），不是则接受并在 Step 4 用 stride。

- [ ] **Step 3: Commit 脚本**

```bash
git add data/score_research_2026-07-21/cf_run.py
git commit -m "feat(research): cf_run逐轮全池反事实驱动——pdetail选中+生产票池语义+因子面板Atr"
```

- [ ] **Step 4: 发起六窗全量（后台，过夜）**

按冒烟耗时定 stride：目标单窗 ≤6h（估算 = s/轮 × 1464/stride；IS 是 4 个月窗 ≈ 2928 轮，单独给更大 stride 或接受更久）。默认先试 stride=1；超预算用 2（HOLD 窗轮数少，恒 stride=1）。**并行 ≤2 个窗**（每窗峰值内存 ~4-6GB，1m 缓存）：

```bash
cd /Users/thomaschang/Projects/GridTradeBi
nohup .venv/bin/python data/score_research_2026-07-21/cf_run.py W1 1 > data/score_research_2026-07-21/ablation/cf_run_W1.log 2>&1 &
nohup .venv/bin/python data/score_research_2026-07-21/cf_run.py W2 1 > data/score_research_2026-07-21/ablation/cf_run_W2.log 2>&1 &
# W1/W2 完成后依次: OOS, IS, HOLD-A, HOLD-B(同样两两并行)
```
Expected: 各窗 log 末行 `DONE`；产物 `cf_<win>.parquet` 六个。**此步与 Task 4 的代码步并行推进**（Task 4 用冒烟产物开发，全量到齐后再出正式成绩单）。

---

### Task 4: cf_report 成绩单 + A/B 比较

**Files:**
- Create: `data/score_research_2026-07-21/cf_report.py`
- Test: `tests/research/test_cf_report.py`

**Interfaces:**
- Consumes: `cf_<win>.parquet`（Task 3 schema）、标签 parquet、`score_eval_weights.json`。
- Produces: 纯函数 `per_round_metrics(cf: DataFrame) -> DataFrame`（cols: rt/k/alpha_e0/alpha_s030/hit/regret/pool_med/pool_top）、`aggregate(d: DataFrame, cf: DataFrame) -> dict`、`diagnostics(cf, lab) -> dict`、`ab_compare(cf, picks_a, picks_b) -> dict`；CLI 出 `RD/ablation/cf_results.txt`。P2（Task 6）复用前两个纯函数。

- [ ] **Step 1: 写失败测试（合成数据，指标数学）**

```python
# tests/research/test_cf_report.py
"""cf_report 指标数学(合成数据,无资产依赖):alpha/捕获/遗憾/ab_compare 已知答案。"""
import importlib.util
import os

import pandas as pd
import pytest

RD = 'data/score_research_2026-07-21'

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(RD, 'cf_report.py')),
    reason='cf_report not present')


def _mod():
    spec = importlib.util.spec_from_file_location(
        'cf_report', os.path.join(RD, 'cf_report.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _toy_cf():
    rows = []
    # 轮1: 池 A:+10bp B:0 C:-10bp,选中 A(=top1) → alpha=+10bp, hit=1, regret=0
    # 轮2: 同池,选中 B → alpha=0, hit=0, regret=+10bp
    for rt, pick in (('2026-01-01 00:00', 'A'), ('2026-01-01 01:00', 'B')):
        for sym, e0 in (('A', 0.0010), ('B', 0.0), ('C', -0.0010)):
            rows.append({'run_time': pd.Timestamp(rt), 'offset': 0, 'symbol': sym,
                         'in_pool': True, 'picked': sym == pick, 'Atr_5': 0.02,
                         'pnl_e0': e0, 'reason_e0': '窗口结束',
                         'pnl_s030': e0 / 2, 'reason_s030': 'x'})
    return pd.DataFrame(rows)


def test_per_round_metrics_math():
    m = _mod()
    d = m.per_round_metrics(_toy_cf())
    assert len(d) == 2
    r1, r2 = d.iloc[0], d.iloc[1]
    assert r1['hit'] == 1 and abs(r1['alpha_e0'] - 0.0010) < 1e-12
    assert abs(r1['regret']) < 1e-12
    assert r2['hit'] == 0 and abs(r2['regret'] - 0.0010) < 1e-12
    assert abs(r2['alpha_e0']) < 1e-12


def test_aggregate_tax_and_capture():
    m = _mod()
    cf = _toy_cf()
    agg = m.aggregate(m.per_round_metrics(cf), cf)
    assert agg['capture'] == 0.5                       # 2 轮命中 1
    # 止损链税 = e0 − s030 = e0/2;选中桶 {+10bp,0} → 税均 +2.5bp
    assert abs(agg['tax_picks_bp'] - 2.5) < 1e-9
    assert agg['picks_outside_pool'] == 0


def test_ab_compare_disjoint_only():
    m = _mod()
    cf = _toy_cf()
    rt1, rt2 = pd.Timestamp('2026-01-01 00:00'), pd.Timestamp('2026-01-01 01:00')
    pa = pd.DataFrame({'run_time': [rt1, rt2], 'symbol': ['A', 'B']})
    pb = pd.DataFrame({'run_time': [rt1, rt2], 'symbol': ['A', 'C']})
    r = m.ab_compare(cf, pa, pb)
    assert r['n_disjoint'] == 2                        # (rt2,B) vs (rt2,C)
    assert abs(r['mean_diff_bp'] - 10.0) < 1e-9        # 0 − (−10bp)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/research/test_cf_report.py -x -q`
Expected: 全部 SKIP（cf_report.py 不存在）→ 建空文件后 FAIL。直接进 Step 3。

- [ ] **Step 3: 写实现**

```python
# data/score_research_2026-07-21/cf_report.py
"""P1 成绩单(spec §3):六窗 选币alpha/捕获率/遗憾/止损链税/池体温+诊断。
排序类指标一律主口径 pnl_e0;s030 并读。A/B 比较只算分歧格(共同选中精确抵消)。
用法: cf_report.py            # 有 cf_<win>.parquet 的窗全出,写 ablation/cf_results.txt
"""
import json
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import numpy as np
import pandas as pd

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
WINS = ['W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B']
LAB = {w: '%s/sc_labels_%s.parquet' % (RD, w) for w in ('W1', 'W2', 'OOS', 'IS')}
LAB.update({w: '%s/ablation/hold_labels_%s.parquet' % (RD, w)
            for w in ('HOLD-A', 'HOLD-B')})


def per_round_metrics(cf):
    """逐轮:alpha(选中−池均)/hit(∩真实topK)/regret(topK−选中)/池体温。主口径 pnl_e0。"""
    out = []
    for rt, g in cf.groupby('run_time'):
        gp = g[g['in_pool']]
        pk = g[g['picked']]
        if gp.empty or pk.empty:
            continue
        k = len(pk)
        top = gp.nlargest(k, 'pnl_e0')
        out.append({'rt': rt, 'k': k,
                    'alpha_e0': pk['pnl_e0'].mean() - gp['pnl_e0'].mean(),
                    'alpha_s030': pk['pnl_s030'].mean() - gp['pnl_s030'].mean(),
                    'hit': len(set(pk['symbol']) & set(top['symbol'])),
                    'regret': top['pnl_e0'].mean() - pk['pnl_e0'].mean(),
                    'pool_med': gp['pnl_e0'].median(),
                    'pool_top': top['pnl_e0'].mean()})
    return pd.DataFrame(out)


def aggregate(d, cf):
    """窗级聚合(bp 口径)。"""
    pk = cf[cf['picked']]
    pl = cf[cf['in_pool']]
    return {'rounds': len(d),
            'alpha_e0_bp': d['alpha_e0'].mean() * 1e4,
            'alpha_s030_bp': d['alpha_s030'].mean() * 1e4,
            'capture': d['hit'].sum() / d['k'].sum(),
            'regret_bp': d['regret'].mean() * 1e4,
            'pool_med_bp': d['pool_med'].mean() * 1e4,
            'pool_top_bp': d['pool_top'].mean() * 1e4,
            'tax_picks_bp': (pk['pnl_e0'] - pk['pnl_s030']).mean() * 1e4,
            'tax_pool_bp': (pl['pnl_e0'] - pl['pnl_s030']).mean() * 1e4,
            'picks_outside_pool': int((cf['picked'] & ~cf['in_pool']).sum())}


def diagnostics(cf, lab):
    """选中桶 vs 池的燃料/毒药 z、汇率(z_drift/z_cross1,平衡线0.54)、calib 分(bp)。"""
    j = cf.merge(lab.rename(columns={'rt': 'run_time'}),
                 on=['run_time', 'symbol'], how='left')
    pool = j[j['in_pool']].copy()
    for c in ('cross1', 'drift'):
        g = pool.groupby('run_time')[c]
        pool['z_' + c] = (pool[c] - g.transform('mean')) \
            / g.transform('std').replace(0, np.nan)
    pk = pool[pool['picked']]
    zc, zd = float(pk['z_cross1'].mean()), float(pk['z_drift'].mean())
    w = json.load(open('%s/ablation/score_eval_weights.json' % RD))['weights_bp_per_sigma']
    rate = zd / zc if np.isfinite(zc) and abs(zc) > 1e-9 else np.nan
    return {'z_cross1': zc, 'z_drift': zd, 'rate': rate,
            'calib_bp': w['cross1'] * zc + w['drift'] * zd}


def ab_compare(cf, picks_a, picks_b):
    """A/B 选币器配对比较(spec §3):只算分歧格 E0 差。picks_*: DataFrame[run_time,symbol]。"""
    a = set(map(tuple, picks_a[['run_time', 'symbol']].values))
    b = set(map(tuple, picks_b[['run_time', 'symbol']].values))
    px = cf.set_index(['run_time', 'symbol'])['pnl_e0']
    va = np.array([px[t] for t in a - b if t in px.index])
    vb = np.array([px[t] for t in b - a if t in px.index])
    if not len(va) or not len(vb):
        return {'n_disjoint': 0, 'mean_diff_bp': np.nan, 'se_bp': np.nan}
    diff = va.mean() - vb.mean()
    se = np.sqrt(va.var(ddof=1) / len(va) + vb.var(ddof=1) / len(vb)) \
        if len(va) > 1 and len(vb) > 1 else np.nan
    return {'n_disjoint': len(va) + len(vb), 'mean_diff_bp': diff * 1e4,
            'se_bp': se * 1e4 if np.isfinite(se) else np.nan}


def main():
    lines = ['win     rounds alpha_e0 capture regret pool_med pool_top tax_pk/pl '
             'alpha_s030 汇率 calib 池外']
    for wn in WINS:
        p = '%s/ablation/cf_%s.parquet' % (RD, wn)
        if not os.path.exists(p):
            continue
        cf = pd.read_parquet(p)
        d = per_round_metrics(cf)
        a = aggregate(d, cf)
        dg = diagnostics(cf, pd.read_parquet(LAB[wn]))
        lines.append('%-7s %5d %+7.1fbp %5.2f %+6.1fbp %+7.1fbp %+7.1fbp '
                     '%+4.1f/%+4.1f %+7.1fbp %5.2f %+6.1fbp %3d'
                     % (wn, a['rounds'], a['alpha_e0_bp'], a['capture'],
                        a['regret_bp'], a['pool_med_bp'], a['pool_top_bp'],
                        a['tax_picks_bp'], a['tax_pool_bp'], a['alpha_s030_bp'],
                        dg['rate'], dg['calib_bp'], a['picks_outside_pool']))
    txt = '\n'.join(lines)
    print(txt, flush=True)
    with open('%s/ablation/cf_results.txt' % RD, 'w') as f:
        f.write(txt + '\n')


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/research/test_cf_report.py tests/research/test_cf_eval.py -q`
Expected: `6 passed`（含 Task 1 的 3 个）。

- [ ] **Step 5: Commit 代码**

```bash
git add data/score_research_2026-07-21/cf_report.py tests/research/test_cf_report.py
git commit -m "feat(research): cf_report成绩单——alpha/捕获/遗憾/止损税/池体温+AB分歧格比较"
```

- [ ] **Step 6: 全量到齐后出六窗成绩单（P1 验收）**

前置：Task 3 Step 4 的六个 `cf_<win>.parquet` 全部 DONE。

Run: `.venv/bin/python data/score_research_2026-07-21/cf_report.py`
Expected: 六行成绩单 + `ablation/cf_results.txt`。健全性目测（不设门）：`capture` 应显著高于随机基线 K/池币数（~1/250≈0.004）；`tax` 应为正且量级 ~2-3bp（E0 3.5 vs E4 1.0 的先验）；`池外选中` 小。异常大的偏离先怀疑 join/键（run_time vs rt 时区/类型）。

- [ ] **Step 7: 勾 spec §7 复选框 2 并 Commit 产物**

```bash
git add data/score_research_2026-07-21/ablation/cf_results.txt docs/superpowers/specs/2026-07-23-counterfactual-selection-evaluator-design.md
git commit -m "feat(research): 现役rank_sum六窗反事实成绩单——P1交付(spec复选框2)"
```

---

### Task 5: P2 dump 脚本 + cf_patrol 取数构造器

**Files:**
- Create: `scripts/dump_selection_snapshots.py`
- Create: `scripts/cf_patrol.py`
- Test: `tests/research/test_cf_patrol.py`

**Interfaces:**
- Consumes: `cf_eval.eval_grid`、`cf_report.per_round_metrics/aggregate`（importlib by path）、`build_pit_candidates`、`proceed_calc_symbol_factor`（`gridtrade.core.selection`）、`CANDLE_COLS`（`gridtrade.exchanges.base`）。
- Produces: `dump_selection_snapshots.py`（容器内跑，stdout JSON：`[{exchange,run_time,offset,ranked,picks}]`）；`cf_patrol.py` CLI（见 Step 3 docstring）+ 纯构造器 `klines_to_df(rows) -> DataFrame`、`funding_to_df(sym, rows) -> DataFrame`（供测试）。

- [ ] **Step 1: 写 dump 脚本（照 dump_live_grids 模式）**

```python
# scripts/dump_selection_snapshots.py
"""容器内 dump selection_snapshots → JSON(供 scripts/cf_patrol.py 本地反事实巡检)。
用法: flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_selection_snapshots.py > snaps.json
env SNAP_DAYS 回看天数(默认 2)。⚠ranked 实为选中币(triggers.py 写 select_grid_coin
输出,非全池)——票池由 cf_patrol 按选币同规则 PIT 重建(spec §4)。
"""
import json
import os
import time

from sqlalchemy import create_engine, text

url = os.environ['DATABASE_URL']
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql://', 1)
days = float(os.environ.get('SNAP_DAYS', '2'))
lo = int((time.time() - days * 86400) * 1000)
out = []
with create_engine(url).connect() as c:
    q = text('SELECT exchange, run_time, "offset", ranked, picks '
             'FROM selection_snapshots WHERE run_time >= :lo ORDER BY run_time')
    for r in c.execute(q, {'lo': lo}).mappings():
        out.append({'exchange': r['exchange'], 'run_time': int(r['run_time']),
                    'offset': int(r['offset']), 'ranked': json.loads(r['ranked']),
                    'picks': json.loads(r['picks'])})
print(json.dumps(out))
```

- [ ] **Step 2: 写构造器失败测试（无网络）**

```python
# tests/research/test_cf_patrol.py
"""cf_patrol 纯构造器:fapi kline/funding 行 → 引擎可消费 DataFrame(合成数据,无网络)。"""
import importlib.util
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.path.exists('scripts/cf_patrol.py'), reason='cf_patrol not present')


def _mod():
    spec = importlib.util.spec_from_file_location('cf_patrol', 'scripts/cf_patrol.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_klines_to_df_columns_and_types():
    m = _mod()
    # fapi kline 数组: [openTime,open,high,low,close,volume,closeTime,quoteVolume,...]
    rows = [[1753228800000, '1.0', '1.2', '0.9', '1.1', '100', 0, '110', 5, '50', '55', '0'],
            [1753228860000, '1.1', '1.3', '1.0', '1.2', '200', 0, '230', 6, '90', '99', '0']]
    df = m.klines_to_df('ABC/USDT:USDT', rows)
    from gridtrade.exchanges.base import CANDLE_COLS
    assert list(df.columns) == list(CANDLE_COLS)   # 9列: symbol..vol/volCcy/quote_volume
    # 1753228800000ms = 2025-07-23 00:00 UTC
    assert df['candle_begin_time'].iloc[0].strftime('%Y-%m-%d %H:%M') == '2025-07-23 00:00'
    assert float(df['quote_volume'].iloc[1]) == 230.0
    assert float(df['vol'].iloc[1]) == 200.0
    assert float(df['volCcy'].iloc[1]) == 200.0    # binance.py:241: volCcy=vol


def test_funding_to_df_schema():
    m = _mod()
    rows = [{'fundingTime': 1753228800000, 'fundingRate': '-0.0001'}]
    df = m.funding_to_df('ABC/USDT:USDT', rows)
    assert list(df.columns) == ['ts', 'symbol', 'fundingRate', 'realizedRate']
    assert df['ts'].iloc[0] == 1753228800000
    assert abs(df['fundingRate'].iloc[0] + 0.0001) < 1e-12
```

Run: `.venv/bin/python -m pytest tests/research/test_cf_patrol.py -x -q`
Expected: SKIP（脚本不存在）。

- [ ] **Step 3: 写 cf_patrol.py**

```python
# scripts/cf_patrol.py
"""P2 实盘反事实巡检(spec §4 Phase2):对已收盘的实盘选币轮重建票池、逐币双口径
反事实,出当日成绩单 + 选中币对账本 Δ(锚:pv 格复现,Δ中位≤0.5pp)。

用法:
  # ① 容器内 dump(两份):
  flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_selection_snapshots.py > snaps.json
  RECON_ALL=  flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_live_grids.py > grids.json
  # ② 本地巡检(公开 fapi 拉行情,本机 IP 权重与 prod 隔离,仍 250ms pace):
  .venv/bin/python scripts/cf_patrol.py snaps.json grids.json

票池重建=选币同规则(spec fallback:snapshots.ranked 只存选中):fapi exchangeInfo
USDT 本位永续 TRADING − 黑名单 → 1h klines → build_pit_candidates(top55%)。
Atr_5=proceed_calc_symbol_factor 生产因子路径(recon_live 同款)。
只处理 rt+12h 已收盘的轮。产物 data/score_research_2026-07-21/ablation/cf_live_<日期>.parquet。
"""
import contextlib
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest.selection_replay import build_pit_candidates
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.selection import proceed_calc_symbol_factor
from gridtrade.core.tier_policy import effective_blacklist
from gridtrade.exchanges.base import CANDLE_COLS

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
API = 'https://fapi.binance.com/fapi/v1'
TOP_VOLUME_PCT = 0.55
_spec = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf_eval)
_spec2 = importlib.util.spec_from_file_location('cf_report', RD + '/cf_report.py')
cf_report = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(cf_report)


def _get(path, **q):
    time.sleep(0.25)                                  # paced,与选币取数节流同规
    url = '%s/%s?%s' % (API, path, urllib.parse.urlencode(q))
    return json.load(urllib.request.urlopen(url, timeout=20))


def fsym(s):
    return s.split('/')[0] + 'USDT'


def klines_to_df(sym, rows):
    """fapi kline 数组 → CANDLE_COLS(9列) DataFrame。映射与 exchanges/binance.py 同:
    vol=第5列基础量, volCcy=vol(binance.py:241), quote_volume=第7列真 qv。"""
    df = pd.DataFrame([{
        'symbol': sym,
        'candle_begin_time': pd.to_datetime(int(k[0]), unit='ms'),
        'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]),
        'close': float(k[4]), 'vol': float(k[5]), 'volCcy': float(k[5]),
        'quote_volume': float(k[7]),
    } for k in rows])
    if df.empty:
        return df
    return (df.drop_duplicates('candle_begin_time')
            .sort_values('candle_begin_time').reset_index(drop=True)[list(CANDLE_COLS)])


def funding_to_df(sym, rows):
    return pd.DataFrame([{'ts': int(k['fundingTime']), 'symbol': sym,
                          'fundingRate': float(k['fundingRate']),
                          'realizedRate': float(k['fundingRate'])} for k in rows],
                        columns=['ts', 'symbol', 'fundingRate', 'realizedRate'])


def fetch_klines(sym, interval, start_ms, end_ms):
    rows, cur = [], start_ms
    while cur < end_ms:
        ks = _get('klines', symbol=fsym(sym), interval=interval,
                  startTime=cur, endTime=end_ms, limit=1500)
        if not ks:
            break
        rows.extend(ks)
        nxt = int(ks[-1][0]) + 1
        if nxt <= cur or len(ks) < 1500:
            break
        cur = nxt
    return klines_to_df(sym, rows)


def fetch_universe():
    info = _get('exchangeInfo')
    syms = ['%s/USDT:USDT' % s['baseAsset'] for s in info['symbols']
            if s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT'
            and s.get('status') == 'TRADING']
    bl = set(effective_blacklist((), DEFAULT_TIER_POLICY))
    return sorted(set(syms) - bl)


def main(snaps_path, grids_path):
    snaps = json.load(open(snaps_path))
    grids = json.load(open(grids_path))
    now_ms = int(time.time() * 1000)
    rounds = [s for s in snaps if s['run_time'] + 12 * 3600 * 1000 + 300000 < now_ms]
    if not rounds:
        print('无已收盘轮', flush=True)
        return
    lo_rt = min(s['run_time'] for s in rounds)
    hi_rt = max(s['run_time'] for s in rounds)
    syms = fetch_universe()
    print('universe=%d 轮=%d' % (len(syms), len(rounds)), flush=True)
    h1_lo = lo_rt - 200 * 3600 * 1000                 # 160根max_candle_num+24h qv+余量
    series = {}
    for s_ in syms:
        df = fetch_klines(s_, '1h', h1_lo, hi_rt)
        if len(df) >= 24:
            series[s_] = df
    m1_lo = lo_rt - 27 * 3600 * 1000                  # pv 基线 (n+8)×15min=27h
    m1_hi = hi_rt + 12 * 3600 * 1000 + 60000
    m1_map, fd_map = {}, {}
    rows = []
    devnull = open(os.devnull, 'w')
    for s in rounds:
        rt = pd.to_datetime(s['run_time'], unit='ms')
        pool_c = build_pit_candidates(series, rt, max_candle_num=160,
                                      min_quote_volume=0.0,
                                      top_volume_pct=TOP_VOLUME_PCT, blacklist=())
        # 选中真值=实际开出的格(grids 按 offset+created_at≈rt 匹配;快照 picks 并读)
        picked = {g['symbol'] for g in grids
                  if int(g['offset']) == int(s['offset'])
                  and abs(int(g['created_at']) - s['run_time']) < 600000}
        snap_picks = {r['symbol'] for r in s['ranked']}
        with contextlib.redirect_stdout(devnull):
            fdf = proceed_calc_symbol_factor(
                {k: v.copy() for k, v in pool_c.items()}, rt, '12H', int(s['offset']))
        if fdf is None or fdf.empty:
            continue
        atr = dict(zip(fdf['symbol'], fdf['Atr_5']))
        for sym in sorted(set(pool_c) | picked):
            a5 = atr.get(sym)
            if a5 is None or not np.isfinite(a5):
                continue
            m1 = m1_map.get(sym)
            if m1 is None:
                m1 = fetch_klines(sym, '1m', m1_lo, m1_hi)
                m1_map[sym] = m1
            fd = fd_map.get(sym)
            if fd is None:
                fd = funding_to_df(sym, _get('fundingRate', symbol=fsym(sym),
                                             startTime=m1_lo - 86400000,
                                             endTime=m1_hi, limit=1000))
                fd_map[sym] = fd
            out = cf_eval.eval_grid(m1, fd, rt, float(a5), geometry='v2')
            if out is None:
                continue
            rows.append({'run_time': rt, 'offset': int(s['offset']), 'symbol': sym,
                         'in_pool': sym in pool_c, 'picked': sym in picked,
                         'snap_pick': sym in snap_picks, 'Atr_5': float(a5), **out})
    devnull.close()
    cf = pd.DataFrame(rows)
    tag = pd.to_datetime(lo_rt, unit='ms').strftime('%Y-%m-%d')
    cf.to_parquet('%s/ablation/cf_live_%s.parquet' % (RD, tag))
    d = cf_report.per_round_metrics(cf)
    a = cf_report.aggregate(d, cf)
    print('[%s] 轮=%d alpha_e0=%+.1fbp capture=%.2f regret=%+.1fbp 池中位=%+.1fbp '
          '税(选/池)=%+.1f/%+.1fbp 池外选中=%d'
          % (tag, a['rounds'], a['alpha_e0_bp'], a['capture'], a['regret_bp'],
             a['pool_med_bp'], a['tax_picks_bp'], a['tax_pool_bp'],
             a['picks_outside_pool']), flush=True)
    # 账本对照(锚,只覆盖 s030 口径):CLOSED 格 Δ=sim−live
    dd = []
    for g in grids:
        m = cf[(cf['picked']) & (cf['symbol'] == g['symbol'])
               & (cf['offset'] == int(g['offset']))
               & (abs(cf['run_time'].astype('int64') // 10**6
                      - int(g['created_at'])) < 600000)]
        if len(m) == 1 and g.get('pnl_ratio') is not None:
            dd.append({'symbol': g['symbol'], 'offset': int(g['offset']),
                       'sim': float(m['pnl_s030'].iloc[0]),
                       'live': float(g['pnl_ratio']),
                       'reason_sim': m['reason_s030'].iloc[0],
                       'reason_live': g.get('close_reason', '?')})
    if dd:
        ddf = pd.DataFrame(dd)
        ddf['delta_pp'] = (ddf['sim'] - ddf['live']) * 100
        med = ddf['delta_pp'].abs().median()
        print('账本对照 n=%d |Δ|中位=%.3fpp max=%.3fpp 判线≤0.5pp: %s'
              % (len(ddf), med, ddf['delta_pp'].abs().max(),
                 'PASS' if med <= 0.5 else 'FAIL'), flush=True)
        print(ddf.to_string(index=False), flush=True)
    else:
        print('账本对照: 无可匹配 CLOSED 格', flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 4: 跑构造器测试确认通过**

Run: `.venv/bin/python -m pytest tests/research/test_cf_patrol.py -x -q`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/dump_selection_snapshots.py scripts/cf_patrol.py tests/research/test_cf_patrol.py
git commit -m "feat(research): cf_patrol实盘反事实巡检——快照dump+票池重建+fapi真qv取数+账本对照"
```

---

### Task 6: P2 干跑一日（验收）

**Files:**
- 无新文件；产出 `RD/ablation/cf_live_<日期>.parquet` + 巡检输出存档 `RD/ablation/cf_patrol_<日期>.log`

**Interfaces:**
- Consumes: Task 5 全部；prod 容器访问（`flyctl`）。
- Produces: spec §7 复选框 3、4 的验收证据。

- [ ] **Step 1: dump 两份 JSON**

```bash
cd /Users/thomaschang/Projects/GridTradeBi
flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_selection_snapshots.py > /tmp/snaps.json
flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_live_grids.py > /tmp/grids.json
python3 -c "import json;s=json.load(open('/tmp/snaps.json'));print(len(s),'snaps')"
```
Expected: snaps ≥ 24（近 2 天）。若 flyctl 输出混入非 JSON 行导致 parse 失败，取输出中最后一个完整 JSON 行（dump_live_grids 使用同管道，先例可跑通）。

- [ ] **Step 2: 跑巡检（存档输出）**

```bash
.venv/bin/python scripts/cf_patrol.py /tmp/snaps.json /tmp/grids.json 2>&1 | tee data/score_research_2026-07-21/ablation/cf_patrol_$(date +%F).log
```
Expected（判据=spec §7）:
- 跑完无 429/-1003 报错（250ms pace + 本机 IP 预算，理论余量大）。
- 成绩单一行输出 + `cf_live_*.parquet` 落盘。
- 账本对照 `判线≤0.5pp: PASS`，pv 止损格 `reason_sim/reason_live` 一致（对照 recon 先例 5/5）。
- **FAIL 处置**：逐格看 Δ 大的——可解释残差（maker 费差 1.8vs4.5bp、lot 取整、live qv 代理 pv 边缘触发差）记录进 log 即可；不可解释的（bars 对不上/引擎参数漂移）= 保真度 bug，回 Task 1/5 修。**不许挪 0.5pp 门柱**。

- [ ] **Step 3: 勾 spec §7 复选框 3、4，Commit 验收产物**

```bash
git add data/score_research_2026-07-21/ablation/cf_patrol_*.log docs/superpowers/specs/2026-07-23-counterfactual-selection-evaluator-design.md
git commit -m "feat(research): P2干跑验收——实盘窗账本对照PASS,反事实巡检全链路打通"
```

---

## Self-Review 已核对

- **Spec 覆盖**：§2 双口径核心=Task 1；§4 P1 锚=Task 2、驱动=Task 3、成绩单(§3 全指标含 A/B)=Task 4；§4 P2 dump/重建池/取数/账本锚=Task 5-6；§6 边界写进各脚本 docstring；§7 四个复选框分别由 Task 2/4/6 勾。
- **口径一致**：`eval_grid` 返回键、`cf_<win>.parquet` schema、`per_round_metrics` 列名在 Task 3/4/5/6 间逐字一致。
- **已知风险明示**：pdetail pnl 非锚（战役口径差异）；ranked=选中非全池（票池重建 fallback）；IS 窗 2 倍轮数（stride 决策规则给出）；内存 M1_CAP>池币数的原因写在常量注释。
