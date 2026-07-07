# 实盘 PV legacy 满窗语义移植(阶段二)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实盘 PV 信号恢复 legacy 满窗语义(原生 15m×108 根,≈27h 真滚动基线)并把默认参数翻至用户拍板组合(pv_pnl_thr=+0.005、pv_n=100)。

**Architecture:** 只动两处——`LiveSignalProvider._pv_spike` 的取数窗口(open_ms 起→now−108×15min 起、timeframe '1m'→'15m')和 `DEFAULT_STOP_CFG` 两个默认值;`calc_pv_spike`/节流/降级/`get()` 签名/cycles.py 全不动。测试先行(TDD),FakeAdapter 加取数参数记录。

**Tech Stack:** Python 3.11 / pytest / pandas;测试全离线(FakeAdapter)。

## Global Constraints

- 参数组合(spec 钉死):legacy 满窗语义 × `pv_pnl_thr=+0.005` × `pv_mult=3` × `pv_n=100` × con2=0。
- 取数:`fetch_ohlcv(symbol, '15m', now_ms − (n+8)×900_000, now_ms)`(n=100 → 108 根 ≈ 27h)。
- `get(grid_id, symbol, open_ms)` 签名不变(open_ms 保留、不再决定取数窗口);`cycles.py` 零改动。
- 引擎金标不破(`simulate_grid_engine` 独立默认 `pv_pnl_thr=-0.015` 不动)。
- **实现完成后停在"报用户批 push main/testnet 部署"门前,不自行 push。**

---

### Task 1: config 默认值翻转(pv_pnl_thr / pv_n)

**Files:**
- Modify: `gridtrade/config.py`(DEFAULT_STOP_CFG,约 173-176 行)
- Test: `tests/test_config.py`(约 124-129 行断言块)

**Interfaces:**
- Consumes: 无(独立)
- Produces: `DEFAULT_STOP_CFG['pv_pnl_thr'] == 0.005`、`DEFAULT_STOP_CFG['pv_n'] == 100`(Task 2 的 signals 经 factory.py 自动读 `pv_n`)

- [ ] **Step 1: 更新 test_config 断言(写失败测试)**

`tests/test_config.py` 把:

```python
    assert DEFAULT_STOP_CFG['pv_pnl_thr'] == -0.02
```

改为:

```python
    assert DEFAULT_STOP_CFG['pv_pnl_thr'] == 0.005   # 尖峰时浮盈<+0.5%即撤(2026-07-07 PV研究)
    assert DEFAULT_STOP_CFG['pv_mult'] == 3
    assert DEFAULT_STOP_CFG['pv_n'] == 100            # 量能基线 25h 真滚动窗(n 扫描甜点档)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL(`pv_pnl_thr == 0.005` 断言不成立,现值 -0.02)

- [ ] **Step 3: 改 config 默认值**

`gridtrade/config.py` DEFAULT_STOP_CFG 中:

```python
    'pv_pnl_thr': 0.005,               # pv 触发门槛:尖峰时 pnlRatio<+0.005 即撤(2026-07-07 PV研究,evaluate_exit 读此值)
    'pv_mult': 3,                      # 量能尖峰倍数(LiveSignalProvider 算 pv_spike 用)
    'pv_period': '15min',              # 量能重采样周期('15min' 非 '15m'——后者被 pandas 当月)
    'pv_n': 100,                       # 量能基线滚动窗口(15m×100≈25h 真滚动;signals 取 n+8 根前置历史)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gridtrade/config.py tests/test_config.py
git commit -m "feat(config): PV 默认参数翻至研究终配置(pv_pnl_thr +0.005 / pv_n 100)"
```

### Task 2: signals 满窗取数(原生 15m × n+8 根)

**Files:**
- Modify: `gridtrade/execution/signals.py`(模块 docstring + `_pv_spike`)
- Test: `tests/execution/test_signals.py`(FakeAdapter 录参 + 2 个新测试)

**Interfaces:**
- Consumes: `DEFAULT_STOP_CFG['pv_n']`(经 factory.py 传入 `n=100`,Task 1 已翻)
- Produces: `LiveSignalProvider._pv_spike(symbol, open_ms, now_ms)` 行为变更(取数窗口与 open_ms 解耦);`get()` 签名不变

- [ ] **Step 1: FakeAdapter 录参 + 写失败测试**

`tests/execution/test_signals.py` 中 FakeAdapter.fetch_ohlcv 改为录参:

```python
    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        self.ohlcv_calls += 1
        self.last_ohlcv = (symbol, timeframe, int(start_ms), int(end_ms))
        if self.raise_ohlcv:
            raise RuntimeError('boom')
        return self._bars
```

文件顶部工具函数区加 15m bars 构造器,文件末尾追加两个测试:

```python
def _bars_15m(n=108, base_qv=1e5, last_qv=None):
    t = pd.date_range('2026-06-01', periods=n, freq='15min')
    qv = np.full(n, base_qv, dtype=float)
    if last_qv is not None:
        qv[-1] = last_qv
    return pd.DataFrame({'candle_begin_time': t, 'open': 100.0, 'high': 100.0,
                         'low': 100.0, 'close': 100.0, 'quote_volume': qv})


def test_fetch_window_is_15m_lookback_decoupled_from_open_ms():
    """legacy 满窗语义:取数=原生 15m、窗口=now−(n+8)×15min,与 open_ms 解耦
    (spec 2026-07-07-pv-legacy-semantics-live)。"""
    adp = FakeAdapter(bars=_bars_15m(), funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: 1_000_000.0)
    now_ms = 1_000_000_000
    prov.get('g1', 'X', open_ms=now_ms - 60_000)      # 开格才 1 分钟
    sym, tf, start, end = adp.last_ohlcv
    assert tf == '15m'
    assert end == now_ms
    assert start == now_ms - 108 * 900_000            # (n+8)×15min,与 open_ms 无关

    prov2 = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: 1_000_000.0)
    prov2.get('g2', 'X', open_ms=0)                   # 开格很久
    assert adp.last_ohlcv[2] == now_ms - 108 * 900_000  # 窗口不随 open_ms 变


def test_full_window_baseline_detects_spike_vs_long_history():
    """满窗行为差分:107 根低量历史 + 最后一根 5×爆量 → rolling(100) 满窗基线判尖峰。
    (旧实现开格 1 分钟只有 1 根 bar,expanding 基线=自身,永远判不出尖峰。)"""
    bars = _bars_15m(n=108, base_qv=1e5, last_qv=5e5)   # 5×基线 > mult=3
    adp = FakeAdapter(bars=bars, funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: 1_000_000.0)
    pv, _ = prov.get('g1', 'X', open_ms=999_940_000)    # 开格才 1 分钟,旧语义必 0
    assert pv == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_signals.py -q`
Expected: 2 个新测试 FAIL(现实现 timeframe='1m'、start=open_ms);旧测试仍 PASS

- [ ] **Step 3: 改 `_pv_spike` 实现**

`gridtrade/execution/signals.py`:

```python
    def _pv_spike(self, symbol, open_ms, now_ms):
        try:
            # legacy 满窗语义(spec 2026-07-07):原生 15m 取 n+8 根(n=100→108根≈27h),
            # rolling(n) 为真滑动基线;窗口与 open_ms 解耦(open_ms 仅保留签名兼容)。
            since_ms = now_ms - (self.n + 8) * 900_000
            bars = self.adapter.fetch_ohlcv(symbol, '15m', since_ms, now_ms)
            if bars is None or len(bars) == 0 or 'quote_volume' not in bars.columns:
                return 0
            sp = calc_pv_spike(bars, active_period=self.period, mult=self.mult, n=self.n)
            if sp is None or sp.empty:
                return 0
            return int(sp['pv_spike'].iloc[-1])
        except Exception as exc:     # 取数失败降级为「无尖峰」,不误触发也不阻塞
            self.log('[signals] pv_spike %s 失败降级: %r' % (symbol, exc))
            return 0
```

模块 docstring 第一条设计要点改为:

```
- **legacy 满窗语义(2026-07-07)**:pv_spike 复用 core.grid_engine.calc_pv_spike(同一函数、
  同一 15min 粒度),数据取原生 15m K线 n+8 根(≈25h+缓冲),rolling(n) 为真滑动基线——
  对齐 legacy(OKX 时代 15m×rolling 满窗)语义;与开格时刻解耦。
```

- [ ] **Step 4: 跑 signals 全部测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_signals.py -q`
Expected: 全 PASS(含旧 5 个:`test_pv_spike_matches_calc_pv_spike_and_latest_funding` 等只依赖 FakeAdapter 回传 bars,不依赖取数窗口)

- [ ] **Step 5: 全套回归**

Run: `.venv/bin/python -m pytest -q`
Expected: 全 PASS(引擎金标:`simulate_grid_engine` 独立默认未动;stop_rules 显式传参不受影响)

- [ ] **Step 6: Commit**

```bash
git add gridtrade/execution/signals.py tests/execution/test_signals.py
git commit -m "feat(signals): PV 满窗取数(原生 15m×n+8 根),恢复 legacy 语义、窗口与开格解耦"
```

### Task 3: 收尾(STATUS 记档 + 停在部署门)

**Files:**
- Modify: `docs/STATUS.md`(网格/止损相关段落追加一行阶段二记录)

**Interfaces:**
- Consumes: Task 1/2 完成
- Produces: 文档同步;**不 push、不部署**——停在门前报用户

- [ ] **Step 1: STATUS.md 追加记录**

在 gotchas/近期变更相关区追加(措辞按文件现有风格):

```
- **PV 止损换形(2026-07-07,待部署)**:实盘 PV 恢复 legacy 满窗语义(signals 原生 15m×108 根≈27h 真滚动基线,与开格解耦)+ 默认 pv_pnl_thr −0.02→+0.005、pv_n 233→100。回测四窗全正(诚实均值 +2.64%),~70% 格首个真尖峰即撤(策略换形)。spec/plan: docs/superpowers/{specs,plans}/2026-07-07-pv-legacy-semantics-live*。上线:main→testnet(无报错即可)→production 均需用户批。
```

- [ ] **Step 2: Commit**

```bash
git add docs/STATUS.md
git commit -m "docs(status): PV legacy 满窗语义移植记档(待部署)"
```

- [ ] **Step 3: 停——报用户批 push main / testnet 部署**

输出改动摘要+测试结果,等待用户批准 push。**不得自行 push。**

## Self-Review

- Spec 覆盖:signals 取数(Task 2)✓ / config 两默认值(Task 1)✓ / 测试含窗口断言+满窗差分(Task 2)✓ / 停在部署门(Task 3)✓;spec 的"回测管线缺口/非目标"无需任务(记档已在 spec)。
- 占位符:无 TBD/TODO;所有代码块完整。
- 类型/签名一致:`get(grid_id, symbol, open_ms)` 不变;`fetch_ohlcv(symbol, timeframe, start_ms, end_ms)` 与 FakeAdapter/ccxt_adapter 签名一致;`(self.n + 8) * 900_000` 毫秒数正确(15min=900_000ms)。
