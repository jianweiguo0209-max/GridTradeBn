# 币安根本性迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 项目唯一对接币安 USDT-M 永续（删除 Hyperliquid/OKX/Reservoir），回测数据层改用 data.binance.vision 官方归档（2019+ 全历史，含退市币）。

**Architecture:** 端口-适配器不变：新增 `BinanceAdapter(CcxtAdapter)` 实现既有 `ExchangeAdapter` 端口，registry/config 切默认；回测新增 `vision.py` 归档装载器填充既有 ParquetCache（引擎零感知），`backtest_run.py` 只换数据源接缝。执行引擎/状态/面板零改动。

**Tech Stack:** Python 3.9（系统 3.9.6）、ccxt==4.5.61（`ccxt.binanceusdm`）、pandas 1.3.5、requests、pyarrow、pytest。

**Spec:** `docs/superpowers/specs/2026-07-14-binance-migration-design.md`（已批准；本计划引用其章节号）。

## Global Constraints

- Python 3.9 / pandas 1.3.5 / numpy 1.22.4 锁死，不升级栈；同步架构，不引 asyncio。
- 规范符号 = ccxt 统一符号 `BASE/USDT:USDT`；core 视 symbol 为不透明字符串。
- 时间戳一律 UTC 毫秒整数；cache 的 `candle_begin_time` 为 tz-naive UTC。
- pytest 全离线（fake/mock，不触网络）；联网验证只走 `scripts/`（非 pytest）。
- 内部 client_oid 格式 `'{grid_id}:{line}:{seq}'` / `'{gid}:fuse:low|high'` / `'{gid}:close:{n}'` **绝不改**（DB 键+对账依赖）。
- 注释/docstring 风格与仓库一致（中文、口径注记、spec 引用）。
- 回测默认费率 = 币安 USDT-M VIP0 无折扣：maker 0.0002 / taker 0.0005（用户定，2026-07-14）。
- 账户形态 = 普通合约账户（非统一账户 PM；用户定，2026-07-14）。
- 每个任务结尾 commit，消息风格 `feat(scope): 中文摘要`，末尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 测试命令统一 `.venv/bin/python -m pytest`（Task 0 建 venv）。

## 外部事实（2026-07-14 实测，编码时直接引用勿再查）

- fapi USDT-M 永续 TRADING 530 个；fapi 同时挂 38 个 USDC-M 永续（必须按 settle 过滤）。
- tier0 映射：KNEIRO→NEIRO（NEIROUSDT TRADING）；VINE 为 SETTLING（退市中，留黑名单无害）；其余 7 币直改后缀。
- MIN_NOTIONAL：BTCUSDT=50 / ETHUSDT=20 / 多数山寨=5（USDT）。
- 归档 URL：
  - 月度 K 线 `https://data.binance.vision/data/futures/um/monthly/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM}.zip`（BTCUSDT-1m 自 2020-01 起）
  - 日度 K 线 `.../daily/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM-DD}.zip`
  - 月度资金费 `.../monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip`（自 2020-01；**无日度**）
  - 每个 zip 配 `.CHECKSUM`，内容 `"{sha256}  {filename}"`。
  - 目录列举：`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=...`，S3 XML（`CommonPrefixes`/`Contents`，`IsTruncated`+`marker` 翻页，MaxKeys 1000）。
- kline CSV 12 列 `open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore`；**老文件无表头、新文件（约 2022+）带表头**；时间戳 ms（防御：>1e14 视为 µs）。
- fundingRate CSV 3 列带表头 `calc_time,funding_interval_hours,last_funding_rate`，calc_time 为 ms。
- ccxt 4.5.61 `binanceusdm` 具备：`set_sandbox_mode`、`fapiPublicGetKlines`、`fapiPublicGetTickerPrice`、`fapiPublicGetPing`、`fapiPrivateGetIncome`、`fapiPrivateGetPositionSideDual`、`fapiPrivateGetMultiAssetsMargin`、`set_margin_mode`。
- 币安 futures `newClientOrderId` 合法正则 `^[\.A-Z\:/a-z0-9_-]{1,36}$`（含 `:` 与 `.`，内部 cloid 理论上直传合法；以 Task 16 testnet 实测为准）。

---

### Task 0: 开发环境（venv + 基线绿）

**Files:** 无代码改动（生成 `.venv/`，已在 .gitignore）。

- [ ] **Step 1: 建 venv 并装依赖**

```bash
cd /Users/thomaschang/Projects/GridTradeBi
python3 -m venv .venv          # 系统 python3 = 3.9.6，恰为目标版本
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt pytest
```

预期：安装成功。TA-Lib 在 requirements 中被注释，若后续因子测试报 `No module named 'talib'`：`brew install ta-lib && .venv/bin/pip install TA-Lib`。

- [ ] **Step 2: 跑基线测试确认全绿**

Run: `.venv/bin/python -m pytest -q`
预期：全部 PASS（个别真连 Postgres 的测试因 TEST_DATABASE_URL 未设而 skip 属正常）。若基线即红，停下修环境，不得带病开工。

---

### Task 1: `Instrument.min_cost` 字段 + CcxtAdapter 填充（spec §5.3 前半）

**Files:**
- Modify: `gridtrade/exchanges/base.py:18-26`（Instrument dataclass）
- Modify: `gridtrade/exchanges/ccxt_adapter.py:46-54`（list_instruments）
- Test: `tests/exchanges/test_ccxt_adapter.py`

**Interfaces:**
- Produces: `Instrument.min_cost: float = 0.0`（**追加在字段末尾**，保持既有位置参构造兼容）；`CcxtAdapter.list_instruments()` 从 ccxt `limits.cost.min` 填充。
- Consumes: 无。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_ccxt_adapter.py` 末尾追加（并给 `FakeCcxtClient.markets` 的 BTC 条目加 `'cost': {'min': 5.0}`——改 `'limits': {'amount': {'min': 0.001}}` 为 `'limits': {'amount': {'min': 0.001}, 'cost': {'min': 5.0}}`）：

```python
def test_list_instruments_fills_min_cost():
    # Instrument.min_cost 取 ccxt limits.cost.min（币安 MIN_NOTIONAL 语义，spec §5.3）
    insts = _adapter().list_instruments()
    assert insts[0].min_cost == 5.0


def test_instrument_min_cost_defaults_zero():
    from gridtrade.exchanges.base import Instrument
    i = Instrument(symbol='X/USDT:USDT', tick=0.1, lot=0.1, min_size=0.1,
                   state='live', list_ts=0)
    assert i.min_cost == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py -q`
预期：2 个新测试 FAIL（`min_cost` 属性不存在）。

- [ ] **Step 3: 实现**

`base.py` Instrument 追加末尾字段：

```python
@dataclass
class Instrument:
    symbol: str
    tick: float
    lot: float
    min_size: float
    state: str
    list_ts: int  # 上市时间，毫秒
    min_cost: float = 0.0   # 单笔最小名义额（币安 MIN_NOTIONAL；0=交易所无此约束/未知）
```

`ccxt_adapter.py` list_instruments 的 `out.append(Instrument(...))` 增加一行参数：

```python
                min_cost=float(((m.get('limits', {}) or {}).get('cost', {}) or {}).get('min') or 0.0),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/ -q`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/base.py gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_ccxt_adapter.py
git commit -m "feat(exchanges): Instrument.min_cost 字段——币安按币最小名义额的数据面(spec 2026-07-14 §5.3)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: BinanceAdapter 骨架（构造/testnet/settle 过滤/cloid/ping）（spec §3.1、§5.1）

**Files:**
- Create: `gridtrade/exchanges/binance.py`
- Test: `tests/exchanges/test_binance_adapter.py`

**Interfaces:**
- Produces: `BinanceAdapter(CcxtAdapter)`，`name='binance'`，`quote_currency='USDT'`（继承类默认），`FUNDING_INTERVAL_HOURS=8`，`from_credentials(api_key, secret, *, testnet=False, proxies=None, timeout=10000)`，`encode_cloid`（合法直传/非法字符替换 `-`/36 截断），`_include_market`（settle 过滤），`exchange_status()`（ping）。
- Produces（测试侧）: `FakeBinanceClient`（后续 Task 3-6 测试复用，定义在 test 文件顶部）。

- [ ] **Step 1: 写失败测试**

创建 `tests/exchanges/test_binance_adapter.py`：

```python
from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


class FakeBinanceClient(FakeCcxtClient):
    """binanceusdm 桩：在通用 ccxt 桩上补币安原生端点。markets 含 USDT/USDC 双结算。"""
    def __init__(self):
        super().__init__()
        self.pinged = 0
        self.markets = {
            'BTC/USDT:USDT': {'id': 'BTCUSDT', 'symbol': 'BTC/USDT:USDT', 'swap': True,
                              'settle': 'USDT', 'base': 'BTC', 'active': True,
                              'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 50.0}},
                              'info': {'listTime': '0'}},
            'ETH/USDT:USDT': {'id': 'ETHUSDT', 'symbol': 'ETH/USDT:USDT', 'swap': True,
                              'settle': 'USDT', 'base': 'ETH', 'active': True,
                              'precision': {'price': 0.01, 'amount': 0.01},
                              'limits': {'amount': {'min': 0.01}, 'cost': {'min': 20.0}},
                              'info': {'listTime': '0'}},
            'BTC/USDC:USDC': {'id': 'BTCUSDC', 'symbol': 'BTC/USDC:USDC', 'swap': True,
                              'settle': 'USDC', 'base': 'BTC', 'active': True,
                              'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 5.0}},
                              'info': {'listTime': '0'}},
        }
    def load_markets(self):
        return self.markets
    def fapiPublicGetPing(self, params=None):
        self.pinged += 1
        return {}


def _binance(client=None):
    from gridtrade.exchanges.binance import BinanceAdapter
    return BinanceAdapter(client or FakeBinanceClient())


def test_basic_attrs():
    a = _binance()
    assert a.name == 'binance'
    assert a.quote_currency == 'USDT'
    assert a.FUNDING_INTERVAL_HOURS == 8
    # ccxt 统一符号即规范符号：恒等映射
    assert a.to_native('BTC/USDT:USDT') == 'BTC/USDT:USDT'
    assert a.to_canonical('BTC/USDT:USDT') == 'BTC/USDT:USDT'


def test_list_instruments_filters_settle():
    # fapi 同时挂 USDT-M 与 USDC-M：只收本结算币（spec §3.1）
    syms = [i.symbol for i in _binance().list_instruments()]
    assert 'BTC/USDT:USDT' in syms and 'ETH/USDT:USDT' in syms
    assert 'BTC/USDC:USDC' not in syms


def test_encode_cloid_legal_passthrough():
    a = _binance()
    # 内部三种格式均在币安 futures 合法字符集内（含 ':'）→ 原样直传
    for oid in ('12:3:1', '12:fuse:low', '12:close:2'):
        assert a.encode_cloid(oid) == oid
    assert a.encode_cloid(None) is None


def test_encode_cloid_sanitizes_and_truncates():
    a = _binance()
    assert a.encode_cloid('a b中') == 'a-b-'       # 非法字符确定性替换 '-'
    assert len(a.encode_cloid('x' * 50)) == 36          # 36 上限截断


def test_exchange_status_ping():
    c = FakeBinanceClient()
    a = _binance(c)
    assert a.exchange_status() == 'ok' and c.pinged == 1
    def boom(params=None):
        raise RuntimeError('down')
    c.fapiPublicGetPing = boom
    assert a.exchange_status() == 'maintenance'


def test_from_credentials_testnet_sandbox():
    import ccxt
    from gridtrade.exchanges.binance import BinanceAdapter
    a = BinanceAdapter.from_credentials('k', 's', testnet=True)
    assert isinstance(a.client, ccxt.binanceusdm)
    # sandbox 模式生效：api url 指向 testnet
    assert 'testnet' in str(a.client.urls['api']).lower()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：FAIL `No module named 'gridtrade.exchanges.binance'`。

- [ ] **Step 3: 实现 `gridtrade/exchanges/binance.py`**

```python
"""币安 USDT-M 永续适配器：API key 凭证/资金费 8h/结算币过滤/真实 quote_volume。
spec: docs/superpowers/specs/2026-07-14-binance-migration-design.md §3.1
"""
import re

from gridtrade.exchanges.ccxt_adapter import CcxtAdapter

# 币安 futures newClientOrderId 官方正则 ^[\.A-Z\:/a-z0-9_-]{1,36}$（含 ':' '.'）。
# 内部 '{gid}:{line}:{seq}' 直传合法；非法字符确定性替换 '-'（testnet 实测见冒烟脚本）。
_CLOID_BAD = re.compile(r'[^\.A-Z\:/a-z0-9_-]')


class BinanceAdapter(CcxtAdapter):
    name = 'binance'
    FUNDING_INTERVAL_HOURS = 8   # 信息性：部分币 4h/1h；记账走真实流水不受影响（spec §九）

    def __init__(self, client):
        super().__init__(client, name='binance')

    # fapi 同时挂 USDT-M 与 USDC-M 合约：只收本结算币，防 USDC 合约混入票池（spec §3.1）
    def _include_market(self, m) -> bool:
        return m.get('settle') == self.quote_currency

    def encode_cloid(self, client_oid):
        if client_oid is None:
            return None
        s = _CLOID_BAD.sub('-', str(client_oid))[:36]
        return s or None

    def exchange_status(self) -> str:
        # fapi 无期货维护状态公共端点：ping 判定（权重1；spec §3.1）
        try:
            self.client.fapiPublicGetPing()
            return 'ok'
        except Exception:
            return 'maintenance'

    @classmethod
    def from_credentials(cls, api_key, secret, *, testnet=False, proxies=None,
                         timeout=10000):
        import ccxt
        client = ccxt.binanceusdm({
            'apiKey': api_key, 'secret': secret,
            'timeout': timeout, 'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if testnet:
            client.set_sandbox_mode(True)
        return cls(client)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：6 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/binance.py tests/exchanges/test_binance_adapter.py
git commit -m "feat(exchanges): BinanceAdapter 骨架——binanceusdm 凭证/testnet/settle 过滤/cloid 合法化/ping 状态(spec 2026-07-14 §3.1,§5.1)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `fetch_ohlcv` 覆写——真实 quote_volume（spec §5.4 实盘侧）

**Files:**
- Modify: `gridtrade/exchanges/binance.py`
- Test: `tests/exchanges/test_binance_adapter.py`

**Interfaces:**
- Produces: `BinanceAdapter.fetch_ohlcv(symbol, timeframe, start_ms, end_ms) -> DataFrame[CANDLE_COLS]`（真实 quote_volume）；`BinanceAdapter._market_id(symbol) -> 'BTCUSDT'`（Task 4 复用）。
- Consumes: 基类 `CANDLE_COLS` schema、`self.client.parse_timeframe`。

- [ ] **Step 1: 写失败测试**

`FakeBinanceClient` 追加方法：

```python
    def fapiPublicGetKlines(self, params=None):
        self.kline_calls = getattr(self, 'kline_calls', [])
        self.kline_calls.append(dict(params or {}))
        # 原生 12 列（数值为字符串——忠实币安响应）
        return [
            [1704067200000, "1.0", "2.0", "0.5", "1.5", "10.0", 1704070799999,
             "13.7", 5, "4.0", "5.5", "0"],
            [1704070800000, "1.5", "2.5", "1.0", "2.0", "20.0", 1704074399999,
             "36.2", 8, "9.0", "16.3", "0"],
        ]
```

测试追加：

```python
def test_fetch_ohlcv_real_quote_volume():
    from gridtrade.exchanges.base import CANDLE_COLS
    c = FakeBinanceClient()
    a = _binance(c)
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', 0, 10**13)
    assert list(df.columns) == CANDLE_COLS
    # 真实 quote_volume（第8列），非 (open+close)/2*vol 估算（spec §5.4）
    assert df['quote_volume'].tolist() == [13.7, 36.2]
    assert df['volCcy'].tolist() == [10.0, 20.0]
    assert df['close'].tolist() == [1.5, 2.0]
    # 原生 id + interval 直传
    assert c.kline_calls[0]['symbol'] == 'BTCUSDT'
    assert c.kline_calls[0]['interval'] == '1h'
    assert c.kline_calls[0]['limit'] == 1500


def test_fetch_ohlcv_empty():
    c = FakeBinanceClient()
    c.fapiPublicGetKlines = lambda params=None: []
    df = _binance(c).fetch_ohlcv('BTC/USDT:USDT', '1h', 0, 10**13)
    assert df.empty
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：新测试 FAIL（quote_volume 为估算值 12.5/35.0，走了基类路径）。

- [ ] **Step 3: 实现（binance.py 追加）**

```python
import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS
```

（import 合并到文件顶部）类内追加：

```python
    def _market_id(self, symbol):
        """canonical → 币安原生 id（'BTC/USDT:USDT'→'BTCUSDT'）。markets 惰性加载；
        查不到（极新上市）按命名规则回退拼接。"""
        if not getattr(self.client, 'markets', None):
            self.client.load_markets()
        m = (self.client.markets or {}).get(symbol)
        if m and m.get('id'):
            return m['id']
        return symbol.split('/')[0] + self.quote_currency

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms) -> pd.DataFrame:
        """原生 klines 端点（分页语义同基类），取**真实 quote_volume**（第8列）——
        选币因子 vwap=quote_volume/volCcy 与回测(Vision 归档)同分布（spec §5.4）。"""
        native_id = self._market_id(symbol)
        tf_ms = int(self.client.parse_timeframe(timeframe) * 1000)
        all_rows = []
        cursor = int(start_ms)
        bound = min(int(end_ms), self._now_ms())   # 不向未来翻页（同基类）
        guard = 0
        while cursor <= bound and guard < 10000:
            guard += 1
            batch = self.client.fapiPublicGetKlines({
                'symbol': native_id, 'interval': timeframe,
                'startTime': int(cursor), 'limit': 1500})
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts < cursor:
                break
            cursor = last_ts + tf_ms
            if last_ts >= end_ms:
                break
        if not all_rows:
            return pd.DataFrame(columns=CANDLE_COLS)
        df = pd.DataFrame(all_rows, columns=[
            'ts', 'open', 'high', 'low', 'close', 'vol', 'close_time',
            'quote_volume', 'count', 'tbv', 'tbqv', 'ignore'])
        df['ts'] = df['ts'].astype('int64')
        df = df.drop_duplicates(subset=['ts'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        for c in ('open', 'high', 'low', 'close', 'vol', 'quote_volume'):
            df[c] = df[c].astype(float)
        df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
        df['symbol'] = symbol
        df['volCcy'] = df['vol']
        return df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/binance.py tests/exchanges/test_binance_adapter.py
git commit -m "feat(exchanges): BinanceAdapter.fetch_ohlcv 原生 klines——真实 quote_volume,实盘/回测同分布(spec 2026-07-14 §5.4)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 账户级批量读四覆写（monitor 快照权重预算）（spec §3.1）

**Files:**
- Modify: `gridtrade/exchanges/binance.py`
- Test: `tests/exchanges/test_binance_adapter.py`

**Interfaces:**
- Produces: `_id_map() -> {native_id: canonical}`；`fetch_open_orders_all(symbols) -> List[Order]`；`fetch_positions_all(symbols) -> {sym: signed float}`；`fetch_prices_all(symbols) -> {sym: float}`；`fetch_funding_payments_all(symbols, since_ms=None) -> {sym: [FundingPayment]}`（支付为正，ts 升序）。`fetch_my_trades_all` 沿用基类逐 symbol 合成（不覆写）。
- Consumes: Task 2 的 `_include_market`、Task 3 的 `_market_id`；基类 `_to_order`、`FundingPayment`。

- [ ] **Step 1: 写失败测试**

`FakeBinanceClient` 追加：

```python
    def fetch_positions(self, symbols=None, params=None):
        # 无参=全账户 positionRisk（币安权重5）
        return [{'symbol': 'BTC/USDT:USDT', 'contracts': 3.0, 'side': 'long',
                 'entryPrice': 100.0},
                {'symbol': 'ETH/USDT:USDT', 'contracts': 2.0, 'side': 'short',
                 'entryPrice': 50.0}]
    def fapiPublicGetTickerPrice(self, params=None):
        return [{'symbol': 'BTCUSDT', 'price': '50000.5'},
                {'symbol': 'ETHUSDT', 'price': '3000.25'},
                {'symbol': 'BTCUSDC', 'price': '49999.0'}]
    def fapiPrivateGetIncome(self, params=None):
        self.income_calls = getattr(self, 'income_calls', [])
        self.income_calls.append(dict(params or {}))
        return [
            {'symbol': 'BTCUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.5',
             'time': 2000},
            {'symbol': 'ETHUSDT', 'incomeType': 'FUNDING_FEE', 'income': '0.3',
             'time': 1000},
            {'symbol': 'XRPUSDT', 'incomeType': 'FUNDING_FEE', 'income': '9.9',
             'time': 1500},
        ]
```

测试追加：

```python
def test_fetch_open_orders_all_single_call():
    c = FakeBinanceClient()
    calls = []
    def fetch_open_orders(symbol=None, params=None):
        calls.append(symbol)
        return [{'id': '7', 'clientOrderId': '1:0:0', 'symbol': 'BTC/USDT:USDT',
                 'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
                 'status': 'open'},
                {'id': '8', 'clientOrderId': '2:0:0', 'symbol': 'DOGE/USDT:USDT',
                 'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
                 'status': 'open'}]
    c.fetch_open_orders = fetch_open_orders
    out = _binance(c).fetch_open_orders_all(['BTC/USDT:USDT'])
    assert calls == [None]                       # 无 symbol=全账户一次（权重40）
    assert [o.symbol for o in out] == ['BTC/USDT:USDT']   # 只回请求的 symbols


def test_fetch_positions_all_signed():
    out = _binance().fetch_positions_all(['BTC/USDT:USDT', 'ETH/USDT:USDT',
                                          'SOL/USDT:USDT'])
    assert out['BTC/USDT:USDT'] == 3.0
    assert out['ETH/USDT:USDT'] == -2.0          # short → 负
    assert 'SOL/USDT:USDT' not in out            # 无持仓行=缺省（调用方按0处理）


def test_fetch_prices_all_ticker_price():
    out = _binance().fetch_prices_all(['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    assert out == {'BTC/USDT:USDT': 50000.5, 'ETH/USDT:USDT': 3000.25}


def test_fetch_funding_payments_all_income_grouped():
    c = FakeBinanceClient()
    out = _binance(c).fetch_funding_payments_all(
        ['BTC/USDT:USDT', 'ETH/USDT:USDT'], since_ms=500)
    # income 正=收入 → 统一"支付为正"取负；按币分组、ts 升序；XRP 不在请求内被丢弃
    assert [(p.ts, p.amount) for p in out['BTC/USDT:USDT']] == [(2000, 0.5)]
    assert [(p.ts, p.amount) for p in out['ETH/USDT:USDT']] == [(1000, -0.3)]
    assert c.income_calls[0]['incomeType'] == 'FUNDING_FEE'
    assert c.income_calls[0]['startTime'] == 500
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：4 个新测试 FAIL（基类逐 symbol 语义/无 income 路径）。

- [ ] **Step 3: 实现（binance.py 类内追加；顶部补 `from gridtrade.exchanges.base import FundingPayment`）**

```python
    # ---- 账户级批量读（monitor 5s 快照权重预算核心，spec §3.1）----
    def _id_map(self):
        """原生 id('BTCUSDT') → canonical。只收本结算币 swap；实例缓存。"""
        if getattr(self, '_id_map_cache', None) is None:
            if not getattr(self.client, 'markets', None):
                self.client.load_markets()
            m2 = {}
            for m in (self.client.markets or {}).values():
                if m.get('swap') is not True or not self._include_market(m):
                    continue
                m2[m['id']] = self.to_canonical(m['symbol'])
            self._id_map_cache = m2
        return self._id_map_cache

    def fetch_open_orders_all(self, symbols):
        # 无 symbol 的 openOrders：全账户一次（权重40），替代逐币 N 次
        want = set(symbols)
        return [o for o in (self._to_order(r)
                            for r in self.client.fetch_open_orders(None))
                if o.symbol in want]

    def fetch_positions_all(self, symbols):
        # positionRisk 全账户（权重5）；无持仓行=缺省（monitor 按 0 处理）
        want = set(symbols)
        out = {}
        for p in self.client.fetch_positions():
            sym = self.to_canonical(p['symbol'])
            if sym not in want:
                continue
            contracts = float(p.get('contracts') or 0.0)
            out[sym] = contracts if p.get('side') == 'long' else -contracts
        return out

    def fetch_prices_all(self, symbols):
        # 全市场 ticker/price（权重2），替代逐币 fetchTicker
        want = set(symbols)
        idmap = self._id_map()
        out = {}
        for r in self.client.fapiPublicGetTickerPrice():
            sym = idmap.get(r.get('symbol'))
            if sym in want:
                out[sym] = float(r['price'])
        for s in want - set(out):          # 罕见后备（新上市 markets 未刷新）
            out[s] = float(self.fetch_price(s))
        return out

    def fetch_funding_payments_all(self, symbols, since_ms=None):
        """income(FUNDING_FEE) 账户级单流（权重30）——币安按 symbol 正确打标，
        分组回各币种。无 since → 币安默认近7天。统一"支付为正"（income 正=收入取负）。"""
        idmap = self._id_map()
        out = {s: [] for s in symbols}
        params = {'incomeType': 'FUNDING_FEE', 'limit': 1000}
        if since_ms is not None:
            params['startTime'] = int(since_ms)
        guard = 0
        while guard < 50:
            guard += 1
            rows = self.client.fapiPrivateGetIncome(dict(params))
            for r in rows:
                ts = int(r['time'])
                if since_ms is not None and ts < since_ms:
                    continue
                sym = idmap.get(r.get('symbol'))
                if sym in out:
                    out[sym].append(FundingPayment(ts=ts, amount=-float(r['income'])))
            if len(rows) < 1000:
                break
            params['startTime'] = int(rows[-1]['time']) + 1
        for s in out:
            out[s].sort(key=lambda p: p.ts)
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/binance.py tests/exchanges/test_binance_adapter.py
git commit -m "feat(exchanges): BinanceAdapter 账户级批量读——openOrders/positionRisk/tickerPrice/income 快照四覆写,5s 轮权重~1400/min(spec 2026-07-14 §3.1)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 交易语义——STOP_MARKET / 全仓+整数杠杆 / 账户模式断言 + Resilient 转发 + 启动接线（spec §3.1、§5.2）

**Files:**
- Modify: `gridtrade/exchanges/binance.py`
- Modify: `gridtrade/exchanges/base.py`（`assert_account_mode` 默认无操作）
- Modify: `gridtrade/exchanges/resilient_adapter.py`（直通转发）
- Modify: `gridtrade/runtime/monitor.py`、`gridtrade/runtime/scheduler.py`（main 启动断言）
- Test: `tests/exchanges/test_binance_adapter.py`

**Interfaces:**
- Produces: `create_stop_order`（STOP_MARKET，忽略 slippage）；`set_leverage`（先 cross 后整数杠杆）；`assert_account_mode()`（基类默认 no-op；Binance 断言单向持仓+关联合保证金；ResilientAdapter 直通）。
- Consumes: 基类 `_params`、`_to_order`。

- [ ] **Step 1: 写失败测试**

`FakeBinanceClient` 追加：

```python
    def set_margin_mode(self, mode, symbol=None, params=None):
        self.margin_calls = getattr(self, 'margin_calls', [])
        self.margin_calls.append((mode, symbol))
    def fapiPrivateGetPositionSideDual(self, params=None):
        return {'dualSidePosition': getattr(self, 'dual', False)}
    def fapiPrivateGetMultiAssetsMargin(self, params=None):
        return {'multiAssetsMargin': getattr(self, 'multi', False)}
```

测试追加：

```python
def test_create_stop_order_stop_market():
    c = FakeBinanceClient()
    a = _binance(c)
    a.create_stop_order('BTC/USDT:USDT', 'sell', 1.5, 95.0, client_oid='9:fuse:low')
    sym, typ, side, amount, price, params = c.created[-1]
    assert typ == 'market' and price is None            # STOP_MARKET：无限价
    assert params['stopLossPrice'] == 95.0
    assert params['reduceOnly'] is True
    assert params['clientOrderId'] == '9:fuse:low'
    assert 'slippage' not in params                     # 币安无滑点底线参数（spec §5.2）


def test_set_leverage_cross_then_int():
    c = FakeBinanceClient()
    lev_calls = []
    c.set_leverage = lambda lev, symbol=None, params=None: lev_calls.append((lev, symbol))
    _binance(c).set_leverage('BTC/USDT:USDT', 5.0)
    assert c.margin_calls == [('cross', 'BTC/USDT:USDT')]
    assert lev_calls == [(5, 'BTC/USDT:USDT')]          # 币安要求整数杠杆


def test_set_leverage_swallows_no_need_to_change():
    c = FakeBinanceClient()
    def boom(mode, symbol=None, params=None):
        raise RuntimeError('binanceusdm {"code":-4046,"msg":"No need to change margin type."}')
    c.set_margin_mode = boom
    lev_calls = []
    c.set_leverage = lambda lev, symbol=None, params=None: lev_calls.append(lev)
    _binance(c).set_leverage('BTC/USDT:USDT', 5)        # 不抛
    assert lev_calls == [5]


def test_assert_account_mode_ok_and_rejects():
    import pytest
    c = FakeBinanceClient()
    a = _binance(c)
    a.assert_account_mode()                             # 单向+单币 → 通过
    c.dual = True
    with pytest.raises(RuntimeError):
        a.assert_account_mode()
    c.dual = False; c.multi = 'true'                    # 字符串布尔也要识别
    with pytest.raises(RuntimeError):
        a.assert_account_mode()


def test_base_and_resilient_assert_account_mode():
    from gridtrade.exchanges.fake import FakeExchange
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter
    FakeExchange().assert_account_mode()                # 基类默认 no-op
    called = []
    class Probe(FakeExchange):
        def assert_account_mode(self):
            called.append(1)
    ResilientAdapter(Probe()).assert_account_mode()     # 直通转发
    assert called == [1]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q`
预期：新测试 FAIL（create_stop_order 走基类带 slippage；assert_account_mode 不存在）。

- [ ] **Step 3: 实现**

`base.py`（`encode_cloid` 之后追加）：

```python
    def assert_account_mode(self) -> None:
        """启动断言：账户模式满足引擎假设（净仓语义/单币保证金）。默认无约束。
        monitor/scheduler 启动时调用一次；不满足抛 RuntimeError 拒绝起跑。"""
        return None
```

`binance.py` 类内追加：

```python
    def create_stop_order(self, symbol, side, size, trigger_price, *,
                          reduce_only=True, slippage=0.15, client_oid=None):
        """STOP_MARKET 触发市价单（灾难保险丝）。币安无滑点底线参数——slippage
        接受但忽略（语义差已文档化，spec §5.2；软止损仍是主刹车）。"""
        p = self._params(reduce_only, client_oid)
        p['stopLossPrice'] = trigger_price
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     None, p)
        return self._to_order(r)

    def set_leverage(self, symbol, leverage) -> None:
        """先确保 CROSSED 全仓（幂等，吞 -4046 无需更改），再设杠杆（币安要求整数）。
        全仓对齐 账户杠杆/gearing 仓位体系假设（spec §3.1）。"""
        native = self.to_native(symbol)
        try:
            self.client.set_margin_mode('cross', native)
        except Exception as exc:
            msg = str(exc)
            if '-4046' not in msg and 'No need to change' not in msg:
                raise
        self.client.set_leverage(int(leverage), native)

    def assert_account_mode(self) -> None:
        """单向持仓 + 关闭联合保证金（引擎净仓/单币权益假设，spec §3.1）。"""
        dual = self.client.fapiPrivateGetPositionSideDual() or {}
        if str(dual.get('dualSidePosition')).lower() in ('true', '1'):
            raise RuntimeError('币安账户为双向持仓(hedge)模式：执行引擎按净仓语义工作，'
                               '请在合约偏好设置切换为单向持仓后重启')
        multi = self.client.fapiPrivateGetMultiAssetsMargin() or {}
        if str(multi.get('multiAssetsMargin')).lower() in ('true', '1'):
            raise RuntimeError('币安联合保证金(Multi-Assets)开启：权益口径须为单一 %s，'
                               '请关闭后重启' % self.quote_currency)
```

`resilient_adapter.py`（`quantize_amount` 后追加，同直通模式）：

```python
    def assert_account_mode(self):
        """启动一次性断言，直通 inner（不套重试/熔断——失败须原样抛出拒绝起跑）。"""
        return self._inner.assert_account_mode()
```

`monitor.py::main`——`rt = build_runtime(load_deploy_config())` 之后、print 之前插入：

```python
    rt.adapter.assert_account_mode()   # 账户模式不符→boot 失败（fail-fast，勿带病起跑）
```

`scheduler.py::main` 同位置插入同一行。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/ tests/runtime/test_factory.py -q`
预期：全 PASS（monitor/scheduler main 为 composition root 不单测）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/binance.py gridtrade/exchanges/base.py gridtrade/exchanges/resilient_adapter.py gridtrade/runtime/monitor.py gridtrade/runtime/scheduler.py tests/exchanges/test_binance_adapter.py
git commit -m "feat(exchanges): 币安交易语义——STOP_MARKET 保险丝/全仓整数杠杆/账户模式启动断言+Resilient 直通(spec 2026-07-14 §3.1,§5.2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 快照六方法契约——base docstring 契约化 + fake/binance 共用契约测试（spec §四）

**Files:**
- Modify: `gridtrade/exchanges/base.py:180-201`（批量读 docstring）
- Test: `tests/exchanges/test_snapshot_contract.py`（新建）

**Interfaces:**
- Produces: 契约测试模块（未来 WsFeedAdapter 对同一套用例开发）；base.py 六方法契约 docstring。
- Consumes: `FakeExchange` 测试钩子（`set_price`/`create_limit_order`）、`FakeBinanceClient`。

- [ ] **Step 1: 写契约测试（新建 `tests/exchanges/test_snapshot_contract.py`）**

```python
"""快照六方法契约守卫（spec 2026-07-14 §四）：monitor 唯一读取口。
fake 与 BinanceAdapter(mock) 共用同一套用例——未来 WsFeedAdapter 镜像实现
对着本文件开发，上层零改动。契约：调用时刻最新已知状态、canonical symbol 键、
只读幂等、列表按 ts 升序；不泄漏 REST 假设（分页/权重/时序）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Balance, FundingPayment, Order
from tests.exchanges.test_binance_adapter import FakeBinanceClient, _binance

SYM = 'BTC/USDT:USDT'


def _fake():
    ex = FakeExchange()
    ex.set_price(SYM, 100.0)
    ex.create_limit_order(SYM, 'buy', 90.0, 1.0, client_oid='1:0:0')
    ex.seed_funding_payments(SYM, [(2000, 0.5), (1000, -0.3)])
    return ex


@pytest.fixture(params=['fake', 'binance'])
def adapter(request):
    return _fake() if request.param == 'fake' else _binance(FakeBinanceClient())


def test_prices_all_float_by_canonical(adapter):
    out = adapter.fetch_prices_all([SYM])
    assert set(out) == {SYM} and isinstance(out[SYM], float)


def test_positions_all_signed_float(adapter):
    out = adapter.fetch_positions_all([SYM])
    for v in out.values():
        assert isinstance(v, float)


def test_open_orders_all_only_requested(adapter):
    out = adapter.fetch_open_orders_all([SYM])
    assert all(isinstance(o, Order) and o.symbol == SYM for o in out)


def test_my_trades_all_sorted(adapter):
    out = adapter.fetch_my_trades_all([SYM])
    assert [t.ts for t in out] == sorted(t.ts for t in out)


def test_funding_payments_all_sorted(adapter):
    out = adapter.fetch_funding_payments_all([SYM], since_ms=0)
    assert set(out) == {SYM}
    ts = [p.ts for p in out[SYM]]
    assert ts == sorted(ts)
    assert all(isinstance(p, FundingPayment) for p in out[SYM])


def test_balance_shape(adapter):
    b = adapter.fetch_balance()
    assert isinstance(b, Balance)
```

- [ ] **Step 2: 跑测试确认状态**

Run: `.venv/bin/python -m pytest tests/exchanges/test_snapshot_contract.py -q`
预期：PASS（前序任务已实现全部方法）。若 FAIL 修实现而非测试。

- [ ] **Step 3: base.py 契约 docstring**

把 `base.py` 批量读区块注释与六方法 docstring 替换为：

```python
    # ---- 账户级批量读（monitor 快照唯一读取口，spec 2026-07-14 §四）----
    # 契约：返回调用时刻的最新已知状态（只读幂等，不要求强一致）；键/symbol 一律
    # canonical；列表按 ts 升序；实现不得让上层感知分页游标/权重/调用时序。
    # 未来 WsFeedAdapter 以内存镜像覆写本组方法即可无感升级（契约测试
    # tests/exchanges/test_snapshot_contract.py 为开发基准）。默认逐 symbol 合成。
    def fetch_my_trades_all(self, symbols, since_ms: Optional[int] = None) -> List[Trade]:
        """指定 symbols 的成交流水快照，ts 升序。"""
        ...
    def fetch_open_orders_all(self, symbols) -> List[Order]:
        """指定 symbols 的当前挂单快照（只含请求的 symbols）。"""
        ...
    def fetch_positions_all(self, symbols) -> dict:
        """{canonical: 带符号净仓}；无持仓可缺省（调用方按 0 处理）。"""
        ...
    def fetch_prices_all(self, symbols) -> dict:
        """{canonical: 最新价 float}。"""
        ...
    def fetch_funding_payments_all(self, symbols, since_ms: Optional[int] = None) -> dict:
        """{canonical: [FundingPayment]}，各列表 ts 升序，支付为正。"""
        ...
```

（保留原方法体，只改注释/docstring——`...` 处为原实现不动。）`fetch_balance` 的抽象声明补一行 docstring：`"""账户权益快照（quote_currency 计价）。"""`

- [ ] **Step 4: 全量回归**

Run: `.venv/bin/python -m pytest tests/exchanges/ -q`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/base.py tests/exchanges/test_snapshot_contract.py
git commit -m "feat(exchanges): 快照六方法契约文档化+fake/binance 共用契约守卫——WS 预留接缝落地(spec 2026-07-14 §四)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: registry/config/factory 切换 + 退役键守卫（spec §3.2）

**Files:**
- Modify: `gridtrade/exchanges/registry.py`
- Modify: `gridtrade/config.py:42-148`
- Modify: `gridtrade/runtime/factory.py:47-53`
- Test: `tests/exchanges/test_registry.py`、`tests/test_config.py`

**Interfaces:**
- Produces: `build_adapter({'exchange':'binance','api_key','secret','testnet','quote_currency'})`；`DeployConfig.api_key/api_secret`（替代 wallet_address/private_key）；env `BINANCE_API_KEY/BINANCE_API_SECRET/BINANCE_TESTNET`、`EXCHANGE` 默认 `'binance'`；HL_* 退役键 boot 报错。
- Consumes: Task 2 `BinanceAdapter.from_credentials`。

- [ ] **Step 1: 改写测试**

`tests/exchanges/test_registry.py` 整文件替换为：

```python
import pytest


def test_build_fake():
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.fake import FakeExchange
    assert isinstance(build_adapter({'exchange': 'fake'}), FakeExchange)


def test_build_binance():
    import ccxt
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.binance import BinanceAdapter
    a = build_adapter({'exchange': 'binance', 'api_key': 'k', 'secret': 's'})
    assert isinstance(a, BinanceAdapter) and isinstance(a.client, ccxt.binanceusdm)
    assert a.quote_currency == 'USDT'


def test_build_binance_testnet():
    from gridtrade.exchanges.registry import build_adapter
    a = build_adapter({'exchange': 'binance', 'api_key': 'k', 'secret': 's',
                       'testnet': True})
    assert 'testnet' in str(a.client.urls['api']).lower()


def test_quote_currency_override_applied():
    from gridtrade.exchanges.registry import build_adapter
    a = build_adapter({'exchange': 'binance', 'api_key': 'k', 'secret': 's',
                       'quote_currency': 'USDC'})
    assert a.quote_currency == 'USDC'    # USDC-M 之门保留（spec §3.2）


def test_removed_exchanges_raise():
    from gridtrade.exchanges.registry import build_adapter
    for name in ('hyperliquid', 'okx', 'nope'):
        with pytest.raises(ValueError):
            build_adapter({'exchange': name})
```

`tests/test_config.py` 追加（保留既有用例，凭证相关旧断言按新字段名对齐——先跑一遍看哪些引用 `wallet_address`/`HL_`，同步改名）：

```python
def test_binance_credentials_and_defaults():
    from gridtrade.config import load_deploy_config
    cfg = load_deploy_config({'BINANCE_API_KEY': 'k', 'BINANCE_API_SECRET': 's',
                              'BINANCE_TESTNET': 'true'})
    assert cfg.exchange == 'binance'          # 默认交易所=binance
    assert cfg.api_key == 'k' and cfg.api_secret == 's'
    assert cfg.testnet is True


def test_hl_legacy_keys_rejected():
    import pytest
    from gridtrade.config import load_deploy_config
    for key in ('HL_WALLET_ADDRESS', 'HL_PRIVATE_KEY', 'HL_TESTNET'):
        with pytest.raises(RuntimeError):
            load_deploy_config({key: 'x'})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_registry.py tests/test_config.py -q`
预期：新用例 FAIL。

- [ ] **Step 3: 实现**

`registry.py` 整文件替换：

```python
"""按配置构造交易所适配器（Factory）。"""
from gridtrade.exchanges.base import ExchangeAdapter
from gridtrade.exchanges.binance import BinanceAdapter
from gridtrade.exchanges.fake import FakeExchange


def build_adapter(config: dict) -> ExchangeAdapter:
    name = (config.get('exchange') or '').lower()
    if name == 'fake':
        adapter = FakeExchange()
    elif name == 'binance':
        adapter = BinanceAdapter.from_credentials(
            config.get('api_key', ''), config.get('secret', ''),
            testnet=bool(config.get('testnet', False)),
            proxies=config.get('proxies'))
    else:
        raise ValueError(f'未知交易所: {name!r}（支持: binance/fake）')
    # 可选覆写计价/结算币：非空才覆写（同所多结算之门：USDT-M 默认 / USDC-M 显式设）。
    # 实例属性同时驱动符号拼接与读余额（单一事实源）。
    qc = config.get('quote_currency')
    if qc:
        adapter.quote_currency = qc
    return adapter
```

`config.py`：
1. DeployConfig 字段 `wallet_address: str` / `private_key: str` 改为 `api_key: str` / `api_secret: str`（`testnet`/`quote_currency` 保留；quote_currency 注释改 `'' -> 用适配器类默认（Binance=USDT）`）。
2. `load_deploy_config` 退役键守卫元组扩为：

```python
    for legacy, repl in (('LEVERAGE', 'GRID_GEARING(=旧LEVERAGE×0.68,默认3.4)'),
                         ('CAP_EQUITY_FRAC', 'ACCOUNT_LEVERAGE(frac=AL/(N×gearing/2))'),
                         # 币安迁移(spec 2026-07-14)：HL 键退役,语义变更禁静默映射
                         ('HL_WALLET_ADDRESS', 'BINANCE_API_KEY'),
                         ('HL_PRIVATE_KEY', 'BINANCE_API_SECRET'),
                         ('HL_TESTNET', 'BINANCE_TESTNET')):
```

3. 构造改为：

```python
        exchange=_s(env, 'EXCHANGE', 'binance'),
        api_key=_s(env, 'BINANCE_API_KEY', ''),
        api_secret=_s(env, 'BINANCE_API_SECRET', ''),
        testnet=_b(env, 'BINANCE_TESTNET', False),
```

`factory.py` build_runtime 开头改为：

```python
    inner = build_adapter({
        'exchange': config.exchange,
        'api_key': config.api_key,
        'secret': config.api_secret,
        'testnet': config.testnet,
        'quote_currency': config.quote_currency,
    })
```

- [ ] **Step 4: 全量回归 + 修连带**

Run: `.venv/bin/python -m pytest -q`
预期：可能出现引用 `cfg.wallet_address` / `HL_` env 的既有测试（如 `tests/test_config.py` 旧用例、`tests/runtime/` 个别）失败——逐个把字段/env 名对齐为 `api_key`/`BINANCE_*`（语义等价替换，不改断言意图）。`tests/exchanges/test_hl_testnet.py` 等 HL 专属测试留给 Task 14 删除，此刻若因 registry 变更失败，可在该文件顶部临时加 `pytestmark = pytest.mark.skip(reason='HL 退役,Task 14 删除')`（Task 14 连文件一起删）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/registry.py gridtrade/config.py gridtrade/runtime/factory.py tests/exchanges/test_registry.py tests/test_config.py
git commit -m "feat(config,exchanges): 默认交易所切 binance——BINANCE_* 凭证/HL_* 退役键守卫/registry 只认 binance+fake(spec 2026-07-14 §3.2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: MinNotionalGate 按币下限（spec §5.3 后半）

**Files:**
- Modify: `gridtrade/execution/gates.py:105-142`
- Modify: `gridtrade/runtime/factory.py:69`
- Test: `tests/execution/test_gates.py`

**Interfaces:**
- Produces: `MinNotionalGate(executor, min_notional, *, adapter=None, log=None)`——下限 = `max(全局 env, Instrument.min_cost)`；`begin_batch` 刷新按币映射（fail-open）。
- Consumes: Task 1 `Instrument.min_cost`、`adapter.list_instruments()`。

- [ ] **Step 1: 写失败测试（`tests/execution/test_gates.py` 追加；沿用该文件既有的 executor/proposal 构造辅助——先读该文件复用其 fixture 风格）**

```python
def test_min_notional_gate_per_symbol_floor():
    # env 全局下限 0，但该币 Instrument.min_cost=50（如 BTCUSDT）→ 仍按 50 拒
    from gridtrade.execution.gates import MinNotionalGate, GridProposal
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange

    class _Ex:   # 最小 executor 桩：与该文件既有用例同形
        gearing = 3.4
        min_amount = 0.0
        def _resolve_cap(self):
            return 100.0

    fake = FakeExchange(instruments=[
        Instrument(symbol='BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                   state='live', list_ts=0, min_cost=50.0)])
    gate = MinNotionalGate(_Ex(), 0.0, adapter=fake)
    gate.begin_batch()
    gp = dict(low_price=100.0, high_price=120.0, grid_count=20,
              stop_low_price=95.0, stop_high_price=125.0)
    res = gate.check(GridProposal(exchange='binance', symbol='BTC/USDT:USDT',
                                  grid_params=gp))
    assert not res.passed and 'min 50' in res.reason


def test_min_notional_gate_env_floor_still_applies():
    # 币无 min_cost（映射缺省）→ 退回全局 env 下限。
    # 下限取 1000（远高于 cap100×gearing3.4 摊到 21 档的最坏名义额 ≤16.2），必拒——
    # 不依赖 grid_order_info 精确数学，测试稳健。
    from gridtrade.execution.gates import MinNotionalGate, GridProposal
    from gridtrade.exchanges.fake import FakeExchange

    class _Ex:
        gearing = 3.4
        min_amount = 0.0
        def _resolve_cap(self):
            return 100.0

    gate = MinNotionalGate(_Ex(), 1000.0, adapter=FakeExchange())
    gate.begin_batch()
    gp = dict(low_price=100.0, high_price=120.0, grid_count=20,
              stop_low_price=95.0, stop_high_price=125.0)
    res = gate.check(GridProposal(exchange='binance', symbol='X/USDT:USDT',
                                  grid_params=gp))
    assert not res.passed and 'min 1000' in res.reason
    # 微小下限 → 放行（同一提案两个下限对照，锁住 max(env, min_cost) 的方向性）
    gate2 = MinNotionalGate(_Ex(), 0.001, adapter=FakeExchange())
    gate2.begin_batch()
    assert gate2.check(GridProposal(exchange='binance', symbol='X/USDT:USDT',
                                    grid_params=gp)).passed


def test_min_notional_gate_disabled_when_no_floor():
    from gridtrade.execution.gates import MinNotionalGate, GridProposal
    class _Ex:
        gearing = 3.4
        min_amount = 0.0
        def _resolve_cap(self):
            return 100.0
    gate = MinNotionalGate(_Ex(), 0.0)           # 无 adapter、env=0 → 停用
    gp = dict(low_price=100.0, high_price=120.0, grid_count=20,
              stop_low_price=95.0, stop_high_price=125.0)
    assert gate.check(GridProposal(exchange='binance', symbol='X/USDT:USDT',
                                   grid_params=gp)).passed
```

（若既有用例断言 reason 文案，按新格式对齐；`worst` 计算路径不变。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_gates.py -q`
预期：新用例 FAIL（`adapter` kwarg 不存在）。

- [ ] **Step 3: 实现（gates.py MinNotionalGate 替换）**

```python
class MinNotionalGate(AdmissionGate):
    """最小名义额门：预检每笔挂单名义额 ≥ 下限。下限 = max(全局 env MIN_ORDER_NOTIONAL,
    该币 Instrument.min_cost)——币安各币 MIN_NOTIONAL 不同（多数 5、BTC 50、ETH 20 USDT，
    2026-07-14 fapi 实测），单一全局值必漏（spec 2026-07-14 §5.3）。

    动机（mainnet 2026-07-05 实证）：单笔 < 交易所下限 → 开仓首单即被拒 → 留零挂单死
    OPENING。门链预检直接拒提案：不建死网格、拒因可观测。

    口径与 executor.open 同源：grid_order_info + executor._resolve_cap()；
    最低档名义额 = 每笔数量 × low_price。adapter=None 且 env<=0 = 停用（向后兼容）。
    begin_batch 刷新按币映射；取数失败 fail-open 退回全局下限。"""

    def __init__(self, executor, min_notional, *, adapter=None, log=None):
        self.executor = executor
        self.min_notional = float(min_notional)
        self.adapter = adapter          # 可选：按币 min_cost 来源（Instrument.min_cost）
        self._min_cost = None           # None=未加载；{}=无数据（fail-open 只用全局下限）
        self.log = log

    def begin_batch(self) -> None:
        if self.adapter is None:
            self._min_cost = {}
            return
        try:
            self._min_cost = {i.symbol: float(getattr(i, 'min_cost', 0.0) or 0.0)
                              for i in self.adapter.list_instruments()}
        except Exception as exc:        # fail-open：精度表读不到只退化，不拒单
            self._min_cost = {}
            if self.log is not None:
                self.log('[gate] MinNotionalGate: list_instruments failed %r' % (exc,))

    def check(self, proposal: GridProposal) -> GateResult:
        if self._min_cost is None:      # 未经 begin_batch 的独立 evaluate → 惰性加载一次
            self.begin_batch()
        floor = max(self.min_notional,
                    (self._min_cost or {}).get(proposal.symbol, 0.0))
        if floor <= 0:
            return GateResult(True, 'MinNotionalGate')
        from gridtrade.core.grid_engine import grid_order_info
        gp = proposal.grid_params
        cap = (proposal.cap if proposal.cap is not None
               else self.executor._resolve_cap())
        gi = grid_order_info(cap, self.executor.gearing, gp['low_price'],
                             gp['high_price'], int(gp['grid_count']),
                             gp['stop_low_price'], gp['stop_high_price'],
                             min_amount=self.executor.min_amount,
                             max_rate=1.0)
        if gi is None:
            return GateResult(False, 'MinNotionalGate',
                              'cap %.2f 无法建网（每笔数量<=0）' % cap)
        worst = float(gi['每笔数量']) * float(gp['low_price'])   # 最低档名义额
        if worst < floor:
            return GateResult(False, 'MinNotionalGate',
                              'per-order notional %.2f < min %.2f '
                              '(cap=%.2f grids=%d)' % (worst, floor,
                                                       cap, int(gp['grid_count'])))
        return GateResult(True, 'MinNotionalGate')
```

`factory.py:69` 改为：

```python
        MinNotionalGate(executor, config.min_order_notional, adapter=adapter,
                        log=_flush_log),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_gates.py tests/runtime/test_factory.py -q`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/execution/gates.py gridtrade/runtime/factory.py tests/execution/test_gates.py
git commit -m "feat(execution): MinNotionalGate 按币下限——max(env, Instrument.min_cost),BTC50/ETH20/山寨5 实测口径(spec 2026-07-14 §5.3)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: tier0 硬禁名单迁移（spec §5.5）

**Files:**
- Modify: `gridtrade/config.py:156-165`
- Test: `tests/test_config.py`、`tests/core/test_tier_policy.py`（如有引用旧符号则对齐）

- [ ] **Step 1: 写失败测试（tests/test_config.py 追加）**

```python
def test_tier0_binance_usdt_symbols():
    from gridtrade.config import DEFAULT_TIER_POLICY
    t0 = DEFAULT_TIER_POLICY.tier0
    assert 'BTC/USDT:USDT' in t0 and 'NEIRO/USDT:USDT' in t0
    assert all(s.endswith('/USDT:USDT') for s in t0)     # 无 USDC 残留
    assert len(t0) == 9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

- [ ] **Step 3: 实现（config.py DEFAULT_TIER_POLICY 替换）**

```python
DEFAULT_TIER_POLICY = TierPolicy(
    # 币安迁移映射(2026-07-14 fapi 实查,spec §5.5)：HL 9 币直译,KNEIRO(k 前缀千倍币)→
    # 币安 NEIRO(NEIROUSDT TRADING)；VINE 为 SETTLING(退市中)留名单无害(黑名单 fail-safe)。
    tier0=('BTC/USDT:USDT', 'ETH/USDT:USDT', 'VINE/USDT:USDT', 'NEO/USDT:USDT',
           'PEOPLE/USDT:USDT', 'NEIRO/USDT:USDT', 'MOODENG/USDT:USDT',
           'FARTCOIN/USDT:USDT', 'CFX/USDT:USDT'),
    # legacy black_dict["0"] 其余 16 币未上币安永续，不猜译名，上市巡检再补。
    tier1=(),
    tier2_cap=2,   # 同币开仓上限(2026-07-12 用户定)
)
```

- [ ] **Step 4: 回归**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/core/test_tier_policy.py -q`
预期：PASS（tier_policy 测试若用自造名单则天然无关；若断言默认名单内容，对齐新符号）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/config.py tests/test_config.py
git commit -m "feat(config): tier0 硬禁名单迁移币安 USDT 后缀——KNEIRO→NEIRO,fapi 实查映射(spec 2026-07-14 §5.5)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: vision.py 核心——URL/解析/校验/目录列举（spec §6.1 前半）

**Files:**
- Create: `gridtrade/backtest/vision.py`
- Test: `tests/backtest/test_vision.py`

**Interfaces:**
- Produces（Task 11-13、Task 13 回测入口消费，签名固定）:
  - `canonical_of(native: str, quote='USDT') -> Optional[str]`；`native_of(symbol: str) -> str`
  - `month_list(start_ms, end_ms) -> List['YYYY-MM']`
  - `kline_month_url(native, tf, month) / kline_day_url(native, tf, day) / funding_month_url(native, month) -> str`
  - `parse_kline_zip(data: bytes, symbol) -> DataFrame[CANDLE_COLS]`；`parse_funding_zip(data: bytes, symbol) -> DataFrame[FUNDING_COLS]`
  - `verify_checksum(data: bytes, checksum_text: str) -> bool`
  - `list_archive_symbols(quote='USDT', *, session=None) -> List[canonical]`（marker 翻页）
  - `list_available_months(native, kind, tf=None, *, session=None) -> Optional[set]`（kind∈{'klines','fundingRate'}；失败返回 None）
  - `default_cache_root() -> str`（`BT_DATA_DIR` env 或 `<repo>/data/binance`）
- Consumes: `gridtrade.exchanges.base.CANDLE_COLS/FUNDING_COLS`、requests。

- [ ] **Step 1: 写失败测试（新建 `tests/backtest/test_vision.py`）**

```python
"""vision 归档装载层单测——全离线：zip 在内存现造，HTTP 经注入桩。"""
import hashlib
import io
import zipfile

import pandas as pd


def _zip_bytes(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(name, text)
    return buf.getvalue()


KLINE_NOHDR = ("1577836800000,1.0,2.0,0.5,1.5,10.0,1577836859999,13.7,5,4.0,5.5,0\n"
               "1577836860000,1.5,2.5,1.0,2.0,20.0,1577836919999,36.2,8,9.0,16.3,0\n")
KLINE_HDR = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
             "count,taker_buy_volume,taker_buy_quote_volume,ignore\n" + KLINE_NOHDR)
FUNDING_CSV = ("calc_time,funding_interval_hours,last_funding_rate\n"
               "1577836800000,8,-0.00012359\n"
               "1577865600000,8,0.00030000\n")


def test_symbol_mapping_roundtrip():
    from gridtrade.backtest import vision as V
    assert V.canonical_of('BTCUSDT') == 'BTC/USDT:USDT'
    assert V.canonical_of('1000BONKUSDC') is None          # 非本 quote → None
    assert V.canonical_of('BTCUSDT', quote='USDT') == V.canonical_of('BTCUSDT')
    assert V.native_of('BTC/USDT:USDT') == 'BTCUSDT'


def test_month_list_and_urls():
    from gridtrade.backtest import vision as V
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    assert V.month_list(ms('2019-12-15'), ms('2020-02-01')) == \
        ['2019-12', '2020-01', '2020-02']
    assert V.kline_month_url('BTCUSDT', '1m', '2020-01') == \
        ('https://data.binance.vision/data/futures/um/monthly/klines/'
         'BTCUSDT/1m/BTCUSDT-1m-2020-01.zip')
    assert V.kline_day_url('BTCUSDT', '1h', '2020-01-02') == \
        ('https://data.binance.vision/data/futures/um/daily/klines/'
         'BTCUSDT/1h/BTCUSDT-1h-2020-01-02.zip')
    assert V.funding_month_url('BTCUSDT', '2020-01') == \
        ('https://data.binance.vision/data/futures/um/monthly/fundingRate/'
         'BTCUSDT/BTCUSDT-fundingRate-2020-01.zip')


def test_parse_kline_zip_with_and_without_header():
    from gridtrade.backtest import vision as V
    from gridtrade.exchanges.base import CANDLE_COLS
    for text in (KLINE_NOHDR, KLINE_HDR):
        df = V.parse_kline_zip(_zip_bytes('x.csv', text), 'BTC/USDT:USDT')
        assert list(df.columns) == CANDLE_COLS
        assert df['quote_volume'].tolist() == [13.7, 36.2]   # 真实报价成交额
        assert df['volCcy'].tolist() == [10.0, 20.0]
        assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2020-01-01 00:00:00')


def test_parse_kline_zip_microsecond_defense():
    from gridtrade.backtest import vision as V
    text = "1577836800000000,1.0,2.0,0.5,1.5,10.0,1577836859999999,13.7,5,4.0,5.5,0\n"
    df = V.parse_kline_zip(_zip_bytes('x.csv', text), 'BTC/USDT:USDT')
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2020-01-01 00:00:00')


def test_parse_funding_zip():
    from gridtrade.backtest import vision as V
    from gridtrade.exchanges.base import FUNDING_COLS
    df = V.parse_funding_zip(_zip_bytes('f.csv', FUNDING_CSV), 'BTC/USDT:USDT')
    assert list(df.columns) == FUNDING_COLS
    assert df['fundingRate'].tolist() == [-0.00012359, 0.0003]
    assert df['realizedRate'].tolist() == df['fundingRate'].tolist()
    assert df['ts'].tolist() == [1577836800000, 1577865600000]


def test_verify_checksum():
    from gridtrade.backtest import vision as V
    data = b'hello'
    good = hashlib.sha256(data).hexdigest() + '  file.zip'
    assert V.verify_checksum(data, good)
    assert not V.verify_checksum(data, 'deadbeef  file.zip')


class _FakeResp:
    def __init__(self, status, content=b''):
        self.status_code = status
        self.content = content


class _FakeSession:
    """按 URL 查表的 requests.Session 桩。"""
    def __init__(self, table):
        self.table = table
        self.calls = []
    def get(self, url, timeout=None):
        self.calls.append(url)
        v = self.table.get(url)
        if v is None:
            return _FakeResp(404)
        return _FakeResp(200, v)


LIST_XML_P1 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>true</IsTruncated>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/BTCUSDT/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/1000BONKUSDC/</Prefix></CommonPrefixes>
</ListBucketResult>"""
LIST_XML_P2 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>false</IsTruncated>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/ETHUSDT/</Prefix></CommonPrefixes>
</ListBucketResult>"""


def test_list_archive_symbols_paginates_and_filters():
    from gridtrade.backtest import vision as V
    base = V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/klines/'
    sess = _FakeSession({
        base: LIST_XML_P1.encode(),
        base + '&marker=data/futures/um/monthly/klines/1000BONKUSDC/':
            LIST_XML_P2.encode(),
    })
    syms = V.list_archive_symbols(session=sess)
    assert syms == ['BTC/USDT:USDT', 'ETH/USDT:USDT']   # USDC 目录被 quote 过滤


MONTHS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>false</IsTruncated>
<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-01.zip</Key></Contents>
<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-01.zip.CHECKSUM</Key></Contents>
<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-02.zip</Key></Contents>
</ListBucketResult>"""


def test_list_available_months():
    from gridtrade.backtest import vision as V
    url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/klines/'
           'BTCUSDT/1m/')
    sess = _FakeSession({url: MONTHS_XML.encode()})
    assert V.list_available_months('BTCUSDT', 'klines', tf='1m',
                                   session=sess) == {'2020-01', '2020-02'}
    assert V.list_available_months('BTCUSDT', 'klines', tf='1m',
                                   session=_FakeSession({})) is None
```

（注意 `_FakeSession` 表键须与实现拼 URL 逐字符一致——实现时以本测试为准拼接。404 桩：`list_available_months` 对 404 返回 None。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/backtest/test_vision.py -q`
预期：FAIL `No module named`。

- [ ] **Step 3: 实现 `gridtrade/backtest/vision.py`（第一部分）**

```python
"""data.binance.vision 官方归档 → ParquetCache 装载层（替代 Reservoir）。
spec: docs/superpowers/specs/2026-07-14-binance-migration-design.md §6.1

归档结构（2026-07-14 实测）：
  月度K线  {BASE_URL}/data/futures/um/monthly/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM}.zip
  日度K线  {BASE_URL}/data/futures/um/daily/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM-DD}.zip
  月度资金费 {BASE_URL}/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip
  每个 zip 配 .CHECKSUM("{sha256}  {filename}")；kline CSV 12 列（老文件无表头/新文件带）；
  fundingRate CSV 带表头 calc_time,funding_interval_hours,last_funding_rate；时间戳 ms
  （防御：>1e14 视为 µs）。目录列举走 S3 XML（delimiter/prefix/marker 翻页），
  含**已退市合约**——全历史选币回放无幸存者偏差。免费无鉴权。
"""
import hashlib
import io
import os
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS

BASE_URL = 'https://data.binance.vision'
LIST_URL = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
_S3NS = '{http://s3.amazonaws.com/doc/2006-03-01/}'


def default_cache_root():
    """回测缓存根目录：BT_DATA_DIR env 覆写，默认 <repo>/data/binance。"""
    base = os.environ.get('BT_DATA_DIR')
    if base:
        return base
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', '..', 'data', 'binance')


def canonical_of(native, quote='USDT'):
    """'BTCUSDT' → 'BTC/USDT:USDT'；非本 quote 后缀 → None。"""
    if not native or not native.endswith(quote):
        return None
    base = native[:-len(quote)]
    if not base:
        return None
    return '%s/%s:%s' % (base, quote, quote)


def native_of(symbol):
    """'BTC/USDT:USDT' → 'BTCUSDT'。"""
    base, rest = symbol.split('/', 1)
    quote = rest.split(':')[0]
    return base + quote


def month_list(start_ms, end_ms):
    s = pd.to_datetime(start_ms, unit='ms').strftime('%Y-%m')
    e = pd.to_datetime(end_ms, unit='ms').strftime('%Y-%m')
    return [d.strftime('%Y-%m')
            for d in pd.date_range(s + '-01', e + '-01', freq='MS')]


def kline_month_url(native, tf, month):
    return ('%s/data/futures/um/monthly/klines/%s/%s/%s-%s-%s.zip'
            % (BASE_URL, native, tf, native, tf, month))


def kline_day_url(native, tf, day):
    return ('%s/data/futures/um/daily/klines/%s/%s/%s-%s-%s.zip'
            % (BASE_URL, native, tf, native, tf, day))


def funding_month_url(native, month):
    return ('%s/data/futures/um/monthly/fundingRate/%s/%s-fundingRate-%s.zip'
            % (BASE_URL, native, native, month))


def _read_zip_csv(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.read(z.namelist()[0]).decode('utf-8')


def parse_kline_zip(data, symbol):
    """归档 kline zip → CANDLE_COLS df（真实 quote_volume 直取，spec §5.4）。"""
    lines = _read_zip_csv(data).splitlines()
    if lines and lines[0].startswith('open_time'):
        lines = lines[1:]
    rows = [l.split(',') for l in lines if l]
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLS)
    df = pd.DataFrame(rows, columns=[
        'ts', 'open', 'high', 'low', 'close', 'vol', 'close_time',
        'quote_volume', 'count', 'tbv', 'tbqv', 'ignore'])
    df['ts'] = df['ts'].astype('int64')
    if len(df) and int(df['ts'].iloc[0]) > 10 ** 14:   # 2025+ 个别归档升微秒
        df['ts'] = df['ts'] // 1000
    for c in ('open', 'high', 'low', 'close', 'vol', 'quote_volume'):
        df[c] = df[c].astype(float)
    df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
    df['symbol'] = symbol
    df['volCcy'] = df['vol']
    return df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)


def parse_funding_zip(data, symbol):
    lines = _read_zip_csv(data).splitlines()
    if lines and lines[0].startswith('calc_time'):
        lines = lines[1:]
    rows = [l.split(',') for l in lines if l]
    if not rows:
        return pd.DataFrame(columns=FUNDING_COLS)
    df = pd.DataFrame([{'ts': int(float(r[0])), 'symbol': symbol,
                        'fundingRate': float(r[2]), 'realizedRate': float(r[2])}
                       for r in rows])
    return df[FUNDING_COLS].sort_values('ts').reset_index(drop=True)


def verify_checksum(data, checksum_text):
    want = (checksum_text or '').strip().split()[0].lower()
    return hashlib.sha256(data).hexdigest() == want


def _get(url, session, *, tries=3, timeout=60):
    """GET → bytes；404/耗尽 → None（调用方按'未发布'处理，不落哨兵）。"""
    import time as _t
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
        except Exception:
            _t.sleep(1.0 + i)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.content
        _t.sleep(1.0 + i)
    return None


def _list_page(session, prefix, marker=None):
    url = LIST_URL + '?delimiter=/&prefix=' + prefix
    if marker:
        url += '&marker=' + marker
    data = _get(url, session)
    if data is None:
        return None
    return ET.fromstring(data.decode('utf-8'))


def list_archive_symbols(quote='USDT', *, session=None):
    """归档目录全量合约（含退市）→ canonical 列表。marker 翻页（MaxKeys 1000）。"""
    session = session or _default_session()
    prefix = 'data/futures/um/monthly/klines/'
    out, marker = [], None
    while True:
        root = _list_page(session, prefix, marker)
        if root is None:
            raise RuntimeError('data.binance.vision 目录列举失败: %s' % prefix)
        prefixes = [p.find(_S3NS + 'Prefix').text
                    for p in root.findall(_S3NS + 'CommonPrefixes')]
        for p in prefixes:
            native = p[len(prefix):].strip('/')
            sym = canonical_of(native, quote)
            if sym:
                out.append(sym)
        trunc = (root.findtext(_S3NS + 'IsTruncated') or 'false') == 'true'
        if not trunc or not prefixes:
            break
        marker = prefixes[-1]
    return sorted(set(out))


def list_available_months(native, kind, tf=None, *, session=None):
    """该合约归档已发布的月份集合（'YYYY-MM'）；列举失败 → None（调用方逐月盲试）。
    kind: 'klines'（需 tf）/ 'fundingRate'。"""
    session = session or _default_session()
    if kind == 'klines':
        prefix = 'data/futures/um/monthly/klines/%s/%s/' % (native, tf)
    else:
        prefix = 'data/futures/um/monthly/fundingRate/%s/' % native
    months, marker = set(), None
    while True:
        root = _list_page(session, prefix, marker)
        if root is None:
            return None
        keys = [c.findtext(_S3NS + 'Key') or ''
                for c in root.findall(_S3NS + 'Contents')]
        for k in keys:
            if k.endswith('.zip'):
                months.add(k[-11:-4])          # ...-YYYY-MM.zip → 'YYYY-MM'
        trunc = (root.findtext(_S3NS + 'IsTruncated') or 'false') == 'true'
        if not trunc or not keys:
            break
        marker = keys[-1]
    return months


def _default_session():
    import requests
    return requests.Session()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/backtest/test_vision.py -q`
预期：全 PASS。测试中 `_FakeSession` 表键若与实现拼 URL 不一致，改**实现**对齐测试的 URL 形状。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/backtest/vision.py tests/backtest/test_vision.py
git commit -m "feat(backtest): vision 归档装载层核心——URL/12列CSV双格式解析/CHECKSUM/S3目录翻页列举(spec 2026-07-14 §6.1)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: `warm_vision`——按天写 ParquetCache（幂等/空哨兵/日度回退/线程池）（spec §6.1 后半）

**Files:**
- Modify: `gridtrade/backtest/vision.py`
- Test: `tests/backtest/test_vision.py`

**Interfaces:**
- Produces: `warm_vision(cache, universe, start_ms, end_ms, *, timeframes=('1m',), quote='USDT', workers=None, session=None, log=print) -> stats`。`timeframes` 元素 ∈ {'1m','1h','funding'}（即 cache namespace）。stats 形如 `{'1m': {'rows': int, 'files': int}, 'skipped_cached': int, 'retry_later': int, 'empty_days': int}`。
- Consumes: Task 10 全部纯函数；`ParquetCache.exists/write/write_empty`。

**语义（与 reservoir 同幂等契约）**：
- 窗口内、该月、**已过完(UTC)的天** 全命中缓存 → 该月 skipped_cached。
- 月度 zip 未发布：kline 走日度回退（逐缺失天）；funding 无日度 → retry_later（尾部交 API 补，见 Task 13）。
- 归档月份列表可得且该月 < 首个可用月 → 上市前，真·无数据 → 落空哨兵（不再反复重试）。
- 下载失败/404 → retry_later，**不落哨兵**。
- CHECKSUM 可得则校验，不符 → retry_later；CHECKSUM 拉不到 → 放行（尽力校验）。
- 今天/未过完的天恒不写。

- [ ] **Step 1: 写失败测试（test_vision.py 追加）**

```python
def _cache(tmp_path):
    from gridtrade.backtest.cache import ParquetCache
    return ParquetCache(str(tmp_path))


def _month_zip_urls(native, tf, month):
    from gridtrade.backtest import vision as V
    u = V.kline_month_url(native, tf, month)
    return u, u + '.CHECKSUM'


def test_warm_vision_writes_days_and_idempotent(tmp_path):
    from gridtrade.backtest import vision as V
    # 2020-01 两根 1m（1/1 与 1/2 各一根）→ 两天各 1 行，其余窗口天=空哨兵
    csv = ("1577836800000,1.0,2.0,0.5,1.5,10.0,1577836859999,13.7,5,4.0,5.5,0\n"
           "1577923200000,1.5,2.5,1.0,2.0,20.0,1577923259999,36.2,8,9.0,16.3,0\n")
    data = _zip_bytes('x.csv', csv)
    import hashlib as _h
    u, cs_u = _month_zip_urls('BTCUSDT', '1m', '2020-01')
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'klines/BTCUSDT/1m/')
    sess = _FakeSession({
        u: data,
        cs_u: (_h.sha256(data).hexdigest() + '  x.zip').encode(),
        months_url: MONTHS_XML.encode(),          # 可用月 {2020-01, 2020-02}
    })
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-03'),
                       timeframes=('1m',), workers=1, session=sess)
    assert st['1m']['rows'] == 2 and st['1m']['files'] == 1
    assert cache.read('1m', 'BTC/USDT:USDT', '2020-01-01')['close'].tolist() == [1.5]
    assert cache.read('1m', 'BTC/USDT:USDT', '2020-01-02')['close'].tolist() == [2.0]
    empty = cache.read('1m', 'BTC/USDT:USDT', '2020-01-03')
    assert empty is not None and empty.empty          # 月内无数据天=空哨兵
    # 幂等：第二遍全命中，零下载
    n_calls = len(sess.calls)
    st2 = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-03'),
                        timeframes=('1m',), workers=1, session=sess)
    assert st2['skipped_cached'] == 1 and len(sess.calls) == n_calls


def test_warm_vision_prelisting_month_empty_sentinel(tmp_path):
    from gridtrade.backtest import vision as V
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'klines/BTCUSDT/1m/')
    sess = _FakeSession({months_url: MONTHS_XML.encode()})   # 可用月起点 2020-01
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2019-12-30'), ms('2019-12-31'),
                       timeframes=('1m',), workers=1, session=sess)
    assert st['empty_days'] == 2                       # 上市前=真·无数据
    assert cache.exists('1m', 'BTC/USDT:USDT', '2019-12-30')


def test_warm_vision_missing_month_daily_fallback(tmp_path):
    from gridtrade.backtest import vision as V
    # 月度缺且窗口月 > 首个可用月（近月未发布），走日度：1/1 有文件、1/2 404 → retry_later
    csv = "1577836800000,1.0,2.0,0.5,1.5,10.0,1577836859999,13.7,5,4.0,5.5,0\n"
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'klines/BTCUSDT/1m/')
    # 可用月={'2019-12'}：目标月 2020-01 不在其中、且不小于 min(avail) → 日度回退分支
    xml_only_dec = MONTHS_XML.replace('2020-01', '2019-12').replace('2020-02', '2019-12')
    sess = _FakeSession({
        months_url: xml_only_dec.encode(),
        V.kline_day_url('BTCUSDT', '1m', '2020-01-01'): _zip_bytes('d.csv', csv),
    })
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-02'),
                       timeframes=('1m',), workers=1, session=sess)
    assert st['1m']['rows'] == 1
    assert st['retry_later'] == 1                      # 1/2 未发布，下次重取
    assert not cache.exists('1m', 'BTC/USDT:USDT', '2020-01-02')


def test_warm_vision_funding_namespace(tmp_path):
    from gridtrade.backtest import vision as V
    data = _zip_bytes('f.csv', FUNDING_CSV)
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'fundingRate/BTCUSDT/')
    xml = MONTHS_XML.replace('klines/BTCUSDT/1m/BTCUSDT-1m', 'fundingRate/BTCUSDT/BTCUSDT-fundingRate')
    sess = _FakeSession({
        months_url: xml.encode(),
        V.funding_month_url('BTCUSDT', '2020-01'): data,
        V.funding_month_url('BTCUSDT', '2020-01') + '.CHECKSUM': None and b'',
    })
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-01'),
                       timeframes=('funding',), workers=1, session=sess)
    # 两条记录 ts=00:00 与 08:00 同属 2020-01-01（8h 资金费一天多条）
    assert st['funding']['rows'] == 2
    got = cache.read('funding', 'BTC/USDT:USDT', '2020-01-01')
    assert got['fundingRate'].tolist() == [-0.00012359, 0.0003]
```

（`test_warm_vision_funding_namespace` 中 CHECKSUM 表项值为 `None and b''` 即 None——`_FakeSession` 返回 404，实现须放行"CHECKSUM 拉不到"。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/backtest/test_vision.py -q`
预期：新 4 用例 FAIL（`warm_vision` 不存在）。

- [ ] **Step 3: 实现（vision.py 追加）**

```python
def _month_days(month, start_ms, end_ms, now_ms):
    """该月 ∩ 窗口、且已过完(UTC)的天列表（'YYYY-MM-DD'）。"""
    m0 = pd.Timestamp(month + '-01')
    m1 = m0 + pd.offsets.MonthBegin(1)
    lo = max(m0, pd.to_datetime(start_ms, unit='ms').normalize())
    hi = min(m1 - pd.Timedelta(days=1),
             pd.to_datetime(end_ms, unit='ms').normalize())
    if lo > hi:
        return []
    days = [d.strftime('%Y-%m-%d') for d in pd.date_range(lo, hi, freq='D')]
    return [d for d in days
            if int((pd.Timestamp(d) + pd.Timedelta(days=1)).value // 1_000_000)
            <= now_ms]


def _day_bounds_ms(day):
    d0 = pd.Timestamp(day)
    return (int(d0.value // 1_000_000),
            int((d0 + pd.Timedelta(days=1)).value // 1_000_000) - 1)


def _write_day(cache, ns, sym, day, df, cols, time_col, st):
    d_lo, d_hi = _day_bounds_ms(day)
    if df.empty:
        cache.write_empty(ns, sym, day, cols)
        st['empty_days'] += 1
        return
    ms = (df[time_col].astype('int64') if time_col == 'ts'
          else df[time_col].view('int64') // 1_000_000)
    day_df = df[(ms >= d_lo) & (ms <= d_hi)]
    if day_df.empty:
        cache.write_empty(ns, sym, day, cols)
        st['empty_days'] += 1
    else:
        cache.write(ns, sym, day, day_df.reset_index(drop=True))
        st['rows'] += int(len(day_df))


def _fetch_month(native, ns, month, session):
    """月度 zip（含尽力 CHECKSUM 校验）→ bytes / None。"""
    url = (funding_month_url(native, month) if ns == 'funding'
           else kline_month_url(native, ns, month))
    data = _get(url, session)
    if data is None:
        return None
    cs = _get(url + '.CHECKSUM', session)
    if cs is not None and not verify_checksum(data, cs.decode('utf-8', 'ignore')):
        return None
    return data


_UNSET = object()


def _warm_symbol(cache, sym, ns, months, start_ms, end_ms, now_ms, session, log):
    native = native_of(sym)
    kind = 'fundingRate' if ns == 'funding' else 'klines'
    avail = _UNSET   # 惰性加载：整窗全命中缓存时零 HTTP（幂等重跑不浪费 530×ns 次列举）
    parse = parse_funding_zip if ns == 'funding' else parse_kline_zip
    cols = FUNDING_COLS if ns == 'funding' else CANDLE_COLS
    time_col = 'ts' if ns == 'funding' else 'candle_begin_time'
    st = {'rows': 0, 'files': 0, 'skipped_cached': 0, 'retry_later': 0,
          'empty_days': 0}
    for month in months:
        days = _month_days(month, start_ms, end_ms, now_ms)
        if not days:                      # 当月全部天未过完 → 下次重取
            st['retry_later'] += 1
            continue
        missing = [d for d in days if not cache.exists(ns, sym, d)]
        if not missing:
            st['skipped_cached'] += 1
            continue
        if avail is _UNSET:
            avail = list_available_months(native, kind,
                                          tf=None if ns == 'funding' else ns,
                                          session=session)
        if avail is not None and month not in avail:
            if avail and month < min(avail):
                # 上市前月份：真·无数据 → 空哨兵（不再反复重试）
                for d in missing:
                    cache.write_empty(ns, sym, d, cols)
                    st['empty_days'] += 1
                continue
            # 月度未发布（近月）：kline 日度回退；funding 无日度 → 尾部交 API 补
            if ns == 'funding':
                st['retry_later'] += 1
                continue
            for d in missing:
                data = _get(kline_day_url(native, ns, d), session)
                if data is None:
                    st['retry_later'] += 1
                    continue
                st['files'] += 1
                _write_day(cache, ns, sym, d, parse(data, sym), cols, time_col, st)
            continue
        data = _fetch_month(native, ns, month, session)
        if data is None:                  # 404/校验不符 → 不落哨兵，下次重取
            st['retry_later'] += 1
            continue
        st['files'] += 1
        df = parse(data, sym)
        for d in missing:
            _write_day(cache, ns, sym, d, df, cols, time_col, st)
    return st


def warm_vision(cache, universe, start_ms, end_ms, *, timeframes=('1m',),
                quote='USDT', workers=None, session=None, log=print):
    """把窗口内归档数据写入 cache 各命名空间（'1m'/'1h'/'funding'）。幂等：
    整月全命中即跳过；失败/未发布不落哨兵（retry_later）。线程池按 (ns,symbol)
    并行（BT_VISION_WORKERS，默认 8）。返回 stats（形状见测试）。"""
    from concurrent.futures import ThreadPoolExecutor
    sess = session or _default_session()
    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)
    months = month_list(start_ms, end_ms)
    nworkers = int(workers if workers is not None
                   else os.environ.get('BT_VISION_WORKERS', '8'))
    stats = {ns: {'rows': 0, 'files': 0} for ns in timeframes}
    stats.update({'skipped_cached': 0, 'retry_later': 0, 'empty_days': 0})
    units = [(ns, s) for ns in timeframes for s in universe]

    def run(unit):
        ns, s = unit
        return ns, _warm_symbol(cache, s, ns, months, start_ms, end_ms,
                                now_ms, sess, log)

    if nworkers > 1 and len(units) > 1:
        with ThreadPoolExecutor(max_workers=nworkers) as ex:
            results = list(ex.map(run, units))
    else:
        results = [run(u) for u in units]
    done = 0
    for ns, st in results:
        stats[ns]['rows'] += st['rows']
        stats[ns]['files'] += st['files']
        for k in ('skipped_cached', 'retry_later', 'empty_days'):
            stats[k] += st[k]
        done += 1
        if done % 50 == 0:
            log('[vision] %d/%d units done' % (done, len(units)))
    return stats
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/backtest/test_vision.py -q`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/backtest/vision.py tests/backtest/test_vision.py
git commit -m "feat(backtest): warm_vision 归档预热——月度为主/日度回退/上市前空哨兵/失败不落哨兵/线程池,幂等契约同 Reservoir(spec 2026-07-14 §6.1)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: `vision_sync` CLI（spec §6.1）

**Files:**
- Create: `gridtrade/backtest/vision_sync.py`
- Test: `tests/backtest/test_vision_sync.py`

**Interfaces:**
- Produces: `python -m gridtrade.backtest.vision_sync <start> <end> [--tf 1m,1h,funding] [--symbols A/USDT:USDT,B/USDT:USDT] [--quote USDT] [--workers N]`；`main(argv=None)` 可注入 warm/list 函数便于测试。
- Consumes: Task 10/11 `warm_vision`、`list_archive_symbols`、`default_cache_root`。

- [ ] **Step 1: 写失败测试（新建 `tests/backtest/test_vision_sync.py`）**

```python
import pandas as pd


def test_vision_sync_main_wires_args(monkeypatch, tmp_path):
    from gridtrade.backtest import vision_sync as VS
    calls = {}
    monkeypatch.setenv('BT_DATA_DIR', str(tmp_path))
    monkeypatch.setattr('gridtrade.backtest.vision.list_archive_symbols',
                        lambda quote='USDT', **kw: ['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    def fake_warm(cache, universe, start_ms, end_ms, *, timeframes, quote, workers,
                  session=None, log=print):
        calls.update(universe=universe, start_ms=start_ms, end_ms=end_ms,
                     timeframes=timeframes, workers=workers)
        return {'1h': {'rows': 1, 'files': 1}, 'skipped_cached': 0,
                'retry_later': 0, 'empty_days': 0}
    monkeypatch.setattr('gridtrade.backtest.vision.warm_vision', fake_warm)
    VS.main(['2020-01-01', '2020-01-31', '--tf', '1h', '--workers', '2'])
    assert calls['universe'] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    assert calls['timeframes'] == ('1h',)
    assert calls['workers'] == 2
    assert calls['start_ms'] == int(pd.Timestamp('2020-01-01').value // 1_000_000)
    # end 含当天：end_ms = 2020-02-01 00:00 - 1ms
    assert calls['end_ms'] == int(pd.Timestamp('2020-02-01').value // 1_000_000) - 1


def test_vision_sync_symbols_override(monkeypatch, tmp_path):
    from gridtrade.backtest import vision_sync as VS
    monkeypatch.setenv('BT_DATA_DIR', str(tmp_path))
    seen = {}
    monkeypatch.setattr('gridtrade.backtest.vision.warm_vision',
                        lambda cache, universe, s, e, **kw: seen.update(u=universe) or
                        {'1m': {'rows': 0, 'files': 0}, 'skipped_cached': 0,
                         'retry_later': 0, 'empty_days': 0})
    VS.main(['2020-01-01', '2020-01-02', '--symbols', 'DOGE/USDT:USDT'])
    assert seen['u'] == ['DOGE/USDT:USDT']
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/backtest/test_vision_sync.py -q`

- [ ] **Step 3: 实现 `gridtrade/backtest/vision_sync.py`**

```python
"""vision 归档独立预热 CLI（spec 2026-07-14 §6.1）：
  .venv/bin/python -m gridtrade.backtest.vision_sync 2019-09-01 2026-07-13 \
      --tf 1h,1m,funding [--symbols BTC/USDT:USDT,...] [--workers 8]
不传 --symbols 则从归档目录列举全量合约（含退市）。幂等可断点续跑。"""
import argparse

import pandas as pd

from gridtrade.backtest import vision
from gridtrade.backtest.cache import ParquetCache


def main(argv=None):
    ap = argparse.ArgumentParser(description='data.binance.vision 归档预热')
    ap.add_argument('start')                      # YYYY-MM-DD
    ap.add_argument('end')                        # YYYY-MM-DD（含当天）
    ap.add_argument('--tf', default='1m', help='逗号分隔: 1m,1h,funding')
    ap.add_argument('--symbols', default='', help='canonical 逗号分隔；空=归档全量')
    ap.add_argument('--quote', default='USDT')
    ap.add_argument('--workers', type=int, default=None)
    args = ap.parse_args(argv)

    start_ms = int(pd.Timestamp(args.start).value // 1_000_000)
    end_ms = int((pd.Timestamp(args.end) + pd.Timedelta(days=1)).value
                 // 1_000_000) - 1
    tfs = tuple(t.strip() for t in args.tf.split(',') if t.strip())
    if args.symbols.strip():
        universe = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        universe = vision.list_archive_symbols(quote=args.quote)
        print('[vision_sync] 归档全量 %d 合约（含退市）' % len(universe))
    cache = ParquetCache(vision.default_cache_root())
    st = vision.warm_vision(cache, universe, start_ms, end_ms, timeframes=tfs,
                            quote=args.quote, workers=args.workers)
    print('[vision_sync] done:', st)
    return st


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/backtest/test_vision_sync.py -q`

- [ ] **Step 5: Commit**

```bash
git add gridtrade/backtest/vision_sync.py tests/backtest/test_vision_sync.py
git commit -m "feat(backtest): vision_sync 预热 CLI——全量/指定合约按窗口幂等补拉(spec 2026-07-14 §6.1)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: backtest_run 接缝替换——币安数据源 + VIP0 费率（spec §3.3、§6.2）

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`
- Test: `tests/backtest/test_backtest_run.py`（对齐重命名）；离线核心逻辑不动

**Interfaces:**
- Produces: `_binance_datasource_1h(cache) -> (adapter, ds_1h)`；`prewarm_1h`/`prewarm_sim_and_funding` 走 vision+API 尾补；`BT_STRATEGY`/`BT_FACTORS`（原 HL_STRATEGY/HL_FACTORS 更名）；费率默认 maker 0.0002/taker 0.0005。
- Consumes: Task 10-11 `vision.warm_vision/list_archive_symbols/default_cache_root`、Task 2-3 `BinanceAdapter`。

- [ ] **Step 1: 修改（逐处，无新测试先行——本任务是接缝迁移，回归靠既有离线测试 + Step 3 补断言）**

1. 模块 docstring 第 6-11 行改：`prewarm_1h` 描述为 `全市场(−黑名单) 1h 选币 OHLCV(Vision 归档+API 尾补)`；`prewarm_sim_and_funding` 描述为 `仅选中币 持仓K线+funding(Vision 归档+API 尾补)`；CLI 跑法保持。
2. `HL_STRATEGY` → `BT_STRATEGY`、`HL_FACTORS` → `BT_FACTORS`（模块内全部引用同步；注释"HL 回测默认策略"→"回测默认策略"）。
3. **删除** `RESERVOIR_START`、`_API_1H_MAX_DAYS`、`_pick_1h_source`（含 main 中 source 分支与过早窗口 SystemExit 守卫——Vision 覆盖全历史，无拼缝切换）。
4. `_hl_datasource_1h` 整函数替换为：

```python
def _binance_datasource_1h(cache):
    """构造带退避的币安公共适配器 + 1h DataSource（网络；惰性导入；无需 API key）。"""
    import time
    import ccxt
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.exchanges.binance import BinanceAdapter

    class _RetryBinance(BinanceAdapter):
        """对间歇 5xx/网络错误指数退避（预热用；不污染 core/live）。"""
        def _retry(self, fn, *a, **k):
            last = None
            for i in range(12):
                try:
                    return fn(*a, **k)
                except (ccxt.ExchangeNotAvailable, ccxt.NetworkError,
                        ccxt.RequestTimeout) as e:
                    last = e
                    time.sleep(min(2.0 * (i + 1), 8.0))
            raise last

        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            return self._retry(super().fetch_ohlcv, symbol, timeframe,
                               start_ms, end_ms)

        def fetch_funding_history(self, symbol, start_ms, end_ms):
            return self._retry(super().fetch_funding_history, symbol,
                               start_ms, end_ms)

    adapter = _RetryBinance(ccxt.binanceusdm({'enableRateLimit': True,
                                              'timeout': 30000}))
    return adapter, DataSource(adapter, cache, timeframe='1h')
```

（`BT_BUILDER_DEXES` 块一并删除——HL builder-dex 专属。）

5. `prewarm_1h` 替换：

```python
def prewarm_1h(cache, universe, warm_start_ms, end_ms, *, log=print):
    """phase1：全市场 1h 选币 OHLCV——Vision 归档批量 + API 尾补(归档滞后1-2天)。
    返回 adapter（复用于 phase2）。"""
    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest import vision as V
    adapter, ds_1h = _binance_datasource_1h(cache)
    st = V.warm_vision(cache, universe, warm_start_ms, end_ms,
                       timeframes=('1h',), log=log)
    log('[prewarm] 1h@Vision(全市场 %d): %s' % (len(universe), st))
    log('[prewarm] 1h 尾补@API: %s'
        % PW.prewarm_ohlcv(ds_1h, universe, warm_start_ms, end_ms))
    return adapter
```

6. `prewarm_sim_and_funding` 替换：

```python
def prewarm_sim_and_funding(cache, adapter, selected, win_start_ms, end_ms, *,
                            sim_timeframe='1m', log=print):
    """phase2：仅选中币 持仓K线(Vision+API 尾补) + funding(Vision+API 尾补)。
    funding 月度归档无日度文件，当月尾部天然由 API 补（spec §6.2）。"""
    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.datasource import DataSource
    sim_tf = sim_timeframe or '1h'
    if sim_tf != '1h':
        st = V.warm_vision(cache, selected, win_start_ms, end_ms,
                           timeframes=(sim_tf,), log=log)
        log('[prewarm] %s@Vision(选中 %d): %s' % (sim_tf, len(selected), st))
        ds = DataSource(adapter, cache, timeframe=sim_tf)
        api = PW.prewarm_ohlcv(ds, selected, win_start_ms, end_ms)
        log('[prewarm] %s 尾补@API: %s' % (sim_tf, api))
        if selected and st[sim_tf]['rows'] == 0 and st['skipped_cached'] == 0 \
                and api['rows'] == 0:
            raise RuntimeError('%s 数据完全缺失——检查网络/币种/窗口 (retry_later=%d)'
                               % (sim_tf, st['retry_later']))
    fst = V.warm_vision(cache, selected, win_start_ms, end_ms,
                        timeframes=('funding',), log=log)
    log('[prewarm] funding@Vision(选中 %d): %s' % (len(selected), fst))
    ds_1h = DataSource(adapter, cache, timeframe='1h')
    log('[prewarm] funding 尾补@API: %s'
        % PW.prewarm_funding(ds_1h, selected, win_start_ms, end_ms))
```

7. 费率（用户定 2026-07-14，币安 USDT-M VIP0 无折扣）：
   - `simulate_tasks` 签名默认 `fee_rate=0.0002, taker_rate=0.0005`；docstring 改 `对齐币安 USDT-M VIP0 无折扣费率（maker 2bps/taker 5bps，用户定 2026-07-14）`。
   - `run_backtest` 签名默认同改 `fee_rate=0.0002, taker_rate=0.0005`。
   - `_simulate_grid_task` 内 `cfg.get('taker_rate', 0.00045)` → `cfg.get('taker_rate', 0.0005)`。
8. `main()`：
   - `root = os.path.join(...)` 行替换为：

```python
    from gridtrade.backtest import vision as V
    root = V.default_cache_root()
```

   - 票池块（原 `_adapter, _ds1h = _hl_datasource_1h(cache)` 至 1h 预热打印）替换为：

```python
    _adapter, _ds1h = _binance_datasource_1h(cache)
    tiers = _tiers_from_env()
    if tiers is not None and os.environ.get('BT_SYMBOL_LOCK', '').lower() in ('1', 'true', 'on'):
        raise SystemExit('BT_TIER* 与 BT_SYMBOL_LOCK 互斥（两套口径不叠加）')
    bt_blacklist = BT_BLACKLIST
    if tiers is not None:
        from gridtrade.core.tier_policy import effective_blacklist
        bt_blacklist = effective_blacklist(BT_BLACKLIST, tiers)
        print('[BT] tiers 启用: tier0=%d tier1=%d cap=%d' %
              (len(tiers.tier0), len(tiers.tier1), tiers.tier2_cap))
    # 票池=归档全量合约（含退市，无幸存者偏差，spec §6.1）−黑名单
    universe = sorted(set(V.list_archive_symbols()) - set(bt_blacklist))
    print('[BT] 全市场票池 %d 币(归档含退市,−黑名单 %d)' % (len(universe), len(bt_blacklist)))
    st1h = V.warm_vision(cache, universe, _ms(warm_start), _ms(win_end),
                         timeframes=('1h',))
    print('[BT] 1h 预热@Vision: %s' % st1h)
    from gridtrade.backtest import prewarm as PW
    print('[BT] 1h 尾补@API: %s'
          % PW.prewarm_ohlcv(_ds1h, universe, _ms(warm_start), _ms(win_end)))
```

   - 其后 `HL_STRATEGY`/`HL_FACTORS` 引用改 `BT_STRATEGY`/`BT_FACTORS`；`tag = '@Reservoir+funding'` → `tag = '@Vision+funding'`。
   - `from gridtrade.backtest.prewarm import resolve_universe` import 删除（main 不再用；`prewarm.resolve_universe` 本体保留，测试仍用）。

- [ ] **Step 2: 全量回归 + 对齐引用**

Run: `.venv/bin/python -m pytest tests/backtest/ -q`
预期：`test_backtest_run.py` 等离线用例 PASS（它们直接喂 cache，不触预热路径）。若有测试引用被删符号（`_pick_1h_source`/`RESERVOIR_START`/`_hl_datasource_1h`），该用例功能已不存在——删除对应用例；引用 `HL_STRATEGY` 的改 `BT_STRATEGY`。

- [ ] **Step 3: 补一条守卫测试（tests/backtest/test_backtest_run.py 追加）**

```python
def test_default_fee_rates_binance_vip0():
    import inspect
    from gridtrade.backtest.backtest_run import simulate_tasks, run_backtest
    for fn in (simulate_tasks, run_backtest):
        sig = inspect.signature(fn)
        assert sig.parameters['fee_rate'].default == 0.0002
        assert sig.parameters['taker_rate'].default == 0.0005
```

Run: `.venv/bin/python -m pytest tests/backtest/test_backtest_run.py -q` → PASS。

- [ ] **Step 4: Commit**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/
git commit -m "feat(backtest): 回测数据层切币安——Vision 归档+API 尾补/归档票池含退市/VIP0 费率默认/删 Reservoir 分支(spec 2026-07-14 §3.3,§6.2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: 删除 HL/OKX/Reservoir 及其测试 + dbadmin validate1m + resilience 418 守卫（spec §3.3）

**Files:**
- Delete: `gridtrade/exchanges/hyperliquid.py`、`gridtrade/exchanges/okx.py`、`gridtrade/backtest/reservoir.py`
- Delete: `tests/exchanges/test_hl_account_batch.py`、`test_hl_builder_dex.py`、`test_hl_cancel_all.py`、`test_hl_market_order.py`、`test_hl_symbol_none.py`、`test_hl_testnet.py`、`test_hyperliquid_adapter.py`、`test_okx_adapter.py`；`tests/backtest/test_reservoir.py`、`test_reservoir_dex.py`、`test_reservoir_selfheal.py`、`test_validate_1m.py`
- Modify: `gridtrade/runtime/dbadmin.py`（移除 validate1m 子命令及 reservoir import）+ Delete: `tests/runtime/test_dbadmin_validate1m.py`
- Modify: `tests/exchanges/test_encode_cloid.py`、`test_account_batch_base.py`、`test_funding_payments.py`、`test_balance_currency.py`、`test_ccxt_smoke.py`（若 import HL/OKX——先 grep，把用 HL/OKX 作被测对象的用例改用 `BinanceAdapter`/`CcxtAdapter` 等价重写，纯 HL 语义用例删除）

- [ ] **Step 1: 摸清引用面**

Run: `grep -rln "hyperliquid\|okx\|reservoir\|HyperliquidAdapter\|OkxAdapter" gridtrade/ tests/ scripts/ --include="*.py"`
逐文件归类：删除 / 改写 / 留待 Task 15（scripts、注释）。

- [ ] **Step 2: 删除与改写**

```bash
git rm gridtrade/exchanges/hyperliquid.py gridtrade/exchanges/okx.py gridtrade/backtest/reservoir.py
git rm tests/exchanges/test_hl_account_batch.py tests/exchanges/test_hl_builder_dex.py \
       tests/exchanges/test_hl_cancel_all.py tests/exchanges/test_hl_market_order.py \
       tests/exchanges/test_hl_symbol_none.py tests/exchanges/test_hl_testnet.py \
       tests/exchanges/test_hyperliquid_adapter.py tests/exchanges/test_okx_adapter.py \
       tests/backtest/test_reservoir.py tests/backtest/test_reservoir_dex.py \
       tests/backtest/test_reservoir_selfheal.py tests/backtest/test_validate_1m.py \
       tests/runtime/test_dbadmin_validate1m.py
```

`dbadmin.py`：删除 validate1m 子命令分支、其 help 文案与 `from gridtrade.backtest.reservoir import ...` 惰性导入（该命令是 HL 1m 缓存自愈工具，Vision 归档数据完整且有 CHECKSUM，不再需要）。
其余被改写测试：被测对象换 `BinanceAdapter`（如 encode_cloid 用例——HL 返回 None 的断言改为币安直传断言，若与 Task 2 用例重复则删本文件保留 test_binance_adapter.py 版本）。
`ccxt_adapter.py` 中提及 HL/OKX 的注释改为一般化表述（如 fetch_funding_payments 的 HL 例外注记改为"按 symbol 正确打标的交易所走本通用路径"）。

- [ ] **Step 3: resilience 418 守卫测试（tests/exchanges/test_resilience.py 追加；无生产代码改动——ccxt 已把 429→RateLimitExceeded/418→DDoSProtection，classify_error 均归 rate_limit）**

```python
def test_binance_418_ban_classified_rate_limit():
    # 币安 418(IP ban)/-1003 → ccxt DDoSProtection/RateLimitExceeded → rate_limit
    # （退避用 rate_limit_base_delay 长冷却，spec 2026-07-14 §九）
    import ccxt
    from gridtrade.exchanges.resilience import classify_error
    assert classify_error(ccxt.DDoSProtection('418 I am a teapot')) == 'rate_limit'
    assert classify_error(ccxt.RateLimitExceeded('-1003 TOO_MANY_REQUESTS')) == 'rate_limit'
```

- [ ] **Step 4: 全量回归**

Run: `.venv/bin/python -m pytest -q`
预期：全 PASS。`grep -rn "import.*hyperliquid\|import.*okx\|import.*reservoir" gridtrade/ tests/` → 零命中。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat!: 删除 Hyperliquid/OKX 适配器与 Reservoir 数据层——币安唯一对接,dbadmin validate1m 随退(spec 2026-07-14 §3.3)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: env/deploy/docs/scripts 清理（spec §7.1）

**Files:**
- Modify: `.env.example`、`deploy/fly.toml`、`deploy/fly.prod.toml`、`docs/回测使用文档.md`
- Delete: `scripts/validate_hl.py`；Create: `scripts/validate_binance.py`
- Modify: `scripts/testnet_status.py`（HL 措辞/env → 币安）

- [ ] **Step 1: `.env.example` 整文件替换**

```bash
# GridTradeBi 部署配置（复制本文件为 .env 并填入真实值）
# .env 含敏感信息，已被 .gitignore 忽略，请勿提交到仓库。
# 变量与默认值见 gridtrade/config.py::load_deploy_config；非敏感项也在 deploy/fly*.toml [env]，
# 敏感项（BINANCE_API_KEY / BINANCE_API_SECRET / DATABASE_URL）线上走 `fly secrets set`。

# ---- 凭证（敏感，必填）----
# 币安 API Key（只开合约交易权限、禁提现；spec 2026-07-14 §7.3）
BINANCE_API_KEY=
# 币安 API Secret
BINANCE_API_SECRET=
# 数据库连接串，例：postgresql+psycopg2://user:pass@host:5432/dbname
DATABASE_URL=

# ---- 交易所 / 网络 ----
# 交易所标识（当前仅 binance；fake 供离线测试）
EXCHANGE=binance
# 是否币安期货测试网：true=testnet.binancefuture.com，false=主网
BINANCE_TESTNET=false
# 计价/结算币覆写（可选）；留空=USDT（USDT-M）。仅切 USDC-M 时显式设 USDC。
QUOTE_CURRENCY=

# ---- 资金 / 风控 ----
CAP=100
DEFAULT_CAP=
TOTAL_BUDGET=1000000
MAX_CONCURRENT=12
# 单笔挂单名义额全局下限：与按币 MIN_NOTIONAL(Instrument.min_cost,BTC=50/ETH=20/多数=5)
# 取 max 生效（spec 2026-07-14 §5.3）
MIN_ORDER_NOTIONAL=5

# ---- 运行 / 调度 ----
MONITOR_INTERVAL_SEC=5
SCHEDULER_PERIOD=12H
DISPLAY_TZ=UTC
SCHEDULER_RUN_ON_START=false

# ---- 灾难止损保险丝（币安 STOP_MARKET reduce-only 触发市价单）----
# 注意：币安触发后纯市价，无滑点底线参数——STOP_SLIPPAGE 在币安上被忽略（spec §5.2），
# 软止损仍是主刹车。
STOP_ORDERS_ENABLED=true
STOP_SLIPPAGE=0.15

# ---- 标的过滤（逗号分隔，可留空）----
BLACKLIST_SYMBOLS=
UNIVERSE_WHITELIST=

# ---- 回测 ----
# 回测缓存根目录覆写（默认 <repo>/data/binance）
# BT_DATA_DIR=
# vision 归档并行下载线程数（默认 8）
# BT_VISION_WORKERS=8

# ---- 测试 ----
TEST_DATABASE_URL=
```

- [ ] **Step 2: fly toml 两文件 [env] 对齐**

`deploy/fly.toml`（testnet）：头注释改 `GridTradeBi（Binance USDT-M testnet）`；`EXCHANGE = "binance"`；`HL_TESTNET` 行换 `BINANCE_TESTNET = "true"`；secrets 注释换 `BINANCE_API_KEY / BINANCE_API_SECRET / DATABASE_URL`；`MIN_ORDER_NOTIONAL = "5"` 并改注释 `全局下限,与按币 MIN_NOTIONAL 取 max（spec 2026-07-14 §5.3）`；区域注释改 `nrt（东京）：币安 API 亚太直连无地域封锁`。
`deploy/fly.prod.toml`（mainnet）同改：`EXCHANGE = "binance"`、`BINANCE_TESTNET = "false"`、secrets 注释、`MIN_ORDER_NOTIONAL = "5"`。app 名（gridtrade-hl/gridtrade-prod）**不改**——改名等于换 fly app，属基础设施操作，切换 runbook 里说明。

- [ ] **Step 3: `docs/回测使用文档.md` 更新**

先读全文，把 Reservoir/AWS 凭证/hl_validate 相关段替换为 Vision 用法：数据源说明（官方免费归档、含退市、CHECKSUM）、缓存目录 `data/binance`（`BT_DATA_DIR` 覆写）、预热 CLI `vision_sync` 示例、`BT_VISION_WORKERS`、费率默认 VIP0（0.0002/0.0005）注记、全历史窗口示例 `python -m gridtrade.backtest.backtest_run 2020-01-01 2026-06-30 1m`。

- [ ] **Step 4: scripts**

`git rm scripts/validate_hl.py`；新建 `scripts/validate_binance.py`：

```python
"""真实币安端到端验证（联网、非 pytest）：小窗口 Vision 预热 + 离线回测。
跑：TZ=Asia/Shanghai .venv/bin/python scripts/validate_binance.py
证明同一份回测代码在币安数据上可拉数回测（验收②的最小前哨）。"""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gridtrade.backtest import backtest_run as BR
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

SYMS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'DOGE/USDT:USDT']


def main():
    end = pd.Timestamp.utcnow().normalize().tz_localize(None) - pd.Timedelta(days=2)
    start = end - pd.Timedelta(days=21)
    warm = start - pd.Timedelta(days=14)
    ms = lambda t: int(t.value // 1_000_000)
    cache = ParquetCache(V.default_cache_root())
    t0 = time.time()
    print('[validate] 1h+1m+funding 预热 %s -> %s' % (warm.date(), end.date()))
    print(V.warm_vision(cache, SYMS, ms(warm), ms(end),
                        timeframes=('1h', '1m', 'funding')))
    df = BR.run_backtest(cache, SYMS, start, end, BR.BT_STRATEGY, BR.BT_FACTORS,
                         timeframe='1h', sim_timeframe='1m', workers=2)
    print('[validate] %.1fs, %d grids' % (time.time() - t0, len(df)))
    for k, v in BR.summarize(df).items():
        print('  %s: %s' % (k, v))


if __name__ == '__main__':
    main()
```

`scripts/testnet_status.py`：docstring/输出里 "HL testnet" 措辞改 "币安 testnet"；若直接读 `HL_*` env 或 import HL 适配器，改走 `build_runtime(load_deploy_config())` 的 adapter（grep 后等价替换）。`scripts/testnet_status.sh` 同步措辞。

- [ ] **Step 5: 回归 + Commit**

Run: `.venv/bin/python -m pytest -q` → 全 PASS。

```bash
git add -A
git commit -m "chore(deploy,docs,scripts): env/fly/回测文档/验证脚本切币安——BINANCE_* 键,validate_binance,MIN_ORDER_NOTIONAL 语义更新(spec 2026-07-14 §7.1)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: testnet 冒烟脚本 + 切换 runbook（spec §7.2、§八、§5.1 实测）

**Files:**
- Create: `scripts/binance_testnet_smoke.py`
- Create: `docs/币安切换runbook.md`

- [ ] **Step 1: 冒烟脚本（联网、需 testnet key，非 pytest）**

```python
"""币安期货 testnet 端到端冒烟（联网、非 pytest；spec 2026-07-14 §八/§5.1 实测）。
前置：export BINANCE_API_KEY/BINANCE_API_SECRET（testnet.binancefuture.com 的 key）。
跑：.venv/bin/python scripts/binance_testnet_smoke.py
验证：账户模式断言 / cloid 直传合法性(冒号) / 限价挂撤 / STOP_MARKET 挂撤 /
批量读五方法 / 精度量化。全程远离盘口价，不产生成交。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gridtrade.exchanges.binance import BinanceAdapter

SYM = 'BTC/USDT:USDT'


def main():
    a = BinanceAdapter.from_credentials(os.environ['BINANCE_API_KEY'],
                                        os.environ['BINANCE_API_SECRET'],
                                        testnet=True)
    print('== assert_account_mode ==')
    a.assert_account_mode()
    print('   OK（单向持仓/单币保证金）')

    print('== 行情/精度 ==')
    px = a.fetch_price(SYM)
    qty = a.quantize_amount(SYM, 0.002)
    print('   price=%s quantized(0.002)=%s' % (px, qty))
    insts = {i.symbol: i for i in a.list_instruments()}
    print('   instruments=%d BTC.min_cost=%s' % (len(insts), insts[SYM].min_cost))

    print('== cloid 直传实测（含冒号，spec §5.1）==')
    a.set_leverage(SYM, 2)
    o = a.create_limit_order(SYM, 'buy', round(px * 0.5, 1), qty,
                             client_oid='999999:1:1')
    print('   placed id=%s cloid=%s' % (o.id, o.client_oid))
    assert o.client_oid == '999999:1:1', 'cloid 被改写——需启用替换编码并更新 spec §5.1'
    opens = a.fetch_open_orders(SYM)
    print('   open_orders=%d' % len(opens))
    a.cancel_order(SYM, o.id)
    print('   canceled')

    print('== STOP_MARKET 保险丝挂撤 ==')
    s = a.create_stop_order(SYM, 'sell', qty, round(px * 0.5, 1),
                            client_oid='999999:fuse:low')
    print('   stop id=%s' % s.id)
    a.cancel_order(SYM, s.id)
    print('   canceled')

    print('== 批量读快照 ==')
    print('   prices_all:', a.fetch_prices_all([SYM]))
    print('   positions_all:', a.fetch_positions_all([SYM]))
    print('   open_orders_all:', len(a.fetch_open_orders_all([SYM])))
    print('   trades_all:', len(a.fetch_my_trades_all([SYM])))
    print('   funding_all:', {k: len(v) for k, v in
                              a.fetch_funding_payments_all([SYM]).items()})
    print('SMOKE PASS')


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 切换 runbook `docs/币安切换runbook.md`**

```markdown
# 币安生产切换 Runbook（spec 2026-07-14 §7.2）

## 阶段 0：代码就绪
- [ ] main 分支 CI 全绿；本 runbook 前置 = 实施计划 Task 0-17 全部完成。

## 阶段 1：testnet 验证（≥3 天）
- [ ] 注册 testnet.binancefuture.com，创建 API key。
- [ ] `.venv/bin/python scripts/binance_testnet_smoke.py` → SMOKE PASS
      （若 cloid 断言失败：启用 encode_cloid 替换编码，更新 spec §5.1 注记后重跑）。
- [ ] testnet app：`fly secrets set BINANCE_API_KEY=... BINANCE_API_SECRET=... -a gridtrade-hl`
      （app 名沿用 gridtrade-hl，改名=换 app 属独立基础设施操作，不在本次范围）。
- [ ] `fly deploy -c deploy/fly.toml`；观察 ≥3 天：开格/成交映射/补单/部分成交/
      对账自愈/保险丝挂撤/面板五视图/心跳，无人工干预。

## 阶段 2：HL 生产有序退场
- [ ] /controls 暂停 scheduler 开新格（或 fly scale count scheduler=0 -a gridtrade-prod）。
- [ ] 随 12H 换仓自然关格，或经 /controls 逐格手动关闭。
- [ ] **硬门槛**：生产库执行
      `SELECT id, symbol, status FROM grids WHERE status NOT IN ('CLOSED');`
      必须 0 行（残留 open 网格会让 monitor 拿币安适配器管 HL symbol，必然报错）。
- [ ] HL 提资；HL 历史行留库可查（同库延续，盈亏曲线跨所连续）。

## 阶段 3：生产切换
- [ ] 币安主网 API key：只开合约交易、**禁提现**、不绑 IP 白名单
      （Fly 出口 IP 非静态；如启用 Fly static egress 再收紧，spec §7.3）。
- [ ] `fly secrets set BINANCE_API_KEY=... BINANCE_API_SECRET=... -a gridtrade-prod`
      `fly secrets unset HL_WALLET_ADDRESS HL_PRIVATE_KEY -a gridtrade-prod`
      （不 unset 会命中退役键守卫，boot 直接报错——这是刻意的 fail-fast）。
- [ ] `fly deploy -c deploy/fly.prod.toml`（env 已是 EXCHANGE=binance/BINANCE_TESTNET=false，
      SCHEDULER_RUN_ON_START=false 保护在位）。
- [ ] 小资金试跑：临时 `fly secrets set TOTAL_BUDGET=500 MAX_CONCURRENT=3 -a gridtrade-prod`，
      入金小额，观察 ≥1 个换仓周期：无 429/418、无 stuck OPENING、记录/盈亏诚实。
- [ ] 恢复正常参数，逐步加资金。

## 验收核对（spec §八）
- [ ] ① testnet ≥3 天无人工干预
- [ ] ② 全历史回测出报告：
      `.venv/bin/python -m gridtrade.backtest.vision_sync 2019-12-01 <昨日> --tf 1h`（约数 GB）
      `TZ=Asia/Shanghai BT_WORKERS=4 .venv/bin/python -m gridtrade.backtest.backtest_run 2020-01-01 <昨日> 1m`
- [ ] ③ CI 全绿  ④ 生产小资金 ≥1 周期  ⑤ 快照契约测试在位
```

- [ ] **Step 3: Commit**

```bash
git add scripts/binance_testnet_smoke.py docs/币安切换runbook.md
git commit -m "feat(scripts,docs): testnet 冒烟脚本(cloid/STOP_MARKET/批量读实测)+生产切换 runbook(spec 2026-07-14 §7.2,§八)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 17: 全量验证收尾

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m pytest -q`
预期：全 PASS（Postgres 相关 skip 正常）。

- [ ] **Step 2: golden parity**

Run: `.venv/bin/python -m pytest tests/golden/ tests/core/ -q`
预期：全 PASS（引擎交易所无关，逐位复现）。

- [ ] **Step 3: 残留扫尾**

```bash
grep -rn "hyperliquid\|Hyperliquid\|HL_WALLET\|HL_PRIVATE\|HL_TESTNET\|reservoir\|Reservoir\|okx\|OkxAdapter" \
  gridtrade/ scripts/ deploy/ .env.example --include="*" | grep -v "退役\|已退役\|legacy"
```

预期：零命中（历史 spec 文档 docs/superpowers/specs/ 与 legacy/ 不在扫描范围，留档不改）。命中则清理措辞后重跑。

- [ ] **Step 4: 联网前哨（可选但推荐，非 CI）**

Run: `TZ=Asia/Shanghai .venv/bin/python scripts/validate_binance.py`
预期：小窗口预热 + 回测跑通，打印 summarize。

- [ ] **Step 5: 最终 commit（如扫尾有改动）**

```bash
git add -A
git commit -m "chore: 币安迁移收尾——残留措辞清零,全量测试+golden parity 绿(spec 2026-07-14)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 未尽事项（明确留给运行时/用户，不在本计划内）

- 全历史数据实际下载（数十 GB、小时级）与全历史回测报告——runbook 验收②，联网跑。
- testnet ≥3 天观察、HL 退场操作、生产 secrets 切换——runbook 阶段 1-3，人工执行。
- 若 testnet 实测 cloid 冒号被拒（与官方正则不符）：把 `_CLOID_BAD` 扩为把 `:` 一并替换 `-`（`'{gid}:{line}:{seq}'`→`'{gid}-{line}-{seq}'` 注入性保持），并更新 spec §5.1 注记与 test_encode_cloid 断言——改动收敛在 `encode_cloid` 一处（成交映射走 exchange order id，无解码依赖）。
