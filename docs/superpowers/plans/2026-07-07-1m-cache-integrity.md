# 1m 缓存完整性闸 + 自愈重取 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 1m 缓存加完整性校验（振幅匹配 1h + 逐小时无空洞），warm 时坏格自动重取，并提供 validate-1m 清库命令清现存坏格。

**Architecture:** 纯函数 `validate_1m_cell` 承载判据（可单测无 IO）；`warm_reservoir_ohlcv` 跳过条件从"文件存在"升级为"存在且过校验"（自愈）；`dbadmin validate-1m` 复用校验器做一次性全库扫描+重取。

**Tech Stack:** Python 3.9、pandas、pytest。

**Spec:** `docs/superpowers/specs/2026-07-07-1m-cache-integrity-design.md`

## Global Constraints

- 判据默认值：振幅容差 `range_tol=0.05`（TRUMP 型）；逐小时零 bar → 坏（GMX 型）。
- 重取失败保留旧坏格、不删不改、计入 retry_later（不留空洞）。
- 1h 缺 → `no_1h_ref`、判 ok、清库另行标记（不误判为坏）。
- 幂等：warm 自愈与 validate-1m 均可反复跑，只对仍坏的重取。
- 回测/选币/实盘/1h 缓存零改动。
- 测试跑法 `.venv/bin/python -m pytest <path> -q`；每 Task 一 commit。
- cache API（已核）：`read(ns,sym,day)->df|None`、`exists(ns,sym,day)->bool`、
  `write/write_empty`、`list_days(ns,sym)->[day]`；1m/1h 命名空间为 '1m'/'1h'；
  df 列含 `candle_begin_time`(tz-naive UTC)、open/high/low/close。

---

### Task 1: validate_1m_cell 校验器（纯函数）

**Files:**
- Modify: `gridtrade/backtest/reservoir.py`（`candles_1s_to_1m` 之后新增）
- Test: `tests/backtest/test_validate_1m.py`（新建）

**Interfaces:**
- Produces: `validate_1m_cell(m_df, h_df, *, range_tol=0.05) -> (ok: bool, reason: str)`
  reason ∈ {'ok','no_1h_ref','range_mismatch','hour_gap'}。m_df/h_df 为缓存 read 的
  DataFrame 或 None；列含 candle_begin_time/high/low/close。

- [ ] **Step 1: 写失败测试**

```python
# tests/backtest/test_validate_1m.py
"""1m 缓存完整性判据（spec 2026-07-07-1m-cache-integrity）。"""
import pandas as pd
from gridtrade.backtest.reservoir import validate_1m_cell


def _bars(begin, closes, tf='1min'):
    idx = pd.date_range(begin, periods=len(closes), freq=tf)
    return pd.DataFrame({'candle_begin_time': idx,
                         'open': closes, 'high': [c * 1.001 for c in closes],
                         'low': [c * 0.999 for c in closes], 'close': closes})


def test_no_1h_ref_is_ok():
    m = _bars('2026-03-15', [4.0] * 100)
    assert validate_1m_cell(m, None) == (True, 'no_1h_ref')
    assert validate_1m_cell(m, pd.DataFrame(columns=['candle_begin_time', 'high',
                                                     'low', 'close'])) == (True, 'no_1h_ref')


def test_empty_1m_with_no_1h_is_ok():
    # 币真无成交：1m 空 + 1h 空/缺 → 合法
    empty = pd.DataFrame(columns=['candle_begin_time', 'high', 'low', 'close'])
    assert validate_1m_cell(empty, None)[0] is True


def test_range_mismatch_flagged():
    # TRUMP 型：1h 平静(4.0±5%)，1m 假崩到 2.0 → 振幅差远超 5%
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    m = _bars('2026-03-15', [4.0] * 30 + [2.0] * 30)   # 假崩
    ok, reason = validate_1m_cell(m, h)
    assert ok is False and reason == 'range_mismatch'


def test_hour_gap_flagged():
    # GMX 型：1h 满 24 根，1m 只覆盖前 3 小时(180根) → 后续小时零 bar
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    m = _bars('2026-03-15', [4.0] * 180)               # 只有前 3h
    ok, reason = validate_1m_cell(m, h)
    assert ok is False and reason == 'hour_gap'


def test_legit_sparse_is_ok():
    # 合法稀疏：每个 1h 小时里都有 1m bar，只是分钟级有缺（不是整小时空洞）
    h = _bars('2026-03-15', [4.0] * 24, tf='1H')
    idx = pd.date_range('2026-03-15', periods=24 * 6, freq='10min')  # 每 10 分钟一根
    m = pd.DataFrame({'candle_begin_time': idx, 'open': 4.0,
                      'high': 4.004, 'low': 3.996, 'close': 4.0})
    assert validate_1m_cell(m, h) == (True, 'ok')
```

- [ ] **Step 2: 确认失败** `.venv/bin/python -m pytest tests/backtest/test_validate_1m.py -q` → ImportError
- [ ] **Step 3: 实现**（reservoir.py）

```python
def validate_1m_cell(m_df, h_df, *, range_tol=0.05):
    """判定 (币,天) 的缓存 1m 是否可信。返回 (ok, reason)。
    1h 缺/空 → 无基准，视为合法(真·不成交或无参照)；
    振幅：|1m 日高低幅 − 1h 日高低幅|/入场价 > range_tol → range_mismatch；
    完整性：1h 有 bar 的每个整点小时，1m 该小时窗零 bar → hour_gap。"""
    if h_df is None or len(h_df) == 0:
        return True, 'no_1h_ref'
    h_hi = float(h_df['high'].max()); h_lo = float(h_df['low'].min())
    entry = float(h_df['close'].iloc[0])
    if entry <= 0:
        return True, 'no_1h_ref'
    if m_df is None or len(m_df) == 0:
        # 1h 有数据但 1m 空 = 坏（该交易的天缺 1m）
        return False, 'hour_gap'
    m_hi = float(m_df['high'].max()); m_lo = float(m_df['low'].min())
    if abs((m_hi - m_lo) - (h_hi - h_lo)) / entry > range_tol:
        return False, 'range_mismatch'
    # 逐小时：1h 每根 bar 的整点小时，1m 该 [h, h+1) 窗须有 ≥1 根
    m_hours = set(pd.to_datetime(m_df['candle_begin_time']).dt.floor('H'))
    for ht in pd.to_datetime(h_df['candle_begin_time']).dt.floor('H'):
        if ht not in m_hours:
            return False, 'hour_gap'
    return True, 'ok'
```

- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** `feat(backtest): validate_1m_cell 缓存完整性判据（振幅+逐小时空洞）`

---

### Task 2: warm_reservoir 跳过自愈化

**Files:**
- Modify: `gridtrade/backtest/reservoir.py`（`warm_reservoir_ohlcv` 跳过条件 + 新增 `_day_1m_all_valid`）
- Test: `tests/backtest/test_reservoir_selfheal.py`（新建）

**Interfaces:**
- Consumes: Task 1 `validate_1m_cell`。
- Produces: `_day_1m_all_valid(cache, universe, day) -> bool`。

- [ ] **Step 1: 写失败测试**（桩 cache + mock _s3_cp）

```python
# tests/backtest/test_reservoir_selfheal.py
"""warm 自愈：坏 1m 格触发重下、好格跳过、重取失败保留旧格。"""
import pandas as pd
import pytest
from gridtrade.backtest import reservoir as RV


class _StubCache:
    """内存桩：模拟 1h/1m 缓存。data[(ns,sym,day)] = df。"""
    def __init__(self, data):
        self.data = dict(data)
        self.writes = []
    def exists(self, ns, sym, day):
        return (ns, sym, day) in self.data
    def read(self, ns, sym, day):
        return self.data.get((ns, sym, day))
    def write(self, ns, sym, day, df):
        self.data[(ns, sym, day)] = df; self.writes.append((ns, sym, day))
    def write_empty(self, ns, sym, day, cols):
        self.data[(ns, sym, day)] = pd.DataFrame(columns=cols)
        self.writes.append((ns, sym, day))


def _h(closes):
    idx = pd.date_range('2026-03-15', periods=len(closes), freq='1H')
    return pd.DataFrame({'candle_begin_time': idx, 'open': closes,
                         'high': [c*1.001 for c in closes], 'low': [c*0.999 for c in closes],
                         'close': closes, 'vol': 1.0, 'volCcy': 1.0, 'quote_volume': 1.0})


def test_day_1m_all_valid_detects_bad(monkeypatch):
    SYM = 'BTC/USDC:USDC'; day = '2026-03-15'
    good_h = _h([4.0]*24)
    # 1m 只覆盖前 3h → hour_gap
    bad_m = good_h.iloc[:0].copy()   # 空 1m，1h 有 → 坏
    c = _StubCache({('1h', SYM, day): good_h, ('1m', SYM, day): bad_m})
    assert RV._day_1m_all_valid(c, [SYM], day) is False
    # 好 1m（每小时都有）
    idx = pd.date_range('2026-03-15', periods=24*6, freq='10min')
    good_m = pd.DataFrame({'candle_begin_time': idx, 'open':4.0,'high':4.004,'low':3.996,
                           'close':4.0,'vol':1.0,'volCcy':1.0,'quote_volume':1.0})
    c2 = _StubCache({('1h', SYM, day): good_h, ('1m', SYM, day): good_m})
    assert RV._day_1m_all_valid(c2, [SYM], day) is True


def test_warm_refetches_bad_cell(monkeypatch, tmp_path):
    SYM = 'BTC/USDC:USDC'; day = '2026-03-15'
    good_h = _h([4.0]*24)
    c = _StubCache({('1h', SYM, day): good_h,
                    ('1m', SYM, day): good_h.iloc[:0].copy()})   # 坏：1m 空
    # mock S3 下载成功；重采样返回好 1m
    monkeypatch.setattr(RV, '_s3_cp', lambda day, dest, log=print: _touch(dest))
    idx = pd.date_range('2026-03-15', periods=24*60, freq='1min')
    good_1m = pd.DataFrame({'candle_begin_time': idx, 'open':4.0,'high':4.004,'low':3.996,
                            'close':4.0,'vol':1.0,'volCcy':1.0,'quote_volume':1.0})
    monkeypatch.setattr(RV, 'candles_1s_resample',
                        lambda raw, smap, rule: {SYM: good_1m} if rule == '1min'
                        else {SYM: good_h})
    monkeypatch.setattr(RV.pd, 'read_parquet', lambda p: pd.DataFrame({'coin':['BTC']}))
    import os
    start = int(pd.Timestamp(day).value//1_000_000); end = start + 86400000 - 1
    stat = RV.warm_reservoir_ohlcv(c, [SYM], start, end, workdir=str(tmp_path))
    assert ('1m', SYM, day) in c.writes          # 坏格被重写
    assert stat['skipped_cached'] == 0            # 未跳过（因校验不过）


def _touch(dest):
    open(dest, 'w').close(); return True
```

- [ ] **Step 2: 确认失败**（`_day_1m_all_valid` 不存在）
- [ ] **Step 3: 实现**（reservoir.py）

新增 `_day_1m_all_valid`：

```python
def _day_1m_all_valid(cache, universe, day):
    """该天所有币的缓存 1m 是否都过完整性校验（配合 warm 跳过判定）。"""
    for s in universe:
        ok, _ = validate_1m_cell(cache.read('1m', s, day), cache.read('1h', s, day))
        if not ok:
            return False
    return True
```

`warm_reservoir_ohlcv` 跳过行改：

```python
        if (all(cache.exists(tf, s, day) for tf in timeframes for s in universe)
                and _day_1m_all_valid(cache, universe, day)):
            stat['skipped_cached'] += 1
            continue
```

- [ ] **Step 4: 确认通过** + `tests/backtest/ -q` 全绿
- [ ] **Step 5: Commit** `feat(backtest): warm_reservoir 跳过自愈化（坏 1m 触发重下）`

---

### Task 3: validate-1m 清库命令

**Files:**
- Modify: `gridtrade/runtime/dbadmin.py`（`validate_1m_cache` + run() 分支）
- Test: `tests/runtime/test_dbadmin_validate1m.py`（新建）

**Interfaces:**
- Consumes: Task 1 `validate_1m_cell`、Task 2 `warm_reservoir_ohlcv`。
- Produces: `validate_1m_cache(cache, *, dry_run=False, warm_fn=None, log=print) -> dict`
  返回 `{'scanned','ok','range_mismatch','hour_gap','no_1h_ref','refetched_days','still_bad'}`；
  run('validate-1m') 分支（`--dry-run` 走 dry）。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_dbadmin_validate1m.py
"""validate-1m 清库：扫描分类 + 坏格聚合成天重取 + dry-run + 幂等。"""
import pandas as pd
from gridtrade.runtime.dbadmin import validate_1m_cache


class _Cache:
    def __init__(self):
        self.data = {}
        self.days = {}   # (ns,sym)->[day]
    def put(self, ns, sym, day, df):
        self.data[(ns, sym, day)] = df
        self.days.setdefault((ns, sym), []).append(day)
    def read(self, ns, sym, day): return self.data.get((ns, sym, day))
    def list_days(self, ns, sym): return sorted(self.days.get((ns, sym), []))
    def list_symbols(self, ns):
        return sorted({sym for (n, sym) in self.days if n == ns})


def _h(closes):
    idx = pd.date_range('2026-03-15', periods=len(closes), freq='1H')
    return pd.DataFrame({'candle_begin_time': idx, 'open': closes,
                         'high': [c*1.001 for c in closes], 'low': [c*0.999 for c in closes],
                         'close': closes})


def _m(n, freq='1min'):
    idx = pd.date_range('2026-03-15', periods=n, freq=freq)
    return pd.DataFrame({'candle_begin_time': idx, 'open': 4.0,
                         'high': 4.004, 'low': 3.996, 'close': 4.0})


def _seed():
    c = _Cache()
    # 好格：1h 满 + 1m 每小时有
    c.put('1h', 'GOOD/USDC:USDC', '2026-03-15', _h([4.0]*24))
    c.put('1m', 'GOOD/USDC:USDC', '2026-03-15', _m(24*6, '10min'))
    # 坏格：1h 满 + 1m 只前 3h
    c.put('1h', 'BAD/USDC:USDC', '2026-03-15', _h([4.0]*24))
    c.put('1m', 'BAD/USDC:USDC', '2026-03-15', _m(180))
    # 空币：1h 空 + 1m 空 → no_1h_ref
    c.put('1h', 'EMPTY/USDC:USDC', '2026-03-15', _h([])[:0])
    c.put('1m', 'EMPTY/USDC:USDC', '2026-03-15', _m(0))
    return c


def test_dry_run_classifies_no_refetch():
    c = _seed()
    calls = []
    rep = validate_1m_cache(c, dry_run=True,
                            warm_fn=lambda *a, **k: calls.append(a), log=lambda *a: None)
    assert rep['scanned'] == 3 and rep['ok'] == 1
    assert rep['hour_gap'] == 1 and rep['no_1h_ref'] == 1
    assert rep['refetched_days'] == 0 and calls == []       # dry-run 不重取


def test_refetch_bad_day_and_fix():
    c = _seed()
    def _warm(cache, syms, s_ms, e_ms, **k):
        # 模拟重取修好 BAD 的 1m
        cache.put('1m', 'BAD/USDC:USDC', '2026-03-15', _m(24*6, '10min'))
        cache.data[('1m', 'BAD/USDC:USDC', '2026-03-15')] = _m(24*6, '10min')
    rep = validate_1m_cache(c, dry_run=False, warm_fn=_warm, log=lambda *a: None)
    assert rep['hour_gap'] == 1 and rep['refetched_days'] == 1
    assert rep['still_bad'] == 0                            # 修好了
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（dbadmin.py）

顶部加 import：`from gridtrade.backtest.cache import ParquetCache`、
`from gridtrade.backtest.reservoir import validate_1m_cell, warm_reservoir_ohlcv`
（惰性放函数内，避免 dbadmin 常规路径引入 pandas/backtest 依赖）。

```python
def validate_1m_cache(cache, *, dry_run=False, warm_fn=None, log=print):
    """扫全 1m 缓存 → 分类 → 坏格聚合成天 → warm 重取（dry_run 时只报告）。幂等。"""
    import pandas as pd
    from gridtrade.backtest.reservoir import validate_1m_cell, warm_reservoir_ohlcv
    warm_fn = warm_fn or warm_reservoir_ohlcv
    rep = {'scanned': 0, 'ok': 0, 'range_mismatch': 0, 'hour_gap': 0,
           'no_1h_ref': 0, 'refetched_days': 0, 'still_bad': 0}
    bad_by_day = {}   # day -> set(symbol)
    for sym in cache.list_symbols('1m'):
        for day in cache.list_days('1m', sym):
            rep['scanned'] += 1
            ok, reason = validate_1m_cell(cache.read('1m', sym, day),
                                          cache.read('1h', sym, day))
            rep[reason] = rep.get(reason, 0) + 1
            if not ok:
                bad_by_day.setdefault(day, set()).add(sym)
    if dry_run:
        log('[validate-1m] DRY scanned=%d ok=%d range=%d gap=%d no1h=%d 坏天=%d'
            % (rep['scanned'], rep['ok'], rep['range_mismatch'], rep['hour_gap'],
               rep['no_1h_ref'], len(bad_by_day)))
        return rep
    for day, syms in sorted(bad_by_day.items()):
        s_ms = int(pd.Timestamp(day).value // 1_000_000)
        e_ms = s_ms + 86_400_000 - 1
        warm_fn(cache, sorted(syms), s_ms, e_ms, log=log)
        rep['refetched_days'] += 1
        for sym in syms:      # 复检
            ok, _ = validate_1m_cell(cache.read('1m', sym, day),
                                     cache.read('1h', sym, day))
            if not ok:
                rep['still_bad'] += 1
    log('[validate-1m] scanned=%d refetched_days=%d still_bad=%d'
        % (rep['scanned'], rep['refetched_days'], rep['still_bad']))
    return rep
```

run() 加分支（`_store()` 前，因本命令不需要 DB store）：

```python
    if action == 'validate-1m':
        from gridtrade.backtest.cache import ParquetCache
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', '..', 'data', 'hl_validate')
        dry = '--dry-run' in sys.argv
        return validate_1m_cache(ParquetCache(root), dry_run=dry)
```

- [ ] **Step 4: 补 cache.list_symbols**（若不存在）：`ParquetCache` 加

```python
    def list_symbols(self, namespace):
        """列举某 namespace 下已缓存的所有 canonical symbol。"""
        base = os.path.join(self._root, namespace)
        if not os.path.isdir(base):
            return []
        out = []
        for a in sorted(os.listdir(base)):
            ad = os.path.join(base, a)
            if os.path.isdir(ad):
                for b in sorted(os.listdir(ad)):
                    if os.path.isdir(os.path.join(ad, b)):
                        out.append('%s/%s' % (a, b))
        return out
```

（先核对 `_path`/`_dir` 的目录结构：symbol='BTC/USDC:USDC' 如何落盘——按现有
`_dir(namespace,symbol)` 实现推导层级，测试用桩 cache 不依赖此。实现时以真实 `_dir` 为准。）

- [ ] **Step 5: 确认通过** + run() smoke（`dbadmin validate-1m --dry-run` 不炸）
- [ ] **Step 6: Commit** `feat(dbadmin): validate-1m 清库命令（扫描分类+坏天重取+dry-run+幂等）`

---

### Task 4: 全套 + 真实 dry-run 冒烟

- [ ] **Step 1:** `.venv/bin/python -m pytest -q` 全绿
- [ ] **Step 2:** 真实缓存 dry-run：`.venv/bin/python -m gridtrade.runtime.dbadmin validate-1m --dry-run`
  → 核对 hour_gap 坏格数 ≈ 诊断的 6.4%（scanned ~78k 的量级、range_mismatch≈0）
- [ ] **Step 3: Commit（若有 STATUS/文档更新）** `docs: 1m 缓存完整性闸落地记录`

## Self-Review 结果

- **Spec 覆盖**：组件①→Task 1；组件②(warm 自愈+_day_1m_all_valid)→Task 2；组件③(validate-1m)
  →Task 3；错误处理（重取失败保留/当天未过完/1h缺/幂等）→Task 1 判据+Task 2 现有 retry_later
  路径+Task 3 复检；测试矩阵→各 Task 单测+Task 4 真实 dry-run。
- **占位符**：Task 3 Step 4 的"以真实 _dir 为准"是 list_symbols 落盘层级的现场核对（纯路径、
  无语义决策），桩测试不依赖它——允许范围。其余零占位。
- **类型一致**：validate_1m_cell 签名/reason 取值在 Task 1 定义、Task 2/3 消费一致；
  warm_fn 注入口在 Task 3 与 warm_reservoir_ohlcv(cache,syms,s_ms,e_ms,log=) 签名一致。
