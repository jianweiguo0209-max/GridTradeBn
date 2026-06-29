# quote_volume 真实成交额映射（通用回退修复）— 设计

> 来源：P5 carry-forward（design.md §225 / `ccxt_adapter.py:77` 的 `TODO(P5)`）。
> 日期：2026-06-29。范围：方案 A（通用回退），OKX 精确 volCcyQuote 显式延后。

## 背景与缺陷

`CcxtAdapter.fetch_ohlcv` 当前用 `quote_volume = vol * close` 近似成交额。因子
`Vwapbias_signal`/`MarketPl`/归一成交额按 `vwap = quote_volume / volCcy` 计算，而
`volCcy = vol`，于是 `vwap = (vol*close)/vol = close` **恒等于收盘价**——vwap 因子塌陷，
在真实 ccxt 数据上失真（需求2 因子保真受损）。金标因子测试喂 account_0 fixture（带真实
列），不走此映射，故未暴露；缺陷只在实盘/ccxt 回测的取数路径。

## 已确认事实（ccxt 源码核对）

- ccxt 统一 `fetch_ohlcv` 仅 6 列 `[ts,o,h,l,c,vol]`，丢弃成交额字段。
- **OKX 永续**：`parse_ohlcv` 的 `volumeIndex=6` → 统一 `vol` = **真实 base 成交量**（coin 量，
  legacy 的 volCcy）。真实报价成交额 `volCcyQuote`（raw 第 7 列）被丢弃。
- **Hyperliquid**：统一 `vol` = candle 字段 `v` = **真实 base 成交量**。HL **无任何报价成交额字段**。
- 故 `volCcy = vol` **本就正确**，无需改；唯一缺陷是 `quote_volume` 的公式。
- legacy 文档化回退公式即 `quote_volume = (open+close)/2 * volCcy`（`account_0/utils/stop_loss.py:280`
  实际在用）。

## 改动

单文件 `gridtrade/exchanges/ccxt_adapter.py::fetch_ohlcv`：

- 将 `df['quote_volume'] = df['vol'] * df['close']`
  改为 `df['quote_volume'] = (df['open'] + df['close']) / 2.0 * df['vol']`。
- `df['volCcy'] = df['vol']` 保持不变。
- 替换 `TODO(P5)` 注释：说明此为 legacy 文档化回退；OKX 真实 `volCcyQuote` 经 ccxt 统一接口
  取不到、HL 无此字段，故 `(o+c)/2*vol` 为最优可得近似（已知限制）。

## 为何正确

`vwap = quote_volume / volCcy = (open+close)/2`，不再恒等于 close → Vwapbias/MarketPl/
归一成交额因子在真实数据上恢复有效。与 legacy 回退口径一致。

## 测试

- 改 `tests/exchanges/test_ccxt_adapter.py` 中任何断言 `quote_volume == vol*close` 的用例。
- 新增用例：桩 ccxt client 返回 `open != close` 的 K 线，断言
  `quote_volume == (open+close)/2 * vol`、`volCcy == vol`、且 `quote_volume/volCcy != close`
  （证明 vwap 未塌陷）。
- 跑全套确认金标零回归（因子金标走 fixture，不走本映射）。

## 范围外（YAGNI / 延后）

- OKX 原始 candles 端点取精确 `volCcyQuote`（方案 B）——HL 实盘本无此字段，需要时再单独做。
- HL 报价成交额（不存在，无法实现）。
