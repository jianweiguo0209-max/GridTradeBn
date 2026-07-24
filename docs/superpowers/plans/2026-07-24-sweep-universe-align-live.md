# sweep 票池对齐实盘 + s030 对齐池复测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提取共享票池构建器 `resolve_bt_universe`（COIN-only+低杠杆过滤），sweep_run 接线复用；随后在同一 harness 上做 s030 六窗宽池/对齐池 A/B 复测。

**Architecture:** `backtest_run.main()` 内联票池块原样搬移为模块级函数（注入点 `archive_symbols`/`min_lev` 便于测试，默认行为逐字节不变）；sweep_run 删自建裸池改调共享函数；复测走 sweep 自身机件（select_grids→warm 1m/funding→preload_window→run_arm(baseline)→metrics），宽/对齐两池同码同数据 A/B。

**Tech Stack:** Python 3.9、pytest、既有 sweep/backtest 基建、Vision ParquetCache。

**Spec:** `docs/superpowers/specs/2026-07-24-sweep-universe-align-live-design.md`

## Global Constraints

- Python 3.9 兼容；注释中文讲"为什么"
- `resolve_bt_universe` 默认路径行为与现 `main()` 票池块**逐字节等价**（打印统计行含语义不变）
- 杠杆档私有端点不可用 → **fail-loud 拒跑**（现 exclude_low_leverage 语义不变）；`BT_MIN_LEVERAGE=0` 显式停用
- score_research 归档脚本（data/score_research_2026-07-21/）**一个字不改**
- 复测脚本必须：`if __name__ == '__main__'` 守卫 + `import gridtrade.backtest`（锁线程）+ `BT_WORKERS≤4`（本地死机坑，memory 在案）
- TDD；提交尾注 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- 当前在 `snapshot-reads-ttl` 分支干活（并行会话占工作区），提交后 `git push . HEAD:main` 同步 main；**本改动不部署 prod**（纯回测侧）

---

### Task 1: 提取 `resolve_bt_universe`（含 main() 重构等价）

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（新函数 + `main()` :628-640 改调）
- Test: `tests/backtest/test_backtest_run.py`（追加）

**Interfaces:**
- Consumes: 既有 `exclude_non_coin(symbols, adapter)`、`exclude_low_leverage(symbols, tiers_fetch, *, notional, gearing, min_lev, log)`、`BT_STRATEGY['leverage']`
- Produces: `resolve_bt_universe(adapter, blacklist, *, archive_symbols=None, min_lev=None, log=print) -> (universe: list[str], stats: dict)`；stats 键=`{'n_blacklist','n_tradfi','n_lowlev','min_lev'}`。Task 2/3 依赖此签名。

- [ ] **Step 1: Write the failing tests**

`tests/backtest/test_backtest_run.py` 末尾追加（fake 形状抄同文件 :53-66 的 exclude_* 测试与 tests/exchanges 的 FakeBinanceClient 惯例）：

```python
class _UniClient:
    """resolve_bt_universe 契约桩：markets 含 COIN/非COIN,杠杆档含高/低杠杆。"""
    def __init__(self):
        self.markets = {
            'AAA/USDT:USDT': {'symbol': 'AAA/USDT:USDT', 'swap': True, 'settle': 'USDT',
                              'info': {'underlyingType': 'COIN'}},
            'TRAD/USDT:USDT': {'symbol': 'TRAD/USDT:USDT', 'swap': True, 'settle': 'USDT',
                               'info': {'underlyingType': 'EQUITY'}},   # TradFi → 剔
            'LOW/USDT:USDT': {'symbol': 'LOW/USDT:USDT', 'swap': True, 'settle': 'USDT',
                              'info': {'underlyingType': 'COIN'}},      # 低杠杆 → 剔
        }

    def load_markets(self):
        return self.markets

    def fetch_leverage_tiers(self, symbols=None, params=None):
        # 档位表:AAA 高杠杆(50x@任意名义), LOW 低杠杆(5x);退市 DEAD 不在表 → 保留
        def tier(lev):
            return [{'maxNotional': 1e12, 'maxLeverage': lev,
                     'info': {'initialLeverage': str(lev), 'notionalCap': '1000000000000'}}]
        # ⚠ tier 行形状以同文件 test_exclude_low_leverage_* 既有桩为准——若 exclude_low_leverage
        # 解析报 KeyError,逐字段对齐既有桩,勿改生产代码
        return {'AAA/USDT:USDT': tier(50), 'LOW/USDT:USDT': tier(5)}


def _uni_adapter():
    from gridtrade.exchanges.binance import BinanceAdapter
    return BinanceAdapter(_UniClient())


def test_resolve_bt_universe_applies_both_filters_keeps_delisted():
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    arch = ['AAA/USDT:USDT', 'TRAD/USDT:USDT', 'LOW/USDT:USDT',
            'DEAD/USDT:USDT',            # 退市:不在 markets/档位表 → 双过滤都保留
            'BL/USDT:USDT']              # 黑名单
    uni, stats = resolve_bt_universe(_uni_adapter(), ['BL/USDT:USDT'],
                                     archive_symbols=arch, min_lev=10.0,
                                     log=lambda *a: None)
    assert uni == ['AAA/USDT:USDT', 'DEAD/USDT:USDT']   # 非COIN剔/低杠杆剔/退市留/黑名单剔
    assert stats == {'n_blacklist': 1, 'n_tradfi': 1, 'n_lowlev': 1, 'min_lev': 10.0}


def test_resolve_bt_universe_minlev_zero_bypasses_leverage_filter():
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    arch = ['AAA/USDT:USDT', 'LOW/USDT:USDT']
    uni, stats = resolve_bt_universe(_uni_adapter(), (), archive_symbols=arch,
                                     min_lev=0.0, log=lambda *a: None)
    assert uni == ['AAA/USDT:USDT', 'LOW/USDT:USDT']    # =0 显式停用回旧口径
    assert stats['n_lowlev'] == 0


def test_resolve_bt_universe_env_default_minlev(monkeypatch):
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    monkeypatch.delenv('BT_MIN_LEVERAGE', raising=False)
    _, stats = resolve_bt_universe(_uni_adapter(), (), archive_symbols=['AAA/USDT:USDT'],
                                   log=lambda *a: None)
    assert stats['min_lev'] == 10.0                     # min_lev=None → env 默认 10.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/backtest/test_backtest_run.py -v -k "resolve_bt_universe"`
Expected: 3 FAIL with `ImportError: cannot import name 'resolve_bt_universe'`

- [ ] **Step 3: Implement**

`gridtrade/backtest/backtest_run.py`，`exclude_low_leverage` 之后新增：

```python
def resolve_bt_universe(adapter, blacklist, *, archive_symbols=None, min_lev=None,
                        log=print):
    """回测票池单一构建入口(spec 2026-07-24-sweep-universe-align):归档全量(含退市,无幸存者
    偏差)−黑名单−非COIN(TradFi)−低杠杆(BT_MIN_LEVERAGE 默认10=实盘同值,=0 显式停用)。
    main() 与 sweep_run 共用——2026-07-24 选币可预测性 recon 发现 sweep/研究脚本自建裸池
    绕过两道过滤(s030 出自宽池),口径分叉在此收口。archive_symbols/min_lev 注入点仅供测试,
    生产路径默认 None。"""
    from gridtrade.backtest import vision as V
    arch_full = (V.list_archive_symbols() if archive_symbols is None
                 else list(archive_symbols))
    _arch = set(arch_full) - set(blacklist)
    universe, _n_tradfi = exclude_non_coin(_arch, adapter)
    _minlev = (float(os.environ.get('BT_MIN_LEVERAGE', 10.0))
               if min_lev is None else float(min_lev))
    _gear = BT_STRATEGY['leverage'] * 0.68
    universe, _n_lowlev = exclude_low_leverage(
        universe, adapter.client.fetch_leverage_tiers,
        notional=1000.0 * _gear, gearing=_gear, min_lev=_minlev)
    stats = {'n_blacklist': len(set(arch_full) & set(blacklist)),
             'n_tradfi': _n_tradfi, 'n_lowlev': _n_lowlev, 'min_lev': _minlev}
    log('[BT] 全市场票池 %d 币(归档含退市,−黑名单 %d,−非COIN %d,−低杠杆 %d@min_lev=%g)'
        % (len(universe), stats['n_blacklist'], _n_tradfi, _n_lowlev, _minlev))
    return universe, stats
```

`main()` 中 :628-640 的原块（`_arch = set(...)` 起至统计 print 止）替换为：

```python
    universe, _uni_stats = resolve_bt_universe(_adapter, bt_blacklist)
```

（注意：原 `_minlev`/`_gear` 局部变量随块删除；`main()` 后续不引用它们——实现前 grep 确认。）

- [ ] **Step 4: Run tests + backtest suite**

Run: `.venv/bin/python -m pytest tests/backtest/test_backtest_run.py tests/backtest/ -q`
Expected: 全部 PASS（重构等价；exclude_* 既有单测不动）。

- [ ] **Step 5: Commit**

```bash
git add tests/backtest/test_backtest_run.py gridtrade/backtest/backtest_run.py
git commit -m "refactor(backtest): 提取resolve_bt_universe——票池构建单一入口(COIN+低杠杆过滤)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: sweep_run 接线共享构建器

**Files:**
- Modify: `scripts/sweep_run.py:71-73`（universe 构建两行）
- Test: `tests/backtest/test_backtest_run.py`（追加一条接线测试）

**Interfaces:**
- Consumes: Task 1 的 `resolve_bt_universe(adapter, blacklist, *, archive_symbols=None, min_lev=None, log=print)`；既有 `_binance_datasource_1h(cache) -> (adapter, ds)`
- Produces: `scripts/sweep_run.py` 新函数 `resolve_sweep_universe(cache) -> list[str]`（main 调用；可测）

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_backtest_run.py` 追加：

```python
def test_sweep_run_uses_shared_universe_builder(monkeypatch):
    """sweep_run 票池必须走 resolve_bt_universe(口径分叉回归锁,spec 2026-07-24)。"""
    import scripts.sweep_run as SRU
    calls = {}

    def fake_ds(cache):
        return _uni_adapter(), None

    def fake_resolve(adapter, blacklist, **kw):
        calls['blacklist'] = tuple(blacklist)
        return ['AAA/USDT:USDT'], {'n_tradfi': 0, 'n_lowlev': 0,
                                   'n_blacklist': 0, 'min_lev': 10.0}

    monkeypatch.setattr(SRU, '_binance_datasource_1h', fake_ds)
    monkeypatch.setattr(SRU, 'resolve_bt_universe', fake_resolve)
    uni = SRU.resolve_sweep_universe(cache=None)
    assert uni == ['AAA/USDT:USDT']
    assert len(calls['blacklist']) > 0          # tier0 黑名单已传入
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/backtest/test_backtest_run.py -v -k "sweep_run_uses"`
Expected: FAIL with `AttributeError: ... 'resolve_sweep_universe'`

- [ ] **Step 3: Implement**

`scripts/sweep_run.py`：import 区加
`from gridtrade.backtest.backtest_run import _binance_datasource_1h, resolve_bt_universe`；
新增模块级函数并改 main()：

```python
def resolve_sweep_universe(cache):
    """sweep 票池=共享构建器(COIN-only+低杠杆,与实盘/backtest_run 同口径;
    spec 2026-07-24——此前自建裸池致 s030 在实盘开不出的币上打分)。
    杠杆档私有端点凭证由底部 load_env_file 注入;不可用 fail-loud,
    BT_MIN_LEVERAGE=0 显式回旧口径(与历史 CSV 可比时用)。"""
    adapter, _ = _binance_datasource_1h(cache)
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe, _stats = resolve_bt_universe(adapter, bl)
    return universe
```

main() 中原两行：

```python
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
```

替换为：

```python
    universe = resolve_sweep_universe(cache)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/backtest/ -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/sweep_run.py tests/backtest/test_backtest_run.py
git commit -m "feat(sweep): 票池改走resolve_bt_universe——收口与实盘/backtest_run的口径分叉

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: s030 六窗宽池/对齐池 A/B 复测

**Files:**
- Create: `data/score_research_2026-07-24-aligned/s030_pool_ab.py`（复测 runner）
- 产物: `data/score_research_2026-07-24-aligned/s030_pool_ab_results.csv` + 对比报告（控制台/记忆）

**Interfaces:**
- Consumes: Task 1 `resolve_bt_universe`；sweep 机件 `SW.WINDOWS/HOLDOUT/preload_window/Arm/run_arm/metrics`、`select_grids`、`V.warm_vision`
- Produces: 每窗×两池的 metrics 行（name,pool,n_grids,ret,ann,mdd,calmar,n_broke,n_blown,n_pvstop,win_rate）

**对比基准裁定（写死在 runner 注释里）**：原研究 CSV 早于引擎首触丢弃修复(69754cf)，
数字不可比——**唯一诚实比较=同 harness 同数据的宽池 vs 对齐池 A/B**；原研究数字仅作背景。

- [ ] **Step 1: Write the runner**

新建 `data/score_research_2026-07-24-aligned/s030_pool_ab.py`：

```python
"""s030 六窗 宽池/对齐池 A/B(spec 2026-07-24-sweep-universe-align 改动二)。
对比基准=同 harness A/B(原研究 CSV 早于 69754cf 引擎修复,不可比,仅作背景)。
预注册判据:只裁方向性翻转(对齐池 s030 留出是否仍正);数值漂移是预期。
资源纪律:__main__ 守卫+锁线程+BT_WORKERS≤4(死机坑)。"""
import os
import sys
import time

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程(必须最先)
import pandas as pd


def _universe(pool, cache):
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.backtest_run import (_binance_datasource_1h,
                                                 resolve_bt_universe)
    from gridtrade.core.tier_policy import effective_blacklist
    from gridtrade.config import DEFAULT_TIER_POLICY
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    if pool == 'wide':      # 原研究口径:归档−tier0 裸池(复刻 cf_run.py:57)
        return sorted(set(V.list_archive_symbols()) - set(bl))
    adapter, _ = _binance_datasource_1h(cache)
    uni, _ = resolve_bt_universe(adapter, bl)
    return uni


def main():
    from gridtrade.backtest import sweep as SW
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.backtest.backtest_run import (BT_STRATEGY, BT_FACTORS,
                                                 BT_UNIVERSE_TOP_PCT, select_grids)
    from gridtrade.core.tier_policy import effective_blacklist
    from gridtrade.config import DEFAULT_TIER_POLICY

    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    wins = dict(SW.WINDOWS)
    wins.update(SW.HOLDOUT)
    out = []
    for pool in ('wide', 'aligned'):
        universe = _universe(pool, cache)
        print('[ab] pool=%s universe=%d' % (pool, len(universe)), flush=True)
        for name, (ws_s, we_s) in wins.items():
            t0 = time.time()
            ws = pd.Timestamp(ws_s)
            we = pd.Timestamp(we_s) + pd.Timedelta(days=1)   # 与 preload 同口径
            # 1h 预热(宽池窗数据研究期已暖,增量近 no-op;对齐池⊂宽池)
            V.warm_vision(cache, universe,
                          int((ws - pd.Timedelta(days=10)).value // 1_000_000),
                          int(we.value // 1_000_000), timeframes=('1h',))
            # 先选(与 preload_window 完全同参→cache 共键,preload 时 HIT 不重算)
            picks = select_grids(cache, universe, ws, we, BT_STRATEGY, BT_FACTORS,
                                 timeframe='1h', min_quote_volume=0.0,
                                 top_volume_pct=BT_UNIVERSE_TOP_PCT, blacklist=bl,
                                 workers=int(os.environ.get('BT_WORKERS', '4')),
                                 candidates_per_rt=SW.TIER_CAND_K)
            syms = sorted({row['symbol'] for _, _, row in picks})
            # 选中币 1m+funding 预热(preload 对缺 1m 币静默跳格→不暖会虚漏)
            V.warm_vision(cache, syms, int(ws.value // 1_000_000),
                          int(we.value // 1_000_000), timeframes=('1m', 'funding'))
            wd = SW.preload_window(cache, universe, name, ws_s, we_s,
                                   workers=int(os.environ.get('BT_WORKERS', '4')))
            arm = SW.Arm('baseline', 's030', {})
            df = SW.run_arm(wd, arm, {},
                            workers=int(os.environ.get('BT_WORKERS', '4')))
            m = SW.metrics(df, wd.days)
            m.update({'window': name, 'pool': pool, 'n_syms': wd.n_symbols,
                      'elapsed_s': round(time.time() - t0)})
            out.append(m)
            print('[ab] %s/%s: ret=%+.4f calmar=%.1f broke=%d blown=%d (%ds)'
                  % (pool, name, m['ret'], m['calmar'], m['n_broke'],
                     m['n_blown'], m['elapsed_s']), flush=True)
            pd.DataFrame(out).to_csv(
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             's030_pool_ab_results.csv'), index=False)  # 逐窗落盘防中断丢
    print('[ab] done rows=%d' % len(out), flush=True)


if __name__ == '__main__':
    from gridtrade.backtest.envfile import load_env_file
    load_env_file()          # 杠杆档私有端点凭证
    main()
```

- [ ] **Step 2: 冒烟（单窗宽池，验证机件接通）**

Run: `BT_WORKERS=2 PYTHONPATH=. .venv/bin/python -u data/score_research_2026-07-24-aligned/s030_pool_ab.py 2>&1 | head -20`（人工观察首窗 `[ab] wide/W1` 行出现即 Ctrl-C；或临时把 wins 缩成单窗跑通再还原——还原后再进 Step 3）
Expected: `[ab] pool=wide universe=772` + 首窗 select cache HIT + metrics 行。

- [ ] **Step 3: 全量后台跑**

```bash
cd /Users/thomaschang/Projects/GridTradeBi
nohup env BT_WORKERS=4 PYTHONPATH=. .venv/bin/python -u \
  data/score_research_2026-07-24-aligned/s030_pool_ab.py \
  > data/score_research_2026-07-24-aligned/ab_run.log 2>&1 &
```
预算：宽池 6 窗选币全 cache HIT（~分钟级）+ 对齐池 6 窗选币全 MISS
（~25-40min/窗×6 ≈ 2.5-4h）+ simulate。总 ~3-5h，后台过夜可。

- [ ] **Step 4: 对比报告**

跑完后读 `s030_pool_ab_results.csv`，产出对比表（逐窗 ret/calmar/vetoes 宽 vs 对齐 +
Δ列），按 spec 预注册判据裁定：**只看方向性——对齐池 s030 六窗是否仍正、双留出
(HOLD-A/HOLD-B)是否仍过（ret>0 且零爆仓）**；数值漂移≤若干 pp 属预期不触发任何回滚。
报告交用户 + 写入记忆（`backtest-vs-live-data-source-gap` 或新档）。

- [ ] **Step 5: Commit（runner + 结果）**

```bash
git add data/score_research_2026-07-24-aligned/
git commit -m "research(s030): 六窗宽池/对齐池A/B复测——runner+结果落档

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push . HEAD:main
```
