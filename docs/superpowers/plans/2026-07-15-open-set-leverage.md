# 开格设杠杆(修 -2027) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `open()` 挂单前按杠杆档位 `set_leverage(symbol, L)`(减一档 L),修币安"默认杠杆档位撑不住 worst 名义 → -2027 拒单";fail-open 不阻断开格。

**Architecture:** 三层:①适配器 `fetch_leverage_tiers` 取归一化档位表(私有只读,实例缓存,fail-open []);②纯函数 `leverage_policy` 从档位+worst 名义算 L(减一档、clamp[ceil(gearing),symbol_maxLev])与 feasible(告警);③`open()` 算 worst 名义→pick_leverage→set_leverage,异常 fail-open。纯实盘 API,回测/FakeExchange no-op、几何零变化。

**Tech Stack:** Python 3.9 / pytest / ccxt 4.5.61(`fetch_leverage_tiers`=私有 `fapiPrivateGetLeverageBracket`)。

## Global Constraints

- **fail-open 是红线**:tiers 取不到 / pick 返 None / set_leverage 抛异常 → 一律告警+继续开格,绝不因设杠杆失败而阻断(现状:-2027 由 open_proposals f4d053b 逐提议隔离兜底)。
- **减一档 L**:`pick_leverage` = 能容 worst 名义的最紧档的**下一档** maxLev,`clamp[ceil(gearing), 最高档 maxLev]`。
- **worst 名义** = `order_num × grid_count × entry`(open 内已有 order_num/entry;与 fuse worst 同源 max_rate=1.0)。
- **不缓存 set_leverage**(权重可忽略、避 staleness);**缓存 tiers**(每币一次,档位表稳定)。
- **不碰** `core/` 与回测引擎几何:`tests/core/`+`tests/golden/` 必须逐位不变(只在 open 加一段实盘 API)。
- 测试命令:`.venv/bin/python -m pytest <path> -q -o addopts=""`。提交尾注 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- **不部署**(部署由主运维会话按避开整点 HH:00–HH:12 手动做)。
- 无块 D(选币可行性排除/回填):实证证当前 0 币受益,暂缓。

---

### Task 1: A — 适配器 `fetch_leverage_tiers` + FakeExchange 钩子

**Files:**
- Modify: `gridtrade/exchanges/base.py`(加默认 `fetch_leverage_tiers` 返 `[]`,紧邻 `max_leverage` 后 ~:187)
- Modify: `gridtrade/exchanges/ccxt_adapter.py`(加 `fetch_leverage_tiers` 归一化+按币缓存,紧邻 `fetch_max_leverages` 后 ~:156)
- Modify: `gridtrade/exchanges/fake.py`(`__init__` 加两状态;加 `seed_leverage_tiers`/`fetch_leverage_tiers`;`set_leverage` 记录调用)
- Test: `tests/exchanges/test_fake.py`、`tests/exchanges/test_ccxt_adapter.py`

**Interfaces:**
- Produces: `ExchangeAdapter.fetch_leverage_tiers(symbol) -> list[{'maxLeverage': int, 'maxNotional': float}]`(Task 3 用);base 默认 `[]`;ccxt 真实现;`FakeExchange.seed_leverage_tiers(symbol, tiers)`、`FakeExchange._leverage_calls`(Task 3 断言用)。

- [ ] **Step 1: 写 FakeExchange 钩子失败测试**

在 `tests/exchanges/test_fake.py` 末尾追加:

```python
def test_leverage_tiers_seed_and_fetch_default_empty():
    from gridtrade.exchanges.fake import FakeExchange
    ex = FakeExchange()
    assert ex.fetch_leverage_tiers('BTC/USDT:USDT') == []      # 默认空(fail-open)
    ex.seed_leverage_tiers('BTC/USDT:USDT',
                           [{'maxLeverage': 5, 'maxNotional': 5000.0}])
    assert ex.fetch_leverage_tiers('BTC/USDT:USDT') == [{'maxLeverage': 5, 'maxNotional': 5000.0}]


def test_set_leverage_records_calls():
    from gridtrade.exchanges.fake import FakeExchange
    ex = FakeExchange()
    ex.set_leverage('BTC/USDT:USDT', 4)
    ex.set_leverage('ETH/USDT:USDT', 7)
    assert ex._leverage_calls == [('BTC/USDT:USDT', 4), ('ETH/USDT:USDT', 7)]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_fake.py::test_leverage_tiers_seed_and_fetch_default_empty tests/exchanges/test_fake.py::test_set_leverage_records_calls -q -o addopts=""`
Expected: FAIL —— `AttributeError: 'FakeExchange' object has no attribute 'seed_leverage_tiers'` / `_leverage_calls`。

- [ ] **Step 3: 实现 FakeExchange 钩子**

`gridtrade/exchanges/fake.py` 的 `__init__` 末尾(`self._quote_volumes = {}` 之后)加:

```python
        self._leverage_tiers = {}       # symbol -> [{'maxLeverage','maxNotional'}]（测试钩子）
        self._leverage_calls = []       # [(symbol, leverage)]（open 设杠杆断言用）
```

在测试钩子区(`seed_quote_volumes` 之后)加:

```python
    def seed_leverage_tiers(self, symbol: str, tiers: list) -> None:
        self._leverage_tiers[symbol] = [dict(t) for t in tiers]
```

将现有 `set_leverage` 改为记录调用:

```python
    def set_leverage(self, symbol, leverage) -> None:
        self._leverage_calls.append((symbol, leverage))
```

在 `set_leverage` 附近加:

```python
    def fetch_leverage_tiers(self, symbol) -> list:
        return list(self._leverage_tiers.get(symbol, []))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_fake.py -q -o addopts=""`
Expected: PASS(新增 2 个 + 既有 fake 测试不回归)

- [ ] **Step 5: 写 base 默认 + ccxt 归一化失败测试**

在 `tests/exchanges/test_ccxt_adapter.py` 末尾追加:

```python
def test_base_fetch_leverage_tiers_default_empty():
    # 基类默认 []（fail-open）。ExchangeAdapter 是 ABC 不能直接实例化,故用具体实例
    # 直调基类未覆写方法(绕过子类覆写)验证默认契约。
    from gridtrade.exchanges.base import ExchangeAdapter
    from gridtrade.exchanges.fake import FakeExchange
    assert ExchangeAdapter.fetch_leverage_tiers(FakeExchange(), 'BTC/USDT:USDT') == []


def test_ccxt_fetch_leverage_tiers_normalizes_and_caches():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    calls = []
    def flt(symbols, params=None):
        calls.append(list(symbols))
        return {'BTC/USDT:USDT': [
            {'tier': 1, 'maxLeverage': 20, 'maxNotional': 10000.0, 'info': {}},
            {'tier': 2, 'maxLeverage': 10, 'maxNotional': 50000.0, 'info': {}}]}
    c.fetch_leverage_tiers = flt
    a = CcxtAdapter(c, name='x')
    out = a.fetch_leverage_tiers('BTC/USDT:USDT')
    assert out == [{'maxLeverage': 20, 'maxNotional': 10000.0},
                   {'maxLeverage': 10, 'maxNotional': 50000.0}]
    a.fetch_leverage_tiers('BTC/USDT:USDT')          # 二次
    assert len(calls) == 1                            # 按币缓存,不重取


def test_ccxt_fetch_leverage_tiers_failopen_on_exception():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    def boom(symbols, params=None):
        raise RuntimeError('leverageBracket down')
    c.fetch_leverage_tiers = boom
    assert CcxtAdapter(c, name='x').fetch_leverage_tiers('BTC/USDT:USDT') == []   # fail-open
```

(`FakeCcxtClient` 已在 test_ccxt_adapter.py 顶部导入/定义。)

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py -q -o addopts="" -k "leverage_tiers"`
Expected: FAIL —— base/ccxt 无 `fetch_leverage_tiers`(base 默认未加 / ccxt 未覆写)。

- [ ] **Step 7: 实现 base 默认 + ccxt 归一化**

`gridtrade/exchanges/base.py` 的 `max_leverage`(~:186)之后加:

```python
    # ---- 可选：杠杆档位表（open 设杠杆用；spec 2026-07-15-open-set-leverage）----
    def fetch_leverage_tiers(self, symbol: str) -> list:
        """[{'maxLeverage': int, 'maxNotional': float}, …]；默认 []（fail-open：
        子类未实现即不设杠杆，退化为交易所默认）。"""
        return []
```

`gridtrade/exchanges/ccxt_adapter.py` 的 `fetch_max_leverages` 之后加:

```python
    def fetch_leverage_tiers(self, symbol: str) -> list:
        """自 ccxt fetch_leverage_tiers([symbol]) 归一化为 [{'maxLeverage':int,'maxNotional':float}]；
        按币实例缓存（档位表稳定）；取数/归一化任何异常 → []（fail-open，调用方不设杠杆）。"""
        cache = getattr(self, '_lev_tiers_cache', None)
        if cache is None:
            cache = self._lev_tiers_cache = {}
        if symbol not in cache:
            try:
                raw = self.client.fetch_leverage_tiers([symbol]) or {}
                brs = raw.get(symbol) or []
                cache[symbol] = [{'maxLeverage': int(t['maxLeverage']),
                                  'maxNotional': float(t['maxNotional'])}
                                 for t in brs
                                 if t.get('maxLeverage') and t.get('maxNotional') is not None]
            except Exception:
                cache[symbol] = []
        return cache[symbol]
```

- [ ] **Step 8: 跑测试确认通过 + 提交**

Run: `.venv/bin/python -m pytest tests/exchanges/ -q -o addopts=""`
Expected: PASS(全绿)

```bash
git add gridtrade/exchanges/base.py gridtrade/exchanges/ccxt_adapter.py \
        gridtrade/exchanges/fake.py tests/exchanges/test_fake.py tests/exchanges/test_ccxt_adapter.py
git commit -m "feat(exchanges): fetch_leverage_tiers 适配器方法——开格设杠杆的档位数据面(spec 2026-07-15 §3.1)" \
  -m "base 默认 [](fail-open);ccxt 自 fetch_leverage_tiers 归一化 [{maxLeverage,maxNotional}]+按币缓存+异常 fail-open;FakeExchange seed_leverage_tiers 钩子 + set_leverage 记录调用(open 断言用)。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: B — 纯函数 `leverage_policy`

**Files:**
- Create: `gridtrade/execution/leverage_policy.py`
- Test: `tests/execution/test_leverage_policy.py`

**Interfaces:**
- Produces: `pick_leverage(worst_notional, tiers, gearing) -> int|None`、`feasible(worst_notional, tiers, gearing) -> bool`、`cap_at_leverage(tiers, L) -> float`(Task 3 用)。`tiers` = `[{'maxLeverage':int,'maxNotional':float}]`(Task 1 产出形态)。

- [ ] **Step 1: 写纯函数失败测试**

创建 `tests/execution/test_leverage_policy.py`:

```python
"""开格设杠杆纯函数（spec 2026-07-15-open-set-leverage §3.2）。
用 demo 实测档位当夹具：KITE(5档 maxLev 5→1) / 1000PEPE(高杠杆多档)。"""
from gridtrade.execution.leverage_policy import cap_at_leverage, feasible, pick_leverage

GEARING = 3.4          # ceil = 4
KITE = [{'maxLeverage': 5, 'maxNotional': 5000.0}, {'maxLeverage': 4, 'maxNotional': 10000.0},
        {'maxLeverage': 3, 'maxNotional': 30000.0}, {'maxLeverage': 2, 'maxNotional': 80000.0},
        {'maxLeverage': 1, 'maxNotional': 200000.0}]
PEPE = [{'maxLeverage': 25, 'maxNotional': 5000.0}, {'maxLeverage': 20, 'maxNotional': 10000.0},
        {'maxLeverage': 13, 'maxNotional': 50000.0}, {'maxLeverage': 4, 'maxNotional': 1000000.0}]


def test_cap_at_leverage():
    assert cap_at_leverage(KITE, 4) == 10000.0      # maxLev>=4 的最大 maxNotional = 4x 档 $10k
    assert cap_at_leverage(KITE, 5) == 5000.0
    assert cap_at_leverage(KITE, 1) == 200000.0
    assert cap_at_leverage(KITE, 99) == 0.0         # 无 maxLev>=99 档


def test_feasible():
    assert feasible(8000.0, KITE, GEARING) is True     # $8k <= cap_at(4)=$10k → 可行
    assert feasible(12000.0, KITE, GEARING) is False   # $12k > $10k → 不可行(需 3x<gearing)
    assert feasible(999999.0, [], GEARING) is True     # tiers 空 → fail-open 判可行(不告警)


def test_pick_leverage_steps_down_one_bracket():
    # 1000PEPE worst $2000：tightest=25x($5k,idx0) → 减一档=20x
    assert pick_leverage(2000.0, PEPE, GEARING) == 20


def test_pick_leverage_floor_clamps_to_ceil_gearing():
    # KITE worst $8000：tightest=4x($10k,idx1) → 减一档=3x → floor clamp 到 ceil(3.4)=4
    assert pick_leverage(8000.0, KITE, GEARING) == 4


def test_pick_leverage_infeasible_best_effort():
    # KITE worst $12000（不可行）：超 4x 档 → 减一档到 2x → floor clamp 到 4（尽力；feasible 会告警）
    assert pick_leverage(12000.0, KITE, GEARING) == 4


def test_pick_leverage_worst_exceeds_all_brackets():
    # worst 超最大档($200k) → 最低档 1x 尽力 → floor clamp 到 4
    assert pick_leverage(500000.0, KITE, GEARING) == 4


def test_pick_leverage_empty_tiers_returns_none():
    assert pick_leverage(2000.0, [], GEARING) is None    # fail-open：调用方不设杠杆


def test_pick_leverage_never_exceeds_symbol_max():
    # worst 极小落 bracket0：减一档=20x，但绝不超最高档 25x
    assert pick_leverage(1.0, PEPE, GEARING) == 20
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_leverage_policy.py -q -o addopts=""`
Expected: FAIL —— `ModuleNotFoundError: gridtrade.execution.leverage_policy`。

- [ ] **Step 3: 实现 `leverage_policy.py`**

创建 `gridtrade/execution/leverage_policy.py`:

```python
"""开格设杠杆纯函数（spec 2026-07-15-open-set-leverage §3.2）。

币安杠杆档位：在设定杠杆 L 时最大可持名义 = maxLev>=L 的最大 maxNotional（杠杆越高档位越小）。
worst 名义 ≈ gearing×cap。pick_leverage 取"能容 worst 的最紧档的下一档"（减一档留余量），
clamp[ceil(gearing)（保证金撑得住 gearing×cap 名义所需最低杠杆）, 最高档 maxLev]。
tiers = [{'maxLeverage': int, 'maxNotional': float}]（adapter.fetch_leverage_tiers 产出）。"""
import math


def cap_at_leverage(tiers, L):
    """设定杠杆 L 时的最大可持名义 = maxLev>=L 的最大 maxNotional；无匹配 → 0.0。"""
    vals = [t['maxNotional'] for t in tiers if t['maxLeverage'] >= L]
    return max(vals) if vals else 0.0


def feasible(worst_notional, tiers, gearing):
    """worst 名义能否在 ceil(gearing) 杠杆下持有（保证金撑得住）。tiers 空 → True
    （fail-open，不因读不到档位而判死/告警）。仅供告警，不做排除（块 D 暂缓）。"""
    if not tiers:
        return True
    return worst_notional <= cap_at_leverage(tiers, math.ceil(float(gearing)))


def pick_leverage(worst_notional, tiers, gearing):
    """能容 worst 名义的最紧档的下一档 maxLev（减一档留余量），clamp[ceil(gearing), 最高档 maxLev]。
    tiers 空 → None（fail-open，调用方不设杠杆）。worst 超所有档（不可行）→ 最低档尽力（feasible 告警）。"""
    if not tiers:
        return None
    brs = sorted(tiers, key=lambda t: -t['maxLeverage'])   # 高杠杆(小名义)在前
    floor = math.ceil(float(gearing))
    top = brs[0]['maxLeverage']                            # 最高档 = symbol maxLev
    idx = next((i for i, b in enumerate(brs) if b['maxNotional'] >= worst_notional), None)
    if idx is None:                                        # worst 超所有档(不可行) → 最低档尽力
        raw = brs[-1]['maxLeverage']
    else:
        raw = brs[min(idx + 1, len(brs) - 1)]['maxLeverage']   # 减一档
    return int(min(max(raw, floor), top))
```

- [ ] **Step 4: 跑测试确认通过 + 提交**

Run: `.venv/bin/python -m pytest tests/execution/test_leverage_policy.py -q -o addopts=""`
Expected: PASS(9/9)

```bash
git add gridtrade/execution/leverage_policy.py tests/execution/test_leverage_policy.py
git commit -m "feat(execution): leverage_policy 纯函数——减一档 L + feasible(spec 2026-07-15 §3.2)" \
  -m "pick_leverage=能容 worst 名义的最紧档的下一档 maxLev,clamp[ceil(gearing),最高档];feasible=worst<=cap_at(ceil gearing)(告警用);tiers 空→None/True(fail-open)。用 demo 实测 KITE/1000PEPE 档位验证减一档/floor clamp/不可行各分支。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: C — `open()` 挂单前设杠杆 + 可观测

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`(`open()` 内 entry 后 :89、限价单循环 :119 之前插入)
- Test: `tests/execution/test_open_set_leverage.py`

**Interfaces:**
- Consumes: `adapter.fetch_leverage_tiers(symbol)`(Task 1)、`leverage_policy.pick_leverage/feasible`(Task 2)、`FakeExchange._leverage_calls`/`seed_leverage_tiers`(Task 1)。

- [ ] **Step 1: 写 open 设杠杆失败测试**

创建 `tests/execution/test_open_set_leverage.py`:

```python
"""open() 挂单前设杠杆（spec 2026-07-15-open-set-leverage §3.3）：减一档 L；fail-open 不阻断。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'KITE/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
KITE = [{'maxLeverage': 5, 'maxNotional': 5000.0}, {'maxLeverage': 4, 'maxNotional': 10000.0},
        {'maxLeverage': 3, 'maxNotional': 30000.0}, {'maxLeverage': 2, 'maxNotional': 80000.0},
        {'maxLeverage': 1, 'maxNotional': 200000.0}]


def _gx(store, tiers=None, cap=1000.0, gearing=3.4):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    if tiers is not None:
        ex.seed_leverage_tiers(SYM, tiers)
    return ex, GridExecutor(ex, store, cap=cap, leverage=gearing)


def test_open_sets_leverage_from_tiers(store):
    # cap=1000 gearing=3.4 → worst 名义小,落 KITE bracket0($5k) → 减一档=4x
    ex, gx = _gx(store, tiers=KITE)
    gx.open(ex.name, SYM, GP, tag='t')
    assert ex._leverage_calls == [(SYM, 4)]        # 设了 4x(减一档+floor)


def test_open_no_tiers_skips_set_leverage(store):
    # 未 seed 档位 → fetch 返 [] → pick None → 不设杠杆(退化现状)
    ex, gx = _gx(store, tiers=None)
    gx.open(ex.name, SYM, GP, tag='t')
    assert ex._leverage_calls == []


def test_open_set_leverage_failure_is_failopen(store):
    # set_leverage 抛异常 → open 不中断,挂单/丝照常(fail-open)
    ex, gx = _gx(store, tiers=KITE)
    def boom(symbol, leverage): raise RuntimeError('-4000 set lev failed')
    ex.set_leverage = boom
    gid = gx.open(ex.name, SYM, GP, tag='t')       # 不抛
    from gridtrade.state.grids import GridRepository
    assert GridRepository(store).get(gid).status == 'ACTIVE'
    assert len(ex.fetch_open_orders(SYM)) == 9      # 9 挂单照常


def test_open_infeasible_warns(store, capsys):
    # worst 名义 > 4x 档上限 → WARN(设尽力 L,-2027 由 open_proposals 隔离)
    tiny = [{'maxLeverage': 5, 'maxNotional': 100.0}, {'maxLeverage': 4, 'maxNotional': 200.0},
            {'maxLeverage': 3, 'maxNotional': 500.0}]
    ex, gx = _gx(store, tiers=tiny)                 # worst 名义 ~ gearing×cap ≫ $200
    gx.open(ex.name, SYM, GP, tag='t')
    assert 'WARN' in capsys.readouterr().out and ex._leverage_calls != []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_open_set_leverage.py -q -o addopts=""`
Expected: FAIL —— `test_open_sets_leverage_from_tiers` 断言 `_leverage_calls == [(SYM,4)]` 失败(open 未设杠杆,实际 `[]`)。

- [ ] **Step 3: 实现 open() 设杠杆**

`gridtrade/execution/grid_executor.py` 的 `open()`，在 `entry = float(self.adapter.fetch_price(symbol))`(:89 附近,即 order_num/entry 都已定义)之后、`# 逐线挂限价单`(:119)之前,插入:

```python
        # 设仓位杠杆(spec 2026-07-15-open-set-leverage):HL 从不设、币安默认档位可能撑不住 worst
        # 名义 → -2027。减一档 L 留余量;fail-open:tiers/set_leverage 异常退化为不设(现状,-2027 由
        # open_proposals f4d053b 逐提议隔离兜底)。
        worst_notional = order_num * int(grid_params['grid_count']) * entry
        try:
            from gridtrade.execution.leverage_policy import pick_leverage, feasible
            _tiers = self.adapter.fetch_leverage_tiers(symbol)
            _L = pick_leverage(worst_notional, _tiers, self.gearing)
            if _L is not None:
                self.adapter.set_leverage(symbol, _L)
                if feasible(worst_notional, _tiers, self.gearing):
                    print('[leverage] %s set %dx (worst名义 $%.0f)' % (symbol, _L, worst_notional),
                          flush=True)
                else:
                    print('[leverage] WARN %s worst名义 $%.0f 超 ceil(gearing) 档上限——设 %dx 尽力,'
                          '可能 -2027(极罕见,open_proposals 隔离兜底)'
                          % (symbol, worst_notional, _L), flush=True)
        except Exception as exc:            # fail-open:绝不因设杠杆失败而阻断开格
            print('[leverage] WARN %s set_leverage 跳过(fail-open): %r' % (symbol, exc), flush=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_open_set_leverage.py -q -o addopts=""`
Expected: PASS(4/4)

- [ ] **Step 5: 回归 + golden/core 几何守卫**

Run: `.venv/bin/python -m pytest tests/execution/ -q -o addopts=""`
Expected: PASS —— 既有 open 测试用 FakeExchange 未 seed 档位 → fetch 返 [] → 不设杠杆,行为不变、无回归。

Run: `.venv/bin/python -m pytest tests/core/ tests/golden/ -q -o addopts=""`
Expected: PASS —— open 只加实盘 API,不碰引擎/几何,逐位不变。

- [ ] **Step 6: 提交**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_open_set_leverage.py
git commit -m "feat(execution): open 挂单前 set_leverage(减一档 L)——修币安默认档位撑不住 worst 名义的 -2027" \
  -m "worst 名义=order_num×grid_count×entry;fetch_leverage_tiers→pick_leverage(减一档)→set_leverage;fail-open(tiers/set_leverage 异常退化为不设、-2027 由 open_proposals 隔离);不可行 WARN。每格打 [leverage] 日志。FakeExchange 未 seed→[]→不设,回测/既有测试零变化。spec 2026-07-15 §3.3。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §3.1 A 适配器 fetch_leverage_tiers(base 默认/ccxt 归一化+缓存/FakeExchange seed) → Task 1 ✓
- §3.2 B leverage_policy(cap_at_leverage/feasible/pick_leverage 减一档) → Task 2 ✓
- §3.3 C open 设杠杆(worst 名义/pick/set/fail-open/不缓存) → Task 3 ✓
- §4 可观测(每格 [leverage] 日志/不可行 WARN) → Task 3 Step 3 ✓
- §5 测试(纯函数各分支/适配器/open fail-open/golden 守卫) → 全覆盖 ✓
- §6 非目标(块 D 暂缓/回测无关) → 计划无块 D、Task 3 验回测零变化 ✓

**2. Placeholder scan:** 无 TBD/TODO;每步含完整可抄写代码与确切命令/预期。✓

**3. Type consistency:** `fetch_leverage_tiers(symbol)->list[{'maxLeverage':int,'maxNotional':float}]` Task 1 产出、Task 2/3 消费一致;`pick_leverage(worst_notional,tiers,gearing)->int|None`、`feasible(...)->bool` 签名前后一致;`_leverage_calls`/`seed_leverage_tiers` Task 1 定义、Task 3 断言一致;`self.gearing`/`order_num`/`entry` 均对 open() 实体核准存在。✓

**已知非目标(spec 明确,不做):** 块 D(选币可行性排除/回填、maxQty超低价币准入)、降 cap 救不可行币、set_leverage 缓存、mainnet prod。
