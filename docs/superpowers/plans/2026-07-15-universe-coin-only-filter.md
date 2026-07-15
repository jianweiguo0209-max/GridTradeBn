# 票池 COIN-only 过滤 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让自动选币票池(实盘+回测)只含 COIN 加密永续,剔除币安 mainnet 的 TradFi 代币化永续(美股/韩股/商品/股指/Pre-IPO),消除"非 7×24 标的进网格、隔夜跳空打穿网格+保险丝"的 mainnet 上线一票否决级风险。

**Architecture:** 单一事实源谓词 `is_coin_market(m)`(白名单 `underlyingType=='COIN'`,fail-closed)放 `binance.py` 模块级。实盘侧 `BinanceAdapter._include_market` 调它——一处覆盖 `list_instruments`→`resolve_live_universe`(自动选币)与 `_id_map`(账户快照映射)。回测侧 `backtest_run.py` 抽 `exclude_non_coin(symbols, adapter)` 助手,用**同一谓词**从当前 exchangeInfo 算出非 COIN 集、从归档票池剔除(保留退市 COIN,无幸存者偏差)。可观测性:通用层 `list_instruments` 报 include 剔除数(措辞交易所无关)、回测报非 COIN 剔除数。手动 `OPEN_GRID` 不硬拦(快照半碎作隐性劝阻,加注释记录)。

**Tech Stack:** Python 3.9 / pandas 1.3.5 / pytest / ccxt 4.5.61(`ccxt.binanceusdm`)。

## Global Constraints

- 品类谓词**白名单**语义:`is_coin_market(m)` ⟺ `(m.get('info') or {}).get('underlyingType') == 'COIN'`;**fail-closed**——缺失/None/任何非 'COIN' 取值一律 False(实测 659 个 swap+USDT+active 全带该字段、0 缺失,安全优先)。
- **单一事实源**:实盘 `_include_market` 与回测 `exclude_non_coin` 调**同一** `is_coin_market`,不得各写一份(口径漂移即 bug)。
- 测试命令一律:`.venv/bin/python -m pytest <path> -q -o addopts=""`(`-o addopts=""` 防 `-q` 汇总行被吞)。
- 提交尾注:`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- **不碰** `core/` 与回测引擎几何:本改动只动票池解析(backtest_run 的 universe 行)与适配器过滤,`tests/core/`+`tests/golden/` 必须逐位不变。
- **不部署**:此改动 testnet 为 no-op、mainnet 上线才生效;部署由主运维会话手动做。
- 现有 `FakeBinanceClient` 夹具的 3 个 market 用 `'info': {'listTime': '0'}`、**无 `underlyingType`**——加白名单后它们会 fail-closed 被剔,`test_list_instruments_filters_settle` 等会挂;Task 1 必须同步给这 3 个补 `'underlyingType': 'COIN'`。

---

### Task 1: 实盘 COIN-only 过滤 + 可观测性 + 手动开仓语义注释

**Files:**
- Modify: `gridtrade/exchanges/binance.py`(加 `is_coin_market` 模块函数;改 `_include_market`,现 26 行)
- Modify: `gridtrade/exchanges/ccxt_adapter.py:33-56`(`list_instruments` 加 include 剔除计数+日志)
- Modify: `gridtrade/runtime/commands.py:24-33`(`OPEN_GRID` 加语义注释)
- Modify/Test: `tests/exchanges/test_binance_adapter.py`(夹具补 `underlyingType`;新增过滤/谓词/可观测性测试)

**Interfaces:**
- Produces: `gridtrade.exchanges.binance.is_coin_market(m: dict) -> bool` —— Task 2 回测助手 import 复用。
- Produces:`BinanceAdapter._include_market` 收窄为「本结算币 **且** COIN」;`list_instruments`/`_id_map` 因复用它自动只回 COIN。

- [ ] **Step 1: 写谓词失败测试**

在 `tests/exchanges/test_binance_adapter.py` 末尾追加:

```python
def test_is_coin_market_predicate():
    from gridtrade.exchanges.binance import is_coin_market
    assert is_coin_market({'info': {'underlyingType': 'COIN'}}) is True
    # TradFi 各品类一律 False
    for t in ('EQUITY', 'KR_EQUITY', 'COMMODITY', 'INDEX', 'PREMARKET'):
        assert is_coin_market({'info': {'underlyingType': t}}) is False
    # fail-closed:字段缺失 / info 为 None / underlyingType 为 None → False
    assert is_coin_market({'info': {'listTime': '0'}}) is False
    assert is_coin_market({'info': None}) is False
    assert is_coin_market({}) is False
    assert is_coin_market({'info': {'underlyingType': None}}) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py::test_is_coin_market_predicate -q -o addopts=""`
Expected: FAIL —— `ImportError: cannot import name 'is_coin_market'`

- [ ] **Step 3: 实现 `is_coin_market`**

在 `gridtrade/exchanges/binance.py` 中 `class BinanceAdapter` 定义**之前**(紧跟 `_CLOID_BAD = re.compile(...)` 那一行之后)插入:

```python
def is_coin_market(m) -> bool:
    """币安 market 是否为 COIN(加密)永续。TradFi 代币化永续(EQUITY/KR_EQUITY/COMMODITY/
    INDEX/PREMARKET)非 7×24、隔夜跳空打穿网格,一律剔除。白名单口径(fail-closed):
    underlyingType 缺失/未知也排除(安全优先;实测 swap+USDT+active 659 个全带该字段、0 缺失)。
    实盘 _include_market 与回测 exclude_non_coin 共用此谓词(单一事实源,spec 2026-07-15 §4.1)。"""
    return ((m.get('info') or {}).get('underlyingType')) == 'COIN'
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py::test_is_coin_market_predicate -q -o addopts=""`
Expected: PASS

- [ ] **Step 5: 写 `_include_market` + 过滤失败测试**

先给共享夹具的 3 个 market 补 `underlyingType`(否则下一步实现后既有 `test_list_instruments_filters_settle` 会挂)。在 `tests/exchanges/test_binance_adapter.py` 的 `FakeBinanceClient.__init__` 中,将三处 `'info': {'listTime': '0'}` 改为带 COIN:

```python
        self.markets = {
            'BTC/USDT:USDT': {'id': 'BTCUSDT', 'symbol': 'BTC/USDT:USDT', 'swap': True,
                              'settle': 'USDT', 'base': 'BTC', 'active': True,
                              'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 50.0},
                                         'market': {'min': 0.001, 'max': 120.0}},
                              'info': {'listTime': '0', 'underlyingType': 'COIN'}},
            'ETH/USDT:USDT': {'id': 'ETHUSDT', 'symbol': 'ETH/USDT:USDT', 'swap': True,
                              'settle': 'USDT', 'base': 'ETH', 'active': True,
                              'precision': {'price': 0.01, 'amount': 0.01},
                              'limits': {'amount': {'min': 0.01}, 'cost': {'min': 20.0}},
                              'info': {'listTime': '0', 'underlyingType': 'COIN'}},
            'BTC/USDC:USDC': {'id': 'BTCUSDC', 'symbol': 'BTC/USDC:USDC', 'swap': True,
                              'settle': 'USDC', 'base': 'BTC', 'active': True,
                              'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 5.0}},
                              'info': {'listTime': '0', 'underlyingType': 'COIN'}},
        }
```

再在文件末尾追加过滤测试(用局部 markets 造 TradFi,不污染共享夹具):

```python
_MIXED_MARKETS = {
    'BTC/USDT:USDT': {'id': 'BTCUSDT', 'symbol': 'BTC/USDT:USDT', 'swap': True,
                      'settle': 'USDT', 'precision': {'price': 0.1, 'amount': 0.001},
                      'limits': {}, 'info': {'underlyingType': 'COIN'}},
    'AAPL/USDT:USDT': {'id': 'AAPLUSDT', 'symbol': 'AAPL/USDT:USDT', 'swap': True,
                       'settle': 'USDT', 'precision': {'price': 0.1, 'amount': 0.001},
                       'limits': {}, 'info': {'underlyingType': 'EQUITY'}},
    'XAU/USDT:USDT': {'id': 'XAUUSDT', 'symbol': 'XAU/USDT:USDT', 'swap': True,
                      'settle': 'USDT', 'precision': {'price': 0.1, 'amount': 0.001},
                      'limits': {}, 'info': {'underlyingType': 'COMMODITY'}},
    'BTC/USDC:USDC': {'id': 'BTCUSDC', 'symbol': 'BTC/USDC:USDC', 'swap': True,
                      'settle': 'USDC', 'precision': {'price': 0.1, 'amount': 0.001},
                      'limits': {}, 'info': {'underlyingType': 'COIN'}},
}


def _mixed_client():
    c = FakeBinanceClient()
    c.markets = dict(_MIXED_MARKETS)
    return c


def test_include_market_coin_and_settle():
    a = _binance()
    assert a._include_market({'settle': 'USDT', 'info': {'underlyingType': 'COIN'}}) is True
    assert a._include_market({'settle': 'USDT', 'info': {'underlyingType': 'EQUITY'}}) is False
    assert a._include_market({'settle': 'USDT', 'info': {'listTime': '0'}}) is False  # 缺字段 fail-closed
    assert a._include_market({'settle': 'USDC', 'info': {'underlyingType': 'COIN'}}) is False  # 非本结算币


def test_list_instruments_excludes_tradfi():
    syms = [i.symbol for i in _binance(_mixed_client()).list_instruments()]
    assert syms == ['BTC/USDT:USDT']                       # 只 COIN+本结算币
    assert 'AAPL/USDT:USDT' not in syms and 'XAU/USDT:USDT' not in syms


def test_id_map_excludes_tradfi():
    m = _binance(_mixed_client())._id_map()
    assert m == {'BTCUSDT': 'BTC/USDT:USDT'}                # TradFi 与 USDC 都不入映射


def test_resolve_live_universe_excludes_tradfi():
    from gridtrade.runtime.universe import resolve_live_universe
    out = resolve_live_universe(_binance(_mixed_client()))
    assert out == ['BTC/USDT:USDT']                        # 票池透传过滤,无 TradFi
```

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q -o addopts="" -k "include_market_coin or excludes_tradfi or resolve_live_universe_excludes"`
Expected: FAIL —— TradFi/USDC 仍出现在 `list_instruments`/`_id_map`/票池(过滤未收窄)。

- [ ] **Step 7: 实现 `_include_market` 收窄**

`gridtrade/exchanges/binance.py` 现:

```python
    # fapi 同时挂 USDT-M 与 USDC-M 合约：只收本结算币，防 USDC 合约混入票池（spec §3.1）
    def _include_market(self, m) -> bool:
        return m.get('settle') == self.quote_currency
```

改为:

```python
    # fapi 同时挂 USDT-M 与 USDC-M 合约：只收本结算币，防 USDC 合约混入票池（spec §3.1）；
    # 且只收 COIN 加密永续,剔 TradFi 代币化永续(非 7×24 跳空打穿网格,spec 2026-07-15）。
    # 一处收窄即覆盖 list_instruments→票池 与 _id_map(账户快照映射)。
    def _include_market(self, m) -> bool:
        return m.get('settle') == self.quote_currency and is_coin_market(m)
```

- [ ] **Step 8: 跑测试确认通过(含既有 settle 测试不回归)**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q -o addopts=""`
Expected: PASS(新增 4 个过滤/谓词测试 + 既有 `test_list_instruments_filters_settle` 全绿——夹具已补 underlyingType)

- [ ] **Step 9: 写可观测性失败测试**

在 `tests/exchanges/test_binance_adapter.py` 末尾追加(复用 Step 5 的 `_mixed_client`):

```python
def test_list_instruments_logs_include_excluded(capsys):
    _binance(_mixed_client()).list_instruments()
    out = capsys.readouterr().out
    # AAPL(EQUITY)+XAU(COMMODITY)+BTC-USDC(非本结算)=3 个被 _include_market 剔除
    assert 'include 过滤剔除 3' in out
```

- [ ] **Step 10: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py::test_list_instruments_logs_include_excluded -q -o addopts=""`
Expected: FAIL —— 无该日志输出。

- [ ] **Step 11: 实现 `list_instruments` 剔除计数+日志**

`gridtrade/exchanges/ccxt_adapter.py:33-56` 现:

```python
    def list_instruments(self) -> List[Instrument]:
        self.client.load_markets()
        out = []
        seen = set()
        for sym, m in self.client.markets.items():
            if m.get('swap') is not True:          # 只留永续合约，丢 spot/其它类型
                continue
            if not self._include_market(m):        # 交易所特有剔除（子类按需过滤，见 _include_market）
                continue
            canonical = self.to_canonical(sym)
            if canonical in seen:                   # 同 canonical 去重（部分交易所 spot+swap 等多键折叠）
                continue
            seen.add(canonical)
            info = m.get('info', {}) or {}
            out.append(Instrument(
                symbol=canonical,
                tick=float(m.get('precision', {}).get('price') or 0.0),
                lot=float(m.get('precision', {}).get('amount') or 0.0),
                min_size=float(m.get('limits', {}).get('amount', {}).get('min') or 0.0),
                state='live' if m.get('active', True) else 'expired',
                list_ts=int(info.get('listTime') or 0),
                min_cost=float(((m.get('limits', {}) or {}).get('cost', {}) or {}).get('min') or 0.0),
            ))
        return out
```

改为(仅加 `excluded` 计数与末尾条件日志;其余逐字不变):

```python
    def list_instruments(self) -> List[Instrument]:
        self.client.load_markets()
        out = []
        seen = set()
        excluded = 0                               # 通过 swap 但被 _include_market 剔除的合约数(可观测性)
        for sym, m in self.client.markets.items():
            if m.get('swap') is not True:          # 只留永续合约，丢 spot/其它类型
                continue
            if not self._include_market(m):        # 交易所特有剔除（子类按需过滤，见 _include_market）
                excluded += 1
                continue
            canonical = self.to_canonical(sym)
            if canonical in seen:                   # 同 canonical 去重（部分交易所 spot+swap 等多键折叠）
                continue
            seen.add(canonical)
            info = m.get('info', {}) or {}
            out.append(Instrument(
                symbol=canonical,
                tick=float(m.get('precision', {}).get('price') or 0.0),
                lot=float(m.get('precision', {}).get('amount') or 0.0),
                min_size=float(m.get('limits', {}).get('amount', {}).get('min') or 0.0),
                state='live' if m.get('active', True) else 'expired',
                list_ts=int(info.get('listTime') or 0),
                min_cost=float(((m.get('limits', {}) or {}).get('cost', {}) or {}).get('min') or 0.0),
            ))
        # 措辞交易所无关(通用层不泄漏 COIN 概念);币安下此数≈非 COIN TradFi(+少量 USDC-M)。
        # fail-closed 配套护栏:underlyingType 字段格式漂移致白名单误杀会使此数跳升、可见。
        if excluded:
            print('[universe] include 过滤剔除 %d 个合约' % excluded, flush=True)
        return out
```

- [ ] **Step 12: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py::test_list_instruments_logs_include_excluded tests/exchanges/test_ccxt_adapter.py -q -o addopts=""`
Expected: PASS(通用 ccxt 测试用 base `_include_market`(恒 True)→ excluded=0 → 不打印,不回归)

- [ ] **Step 13: 手动 OPEN_GRID 语义注释**

`gridtrade/runtime/commands.py:24` 的 `if cmd.type == 'OPEN_GRID':` 之后、`if flags.get('trading_halted'):` 之前,插入注释:

```python
    if cmd.type == 'OPEN_GRID':
        # 注:手动开仓直调 ex.open、不经 list_instruments/_include_market 的 COIN 过滤——
        # 手动开 TradFi 代币化永续仍可下单,但其账户快照映射(_id_map)已剔除该品类→快照漏该仓
        # (半碎)。此为有意取舍(spec 2026-07-15 §4.2):作为对"手动玩 TradFi"的隐性劝阻,非 bug。
        if flags.get('trading_halted'):
```

(纯注释,无测试。)

- [ ] **Step 14: 跑实盘侧全量 + 提交**

Run: `.venv/bin/python -m pytest tests/exchanges/ tests/runtime/ -q -o addopts=""`
Expected: PASS(全绿)

```bash
git add gridtrade/exchanges/binance.py gridtrade/exchanges/ccxt_adapter.py \
        gridtrade/runtime/commands.py tests/exchanges/test_binance_adapter.py
git commit -m "feat(exchanges): 票池 COIN-only——_include_market 收窄至 underlyingType=='COIN'，剔 TradFi 代币化永续" \
  -m "白名单谓词 is_coin_market(fail-closed,缺字段=排除)为单一事实源;一处收窄覆盖 list_instruments/票池/_id_map。ccxt_adapter list_instruments 报 include 剔除数(通用措辞,字段漂移误杀可见)。手动 OPEN_GRID 不硬拦(快照半碎作隐性劝阻,加注释)。spec 2026-07-15 §4.1/4.2/4.4。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 回测同口径过滤 + 可观测性 + runbook 前置门槛

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`(加 `exclude_non_coin` 助手 + import `is_coin_market`;rewire 票池行,现 550)
- Test: `tests/backtest/test_backtest_run.py`(新增 `exclude_non_coin` 单测)
- Modify: `docs/币安切换runbook.md:34`(阶段 3 加 COIN-only 前置勾选项)

**Interfaces:**
- Consumes: `gridtrade.exchanges.binance.is_coin_market`(Task 1 产出)。
- Produces: `gridtrade.backtest.backtest_run.exclude_non_coin(symbols, adapter) -> (kept: list[str], removed: int)`。

- [ ] **Step 1: 写 `exclude_non_coin` 失败测试**

在 `tests/backtest/test_backtest_run.py` 末尾追加:

```python
def test_exclude_non_coin_drops_tradfi_keeps_delisted_coin():
    from gridtrade.backtest.backtest_run import exclude_non_coin
    from gridtrade.exchanges.binance import BinanceAdapter
    from tests.exchanges.test_binance_adapter import FakeBinanceClient
    c = FakeBinanceClient()
    c.markets = {
        'BTC/USDT:USDT': {'symbol': 'BTC/USDT:USDT', 'swap': True, 'settle': 'USDT',
                          'info': {'underlyingType': 'COIN'}},
        'SOXL/USDT:USDT': {'symbol': 'SOXL/USDT:USDT', 'swap': True, 'settle': 'USDT',
                           'info': {'underlyingType': 'EQUITY'}},
        'XAU/USDT:USDT': {'symbol': 'XAU/USDT:USDT', 'swap': True, 'settle': 'USDT',
                          'info': {'underlyingType': 'COMMODITY'}},
        'BTC/USDC:USDC': {'symbol': 'BTC/USDC:USDC', 'swap': True, 'settle': 'USDC',
                          'info': {'underlyingType': 'COIN'}},   # 非本结算币,不算入 non_coin
    }
    a = BinanceAdapter(c)
    # 归档含:现存 COIN(BTC)、现存 TradFi(SOXL/XAU)、已退市 COIN(FOO 不在当前 exchangeInfo)
    archive = {'BTC/USDT:USDT', 'SOXL/USDT:USDT', 'XAU/USDT:USDT', 'FOO/USDT:USDT'}
    kept, removed = exclude_non_coin(archive, a)
    assert kept == ['BTC/USDT:USDT', 'FOO/USDT:USDT']   # TradFi 剔除;退市 COIN 保留(无幸存者偏差)
    assert removed == 2                                  # SOXL + XAU
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/backtest/test_backtest_run.py::test_exclude_non_coin_drops_tradfi_keeps_delisted_coin -q -o addopts=""`
Expected: FAIL —— `ImportError: cannot import name 'exclude_non_coin'`

- [ ] **Step 3: 实现 `exclude_non_coin` 助手**

在 `gridtrade/backtest/backtest_run.py` 顶部 import 区加:

```python
from gridtrade.exchanges.binance import is_coin_market
```

并在模块级(建议紧邻 `_binance_datasource_1h` 之后)加助手:

```python
def exclude_non_coin(symbols, adapter):
    """从 canonical 符号集剔除当前 exchangeInfo 的非 COIN 标的(TradFi 代币化永续),与实盘
    _include_market 共用同一 is_coin_market 谓词(单一事实源,spec 2026-07-15 §4.3)。
    保留退市 COIN:退市币不在当前 markets → 不在 non_coin → 不被剔(无幸存者偏差)。
    markets 未加载则 load(幂等;ccxt 缓存,紧随 prewarm 复用,全程一次 exchangeInfo)。
    返回 (kept: sorted list[str], removed: int)。"""
    adapter.client.load_markets()
    markets = adapter.client.markets or {}
    non_coin = {adapter.to_canonical(m['symbol']) for m in markets.values()
                if m.get('swap') and m.get('settle') == adapter.quote_currency
                and not is_coin_market(m)}
    kept = sorted(s for s in symbols if s not in non_coin)
    return kept, len(set(symbols) & non_coin)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/backtest/test_backtest_run.py::test_exclude_non_coin_drops_tradfi_keeps_delisted_coin -q -o addopts=""`
Expected: PASS

- [ ] **Step 5: rewire 票池解析行(main 内)**

`gridtrade/backtest/backtest_run.py:550` 现:

```python
    # 票池=归档全量合约（含退市，无幸存者偏差，spec §6.1）−黑名单
    universe = sorted(set(V.list_archive_symbols()) - set(bt_blacklist))
    print('[BT] 全市场票池 %d 币(归档含退市,−黑名单 %d)' % (len(universe), len(bt_blacklist)))
```

改为:

```python
    # 票池=归档全量合约（含退市，无幸存者偏差，spec §6.1）−黑名单 −非 COIN(TradFi,spec 2026-07-15)
    _arch = set(V.list_archive_symbols()) - set(bt_blacklist)
    universe, _n_tradfi = exclude_non_coin(_arch, _adapter)
    print('[BT] 全市场票池 %d 币(归档含退市,−黑名单 %d,−非COIN %d)'
          % (len(universe), len(bt_blacklist), _n_tradfi))
```

(`_adapter` 即上文 `_adapter, _ds1h = _binance_datasource_1h(cache)` 返回的适配器,已在作用域内。)

- [ ] **Step 6: 跑回测测试确认无回归**

Run: `.venv/bin/python -m pytest tests/backtest/ -q -o addopts=""`
Expected: PASS —— 既有 `test_run_backtest_*` 走 `select_grids`/`run_backtest`/tmp_path,不碰 main 的联网票池行;新增 `exclude_non_coin` 单测绿。

- [ ] **Step 7: golden/core 逐位不变守卫**

Run: `.venv/bin/python -m pytest tests/core/ tests/golden/ -q -o addopts=""`
Expected: PASS —— 本改动只碰票池解析,不碰回测引擎几何,golden/core 必须逐位不变。

- [ ] **Step 8: runbook 阶段 3 加前置门槛**

`docs/币安切换runbook.md:34` 现:

```markdown
## 阶段 3：生产切换
- [ ] 币安主网 API key：只开合约交易、**禁提现**、不绑 IP 白名单
```

在 `## 阶段 3：生产切换` 之后、`- [ ] 币安主网 API key` 之前插入:

```markdown
- [ ] **前置门槛(代码):票池 COIN-only 过滤已落地**——`BinanceAdapter._include_market` 仅收
      `underlyingType=='COIN'`(实盘+回测同口径);切换后核对 mainnet 票池无 TradFi
      (`resolve_live_universe` 结果中 `underlyingType!='COIN'` 应为 0)。背景:币安 mainnet 上
      美股/韩股/商品代币化永续,非 7×24 跳空打穿网格+保险丝(spec 2026-07-15)。
```

- [ ] **Step 9: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_backtest_run.py \
        docs/币安切换runbook.md
git commit -m "feat(backtest,docs): 回测票池 COIN-only 同口径——归档减当前非 COIN,保留退市币;runbook 阶段3 前置门槛" \
  -m "exclude_non_coin 复用实盘 is_coin_market 谓词(单一事实源);从归档全集减当前 exchangeInfo 的非 COIN 集(而非交当前 COIN),退市 COIN 不被剔、无幸存者偏差。回测打非 COIN 剔除数。runbook 阶段3 加 COIN-only 前置勾选项。spec 2026-07-15 §4.3/§六。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §4.1 单一谓词 `is_coin_market`(白名单/fail-closed) → Task 1 Step 1-4 ✓
- §4.2 实盘 `_include_market` 收窄(覆盖 list_instruments/票池/_id_map) → Task 1 Step 5-8 ✓;手动 OPEN_GRID 语义注释 → Step 13 ✓
- §4.3 回测 `exclude_non_coin`(归档减当前非 COIN、留退市、load_markets 幂等、形态同形) → Task 2 Step 1-6 ✓
- §4.4 可观测性(通用层 include 剔除数 + 回测非 COIN 剔除数) → Task 1 Step 9-12 + Task 2 Step 5 ✓
- §5 测试(谓词/_include_market/list_instruments/_id_map/resolve_live_universe/回测/可观测性/golden 守卫) → 全覆盖 ✓
- §六 runbook 前置门槛 → Task 2 Step 8 ✓;记忆更新由控制会话在收尾做(非 plan 任务)。

**2. Placeholder scan:** 无 TBD/TODO;每个代码步骤含完整可抄写代码与确切命令/预期。✓

**3. Type consistency:** `is_coin_market(m)->bool` 在 Task 1 定义、Task 2 import 一致;`exclude_non_coin(symbols, adapter)->(list,int)` 签名前后一致;`_adapter`/`quote_currency`/`to_canonical`/`client.markets` 均已对实体代码核准存在。✓

**已知非目标(spec 明确,不做):** 手动 OPEN_GRID 硬拦、TradFi 专用策略、demo 品类字段修复、已退市 TradFi 的回测剔除(微小 gap,YAGNI)。
