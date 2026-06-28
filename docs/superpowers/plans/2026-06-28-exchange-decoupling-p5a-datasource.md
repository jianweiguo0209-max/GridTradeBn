# 交易所解耦重构 P5a 实现计划（适配器分页 + 回测缓存 + DataSource）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭好"按配置交易所动态拉数 + 离线缓存"的数据访问层：① 给 `CcxtAdapter.fetch_ohlcv` / `fetch_funding_history` 加分页（大窗口经多次 since/limit 拉全，去除 P0–P1 carry-forward）；② 移植按天 parquet `ParquetCache` 到 `gridtrade/backtest/cache.py`；③ 新增 `gridtrade/backtest/datasource.py` 的 `DataSource`（基于 ExchangeAdapter + cache 的区间取数，按天缓存，预热后离线）。全程 FakeExchange 离线 TDD。

**Architecture:** 适配器拥有交易所分页（用 `client.parse_timeframe` 求步长，循环 since/limit 推进 cursor、dedup、按 [start,end] 过滤）。`DataSource(adapter, cache)` 把区间拆成天，命中缓存的天直接读、缺失的天经 adapter 拉取后按天写 parquet——所有天命中即不触网（离线）。这是需求 7（按配置交易所动态拉数）+ 需求 8（预热后离线）的数据底座。

**Tech Stack:** Python 3.9、ccxt 4.5.61、pandas 1.3.5、pyarrow 12、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（分页边界、缓存按天切分、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；ccxt 4.5.61；pandas 1.3.5；pyarrow 12。
- ccxt 统一 timeframe 为**小写**（`'1h'/'1m'/'15m'`）；用 `self.client.parse_timeframe(tf) * 1000` 求步长 ms。不得用 OKX 的 `'1H'`。
- `gridtrade/backtest/` 可 import `gridtrade/core/`、`gridtrade/exchanges/`；不得硬编码交易所（只经 `ExchangeAdapter`）。`DataSource` 不直接调 ccxt，只经 adapter。
- K线统一列 `CANDLE_COLS`、资金费统一列 `FUNDING_COLS`（沿用 base.py）。`candle_begin_time` 为 UTC（`pd.to_datetime(ts, unit='ms')`）。
- 分页：循环 `client.fetch_ohlcv(native, tf, since=cursor, limit=N)`，`cursor = last_ts + tf_ms`；无进展（last_ts < cursor 或空批）即停；最终 dedup on ts、过滤 [start_ms, end_ms]、升序。有死循环兜底 guard。**保持既有 `tests/exchanges/test_ccxt_adapter.py` 全绿**（其 FakeCcxtClient 忽略 since 返回固定 2 行——分页循环须能在"无进展"时安全终止并 dedup 回 2 行）。
- ParquetCache 行为与 `backtest/cache.py` 完全一致（按天 parquet、空哨兵、原子写、exists 廉价 stat、read_all_days）。
- 测试针对 FakeExchange + 临时目录缓存，无外部网络。
- 不修改 `account_0/`、`backtest/`、`gridtrade/{core,state,execution}/`、`gridtrade/exchanges/{base,okx,hyperliquid,fake,registry}.py`（本计划只改 `gridtrade/exchanges/ccxt_adapter.py` 与新增 `gridtrade/backtest/*`）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。

---

## 文件结构（本计划新建/修改）

```
gridtrade/exchanges/ccxt_adapter.py   # 修改：fetch_ohlcv / fetch_funding_history 分页
gridtrade/backtest/__init__.py        # 新增（空）
gridtrade/backtest/cache.py           # 新增：ParquetCache（移植自 backtest/cache.py）
gridtrade/backtest/datasource.py      # 新增：DataSource（adapter+cache 区间取数，按天缓存）
tests/exchanges/test_ccxt_pagination.py
tests/backtest/__init__.py
tests/backtest/test_cache.py
tests/backtest/test_datasource.py
```

---

### Task 1: CcxtAdapter 分页（fetch_ohlcv / fetch_funding_history）

**Files:**
- Modify: `gridtrade/exchanges/ccxt_adapter.py`
- Create: `tests/exchanges/test_ccxt_pagination.py`

**Interfaces:**
- Produces（改 CcxtAdapter，签名不变）：
  - `fetch_ohlcv(symbol, timeframe, start_ms, end_ms)`：内部分页，循环 `client.fetch_ohlcv(native, timeframe, since=cursor, limit=1000)`，`tf_ms = self.client.parse_timeframe(timeframe)*1000`，`cursor=last_ts+tf_ms`，无进展/空批停，guard 上限 10000 轮；最终 DataFrame dedup on ts、过滤 [start_ms,end_ms]、升序，映射 CANDLE_COLS（volCcy=vol、quote_volume=vol*close，沿用现有）。
  - `fetch_funding_history(symbol, start_ms, end_ms)`：同样分页（funding 无固定步长，用 `cursor=last_ts+1` 推进），dedup on ts、过滤范围、升序，映射 FUNDING_COLS（realizedRate=fundingRate）。

- [ ] **Step 1: 写测试**

Create `tests/exchanges/test_ccxt_pagination.py`:

```python
import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS


class PagingClient:
    """模拟分页交易所：每次最多返回 3 根，从 since 起；超过数据末尾返回空。"""
    def __init__(self, start, n, tf_ms=3600_000):
        self.bars = [[start + i * tf_ms, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10.0 + i]
                     for i in range(n)]
        self.tf_ms = tf_ms
        self.calls = 0

    def parse_timeframe(self, tf):
        return self.tf_ms // 1000

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        since = since or 0
        page = [b for b in self.bars if b[0] >= since][:3]
        return page

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        since = since or 0
        rows = [{'timestamp': b[0], 'fundingRate': 0.0001 * (i + 1)}
                for i, b in enumerate(self.bars) if b[0] >= since][:3]
        return rows


def _adapter(client):
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    return CcxtAdapter(client, name='ccxt')


def test_fetch_ohlcv_paginates_full_range():
    start = 1_700_000_000_000
    client = PagingClient(start, n=10)            # 10 根，每页 3 → 需多页
    a = _adapter(client)
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', start, start + 9 * 3600_000)
    assert list(df.columns) == CANDLE_COLS
    assert len(df) == 10                          # 分页拉全
    assert client.calls >= 4                      # 确实分了多页
    assert df['candle_begin_time'].is_monotonic_increasing
    assert df['ts'].is_unique if 'ts' in df.columns else True


def test_fetch_ohlcv_range_filter():
    start = 1_700_000_000_000
    client = PagingClient(start, n=10)
    a = _adapter(client)
    # 只要中间 5 根 [start+2h, start+6h]
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', start + 2 * 3600_000, start + 6 * 3600_000)
    assert len(df) == 5


def test_fetch_funding_history_paginates():
    start = 1_700_000_000_000
    client = PagingClient(start, n=8)
    a = _adapter(client)
    df = a.fetch_funding_history('BTC/USDT:USDT', start, start + 7 * 3600_000)
    assert list(df.columns) == FUNDING_COLS
    assert len(df) == 8


def test_existing_fixed_client_still_terminates():
    # 复刻既有 FakeCcxtClient 语义：忽略 since、返回固定 2 行 → 分页须安全终止、dedup 回 2 行
    class FixedClient:
        def parse_timeframe(self, tf):
            return 3600
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            return [[1704067200000, 1.0, 2.0, 0.5, 1.5, 10.0],
                    [1704070800000, 1.5, 2.5, 1.0, 2.0, 20.0]]
    a = _adapter(FixedClient())
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', 0, 10 ** 13)
    assert len(df) == 2 and list(df.columns) == CANDLE_COLS
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_ccxt_pagination.py -v`
Expected: FAIL（当前 fetch_ohlcv 单次调用，分页断言 `client.calls >= 4` 或 `len==10` 不满足）。

- [ ] **Step 3: 改 ccxt_adapter.py**

把 `fetch_ohlcv` 改为分页：
```python
    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms) -> pd.DataFrame:
        native = self.to_native(symbol)
        tf_ms = int(self.client.parse_timeframe(timeframe) * 1000)
        all_rows = []
        cursor = int(start_ms)
        guard = 0
        while cursor <= end_ms and guard < 10000:
            guard += 1
            batch = self.client.fetch_ohlcv(native, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts < cursor:          # 无进展
                break
            cursor = last_ts + tf_ms
            if len(batch) < 2:            # 末页
                break
        if not all_rows:
            return pd.DataFrame(columns=CANDLE_COLS)
        df = pd.DataFrame(all_rows, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = df.drop_duplicates(subset=['ts'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
        df['symbol'] = symbol
        df['volCcy'] = df['vol']
        df['quote_volume'] = df['vol'] * df['close']
        df = df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)
        return df
```
把 `fetch_funding_history` 改为分页：
```python
    def fetch_funding_history(self, symbol, start_ms, end_ms) -> pd.DataFrame:
        native = self.to_native(symbol)
        all_rows = []
        cursor = int(start_ms)
        guard = 0
        while cursor <= end_ms and guard < 10000:
            guard += 1
            batch = self.client.fetch_funding_rate_history(native, since=cursor, limit=1000)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1]['timestamp'])
            if last_ts < cursor:
                break
            cursor = last_ts + 1
            if len(batch) < 2:
                break
        if not all_rows:
            return pd.DataFrame(columns=FUNDING_COLS)
        df = pd.DataFrame([{'ts': int(r['timestamp']), 'symbol': symbol,
                            'fundingRate': float(r['fundingRate']),
                            'realizedRate': float(r['fundingRate'])} for r in all_rows])
        df = df.drop_duplicates(subset=['ts'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        return df[FUNDING_COLS].sort_values('ts').reset_index(drop=True)
```

- [ ] **Step 4: 运行确认通过 + 既有适配器测试不回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/ -v`
Expected: PASS（新增 4 + 既有 test_ccxt_adapter 等全绿；注意 test_fetch_ohlcv_maps_to_candle_cols 仍 2 行、test_fetch_funding_history_maps_cols 仍 2 行）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_ccxt_pagination.py
git commit -m "feat(exchanges): paginate CcxtAdapter fetch_ohlcv/fetch_funding_history"
```

---

### Task 2: gridtrade/backtest/cache.py（按天 parquet ParquetCache）

**Files:**
- Create: `gridtrade/backtest/__init__.py`, `gridtrade/backtest/cache.py`
- Create: `tests/backtest/__init__.py`, `tests/backtest/test_cache.py`

**Interfaces:**
- Produces: `gridtrade.backtest.cache.ParquetCache`（移植自 `backtest/cache.py`，行为一致）：
  - `__init__(self, root)`；`exists(namespace, symbol, day) -> bool`（廉价 stat，文件存在且 >0 字节）；`read(namespace, symbol, day) -> DataFrame|None`；`write(namespace, symbol, day, df)`（原子：临时文件 + os.replace）；`write_empty(namespace, symbol, day, columns)`（schema-only 空哨兵）；`read_all_days(namespace, symbol) -> DataFrame|None`（合并所有已缓存天）。

- [ ] **Step 1: 写测试**

Create `tests/backtest/__init__.py`（空）。

Create `tests/backtest/test_cache.py`:

```python
import pandas as pd


def _cache(tmp_path):
    from gridtrade.backtest.cache import ParquetCache
    return ParquetCache(str(tmp_path))


def _df():
    return pd.DataFrame({'ts': [1, 2], 'close': [10.0, 11.0]})


def test_write_read_exists(tmp_path):
    c = _cache(tmp_path)
    assert c.exists('1h', 'BTC/USDT:USDT', '2024-01-01') is False
    c.write('1h', 'BTC/USDT:USDT', '2024-01-01', _df())
    assert c.exists('1h', 'BTC/USDT:USDT', '2024-01-01') is True
    got = c.read('1h', 'BTC/USDT:USDT', '2024-01-01')
    assert list(got['close']) == [10.0, 11.0]


def test_read_missing_returns_none(tmp_path):
    assert _cache(tmp_path).read('1h', 'X', '2024-01-01') is None


def test_write_empty_sentinel_exists(tmp_path):
    c = _cache(tmp_path)
    c.write_empty('1h', 'X', '2024-01-01', columns=['ts', 'close'])
    assert c.exists('1h', 'X', '2024-01-01') is True       # 空哨兵也算已缓存
    got = c.read('1h', 'X', '2024-01-01')
    assert got is not None and len(got) == 0


def test_read_all_days_merges(tmp_path):
    c = _cache(tmp_path)
    c.write('1h', 'X', '2024-01-01', pd.DataFrame({'ts': [1], 'close': [10.0]}))
    c.write('1h', 'X', '2024-01-02', pd.DataFrame({'ts': [2], 'close': [11.0]}))
    alld = c.read_all_days('1h', 'X')
    assert len(alld) == 2 and set(alld['ts']) == {1, 2}


def test_read_all_days_none_when_absent(tmp_path):
    assert _cache(tmp_path).read_all_days('1h', 'NOPE') is None
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_cache.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.backtest.cache`）。

- [ ] **Step 3: 移植 cache.py**

Create `gridtrade/backtest/__init__.py`（空）。

执行：`cp backtest/cache.py gridtrade/backtest/cache.py`（`backtest/cache.py` 是纯 pandas/pyarrow、无交易所依赖；逐字复制即可，不改）。不要修改 `backtest/cache.py`。

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_cache.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/__init__.py gridtrade/backtest/cache.py tests/backtest/__init__.py tests/backtest/test_cache.py
git commit -m "feat(backtest): port per-day ParquetCache to gridtrade.backtest.cache"
```

---

### Task 3: gridtrade/backtest/datasource.py（DataSource：区间取数 + 按天缓存 + 离线）

**Files:**
- Create: `gridtrade/backtest/datasource.py`
- Create: `tests/backtest/test_datasource.py`

**Interfaces:**
- Consumes: `ExchangeAdapter`（fetch_ohlcv/fetch_funding_history/list_instruments）、`ParquetCache`、`base.CANDLE_COLS/FUNDING_COLS`。
- Produces: `gridtrade.backtest.datasource.DataSource`：
  - `__init__(self, adapter, cache, *, timeframe='1h')`
  - `fetch_ohlcv_range(self, symbol, start_ms, end_ms) -> DataFrame`：把 [start,end] 按 UTC 天枚举；全部天命中缓存 → 仅读缓存合并（**离线**，不调 adapter）；存在缺失天 → 调一次 `adapter.fetch_ohlcv(symbol, timeframe, day_start, day_end_of_range)` 取缺失跨度，按天切分写 parquet（无数据的天写空哨兵），再合并返回。namespace 用 `timeframe`。返回按 candle_begin_time 升序、列为 CANDLE_COLS。
  - `fetch_funding_range(self, symbol, start_ms, end_ms) -> DataFrame`：同样按天缓存（namespace `'funding'`），返回 FUNDING_COLS。
  - `list_instruments(self) -> list`：直接透传 `adapter.list_instruments()`（不缓存）。

- [ ] **Step 1: 写测试**

Create `tests/backtest/test_datasource.py`:

```python
import pandas as pd

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument, CANDLE_COLS

SYM = 'BTC/USDT:USDT'
DAY = 86_400_000


def _bars(start_ms, n_hours):
    ts = [start_ms + i * 3600_000 for i in range(n_hours)]
    return pd.DataFrame({
        'symbol': SYM,
        'candle_begin_time': pd.to_datetime(ts, unit='ms'),
        'open': [1.0] * n_hours, 'high': [2.0] * n_hours, 'low': [0.5] * n_hours,
        'close': [1.5] * n_hours, 'vol': [10.0] * n_hours,
        'volCcy': [10.0] * n_hours, 'quote_volume': [15.0] * n_hours,
    })


def _ds(tmp_path, ex):
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.backtest.datasource import DataSource
    return DataSource(ex, ParquetCache(str(tmp_path)), timeframe='1h')


def test_fetch_range_warms_cache_then_serves_offline(tmp_path):
    start = 1_704_067_200_000  # 2024-01-01 00:00 UTC
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_ohlcv(SYM, _bars(start, 48))   # 2 天 1h bars
    ds = _ds(tmp_path, ex)
    end = start + 47 * 3600_000
    df1 = ds.fetch_ohlcv_range(SYM, start, end)
    assert list(df1.columns) == CANDLE_COLS and len(df1) == 48

    # 预热后离线：换一个会在 fetch 时报错的交易所，仅靠缓存仍能取到
    class Offline(FakeExchange):
        def fetch_ohlcv(self, *a, **k):
            raise AssertionError('should not hit network after warm')
    off = Offline(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)
    df2 = ds2.fetch_ohlcv_range(SYM, start, end)
    assert len(df2) == 48 and list(df2['close']) == list(df1['close'])


def test_fetch_range_subset_from_cache(tmp_path):
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_ohlcv(SYM, _bars(start, 48))
    ds = _ds(tmp_path, ex)
    ds.fetch_ohlcv_range(SYM, start, start + 47 * 3600_000)   # warm 2 days
    sub = ds.fetch_ohlcv_range(SYM, start + 5 * 3600_000, start + 10 * 3600_000)
    assert len(sub) == 6   # inclusive [5h,10h]


def test_list_instruments_passthrough(tmp_path):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds = _ds(tmp_path, ex)
    insts = ds.list_instruments()
    assert insts[0].symbol == SYM
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_datasource.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.backtest.datasource`）。

- [ ] **Step 3: 写 datasource.py**

Create `gridtrade/backtest/datasource.py`:

```python
"""DataSource：基于 ExchangeAdapter + ParquetCache 的回测取数层。
区间按 UTC 天缓存；全部天命中即离线（不触 adapter），缺失天才拉取。
只经 adapter 访问交易所，不直接调 ccxt。"""
import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS


def _days(start_ms, end_ms):
    s = pd.to_datetime(start_ms, unit='ms').normalize()
    e = pd.to_datetime(end_ms, unit='ms').normalize()
    return [d.strftime('%Y-%m-%d') for d in pd.date_range(s, e, freq='D')]


def _day_bounds_ms(day):
    d0 = pd.Timestamp(day)
    return int(d0.value // 1_000_000), int((d0 + pd.Timedelta(days=1)).value // 1_000_000) - 1


class DataSource:
    def __init__(self, adapter, cache, *, timeframe='1h'):
        self.adapter = adapter
        self.cache = cache
        self.timeframe = timeframe

    def list_instruments(self):
        return self.adapter.list_instruments()

    def _warm(self, namespace, symbol, start_ms, end_ms, fetch_fn, cols, time_col):
        days = _days(start_ms, end_ms)
        missing = [d for d in days if not self.cache.exists(namespace, symbol, d)]
        if missing:
            lo, _ = _day_bounds_ms(missing[0])
            _, hi = _day_bounds_ms(missing[-1])
            fetched = fetch_fn(symbol, lo, hi)
            for d in missing:
                d_lo, d_hi = _day_bounds_ms(d)
                if fetched.empty:
                    self.cache.write_empty(namespace, symbol, d, cols)
                    continue
                ms = (fetched[time_col].astype('int64') if time_col == 'ts'
                      else fetched[time_col].view('int64') // 1_000_000)
                day_df = fetched[(ms >= d_lo) & (ms <= d_hi)]
                if day_df.empty:
                    self.cache.write_empty(namespace, symbol, d, cols)
                else:
                    self.cache.write(namespace, symbol, d, day_df.reset_index(drop=True))
        frames = [self.cache.read(namespace, symbol, d) for d in days]
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return pd.DataFrame(columns=cols)
        return pd.concat(frames, ignore_index=True)

    def fetch_ohlcv_range(self, symbol, start_ms, end_ms):
        df = self._warm(self.timeframe, symbol, start_ms, end_ms,
                        lambda s, lo, hi: self.adapter.fetch_ohlcv(s, self.timeframe, lo, hi),
                        CANDLE_COLS, 'candle_begin_time')
        if df.empty:
            return df
        ms = df['candle_begin_time'].view('int64') // 1_000_000
        df = df[(ms >= start_ms) & (ms <= end_ms)]
        return df.sort_values('candle_begin_time').drop_duplicates(
            subset=['candle_begin_time']).reset_index(drop=True)

    def fetch_funding_range(self, symbol, start_ms, end_ms):
        df = self._warm('funding', symbol, start_ms, end_ms,
                        lambda s, lo, hi: self.adapter.fetch_funding_history(s, lo, hi),
                        FUNDING_COLS, 'ts')
        if df.empty:
            return df
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        return df.sort_values('ts').drop_duplicates(subset=['ts']).reset_index(drop=True)
```

> 注（已实测，照此写）：pandas 1.3.5 上对 datetime64[ns] 列**必须用 `.view('int64')`**（返回纳秒整数，`//1_000_000` 得毫秒，无警告）；**不要**对 datetime 列用 `.astype('int64')`（1.3.5 会发 FutureWarning，污染输出）。对已是整数的 `ts` 列用 `.astype('int64')` 则没问题（int→int 无警告）。上面代码已按此区分（datetime→view、ts→astype），照抄即可。

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_datasource.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 全套回归 + 提交**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 98 + 本计划新增 ≈ 12）。

```bash
git add gridtrade/backtest/datasource.py tests/backtest/test_datasource.py
git commit -m "feat(backtest): DataSource (adapter+cache range fetch, offline after warm)"
```

---

## 完成判定（P5a）

- `pytest -q` 全绿：CcxtAdapter 分页（多页拉全 + 范围过滤 + 固定客户端安全终止）；ParquetCache 移植行为一致；DataSource 预热后离线、子区间从缓存取、list_instruments 透传。
- 全程 FakeExchange + 临时缓存，无外部网络。
- `gridtrade/backtest/` 只经 `ExchangeAdapter` 访问交易所。

## 后续（P5b，不在本计划内）

`gridtrade/backtest/prewarm.py`（按配置交易所/票池/窗口预热 DataSource 缓存）+ `gridtrade/backtest/backtest_run.py`（复用 `core.selection` 选币回放 + `core.grid_params` + `core.grid_engine.simulate_grid_engine` 在缓存 bars 上回测，输出汇总）+ `scripts/validate_hl.py`（真实 Hyperliquid 小窗口 prewarm+回测，跑一次兑现需求 9；联网，非 pytest 套件）。注：ccxt 统一 OHLCV 只含基础量，`quote_volume=vol*close` 为近似（影响交易额分位过滤的精度，非阻塞 HL 验证）。
