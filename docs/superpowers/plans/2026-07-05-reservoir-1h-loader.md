# Reservoir 1s→1h/1m 全市场装载器 + 1h 数据源自动切换 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把回测 1h 选币数据的可回溯起点从 HL API 滚动 ~208 天拓展到 Reservoir 归档起点 2025-07-31（最早窗口起点 2025-08-14），一次 day 文件下载同时产出 1h(全币)+1m(全币)，main() 按窗口自动切源。

**Architecture:** 三步：① `candles_1s_to_1m` 泛化成 `candles_1s_resample(df, symbol_map, rule)`（1s→任意周期，薄包装保兼容）；② `warm_reservoir_1m` 泛化成 `warm_reservoir_ohlcv(..., timeframes=('1h','1m'))`（同一 day 文件循环内写多命名空间，幂等条件=所有 timeframe 全命中，薄包装保兼容）；③ `backtest_run.main()` 加纯函数 `_pick_1h_source` 按窗口自动切源 + Reservoir 起点守卫（守卫在任何网络调用之前）。

**Tech Stack:** Python 3.9 / pandas 1.3.5 / pyarrow；S3 走 `aws s3 cp` subprocess（现状）；不新增第三方依赖。

## Global Constraints

- 依赖冻结：Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / pyarrow；不新增第三方库。
- **近窗口（api 源）行为字节不变**：`_pick_1h_source` 返回 `'api'` 时 main() 走现路径，与改动前逐位一致。
- `warm_reservoir_1m` 公共签名/返回格式向后兼容：现有 `tests/backtest/test_reservoir.py` 5 个测试**零改动**保持绿。
- 「不完整的天不缓存」语义全保留：当天(UTC)未过完 / S3 404 / 拉取失败 → `retry_later` 不落任何文件；日文件成功但某币当天无成交 → 落空哨兵。
- 不动 `gridtrade/core/`（金标不碰）；不动 funding 路径。
- 幂等跳过条件 = **所有** timeframes 的整天全命中才 skip；只差其一也重下 day 文件补齐（覆盖写同值幂等无害）。
- 测试命令：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（单文件加 `<路径> -v`）。
- 常量精确值：`RESERVOIR_START = pd.Timestamp('2025-07-31')`；`_API_1H_MAX_DAYS = 200`；timeframe→rule 映射 `_RULES = {'1m': '1min', '1h': '1H'}`。

**前置（controller 执行，非 SDD 任务）**：提交工作树中的 con2 实验改动（`gridtrade/core/grid_engine.py` + `gridtrade/backtest/backtest_run.py`，默认关、金标已验）。各任务开工时工作树必须干净。

---

### Task 1: `candles_1s_resample`（1s→任意周期泛化）

**Files:**
- Modify: `gridtrade/backtest/reservoir.py`（`candles_1s_to_1m` → 泛化 + 薄包装；加 `_RULES`）
- Test: `tests/backtest/test_reservoir.py`（追加 2 测）

**Interfaces:**
- Consumes: 现有 `candles_1s_to_1m(df, symbol_map)`、`CANDLE_COLS`、测试 fixture `_raw_1s`/`_dec`（已在测试文件中）。
- Produces: `candles_1s_resample(df, symbol_map, rule) -> {symbol: DataFrame[CANDLE_COLS]}`（rule='1min'/'1H'）；模块常量 `_RULES = {'1m': '1min', '1h': '1H'}`；`candles_1s_to_1m` 行为不变（薄包装）。

- [ ] **Step 1: 写失败测试**

在 `tests/backtest/test_reservoir.py` 末尾追加：

```python
def test_1s_to_1h_matches_manual_agg():
    # 2 小时整（7200 秒）：1s→1H 直采，逐列对手工聚合期望
    raw = _raw_1s('BTC', '2026-03-22 00:00:00', 7200, base=100.0)
    out = R.candles_1s_resample(raw, {'BTC': 'BTC/USDC:USDC'}, '1H')
    df = out['BTC/USDC:USDC']
    assert list(df.columns) == CANDLE_COLS and len(df) == 2
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2026-03-22 00:00:00')
    assert df['candle_begin_time'].iloc[1] == pd.Timestamp('2026-03-22 01:00:00')
    # 第一根：open=第0秒 open、close=第3599秒 close、high=第3599秒 high、low=第0秒 low
    assert abs(df['open'].iloc[0] - 100.0) < 1e-9
    assert abs(df['close'].iloc[0] - (100.0 + 3599 * 0.01 + 0.1)) < 1e-9
    assert abs(df['high'].iloc[0] - (100.0 + 3599 * 0.01 + 0.5)) < 1e-9
    assert abs(df['low'].iloc[0] - 99.5) < 1e-9
    assert abs(df['vol'].iloc[0] - 3600.0) < 1e-9
    # quote_volume = Σ volume_quote = Σ px（等差 100.00..135.99）
    assert abs(df['quote_volume'].iloc[0] - sum(100.0 + i * 0.01 for i in range(3600))) < 1e-6
    assert df['candle_begin_time'].dt.tz is None


def test_1h_equals_1m_reaggregated():
    # 一致性：1s→1H 直采 == 1s→1min 再聚 1H（agg 同构 ⇒ 恒等；防重采样口径漂移）
    raw = _raw_1s('BTC', '2026-03-22 00:00:00', 7200, base=100.0)
    smap = {'BTC': 'BTC/USDC:USDC'}
    direct = R.candles_1s_resample(raw, smap, '1H')['BTC/USDC:USDC']
    m = R.candles_1s_resample(raw, smap, '1min')['BTC/USDC:USDC']
    re = (m.set_index('candle_begin_time')
            .resample('1H', label='left', closed='left')
            .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
                  'vol': 'sum', 'volCcy': 'sum', 'quote_volume': 'sum'})
            .reset_index())
    for col in ('open', 'high', 'low', 'close', 'vol', 'quote_volume'):
        np.testing.assert_allclose(direct[col].to_numpy('float64'),
                                   re[col].to_numpy('float64'), rtol=1e-12,
                                   err_msg='%s 口径漂移' % col)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_reservoir.py -v`
Expected: 新 2 测 FAIL —— `AttributeError: module ... has no attribute 'candles_1s_resample'`；旧 5 测 PASS。

- [ ] **Step 3: 泛化实现**

在 `gridtrade/backtest/reservoir.py` 中，`NAMESPACE = '1m'` 行后加：

```python
_RULES = {'1m': '1min', '1h': '1H'}   # cache 命名空间 → pandas resample 规则
```

把 `candles_1s_to_1m(df, symbol_map)` 整个函数替换为（正文即原函数体，仅 `'1min'` 改为参数 `rule`、docstring 更新）：

```python
def candles_1s_resample(df, symbol_map, rule):
    """纯函数：Reservoir 1s candles(df) → {symbol: rule 周期 CANDLE_COLS df}。
    rule: pandas resample 规则（'1min'/'1H'）。symbol_map: {reservoir_coin: canonical_symbol}。
    只处理 symbol_map 里的币；bar-begin 口径（label/closed=left）。"""
    out = {}
    if df is None or df.empty:
        return out
    ts = pd.to_datetime(df['timestamp'], utc=True).dt.tz_localize(None)  # tz-naive UTC，与 cache 同口径
    df = df.assign(candle_begin_time=ts)
    # Reservoir 的 OHLCV 列是 decimal(20,10)→pandas object(Decimal)；重采样前先转 float，
    # 否则 resample 的 max/min/sum 在 object 列上不可靠/极慢。
    for c in ('open', 'high', 'low', 'close', 'volume', 'volume_quote'):
        df[c] = df[c].astype(float)
    for coin, sym in symbol_map.items():
        sub = df[df['coin'] == coin]
        if sub.empty:
            continue
        g = (sub.set_index('candle_begin_time').sort_index()
             .resample(rule, label='left', closed='left')
             .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
                   'volume': 'sum', 'volume_quote': 'sum'}))
        g = g.dropna(subset=['open']).reset_index()
        if g.empty:
            continue
        g['symbol'] = sym
        g['vol'] = g['volume'].astype(float)
        g['volCcy'] = g['volume_quote'].astype(float)
        g['quote_volume'] = g['volume_quote'].astype(float)
        for c in ('open', 'high', 'low', 'close'):
            g[c] = g[c].astype(float)
        out[sym] = g[CANDLE_COLS].reset_index(drop=True)
    return out


def candles_1s_to_1m(df, symbol_map):
    """向后兼容薄包装：1s→1m。"""
    return candles_1s_resample(df, symbol_map, '1min')
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_reservoir.py -v`
Expected: 7 passed（新 2 + 旧 5 全绿 = 薄包装兼容）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/reservoir.py tests/backtest/test_reservoir.py
git commit -m "feat(reservoir): candles_1s_resample 泛化 1s→任意周期（1m 薄包装保兼容）"
```

---

### Task 2: `warm_reservoir_ohlcv`（多 timeframe 一次下载同写）

**Files:**
- Modify: `gridtrade/backtest/reservoir.py`（`warm_reservoir_1m` → 泛化 + 薄包装）
- Test: `tests/backtest/test_reservoir.py`（追加 3 测）

**Interfaces:**
- Consumes: Task 1 的 `candles_1s_resample`、`_RULES`；现有 `_s3_cp`/`_days`/`ParquetCache.exists/write/write_empty`。
- Produces: `warm_reservoir_ohlcv(cache, universe, start_ms, end_ms, *, timeframes=('1h','1m'), workdir=None, log=print) -> {tf: {'days': int, 'rows': int}, 'skipped_cached': int, 'retry_later': int}`；`warm_reservoir_1m` 签名/返回格式不变（薄包装）。

- [ ] **Step 1: 写失败测试**

在 `tests/backtest/test_reservoir.py` 末尾追加：

```python
def _fake_cp_2coins(day, dest, log=print):
    """两币 × 7200 秒（2 根 1h / 120 根 1m 不足——用 7200s 产 2 根 1h、120 根 1m）。"""
    raw = pd.concat([_raw_1s('BTC', day + ' 00:00:00', 7200, base=100.0),
                     _raw_1s('ETH', day + ' 00:00:00', 7200, base=50.0)], ignore_index=True)
    raw.to_parquet(dest, index=False)
    return True


def test_warm_ohlcv_writes_both_namespaces(tmp_path, monkeypatch):
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', _fake_cp_2coins)
    stat = R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert stat['1h']['days'] == 1 and stat['1m']['days'] == 1
    assert stat['1m']['rows'] == 2 * 120      # 2 币 × 120 根 1m（7200s）
    assert stat['1h']['rows'] == 2 * 2        # 2 币 × 2 根 1h
    for s in UNI:
        assert cache.exists('1h', s, _PAST_DAY) and cache.exists('1m', s, _PAST_DAY)
    df = cache.read('1h', 'BTC/USDC:USDC', _PAST_DAY)
    assert len(df) == 2 and list(df.columns) == CANDLE_COLS


def test_warm_ohlcv_idempotent_and_partial_refill(tmp_path, monkeypatch):
    import os as _os
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', _fake_cp_2coins)
    R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)

    calls = []
    def _counting_cp(day, dest, log=print):
        calls.append(day)
        return _fake_cp_2coins(day, dest, log=log)
    monkeypatch.setattr(R, '_s3_cp', _counting_cp)
    # 全命中 → skip、零下载
    st2 = R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert st2['skipped_cached'] == 1 and calls == []
    # 删掉 1h 一边 → 重下补齐两边（幂等条件=所有 timeframe 全命中）
    for s in UNI:
        _os.remove(_os.path.join(str(tmp_path), '1h', s, _PAST_DAY + '.parquet'))
    st3 = R.warm_reservoir_ohlcv(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert calls == [_PAST_DAY] and st3['1h']['days'] == 1
    for s in UNI:
        assert cache.exists('1h', s, _PAST_DAY)


def test_warm_1m_wrapper_old_format_and_no_1h(tmp_path, monkeypatch):
    # 薄包装：返回旧格式 dict、只写 1m 不写 1h
    cache = ParquetCache(str(tmp_path))
    monkeypatch.setattr(R, '_s3_cp', _fake_cp_2coins)
    stat = warm_reservoir_1m(cache, UNI, _ms(_PAST_DAY), _ms(_PAST_DAY) + _DAY_MS - 1)
    assert set(stat) == {'days', 'rows', 'skipped_cached', 'retry_later'}
    assert stat['days'] == 1 and stat['rows'] == 2 * 120
    for s in UNI:
        assert cache.exists('1m', s, _PAST_DAY)
        assert not cache.exists('1h', s, _PAST_DAY)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_reservoir.py -v`
Expected: 新 3 测 FAIL（`no attribute 'warm_reservoir_ohlcv'`；wrapper 测暂 FAIL 因返回旧实现没变——若旧实现直接 PASS 该测则以另两测的 RED 为准）；旧 7 测 PASS。

- [ ] **Step 3: 泛化实现**

把 `gridtrade/backtest/reservoir.py` 的 `warm_reservoir_1m`（整个函数）替换为：

```python
def warm_reservoir_ohlcv(cache, universe, start_ms, end_ms, *, timeframes=('1h', '1m'),
                         workdir=None, log=print):
    """把 [start,end] 每个 UTC 天的 1s 拉下→按 timeframes 重采样→写各命名空间（一次下载多周期同写）。

    幂等：**所有** timeframe 的整天全命中才跳过；只差其一也重下 day 文件补齐（覆盖写同值无害）。
    只缓存**完整**的天：当天(UTC)未过完、或该天在 S3 尚未发布/拉取报错 → 不写任何文件（含空哨兵），
    计入 retry_later，下次重取。只有「日文件已成功下载、但某币当天确无成交」才落该币空哨兵（真空）。"""
    symbol_map = {s.split('/')[0]: s for s in universe}
    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)
    days = _days(start_ms, end_ms)
    tmpdir = workdir or tempfile.mkdtemp(prefix='reservoir_')
    os.makedirs(tmpdir, exist_ok=True)
    stat = {tf: {'days': 0, 'rows': 0} for tf in timeframes}
    stat['skipped_cached'] = 0
    stat['retry_later'] = 0
    for day in days:
        # 当天(UTC)未过完 → 无完整日文件；不缓存、不落哨兵，下次重取
        day_end_ms = int((pd.Timestamp(day) + pd.Timedelta(days=1)).value // 1_000_000)
        if day_end_ms > now_ms:
            stat['retry_later'] += 1
            continue
        if all(cache.exists(tf, s, day) for tf in timeframes for s in universe):
            stat['skipped_cached'] += 1
            continue
        dest = os.path.join(tmpdir, '%s.parquet' % day)
        if not _s3_cp(day, dest, log=log):
            # 拉取失败(404 未发布 / 接口报错) → 不写任何文件，跳过，下次重取
            stat['retry_later'] += 1
            continue
        raw = pd.read_parquet(dest)
        os.remove(dest)
        for tf in timeframes:
            per_sym = candles_1s_resample(raw, symbol_map, _RULES[tf])
            for s in universe:
                df = per_sym.get(s)
                if df is None or df.empty:
                    cache.write_empty(tf, s, day, CANDLE_COLS)  # 日文件已下、该币确无成交 → 真空哨兵
                else:
                    cache.write(tf, s, day, df)
                    stat[tf]['rows'] += int(len(df))
            stat[tf]['days'] += 1
        done = stat[timeframes[0]]['days']
        if done % 10 == 0:
            log('[reservoir] %d days done (%s)' % (
                done, ', '.join('%s rows=%d' % (tf, stat[tf]['rows']) for tf in timeframes)))
    return stat


def warm_reservoir_1m(cache, universe, start_ms, end_ms, *, workdir=None, log=print):
    """向后兼容薄包装：只做 1m，返回旧格式 {'days','rows','skipped_cached','retry_later'}。"""
    st = warm_reservoir_ohlcv(cache, universe, start_ms, end_ms,
                              timeframes=('1m',), workdir=workdir, log=log)
    return {'days': st['1m']['days'], 'rows': st['1m']['rows'],
            'skipped_cached': st['skipped_cached'], 'retry_later': st['retry_later']}
```

- [ ] **Step 4: 跑测试确认通过（含旧 5 测零改动兼容）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_reservoir.py -v`
Expected: 10 passed（新 3 + Task1 的 2 + 原 5，全绿）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/reservoir.py tests/backtest/test_reservoir.py
git commit -m "feat(reservoir): warm_reservoir_ohlcv 一次下载 1h+1m 全币种同写（warm_reservoir_1m 薄包装保兼容）"
```

---

### Task 3: main() 1h 数据源自动切换 + Reservoir 起点守卫

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（常量 + `_pick_1h_source` + main() phase1 分叉）
- Test: `tests/backtest/test_backtest_run.py`（追加 2 测）

**Interfaces:**
- Consumes: Task 2 的 `warm_reservoir_ohlcv(cache, universe, start_ms, end_ms, *, timeframes)`；现有 `_resolve_window`/`_WARMUP_DAYS`/`_hl_datasource_1h`/`resolve_universe`。
- Produces: `_pick_1h_source(warm_start, now) -> 'api' | 'reservoir'`（纯函数）；`RESERVOIR_START = pd.Timestamp('2025-07-31')`；`_API_1H_MAX_DAYS = 200`。

- [ ] **Step 1: 写失败测试**

在 `tests/backtest/test_backtest_run.py` 末尾追加：

```python
def test_pick_1h_source_boundaries():
    import pandas as pd
    from gridtrade.backtest.backtest_run import _pick_1h_source
    now = pd.Timestamp('2026-07-05 00:00:00')
    assert _pick_1h_source(now - pd.Timedelta(days=199), now) == 'api'        # API 可达 → 现路径
    assert _pick_1h_source(now - pd.Timedelta(days=201), now) == 'reservoir'  # 超滚动范围 → 归档


def test_main_reservoir_guard_before_network(monkeypatch):
    # warm_start < RESERVOIR_START → SystemExit，且守卫先于任何网络调用（_hl_datasource_1h 不被触发）
    import pytest
    import gridtrade.backtest.backtest_run as B

    def _no_network(cache):
        raise AssertionError('守卫应在触网之前生效')
    monkeypatch.setattr(B, '_hl_datasource_1h', _no_network)
    with pytest.raises(SystemExit) as ei:
        B.main(['2025-07-01', '2025-07-20', '1m'])   # warm_start=2025-06-17 < 2025-07-31
    msg = str(ei.value)
    assert 'Reservoir' in msg and '2025-08-14' in msg   # 报错含归档起点换算出的最早窗口起点
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py -v`
Expected: 新 2 测 FAIL —— `cannot import name '_pick_1h_source'`；旧测全 PASS。

- [ ] **Step 3: 加常量与纯函数**

在 `gridtrade/backtest/backtest_run.py` 的 `BT_BLACKLIST = ...` 行之后加：

```python
# 1h 选币数据源自动切换：HL API 1h 滚动 ~5000 根≈208 天；更早窗口自动改走 Reservoir 归档。
RESERVOIR_START = pd.Timestamp('2025-07-31')   # Reservoir 1s 归档起点（实测列桶）
_API_1H_MAX_DAYS = 200                          # API 滚动可达阈值（208 天留余量）


def _pick_1h_source(warm_start, now):
    """纯函数：暖机起点早于 API 滚动可达范围 → 'reservoir'，否则 'api'（现路径字节不变）。"""
    return 'reservoir' if warm_start < now - pd.Timedelta(days=_API_1H_MAX_DAYS) else 'api'
```

- [ ] **Step 4: main() phase1 分叉（守卫先于触网）**

把 main() 中这一段：

```python
    t0 = time.time()
    # phase1: 解析全市场票池(−黑名单) + 预热全市场 1h
    _adapter, _ds1h = _hl_datasource_1h(cache)
    universe = resolve_universe(_ds1h, blacklist=BT_BLACKLIST)
    print('[BT] 全市场票池 %d 币(−黑名单 %d)' % (len(universe), len(BT_BLACKLIST)))
    from gridtrade.backtest import prewarm as PW
    print('[BT] 1h 预热: %s' % PW.prewarm_ohlcv(_ds1h, universe, _ms(warm_start), _ms(win_end)))
```

替换为：

```python
    t0 = time.time()
    # 1h 数据源按窗口自动切换（单 run 单源无拼缝）；守卫先于任何网络调用
    source = _pick_1h_source(warm_start, pd.Timestamp.utcnow().tz_localize(None))
    print('[BT] 1h 数据源: %s' % source)
    if source == 'reservoir' and warm_start < RESERVOIR_START:
        raise SystemExit('[BT] 窗口过早：Reservoir 归档起点 %s，含 %d 天暖机最早窗口起点 %s'
                         % (RESERVOIR_START.date(), _WARMUP_DAYS,
                            (RESERVOIR_START + pd.Timedelta(days=_WARMUP_DAYS)).date()))
    # phase1: 解析全市场票池(−黑名单) + 预热全市场 1h
    _adapter, _ds1h = _hl_datasource_1h(cache)
    universe = resolve_universe(_ds1h, blacklist=BT_BLACKLIST)
    print('[BT] 全市场票池 %d 币(−黑名单 %d)' % (len(universe), len(BT_BLACKLIST)))
    if source == 'reservoir':
        from gridtrade.backtest import reservoir as RV
        print('[BT] 1h+1m 预热@Reservoir: %s'
              % RV.warm_reservoir_ohlcv(cache, universe, _ms(warm_start), _ms(win_end),
                                        timeframes=('1h', '1m')))
    else:
        from gridtrade.backtest import prewarm as PW
        print('[BT] 1h 预热: %s' % PW.prewarm_ohlcv(_ds1h, universe, _ms(warm_start), _ms(win_end)))
```

（其余不动：选币/phase2/回测照旧。reservoir 源下 phase2 的选中币 1m 将全命中缓存 `skipped_cached`，funding 照旧 HL API。）

- [ ] **Step 5: 跑测试确认通过 + import smoke**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py -v`
Expected: 全 PASS（新 2 + 旧全部）。

Run: `TZ=Asia/Shanghai .venv/bin/python -c "import gridtrade.backtest.backtest_run"`
Expected: 无输出无报错。

- [ ] **Step 6: 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全套绿（api 源路径字节不变；无其它模块受影响）。

- [ ] **Step 7: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_backtest_run.py
git commit -m "feat(backtest): 1h 数据源按窗口自动切换（api|reservoir）+ Reservoir 起点守卫先于触网"
```

---

### Task 4: 文档更新（回测使用文档 + STATUS）

**Files:**
- Modify: `docs/回测使用文档.md`（§0 / §3 / §4.1 / §7 / §8）
- Modify: `docs/STATUS.md`（§8 加一条）

**Interfaces:**
- Consumes: Task 1-3 的产出语义（自动切源阈值 200 天、归档起点 2025-07-31、最早窗口起点 2025-08-14、1h+1m 同写）。
- Produces: 无代码接口，仅文档。

- [ ] **Step 1: `docs/回测使用文档.md` 五处编辑**

① §0 TL;DR 列表加一行（在「数据按天缓存复用」条目之后）：

```markdown
- **可回测起点 2025-08-14**：窗口早于 HL API 滚动范围(~200 天)时，1h 选币数据自动改走 Reservoir 归档（1s→1h 重采样，同次下载顺手写全 1m），无需任何开关。
```

② §3 性能旋钮表之后加一段：

```markdown
**1h 数据源自动切换**：`warm_start`（窗口起点 −14 天暖机）早于「今天 −200 天」时，phase1 自动改用
Reservoir 归档重采样 1h（同一 day 文件顺手写全市场 1m，phase2 秒命中）；否则走 HL API（行为与旧版
完全一致）。Reservoir 归档起点 **2025-07-31** → 最早可回测窗口起点 **2025-08-14**；更早会直接报错。
注意：两源同一根 bar 数值可能微差（采集源不同），单次 run 恒单源、无拼缝；同一币缓存可跨 run 混源
（按天分界），选币磁盘缓存指纹会自动换 key、不会静默复用过期选币。
```

③ §4.1 数据源表「选币（因子计算）」行替换为：

```markdown
| 选币（因子计算） | 1h | HL 公共 API（近窗口）/ Reservoir S3 1s 重采样（窗口早于 ~200 天，自动切换） | `1h/` |
```

④ §7 常见问题末尾加：

```markdown
**Q: 想回测 2025 年 8~12 月（HL API 已取不到 1h）？**
直接跑，例如 `TZ=Asia/Shanghai BT_WORKERS=8 .venv/bin/python -m gridtrade.backtest.backtest_run 2025-08-15 2025-10-14 1m`。
窗口早于 API 范围时 1h 自动走 Reservoir（需 AWS 凭证，同 §2）。最早窗口起点 2025-08-14；再早会报错。
```

⑤ §8 相关文件 `reservoir.py` 行替换为：

```markdown
- `gridtrade/backtest/reservoir.py` — Reservoir S3 1s→1m/1h 装载器（`warm_reservoir_ohlcv` 多周期一次下载同写）
```

- [ ] **Step 2: `docs/STATUS.md` §8 加一条**（「回测选币性能」条目之后）：

```markdown
- **回测可回溯拓展（Reservoir 1h，纯离线工具）**：窗口早于 HL API 1h 滚动(~200 天)时 main() 自动把 phase1 切到 Reservoir 归档（`warm_reservoir_ohlcv` 1s→1h+1m 全币种一次下载同写，phase2 1m 秒命中）；归档起点 2025-07-31 → 最早窗口起点 **2025-08-14**（更早响亮报错）。近窗口 api 路径字节不变。fidelity：两源 bar 微差、单 run 单源；老窗口票池仍今日上市表（存活者偏差随窗口变早加重）。
```

- [ ] **Step 3: 提交**

```bash
git add docs/回测使用文档.md docs/STATUS.md
git commit -m "docs(backtest): 可回测起点拓展到 2025-08-14（Reservoir 1h 自动切源）使用说明"
```

---

## Self-Review

**1. Spec coverage：** 装载器泛化（spec §1）→ T1+T2；自动切换+守卫（§2）→ T3；忠实度注记+文档（§3）→ T4；测试矩阵（§4）→ T1(2测)/T2(3测+旧5兼容)/T3(2测)。两窗验证（§5）明确不进代码库（controller 在 SDD 后执行）。✓
**2. Placeholder scan：** 无 TBD/TODO；每步含完整代码与命令。✓
**3. Type consistency：** `_RULES` T1 定义、T2 使用；`warm_reservoir_ohlcv(..., timeframes)` T2 定义、T3 以 `timeframes=('1h','1m')` 调用；`_pick_1h_source(warm_start, now)` 测试与 main 调用一致（均 tz-naive）；stats 新格式 `{tf: {'days','rows'}, 'skipped_cached', 'retry_later'}` 与薄包装映射一致。✓
