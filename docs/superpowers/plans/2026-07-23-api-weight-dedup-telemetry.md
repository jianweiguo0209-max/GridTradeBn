# API 权重减负第一步（funding/bar 去重 + 权重遥测）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 cap2 同币双格造成的 funding/klines 重复取数，并给 ResilientAdapter 装上按分钟的权重遥测（header 水位 + 方法级调用归因），为下一刀减重提供裁决数据。

**Architecture:** 三处独立小改动：① `bar_buffer.get_closed_bars` 加"已含最新收盘桶→零请求"短路；② `LiveSignalProvider` 费率加 symbol 级 TTL 缓存（grid 级缓存保留）；③ `ResilientAdapter._call` 单咽喉计数 + `report_weight()` 分钟翻转打一行，`CcxtAdapter.used_weight_1m()` 读 ccxt 响应 header。驱动点 = monitor 轮末尾 + scheduler 选币取数循环，均 getattr 兜底。

**Tech Stack:** Python 3.9（prod 容器版本上限）、pandas、pytest、threading。

**Spec:** `docs/superpowers/specs/2026-07-23-api-weight-dedup-telemetry-design.md`

## Global Constraints

- Python 3.9 兼容（prod 容器 python3.9，禁 3.10+ 语法如 match/`|` 类型联合）
- 日志用 `%` 格式化 + `log=print` 注入约定（跟随现有代码，不用 f-string 打日志）
- **pv 机制口径零变化**：bar_buffer 返回数据必须与改动前完全一致（07-23 recon 刚验证 5/5 复现，不许破坏）
- 遥测只打日志不落库；遥测任何异常不得影响交易路径
- 注释风格跟随现有代码：中文、讲"为什么"
- TDD：每个任务先写失败测试再实现；提交信息末尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- 部署硬规则：合 main → push origin → merge main→production → push production 走 CI/CD（deploy-prod.yml）；部署前后跑 verify-ledger

---

### Task 1: bar_buffer 新鲜度短路

**Files:**
- Modify: `gridtrade/execution/bar_buffer.py:24-53`（`get_closed_bars`）
- Test: `tests/execution/test_bar_buffer.py`

**Interfaces:**
- Consumes: 无（独立改动）
- Produces: `get_closed_bars(symbol)` 行为不变，仅在"缓冲末根 == cutoff−1min"时跳过 `fetch_fn` 调用

- [ ] **Step 1: Write the failing tests**

在 `tests/execution/test_bar_buffer.py` 末尾追加（复用文件既有的 `_series`/`RecordingFetch`/`_now_fn_at`/`_START`）：

```python
def test_fresh_buffer_short_circuits_fetch():
    """同一分钟内重复调用（同币双格场景）：缓冲已含最新收盘桶 → 零新增请求、结果逐字节一致。"""
    full = _series(400)
    fetch = RecordingFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1))
    first = buf.get_closed_bars('X')
    n_calls = len(fetch.calls)                       # 冷载 1 次
    second = buf.get_closed_bars('X')                # 同分钟再调
    assert len(fetch.calls) == n_calls               # 短路：零新增 fetch
    pd.testing.assert_frame_equal(first, second)     # 数据完全同源


def test_fresh_short_circuit_unsticks_after_minute_rollover():
    """分钟翻转后必须恢复增量取数（防过度缓存吃掉新收盘桶）。"""
    full = _series(400)
    fetch = RecordingFetch(full)
    now = {'ts': _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)}
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000,
                             now_fn=lambda: now['ts'].value / 1e9)
    buf.get_closed_bars('X')
    n_calls = len(fetch.calls)
    now['ts'] += pd.Timedelta(minutes=1)             # 进入下一分钟
    bars = buf.get_closed_bars('X')
    assert len(fetch.calls) == n_calls + 1           # 恢复增量 fetch
    assert bars['candle_begin_time'].max() == _START + pd.Timedelta(minutes=200)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/execution/test_bar_buffer.py -v -k "fresh"`
Expected: `test_fresh_buffer_short_circuits_fetch` FAIL（`len(fetch.calls) == n_calls` 断言失败——现实现同分钟也发增量请求）；rollover 用例可能 PASS（回归护栏，保留）。

- [ ] **Step 3: Implement the short-circuit**

`gridtrade/execution/bar_buffer.py` 中 `get_closed_bars` 整体替换为：

```python
    def get_closed_bars(self, symbol):
        now_ms = int(self._now() * 1000)
        cutoff = pd.Timestamp(now_ms, unit='ms').floor('min')   # 当前 forming 分钟起点
        cutoff_ms = int(cutoff.value // 1_000_000)
        lo_ms = cutoff_ms - self.window_ms                      # 窗口按收盘分钟边界对齐
        buf = self._buf.get(symbol)
        last_ms = (int(buf['candle_begin_time'].iloc[-1].value // 1_000_000)
                   if buf is not None and not buf.empty else None)
        # 新鲜度短路：末根==cutoff-1min ⇒ 缓冲已含最新已收盘桶，增量 fetch 只会拉到
        # 被丢弃的 forming 桶=纯浪费权重（cap2 同币双格每分钟重复拉的根因，2026-07-23）。
        # 跳过取数直接走既有切片，返回数据与 fetch 后完全同源——pv 机制口径零变化。
        if last_ms != cutoff_ms - _MIN_MS:
            stale = last_ms is None or last_ms < lo_ms
            try:
                if stale:
                    df = self.fetch_fn(symbol, lo_ms, now_ms)
                    buf = self._closed(df, cutoff)
                else:
                    inc = self._closed(self.fetch_fn(symbol, last_ms + _MIN_MS, now_ms), cutoff)
                    if not inc.empty:
                        buf = (pd.concat([buf, inc], ignore_index=True)
                               .drop_duplicates('candle_begin_time')
                               .sort_values('candle_begin_time'))
            except Exception as exc:        # 降级：沿用旧缓冲，绝不塌回空
                self.log('[bar_buffer] %s fetch 降级,沿用缓冲: %r' % (symbol, exc))
                if buf is None:
                    return pd.DataFrame()
        if buf is None or buf.empty:
            return pd.DataFrame()
        lo = pd.Timestamp(lo_ms, unit='ms')
        buf = buf[(buf['candle_begin_time'] >= lo)
                  & (buf['candle_begin_time'] < cutoff)].reset_index(drop=True)
        self._buf[symbol] = buf
        return buf
```

（等价重构说明：原 `stale = buf is None or buf.empty or last < lo_ms` 改写为基于 `last_ms is None` 判定，行为不变；仅新增 `last_ms == cutoff_ms - _MIN_MS` 短路分支。）

- [ ] **Step 4: Run the full bar_buffer suite**

Run: `python -m pytest tests/execution/test_bar_buffer.py -v`
Expected: 全部 PASS（含既有 5 个用例——冷载/增量/降级/停机重载语义不变）。

- [ ] **Step 5: Commit**

```bash
git add tests/execution/test_bar_buffer.py gridtrade/execution/bar_buffer.py
git commit -m "feat(bar_buffer): 已含最新收盘桶时短路fetch——同币双格分钟内重复拉取归零

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: funding_rate symbol 级 TTL 缓存

**Files:**
- Modify: `gridtrade/execution/signals.py`（`__init__` + `_funding_rate`）
- Test: `tests/execution/test_signals.py`

**Interfaces:**
- Consumes: 无（独立改动）
- Produces: `_funding_rate(symbol, now_ms)` 语义不变；同 symbol 在 `refresh_sec` 内只真 fetch 一次

- [ ] **Step 1: Write the failing tests**

在 `tests/execution/test_signals.py` 末尾追加（复用既有 `FakeAdapter`/`_bars_with_spike`/`_funding`）：

```python
def test_funding_deduped_across_grids_same_symbol():
    """cap2 同币双格：第二格在 refresh 窗内取同 symbol 费率 → 命中 symbol 缓存，零新增请求。"""
    now = {'t': 1000.0}
    adp = FakeAdapter(bars=_bars_with_spike(), funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, refresh_sec=60, now_fn=lambda: now['t'])
    prov.get('g1', 'X/USDT:USDT', open_ms=0)
    prov.get('g2', 'X/USDT:USDT', open_ms=0)         # 同币第二格（不同 grid_id）
    assert adp.funding_calls == 1                    # funding 只真取一次
    now['t'] += 61.0                                 # TTL 过期
    prov.get('g1', 'X/USDT:USDT', open_ms=0)
    assert adp.funding_calls == 2                    # 过期后重取


def test_funding_failure_not_cached_at_symbol_level():
    """取数失败降级返 0.0 但不得写入 symbol 缓存——否则降级值被粘住 refresh_sec。"""
    now = {'t': 1000.0}
    adp = FakeAdapter(bars=_bars_with_spike(), funding=_funding([0.001]),
                      raise_funding=True)
    prov = LiveSignalProvider(adp, refresh_sec=60, now_fn=lambda: now['t'])
    _, fr = prov.get('g1', 'X/USDT:USDT', open_ms=0)
    assert fr == 0.0                                 # 失败降级
    adp.raise_funding = False
    _, fr = prov.get('g2', 'X/USDT:USDT', open_ms=0) # 另一格随后取
    assert abs(fr - 0.001) < 1e-12                   # 未被降级值污染
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/execution/test_signals.py -v -k "funding_deduped or not_cached"`
Expected: `test_funding_deduped_across_grids_same_symbol` FAIL（`funding_calls == 1` 断言失败，现为 2）；failure 用例应 PASS（回归护栏，保留）。

- [ ] **Step 3: Implement the symbol cache**

`gridtrade/execution/signals.py` `__init__` 中 `self._cache = {}` 行之后加：

```python
        # symbol 级费率缓存（2026-07-23）：cap2 同币双格下按 grid 节流会同币重复取数
        # （日志实锤 GWEI 同秒×2）。费率每 8h 才结算，refresh_sec 内复用零信息损失。
        self._fr_cache = {}   # symbol -> (fetched_at_sec, funding_rate)
```

`_funding_rate` 整体替换为：

```python
    def _funding_rate(self, symbol, now_ms):
        now = now_ms / 1000.0
        c = self._fr_cache.get(symbol)
        if c is not None and (now - c[0]) < self.refresh_sec:
            return c[1]
        try:
            # 回看窗=结算周期+1h——币安 8h 结算下固定 3h 窗有 5/8 时间取不到最新费率(终审实证)。
            hours = float(getattr(self.adapter, 'FUNDING_INTERVAL_HOURS', 8)) + 1.0
            fh = self.adapter.fetch_funding_history(
                symbol, now_ms - int(hours * 3600_000), now_ms)
            if fh is None or len(fh) == 0:
                return 0.0            # 空结果（如新上币）不缓存：下次照常重试
            fr = float(fh.sort_values('ts')['fundingRate'].iloc[-1])
            self._fr_cache[symbol] = (now, fr)   # 只缓存真取到的费率；失败/空不污染
            return fr
        except Exception as exc:
            self.log('[signals] funding_rate %s 失败降级: %r' % (symbol, exc))
            return 0.0
```

（`_fr_cache` 不随 `evict` 清理：另一同币格可能仍在用，TTL 自过期；条目数=历史见过的 symbol 数，量级几百、无泄漏之虞。dict 读写在 GIL 下与既有 `_cache` 同级线程安全。）

- [ ] **Step 4: Run the full signals suite**

Run: `python -m pytest tests/execution/test_signals.py -v`
Expected: 全部 PASS（既有节流/降级/对齐用例语义不变——它们全是单 grid 单 symbol，症状不重叠）。

- [ ] **Step 5: Commit**

```bash
git add tests/execution/test_signals.py gridtrade/execution/signals.py
git commit -m "feat(signals): funding按symbol级TTL缓存——cap2同币双格重复取数减半

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: CcxtAdapter.used_weight_1m()

**Files:**
- Modify: `gridtrade/exchanges/ccxt_adapter.py`（类内新增方法，放 `_now_ms` 附近的工具方法区）
- Test: `tests/exchanges/test_weight_telemetry.py`（新建）

**Interfaces:**
- Consumes: ccxt client 的 `last_response_headers` 属性（币安每个响应带 `x-mbx-used-weight-1m`）
- Produces: `used_weight_1m() -> Optional[int]`——Task 4 的 `report_weight` 经 `getattr(self._inner, 'used_weight_1m', None)` 消费

- [ ] **Step 1: Write the failing tests**

新建 `tests/exchanges/test_weight_telemetry.py`：

```python
"""权重遥测：CcxtAdapter.used_weight_1m 读 header + ResilientAdapter 计数/分钟上报。"""
import threading
from types import SimpleNamespace

from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter

NOSLEEP = lambda d: None
FAST = RetryPolicy(max_attempts=4, base_delay=0.01, rate_limit_base_delay=0.01,
                   max_delay=0.01)


def _ccxt_with_headers(headers):
    client = SimpleNamespace(id='binanceusdm', last_response_headers=headers)
    return CcxtAdapter(client)


def test_used_weight_reads_header_case_insensitive():
    assert _ccxt_with_headers({'X-MBX-USED-WEIGHT-1M': '1106'}).used_weight_1m() == 1106
    assert _ccxt_with_headers({'x-mbx-used-weight-1m': '53'}).used_weight_1m() == 53


def test_used_weight_none_when_header_missing_or_bad():
    assert _ccxt_with_headers({}).used_weight_1m() is None
    assert _ccxt_with_headers(None).used_weight_1m() is None
    assert _ccxt_with_headers({'X-MBX-USED-WEIGHT-1M': 'nan?'}).used_weight_1m() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/exchanges/test_weight_telemetry.py -v`
Expected: FAIL with `AttributeError: 'CcxtAdapter' object has no attribute 'used_weight_1m'`

- [ ] **Step 3: Implement used_weight_1m**

`gridtrade/exchanges/ccxt_adapter.py`，`_now_ms` 方法之后加：

```python
    def used_weight_1m(self):
        """当前分钟已用请求权重：读最近一次响应的 x-mbx-used-weight-1m header，
        零额外请求（2026-07-23 权重遥测）。header 缺失/非币安内核 → None。
        供 ResilientAdapter.report_weight 上报水位。"""
        headers = getattr(self.client, 'last_response_headers', None) or {}
        for k, v in headers.items():
            if str(k).lower() == 'x-mbx-used-weight-1m':
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/exchanges/test_weight_telemetry.py tests/exchanges/test_ccxt_adapter.py -v`
Expected: 全部 PASS（新方法纯读属性，不影响既有 CcxtAdapter 用例）。

- [ ] **Step 5: Commit**

```bash
git add tests/exchanges/test_weight_telemetry.py gridtrade/exchanges/ccxt_adapter.py
git commit -m "feat(ccxt): used_weight_1m读响应header——零额外请求取权重水位

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: ResilientAdapter 计数 + report_weight

**Files:**
- Modify: `gridtrade/exchanges/resilient_adapter.py`（`__init__`/`_call` + 新方法）
- Test: `tests/exchanges/test_weight_telemetry.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `used_weight_1m() -> Optional[int]`（getattr 兜底，HL/测试适配器无此方法 → `w1m=?`）
- Produces: `report_weight(log=print, now=None) -> None`——分钟翻转打一行并清零，同分钟 no-op，绝不抛异常。Task 5 的 cycles/scheduler 消费

- [ ] **Step 1: Write the failing tests**

`tests/exchanges/test_weight_telemetry.py` 追加（`_Inner` 模式仿 `test_resilient_adapter.py`，此处只需两个读方法）：

```python
class _Inner:
    name = 'inner-ex'

    def fetch_price(self, symbol):
        return 123.5

    def fetch_balance(self):
        return 'BAL'


def test_report_on_minute_rollover_then_noop_same_minute():
    adp = ResilientAdapter(_Inner(), policy=FAST, sleep=NOSLEEP)
    adp.fetch_price('X'); adp.fetch_price('X'); adp.fetch_balance()
    lines = []
    adp.report_weight(log=lines.append, now=60.0)    # 首次翻转 → 打点
    assert len(lines) == 1
    assert 'fetch_price=2' in lines[0] and 'fetch_balance=1' in lines[0]
    assert 'w1m=?' in lines[0]                       # inner 无 used_weight_1m → 优雅降级
    adp.report_weight(log=lines.append, now=90.0)    # 同一分钟 → no-op
    assert len(lines) == 1
    adp.report_weight(log=lines.append, now=120.0)   # 新分钟但计数已清零 → 静默
    assert len(lines) == 1
    adp.fetch_price('Y')
    adp.report_weight(log=lines.append, now=180.0)   # 有新调用 → 再打点
    assert len(lines) == 2 and 'fetch_price=1' in lines[1]


def test_report_includes_inner_used_weight():
    inner = _Inner()
    inner.used_weight_1m = lambda: 1106
    adp = ResilientAdapter(inner, policy=FAST, sleep=NOSLEEP)
    adp.fetch_price('X')
    lines = []
    adp.report_weight(log=lines.append, now=60.0)
    assert 'w1m=1106' in lines[0]


def test_report_never_raises_on_internal_failure():
    inner = _Inner()

    def _boom():
        raise RuntimeError('boom')
    inner.used_weight_1m = _boom
    adp = ResilientAdapter(inner, policy=FAST, sleep=NOSLEEP)
    adp.fetch_price('X')
    lines = []
    adp.report_weight(log=lines.append, now=60.0)    # 不抛
    assert any('report failed' in ln for ln in lines)


def test_concurrent_counting_no_loss():
    adp = ResilientAdapter(_Inner(), policy=FAST, sleep=NOSLEEP)
    threads = [threading.Thread(target=lambda: [adp.fetch_price('X') for _ in range(200)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = []
    adp.report_weight(log=lines.append, now=60.0)
    assert 'fetch_price=1600' in lines[0]            # 8×200 无丢计
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/exchanges/test_weight_telemetry.py -v`
Expected: 新增 4 个用例 FAIL with `AttributeError: ... no attribute 'report_weight'`；Task 3 的 2 个用例仍 PASS。

- [ ] **Step 3: Implement counting + report**

`gridtrade/exchanges/resilient_adapter.py` `__init__` 末尾（`self._rng = rng` 之后）加：

```python
        # 权重遥测（2026-07-23）：_call 单咽喉计逻辑调用数（分页方法计一、重试不重复计,
        # 找权重大头的归因够用）。分钟翻转由 report_weight 打点并清零。
        self._weight_lock = threading.Lock()
        self._call_counts = {}
        self._last_report_min = None
```

`_call` 开头（`inner_fn = ...` 之前）加：

```python
        with self._weight_lock:
            self._call_counts[_name] = self._call_counts.get(_name, 0) + 1
```

类内新增方法（`_call` 之后）：

```python
    def report_weight(self, log=print, now=None):
        """权重遥测上报：分钟翻转时打一行「header 水位 + 方法级调用计数(降序)」并清零；
        同分钟内 no-op（驱动方每轮无脑调即可）。契约：绝不抛异常——遥测不得影响交易路径。
        计数窗≈上一分钟（驱动粒度 monitor ~13s/轮，边界误差 ≤ 一轮）。"""
        try:
            minute = int((now if now is not None else time.time()) // 60)
            if minute == self._last_report_min:
                return
            self._last_report_min = minute
            with self._weight_lock:
                counts, self._call_counts = self._call_counts, {}
            if not counts:
                return                      # 静默期不刷屏
            fn = getattr(self._inner, 'used_weight_1m', None)
            w = fn() if fn is not None else None
            top = sorted(counts.items(), key=lambda kv: -kv[1])
            log('[weight] w1m=%s calls/min: %s'
                % ('?' if w is None else w,
                   ' '.join('%s=%d' % kv for kv in top)))
        except Exception as exc:
            try:
                log('[weight] report failed: %r' % exc)
            except Exception:
                pass                        # log 本身坏了也不外抛
```

- [ ] **Step 4: Run telemetry + resilient adapter suites**

Run: `python -m pytest tests/exchanges/test_weight_telemetry.py tests/exchanges/test_resilient_adapter.py tests/exchanges/test_resilience.py -v`
Expected: 全部 PASS（计数在 `_call` 咽喉、锁只护 dict 累加，不改变重试/熔断语义）。

- [ ] **Step 5: Commit**

```bash
git add tests/exchanges/test_weight_telemetry.py gridtrade/exchanges/resilient_adapter.py
git commit -m "feat(resilient): 单咽喉调用计数+report_weight分钟上报——权重归因遥测

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 驱动点接线（monitor 轮末尾 + scheduler 取数循环）

**Files:**
- Modify: `gridtrade/runtime/cycles.py:359-366`（轮次总结行之后）
- Modify: `gridtrade/runtime/scheduler.py:44-60`（`_fetch_pass`）
- Test: `tests/runtime/test_weight_report_wiring.py`（新建；`_fetch_pass` 既有测试在 `tests/runtime/test_scheduler.py`，跑它做回归）

**Interfaces:**
- Consumes: Task 4 的 `report_weight(log=print, now=None)`（getattr 兜底——测试 fake/HL 适配器无此方法时静默跳过）
- Produces: monitor/scheduler 进程各自每分钟一行 `[weight] w1m=... calls/min: ...` 日志

- [ ] **Step 1: Write the failing test（scheduler 侧）**

新建 `tests/runtime/test_weight_report_wiring.py`（`_fetch_pass` 签名实读：入参 `(adapter, symbols, timeframe, start_ms, end_ms, pace_ms, sleep)`、返回三元组 `(成功dict, 跳过名单, 首个错误样本)`）：

```python
"""权重遥测接线：_fetch_pass 逐币驱动 report_weight + 无此方法的适配器 getattr 兜底。"""
import pandas as pd

from gridtrade.runtime.scheduler import _fetch_pass


def _one_bar(*_args):
    return pd.DataFrame({'candle_begin_time': [pd.Timestamp('2026-06-01')],
                         'open': [1.0], 'high': [1.0], 'low': [1.0],
                         'close': [1.0], 'volume': [1.0]})


class _PacedAdapter:
    """有 report_weight 的假适配器：验证取数循环逐币驱动上报（分钟节流在 report 内部）。"""
    def __init__(self):
        self.report_calls = 0

    def report_weight(self, log=None):
        self.report_calls += 1

    def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
        return _one_bar()


class _PlainAdapter:
    """无 report_weight 的假适配器：接线必须 getattr 兜底不炸。"""
    def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
        return _one_bar()


def test_fetch_pass_drives_report_weight_per_symbol():
    adp = _PacedAdapter()
    out, skipped, first_err = _fetch_pass(adp, ['A', 'B', 'C'], '1h', 0, 3_600_000,
                                          0, lambda s: None)
    assert adp.report_calls == 3                     # 每币驱动一次
    assert set(out) == {'A', 'B', 'C'} and skipped == []


def test_fetch_pass_tolerates_adapter_without_report_weight():
    out, skipped, first_err = _fetch_pass(_PlainAdapter(), ['A'], '1h', 0, 3_600_000,
                                          0, lambda s: None)
    assert 'A' in out and skipped == []              # 不炸、取数正常
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/runtime/ -v -k "fetch_pass"`
Expected: `test_fetch_pass_drives_report_weight_per_symbol` FAIL（`report_calls == 0`）；`_PlainAdapter` 用例 PASS（现状本来不炸，回归护栏）。

- [ ] **Step 3: Wire both drive points**

`gridtrade/runtime/scheduler.py` `_fetch_pass` 改为（`rw` 在循环外取好一次；循环体内 `sleep` 之后、`try:` 之前驱动）：

```python
def _fetch_pass(adapter, symbols, timeframe, start_ms, end_ms, pace_ms, sleep):
    """单轮逐币拉取:返回 (成功dict, 跳过名单, 首个错误样本)。空 df 不算跳过(合法无数据)。"""
    out, skipped, first_err = {}, [], None
    rw = getattr(adapter, 'report_weight', None)   # 权重遥测(2026-07-23):无此方法的适配器静默跳过
    for i, sym in enumerate(symbols):
        if i and pace_ms > 0:
            sleep(pace_ms / 1000.0)   # 币间节流（默认开；env SCHEDULER_FETCH_PACE_MS 可调，0=关）
        if rw is not None:
            rw()                      # 选币轮分钟内也出归因线（分钟翻转才真打,其余 no-op）
        try:
            df = adapter.fetch_ohlcv(sym, timeframe, start_ms, end_ms)
        except Exception as exc:
            skipped.append(sym)     # 坏币（BadSymbol/无数据/拉取失败）跳过，不阻塞整池
            if first_err is None:
                first_err = '%s -> %r' % (sym, exc)
            continue
        if df is not None and not df.empty:
            out[sym] = df
    return out, skipped, first_err
```

`gridtrade/runtime/cycles.py` 轮次总结 `log('[monitor] round grids=...')` 语句块之后（约 :366，`if commands is not None` 之前）加：

```python
    rw = getattr(getattr(manager.executor, 'adapter', None), 'report_weight', None)
    if rw is not None:      # 权重遥测：每轮驱动一次（分钟翻转才真打;fake/HL 适配器无此方法→跳过）
        rw(log)
```

- [ ] **Step 4: Run wiring tests + full runtime suite**

Run: `python -m pytest tests/runtime/ -v`
Expected: 全部 PASS（cycles 既有用例的 fake executor.adapter 无 `report_weight` → getattr 兜底静默；scheduler 新用例双绿）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/runtime/cycles.py gridtrade/runtime/scheduler.py tests/runtime/
git commit -m "feat(runtime): monitor轮末+选币取数循环接线report_weight——两进程每分钟出权重归因线

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 全量验证 + 部署（CI/CD）

**Files:**
- 无新改动；验证 + 发布

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: 全绿（0 failed）。任何红必须先修复，禁止带红进部署。

- [ ] **Step 2: 部署前 verify-ledger（运维前置门）**

```bash
flyctl ssh console -a gridtrade-bi-prod --machine d8d2219b970958 -C "python -m gridtrade.runtime.dbadmin verify-ledger"
```
Expected: `pairs_bad=0 replay_bad=0 symbol_drift=0`。

- [ ] **Step 3: 推送 main 并合入 production（⚠ 此步执行前需用户确认）**

```bash
git push origin main
git fetch origin production
git log --oneline origin/production -1     # 确认 production 现状（防落后回滚坑）
git checkout production && git pull origin production
git merge main                              # 必须先 merge main 防把 prod 回滚
git push origin production                  # 触发 deploy-prod.yml CI/CD
git checkout main
```

- [ ] **Step 4: 盯 CD 与部署后验证**

```bash
gh run watch --repo <origin> || gh run list --limit 3    # CD success
flyctl status -a gridtrade-bi-prod                       # 机器版本 +1、全部 started
flyctl logs -a gridtrade-bi-prod --no-tail | grep "\[weight\]" | head -5
```
Expected: 日志出现每分钟 `[weight] w1m=<数值> calls/min: ...` 行；monitor 轮 `ok=N degraded=0` 正常。

- [ ] **Step 5: 部署后 verify-ledger + 验收记录**

```bash
flyctl ssh console -a gridtrade-bi-prod -C "python -m gridtrade.runtime.dbadmin verify-ledger"
```
Expected: clean。随后现场 tail 10-15 分钟日志，记录 top 权重消费方法（验收第 4 条：为下一刀供裁决数据）；确认不再出现同币同秒 funding×2。
