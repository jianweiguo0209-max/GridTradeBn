# pv 实盘评估机制对齐回测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让实盘 pv 止损的评估机制（收盘桶 + 每分钟 + 取数鲁棒）与回测 `calc_pv_spike` 一致，从而提高回测对实盘的预测准确性。

**Architecture:** 改动全在 `LiveSignalProvider` 及其新依赖 `OneMinuteBarBuffer`；引擎/回测零改动。新增 per-symbol 收盘 1m 滚动缓冲：冷启动全载一次、之后只拉增量、取数失败沿用旧缓冲（不塌回 0）；缓冲只留已收盘 bar（丢 forming 半截分钟）；pv 每 `SIGNAL_REFRESH_SEC`（默认 60）复算一次。

**Tech Stack:** Python 3.9, pandas, pytest。数据来自 `adapter.fetch_ohlcv(symbol, '1m', start_ms, end_ms)`（返回列含 `candle_begin_time`(datetime64)、`quote_volume`、OHLC）。

## Global Constraints

- 引擎/回测**零改动**——只动 `gridtrade/execution/`、`gridtrade/config.py`、`gridtrade/runtime/factory.py`。
- (b) 收盘桶**无条件**（无旗标）——缓冲只留 `candle_begin_time < floor(now,'1min')` 的已收盘 bar。
- (a) 每分钟评估由 env **`SIGNAL_REFRESH_SEC` 默认 60**（默认开）；设 900 即回退 15min 节奏（仍保收盘桶+缓冲）。
- **取数失败降级不塌 0**：增量拉失败且缓冲非空 → 沿用缓冲算 pv；仅冷启动无缓冲时返 0。
- pv 回看窗宽 = `(n+8)×period`（n=`pv_n`、period=`pv_period`='15min'）。
- 明确**不做**：signal_snapshots 录放、回测退出滑点建模。funding 路径不动。
- 部署走 production 分支 CI/CD（见 memory deploy-prod-via-cicd-only）。

---

### Task 1: OneMinuteBarBuffer —— 冷载 + 收盘过滤 + 增量 + 窗口裁剪

**Files:**
- Create: `gridtrade/execution/bar_buffer.py`
- Test: `tests/execution/test_bar_buffer.py`

**Interfaces:**
- Produces: `class OneMinuteBarBuffer(fetch_fn, window_ms, now_fn=time.time, log=print)`；`fetch_fn(symbol:str, since_ms:int, until_ms:int) -> pd.DataFrame`（含 `candle_begin_time`/`quote_volume`）；方法 `get_closed_bars(symbol:str) -> pd.DataFrame`（返回已收盘、窗内、按时间升序的 bar；无数据返回空 DataFrame）。

- [ ] **Step 1: 写失败测试**（冷载只取一次全窗；收盘过滤丢 forming 桶；增量只拉新根且与全载等价）

```python
# tests/execution/test_bar_buffer.py
import pandas as pd
from gridtrade.execution.bar_buffer import OneMinuteBarBuffer

_START = pd.Timestamp('2026-06-01 00:00')

def _series(n_min, base=1e5):
    t = pd.date_range(_START, periods=n_min, freq='1min')
    return pd.DataFrame({'candle_begin_time': t, 'open': 100.0, 'high': 100.0,
                         'low': 100.0, 'close': 100.0, 'quote_volume': float(base)})

class RecordingFetch:
    """按 [since_ms, until_ms] 切片返回（含末尾 forming 桶，镜像币安）。"""
    def __init__(self, full):
        self.full = full
        self.calls = []
    def __call__(self, symbol, since_ms, until_ms):
        self.calls.append((symbol, int(since_ms), int(until_ms)))
        s = pd.Timestamp(int(since_ms), unit='ms')
        u = pd.Timestamp(int(until_ms), unit='ms')
        m = (self.full['candle_begin_time'] >= s) & (self.full['candle_begin_time'] <= u)
        return self.full[m].copy()

def _now_fn_at(ts):
    return lambda: ts.value / 1e9      # pandas ns → 秒

def test_cold_load_fetches_full_window_and_drops_forming_bar():
    full = _series(200)                                  # 00:00..03:19
    fetch = RecordingFetch(full)
    now = _START + pd.Timedelta(minutes=150) + pd.Timedelta(seconds=20)   # 02:30:20（02:30 桶未收盘）
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(now))
    bars = buf.get_closed_bars('X')
    assert len(fetch.calls) == 1                         # 冷载一次
    assert bars['candle_begin_time'].max() == _START + pd.Timedelta(minutes=149)  # 02:29=最后已收盘
    assert (_START + pd.Timedelta(minutes=150)) not in set(bars['candle_begin_time'])  # forming 桶被丢
    assert len(bars) == 100                              # 窗宽=100 根

def test_incremental_only_fetches_new_bars_and_equals_full_reload():
    full = _series(400)
    fetch = RecordingFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1))
    buf.get_closed_bars('X')
    last_ts_ms = int((_START + pd.Timedelta(minutes=199)).value // 1_000_000)
    t2 = _START + pd.Timedelta(minutes=205) + pd.Timedelta(seconds=5)     # 前进 5 分钟
    buf._now = _now_fn_at(t2)
    bars = buf.get_closed_bars('X')
    assert fetch.calls[1][1] == last_ts_ms + 60_000      # 增量 since = 上次最后收盘 + 1min
    # 与在 t2 全新冷载等价
    fresh = OneMinuteBarBuffer(RecordingFetch(full), window_ms=100 * 60_000, now_fn=_now_fn_at(t2))
    exp = fresh.get_closed_bars('X')
    assert bars['candle_begin_time'].tolist() == exp['candle_begin_time'].tolist()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_bar_buffer.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.bar_buffer`）

- [ ] **Step 3: 写最小实现**

```python
# gridtrade/execution/bar_buffer.py
"""per-symbol 已收盘 1m 滚动缓冲：冷载一次全窗，之后只拉增量；取数失败沿用旧缓冲。
只留 candle_begin_time < floor(now,'1min') 的已收盘 bar（丢 forming 半截桶）。"""
import time

import pandas as pd

_MIN_MS = 60_000


class OneMinuteBarBuffer:
    def __init__(self, fetch_fn, window_ms, now_fn=time.time, log=print):
        self.fetch_fn = fetch_fn          # (symbol, since_ms, until_ms) -> DataFrame
        self.window_ms = int(window_ms)
        self._now = now_fn
        self.log = log
        self._buf = {}                    # symbol -> DataFrame(已收盘, 升序)

    @staticmethod
    def _closed(df, cutoff):
        if df is None or len(df) == 0 or 'candle_begin_time' not in df.columns:
            return pd.DataFrame()
        return df[df['candle_begin_time'] < cutoff].copy()

    def get_closed_bars(self, symbol):
        now_ms = int(self._now() * 1000)
        cutoff = pd.Timestamp(now_ms, unit='ms').floor('min')   # 当前 forming 分钟起点
        buf = self._buf.get(symbol)
        stale = (buf is None or buf.empty
                 or int(buf['candle_begin_time'].iloc[-1].value // 1_000_000)
                 < now_ms - self.window_ms)
        try:
            if stale:
                df = self.fetch_fn(symbol, now_ms - self.window_ms, now_ms)
                buf = self._closed(df, cutoff)
            else:
                last_ms = int(buf['candle_begin_time'].iloc[-1].value // 1_000_000)
                inc = self._closed(self.fetch_fn(symbol, last_ms + _MIN_MS, now_ms), cutoff)
                if not inc.empty:
                    buf = (pd.concat([buf, inc], ignore_index=True)
                           .drop_duplicates('candle_begin_time')
                           .sort_values('candle_begin_time'))
        except Exception as exc:            # 降级：沿用旧缓冲，绝不塌回空
            self.log('[bar_buffer] %s fetch 降级,沿用缓冲: %r' % (symbol, exc))
            if buf is None:
                return pd.DataFrame()
        if buf is None or buf.empty:
            return pd.DataFrame()
        lo = pd.Timestamp(now_ms - self.window_ms, unit='ms')
        buf = buf[(buf['candle_begin_time'] >= lo)
                  & (buf['candle_begin_time'] < cutoff)].reset_index(drop=True)
        self._buf[symbol] = buf
        return buf
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_bar_buffer.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/bar_buffer.py tests/execution/test_bar_buffer.py
git commit -m "feat(signals): OneMinuteBarBuffer——收盘1m滚动缓冲(冷载+增量+丢forming桶)"
```

---

### Task 2: OneMinuteBarBuffer —— 取数失败降级不塌 0 + 停机过久重载

**Files:**
- Modify: `gridtrade/execution/bar_buffer.py`（实现已覆盖，本任务补测证明契约）
- Test: `tests/execution/test_bar_buffer.py`

**Interfaces:**
- Consumes: Task 1 的 `OneMinuteBarBuffer.get_closed_bars`。

- [ ] **Step 1: 写失败测试**（增量拉抛异常时沿用缓冲；冷载无缓冲时返空；停机久触发重载）

```python
# 追加到 tests/execution/test_bar_buffer.py
class FlakyFetch(RecordingFetch):
    def __init__(self, full):
        super().__init__(full)
        self.fail_after = None
    def __call__(self, symbol, since_ms, until_ms):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            self.calls.append((symbol, int(since_ms), int(until_ms)))
            raise RuntimeError('boom')
        return super().__call__(symbol, since_ms, until_ms)

def test_incremental_failure_keeps_existing_buffer():
    full = _series(400)
    fetch = FlakyFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1), log=lambda *a: None)
    first = buf.get_closed_bars('X')
    assert len(first) == 100
    fetch.fail_after = 1                                  # 之后所有拉都抛
    t2 = _START + pd.Timedelta(minutes=201) + pd.Timedelta(seconds=5)
    buf._now = _now_fn_at(t2)
    bars = buf.get_closed_bars('X')                       # 增量拉失败
    assert not bars.empty                                 # 沿用缓冲,不塌回 0
    assert bars['candle_begin_time'].max() == _START + pd.Timedelta(minutes=199)

def test_cold_load_failure_with_empty_buffer_returns_empty():
    fetch = FlakyFetch(_series(200))
    fetch.fail_after = 0                                  # 第一次冷载就抛
    t = _START + pd.Timedelta(minutes=150) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t), log=lambda *a: None)
    assert buf.get_closed_bars('X').empty                 # 无缓冲 + 拉失败 → 空,不抛

def test_long_downtime_triggers_cold_reload():
    full = _series(1000)
    fetch = RecordingFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1), log=lambda *a: None)
    buf.get_closed_bars('X')
    t2 = _START + pd.Timedelta(minutes=600) + pd.Timedelta(seconds=5)   # 跳 400 分钟 > 窗宽100
    buf._now = _now_fn_at(t2)
    buf.get_closed_bars('X')
    # 缓冲最后 ts(02:19 之后=03:39) 已早于 now-window → 走冷载全窗,而非增量
    assert fetch.calls[-1][1] == int((t2.floor('min') - pd.Timedelta(minutes=100)).value // 1_000_000) \
        or fetch.calls[-1][1] == int(t2.value // 1000) - 100 * 60_000
```

- [ ] **Step 2: 跑测试**

Run: `.venv/bin/python -m pytest tests/execution/test_bar_buffer.py -q`
Expected: PASS（Task 1 实现已覆盖这些契约；若某条失败，按测试意图修 `bar_buffer.py`，不放宽测试）

- [ ] **Step 3: 提交**

```bash
git add tests/execution/test_bar_buffer.py gridtrade/execution/bar_buffer.py
git commit -m "test(signals): 缓冲降级契约——增量失败沿用缓冲、无缓冲返空、停机久重载"
```

---

### Task 3: 配置 SIGNAL_REFRESH_SEC + factory 接线

**Files:**
- Modify: `gridtrade/config.py`（DeployConfig 字段 + load_deploy_config 解析）
- Modify: `gridtrade/runtime/factory.py:98-100`（传 refresh_sec）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DeployConfig.signal_refresh_sec: float`（默认 60.0，env `SIGNAL_REFRESH_SEC`）。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_config.py
def test_signal_refresh_sec_default_and_env():
    assert load_deploy_config(env={}).signal_refresh_sec == 60.0
    assert load_deploy_config(env={'SIGNAL_REFRESH_SEC': '900'}).signal_refresh_sec == 900.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config.py::test_signal_refresh_sec_default_and_env -q`
Expected: FAIL（`AttributeError: 'DeployConfig' object has no attribute 'signal_refresh_sec'`）

- [ ] **Step 3: 加字段 + 解析**

`gridtrade/config.py`：在 DeployConfig 的 `monitor_unit_warn_sec` 之后加字段：

```python
    signal_refresh_sec: float = 60.0    # pv/funding 每格复算节流(s);默认60=每分钟(对齐回测逐1m);900=旧节奏
```

在 `load_deploy_config` 的 `return DeployConfig(` 参数里（`monitor_interval_sec=_f(...)` 附近）加：

```python
        signal_refresh_sec=_f(env, 'SIGNAL_REFRESH_SEC', 60.0),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_config.py::test_signal_refresh_sec_default_and_env -q`
Expected: PASS

- [ ] **Step 5: factory 接线**

`gridtrade/runtime/factory.py` 第 98-100 行，`LiveSignalProvider(...)` 调用加 `refresh_sec=config.signal_refresh_sec,`：

```python
    signals = LiveSignalProvider(adapter, mult=DEFAULT_STOP_CFG['pv_mult'],
                                 period=DEFAULT_STOP_CFG['pv_period'], n=DEFAULT_STOP_CFG['pv_n'],
                                 refresh_sec=config.signal_refresh_sec, log=_flush_log)
```

- [ ] **Step 6: 提交**

```bash
git add gridtrade/config.py gridtrade/runtime/factory.py tests/test_config.py
git commit -m "feat(config): SIGNAL_REFRESH_SEC(默认60)+factory接线pv复算节流"
```

---

### Task 4: LiveSignalProvider 接缓冲 + 默认 refresh_sec=60 + 更新既有测试

**Files:**
- Modify: `gridtrade/execution/signals.py`（`__init__` 建缓冲、默认 refresh_sec=60；`_pv_spike` 改读缓冲）
- Modify: `tests/execution/test_signals.py`（既有测试适配缓冲/收盘桶/新默认）

**Interfaces:**
- Consumes: `OneMinuteBarBuffer`（Task 1）。
- Produces: `LiveSignalProvider._pv_spike` 内部改为从缓冲取已收盘 bar 后 `calc_pv_spike`；对外 `get()` 契约不变。

- [ ] **Step 1: 写失败测试**（新增：收盘桶——pv 只按已收盘 bar 算，忽略 forming 桶）

```python
# 追加到 tests/execution/test_signals.py（顶部已 import calc_pv_spike, LiveSignalProvider）
def test_pv_ignores_forming_bar():
    """末根 forming 桶(半截量)不参与 pv;pv == 只用已收盘 bar 的 calc_pv_spike。"""
    t = pd.date_range('2026-06-01', periods=121, freq='1min')
    qv = np.full(121, 1e5, dtype=float)
    qv[-16:-1] = 2e6                 # 已收盘的末 15 根(不含最后 forming)是尖峰
    qv[-1] = 0.0                     # 最后一根=forming,量为0(半截)
    bars = pd.DataFrame({'candle_begin_time': t, 'open': 100.0, 'high': 100.0,
                         'low': 100.0, 'close': 100.0, 'quote_volume': qv})
    closed = bars.iloc[:-1]          # 丢 forming
    expect = int(calc_pv_spike(closed, active_period='15min', mult=3, n=233)['pv_spike'].iloc[-1])
    assert expect == 1
    # now = 最后 forming 桶所在分钟内 → 该桶未收盘应被丢
    now_ms = int(t[-1].value // 1_000_000) + 20_000
    adp = FakeAdapter(bars=bars, funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=233, now_fn=lambda: now_ms / 1000.0)
    pv, _ = prov.get('g1', 'X', 0)
    assert pv == expect              # 只按已收盘算,forming 桶不拉低 cur
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_signals.py::test_pv_ignores_forming_bar -q`
Expected: FAIL（当前 `_pv_spike` 未丢 forming 桶，pv 受 qv[-1]=0 拉低）

- [ ] **Step 3: 改 signals.py 接缓冲 + 默认 refresh_sec=60**

`gridtrade/execution/signals.py`：顶部加 `from gridtrade.execution.bar_buffer import OneMinuteBarBuffer`。`__init__` 默认与建缓冲：

```python
    def __init__(self, adapter, *, mult=3, period='15min', n=233, refresh_sec=60,
                 now_fn=None, log=print):
        self.adapter = adapter
        self.mult = mult
        self.period = period
        self.n = n
        self.refresh_sec = float(refresh_sec)
        self._now = now_fn or time.time
        self.log = log
        self._cache = {}   # grid_id -> (fetched_at_sec, pv_spike, funding_rate)
        self._buffer = OneMinuteBarBuffer(
            fetch_fn=lambda sym, s, u: adapter.fetch_ohlcv(sym, '1m', s, u),
            window_ms=(self.n + 8) * _period_ms(self.period),
            now_fn=self._now, log=log)
```

`_pv_spike` 改为从缓冲取已收盘 bar：

```python
    def _pv_spike(self, symbol, open_ms, now_ms):
        try:
            bars = self._buffer.get_closed_bars(symbol)   # 已收盘1m、增量、失败沿用缓冲(见 bar_buffer)
            if bars is None or bars.empty or 'quote_volume' not in bars.columns:
                return 0
            sp = calc_pv_spike(bars, active_period=self.period, mult=self.mult, n=self.n)
            if sp is None or sp.empty:
                return 0
            return int(sp['pv_spike'].iloc[-1])
        except Exception as exc:
            self.log('[signals] pv_spike %s 失败降级: %r' % (symbol, exc))
            return 0
```

- [ ] **Step 4: 跑新测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_signals.py::test_pv_ignores_forming_bar -q`
Expected: PASS

- [ ] **Step 5: 修既有测试的 now/默认失配**

改动使 `_pv_spike` 走缓冲的收盘过滤（依赖 `now` 与 bar 时间一致），且默认 refresh_sec 60。修三处（其余测试不受影响）：

`test_pv_spike_matches_calc_pv_spike_and_latest_funding`：把 `now_fn=lambda: 1000.0` 改为 bar 之后：

```python
    now_ms = int(bars['candle_begin_time'].iloc[-1].value // 1_000_000) + 60_000
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=233, now_fn=lambda: now_ms / 1000.0)
```
（`expect_pv` 仍用整段 `bars` 算——now 在末根之后 60s，全部已收盘，收盘过滤不丢任何根，pv 一致。）

`test_full_window_baseline_detects_spike_vs_long_history`：把 `now_fn=lambda: 1_000_000.0`、`open_ms=999_940_000` 改为：

```python
    now_ms = int(bars['candle_begin_time'].iloc[-1].value // 1_000_000) + 60_000
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: now_ms / 1000.0)
    pv, _ = prov.get('g1', 'X', open_ms=0)
```

`test_fetch_is_1m_lookback_decoupled_from_open_ms`：该测试断言取数窗 `start == now_ms - 108*900_000`。缓冲冷载窗 = `now_ms - window_ms`，`window_ms=(100+8)*900_000` → 一致；但 now_fn=1_000_000.0 使 bar(2026) 全在 now(1e9,≈1970) 之后=全 forming → 冷载后收盘为空（不影响本测试只查 `last_ohlcv` 取数窗参数）。保持断言不变即可通过（缓冲冷载即以该窗调用 fetch_ohlcv）。若运行失败，仅将 `now_fn` 改为 bar 之后再断言窗宽：`now_ms2 = int(_bars_1m()['candle_begin_time'].iloc[-1].value//1_000_000)+60_000`，`start == now_ms2 - 108*900_000`。

- [ ] **Step 6: 跑整个 signals + buffer + config 测试**

Run: `.venv/bin/python -m pytest tests/execution/test_signals.py tests/execution/test_bar_buffer.py tests/test_config.py -q`
Expected: PASS（全绿）

- [ ] **Step 7: 跑执行/运行时全量回归**

Run: `.venv/bin/python -m pytest tests/execution tests/runtime -q`
Expected: PASS（确认接线未破坏 monitor/manager/factory）

- [ ] **Step 8: 提交**

```bash
git add gridtrade/execution/signals.py tests/execution/test_signals.py
git commit -m "feat(signals): pv接收盘1m缓冲+默认每分钟(refresh60)——机制对齐回测"
```

---

### Task 5: 上线 testnet + 观察

**前置：** Task 1-4 全绿；当前在分支 `feat/pv-live-per-minute-closed-bar`。

- [ ] **Step 1: 合并到 main 并推 origin**（硬规则:合并→推→再部署，见 memory always-push-github-before-deploy）

```bash
cd /Users/thomaschang/Projects/GridTradeBi
git checkout main && git merge --no-ff feat/pv-live-per-minute-closed-bar -m "merge: pv实盘评估机制对齐回测(收盘桶+每分钟+增量缓冲)"
git push origin main
```

- [ ] **Step 2: 部署 testnet**（testnet 可手动 flyctl，见 memory deploy-prod-via-cicd-only）

```bash
flyctl deploy -a gridtrade-bi-test
```
Expected: 部署成功、机器起来。（默认 `SIGNAL_REFRESH_SEC=60` 自动生效，无需设 secret）

- [ ] **Step 3: 观察 testnet（≥15min，覆盖至少一次 pv 复算周期）**

容器内查 pv 是否每分钟在算、无取数报错、缓冲增量生效：
```bash
flyctl logs -a gridtrade-bi-test        # 看 [monitor] round 正常、无 [bar_buffer]/[signals] 降级刷屏、无 429
```
判据（全满足才算过）：① monitor 轮正常 ok=N degraded=0；② 无持续 `[bar_buffer] fetch 降级` 或 429；③ 无 traceback。

- [ ] **Step 4: 记录观察结论**（PASS/FAIL + 关键日志片段），FAIL 则回到实现任务修，不进 Task 6。

---

### Task 6: 上线 mainnet（走 production 分支 CI/CD）

**前置：** Task 5 观察 PASS + 用户确认。**硬规则**（memory deploy-prod-via-cicd-only）：禁手动 flyctl 直推 prod；触发 CD 前必先 merge main→production 防回滚。

- [ ] **Step 1: verify-ledger 预检**（memory verify-ledger-ops-preflight）

```bash
flyctl ssh console -a gridtrade-bi-prod -C "python -m gridtrade.runtime.dbadmin verify-ledger --exchange --records"
```
Expected: `pairs_bad=0 replay_bad=0 symbol_drift=0 records_bad=0`（有背离先对账清零，不上线）。

- [ ] **Step 2: merge main → production 并推**（防 production 落后 main 触发回滚）

```bash
git checkout production && git merge --no-ff main -m "merge main → production: pv实盘评估机制对齐回测" && git push origin production
git checkout main
```

- [ ] **Step 3: 确认 CD 触发**（deploy-prod.yml）

```bash
gh run list --workflow=deploy-prod.yml -L 3
```
Expected: 新 run 触发、in_progress → success。

- [ ] **Step 4: 上线后巡检**

```bash
flyctl releases -a gridtrade-bi-prod -L 2            # 新版本 complete
flyctl logs -a gridtrade-bi-prod                     # monitor ok=N degraded=0、无 429/降级刷屏
flyctl ssh console -a gridtrade-bi-prod -C "python -m gridtrade.runtime.dbadmin verify-ledger --exchange --records"
```
判据：① 新版本部署 complete；② monitor 无 429/无持续降级；③ verify-ledger 仍全 0；④ 观察 pv 止损频率是否落在回测预期附近（若明显偏离→`fly secrets set SIGNAL_REFRESH_SEC=900` 回退 15min 节奏，仍保收盘桶+缓冲）。

---

## Self-Review

**Spec coverage：**
- (b) 收盘桶 → Task 1（`_closed`）+ Task 4（`test_pv_ignores_forming_bar`）✓
- 增量缓冲(治取数降级) → Task 1（冷载+增量）+ Task 2（降级不塌 0、停机重载）✓
- (a) 每分钟 default-on → Task 3（`SIGNAL_REFRESH_SEC=60`+factory）+ Task 4（`__init__` 默认 60）✓
- 确定性(同批收盘 1m 同 calc_pv_spike) → Task 4 既有 `test_full_window_baseline...`/`test_pv_spike_matches...` 复用 `calc_pv_spike` 断言 ✓
- 不做 signal_snapshots/滑点建模 → 计划无相关任务 ✓
- funding 路径不动 → 仅改 `_pv_spike`，`_funding_rate` 未触 ✓

**Placeholder scan：** 无 TBD/TODO；每步含完整代码与命令。Task 4 Step 5 对既有测试给了确切改法（含 fallback 断言）。✓

**Type consistency：** `OneMinuteBarBuffer(fetch_fn, window_ms, now_fn, log)` / `get_closed_bars(symbol)->DataFrame` 在 Task 1 定义、Task 4 消费一致；`fetch_fn(symbol, since_ms, until_ms)` 签名前后一致；`config.signal_refresh_sec` 在 Task 3 定义、factory 消费一致。✓
