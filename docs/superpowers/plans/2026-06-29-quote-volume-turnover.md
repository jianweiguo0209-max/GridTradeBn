# quote_volume 真实成交额映射（通用回退）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `CcxtAdapter.fetch_ohlcv` 的 `quote_volume` 从 `vol*close` 改为 legacy 文档化回退 `(open+close)/2*vol`，消除 vwap 塌陷、恢复 Vwapbias/MarketPl 等因子在真实 ccxt 数据上的有效性。

**Architecture:** 单文件单行逻辑改动 + 一个回归测试。`volCcy = vol` 不变（已确认 ccxt 统一 vol 对 OKX 永续/HL 都是真实 base 成交量）。

**Tech Stack:** Python 3.9 / pandas / pytest。

## Global Constraints

- 跑测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（仓库 `.venv`）。
- 现有 273 测试保持全绿；因子金标喂 account_0 fixture、不走本映射，无回归风险（跑全套确认）。
- `gridtrade/exchanges/` 可 import ccxt；`core/` 不受影响。
- 回退公式取 `(open+close)/2 * vol`，与 legacy `account_0/utils/stop_loss.py:280` 一致。

---

### Task 1: quote_volume 改用 (open+close)/2*vol

**Files:**
- Modify: `gridtrade/exchanges/ccxt_adapter.py:77-81`（`fetch_ohlcv` 末尾的 volCcy/quote_volume 赋值块）
- Test: `tests/exchanges/test_ccxt_adapter.py`（在既有 `test_fetch_ohlcv_maps_to_candle_cols` 之后新增一个测试；`FakeCcxtClient.fetch_ohlcv` 已返回 open≠close 的两行，可直接复用）

**Interfaces:**
- Consumes: 既有 `FakeCcxtClient`（stub）与 `_adapter()` 工厂（同文件，已定义）。stub 的 OHLCV 行为 `[ts, open, high, low, close, vol]`：行0 `[...,1.0,2.0,0.5,1.5,10.0]`、行1 `[...,1.5,2.5,1.0,2.0,20.0]`。
- Produces: 无（叶子改动）。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_ccxt_adapter.py` 末尾追加：

```python
def test_fetch_ohlcv_quote_volume_uses_midprice_not_close():
    # quote_volume = (open+close)/2 * vol（legacy 文档化回退），volCcy = vol。
    # 关键：vwap = quote_volume/volCcy = (open+close)/2，不得塌成 close（否则 Vwapbias 失真）。
    df = _adapter().fetch_ohlcv('BTC/USDT:USDT', '1H', 0, 10**13)
    # 行0: open=1.0 close=1.5 vol=10 -> (1.0+1.5)/2*10 = 12.5 ；行1: (1.5+2.0)/2*20 = 35.0
    assert df['quote_volume'].tolist() == [12.5, 35.0]
    assert df['volCcy'].tolist() == [10.0, 20.0]
    vwap = (df['quote_volume'] / df['volCcy']).tolist()
    assert vwap == [1.25, 1.75]
    assert vwap != df['close'].tolist()        # vwap 未塌成 close
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py::test_fetch_ohlcv_quote_volume_uses_midprice_not_close -q`
Expected: FAIL —— 当前 `quote_volume = vol*close` 给 `[15.0, 40.0]`（断言期望 `[12.5, 35.0]`）。

- [ ] **Step 3: 改实现**

在 `gridtrade/exchanges/ccxt_adapter.py` 的 `fetch_ohlcv` 中，把这一段：

```python
        # TODO(P5): quote_volume=vol*close makes vwap=quote_volume/volCcy collapse to close,
        # degrading Vwapbias/MarketPl on real data. P5 datasource must map quote_volume from
        # the exchange's true turnover field (OKX volCcyQuote / HL turnover); vol*close is a fallback only.
        df['volCcy'] = df['vol']
        df['quote_volume'] = df['vol'] * df['close']
```

替换为：

```python
        # ccxt 统一 vol 即真实 base 成交量（OKX 永续 volumeIndex=6 / HL 字段 v），故 volCcy=vol 正确。
        # 报价成交额：OKX 真实 volCcyQuote 经 ccxt 统一接口取不到、HL 无此字段，故用 legacy 文档化
        # 回退 (open+close)/2*vol（见 account_0/utils/stop_loss.py:280）。这样 vwap=quote_volume/volCcy
        # =(open+close)/2 不再恒等于 close，Vwapbias/MarketPl 因子在真实数据上保持有效。
        df['volCcy'] = df['vol']
        df['quote_volume'] = (df['open'] + df['close']) / 2.0 * df['vol']
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py -q`
Expected: PASS（含既有 `test_fetch_ohlcv_maps_to_candle_cols` 不受影响）。

- [ ] **Step 5: 跑全套确认无回归（尤其金标）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（≥274 passed）。若任何因子金标用例变红，说明有走映射的非预期路径——停下汇报，勿改金标。

- [ ] **Step 6: 提交**

```bash
git add gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_ccxt_adapter.py
git commit -m "fix(exchanges): quote_volume uses (open+close)/2*vol to un-collapse vwap (P5 carry-forward)"
```

---

## 自检（Self-Review）

- **Spec 覆盖**：spec「改动」三点 → Step 3（公式 + volCcy 不变 + 注释替换）；spec「测试」三点 → Step 1（quote_volume/volCcy/vwap≠close 断言）+ Step 5（金标零回归）。覆盖完整。
- **占位符**：无 TBD/TODO；每步含完整代码/命令/预期值。
- **类型/数值一致**：stub 行值 → 断言值 `[12.5,35.0]`/`[10.0,20.0]`/`[1.25,1.75]` 由公式精确推出，自洽。
- **范围外**：OKX 原始 volCcyQuote（方案 B）未触碰，符合 spec 延后。
