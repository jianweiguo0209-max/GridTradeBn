# 候选币池移植设计（§1 去重 + §6 档0 无条件 + 绝对流动性地板 + prod 去白名单）

> 日期：2026-07-04　状态：已实现（代码合入前，待 testnet 验证）
> 目标：把本系统候选票池补齐到 legacy/文档 §1/§6 口径，并把 **prod 从白名单切到全市场动态**（更贴 OKX legacy）。只动票池构建层，不碰 offset/选币因子/SymbolLock/记账。

## 1. 背景

当前票池链路（3 层）与缺口：
- **全量**：`CcxtAdapter.list_instruments` 遍历**全部 markets**（spot+swap+重复），HL `to_canonical` 把它们折成 `BASE/USDC:USDC` → 白名单 26 → universe 56 重复、且 spot 混入（STATUS 记录）。**无 swap 过滤、无去重**（文档 §1 要求）。
- **收窄**：`resolve_live_universe` 在 **whitelist 模式下完全跳过 blacklist**（[universe.py:8-14](../../gridtrade/runtime/universe.py)）→ 生产的 `BLACKLIST_SYMBOLS` **对现 prod 完全无效**（文档 §6 档0「无条件永不参与」没落地）。
- **选币阶段已有**：`select_grid_coin` 里 `交易额分位占比 <= 0.55`（成交额前 55% 相对过滤）+ 极端崩盘过滤 + 因子排名 → 已实现，无需改。

## 2. 已确认决策

| 决策点 | 选择 |
|---|---|
| prod 票池广度 | **去 `UNIVERSE_WHITELIST`，走全市场动态**（贴 legacy） |
| §1 | swap 过滤 + canonical 去重（全市场必须干净永续） |
| §6 | **只移档0**（硬禁名单，无条件生效）；档1/档2 不移——SymbolLockGate「每币 ≤1 网格」已更严覆盖 |
| 绝对流动性地板 | **新增**：24h 成交额 ≥ **$1M**（默认，可配，0=停用） |
| 地板数据源 | ccxt `fetch_tickers().quoteVolume`（交易所无关；HL 实测 = `dayNtlVlm` 直传、精确） |

**$1M 定标依据（HL mainnet 实拉，176 非退市永续）**：$1M → 剩 60 个候选（cut 66% 低流动性尾部）；Top26 里成交额最低 DYDX $2.06M，$1M 远低于它 → **不误伤现有币**；60 个过 55% 相对过滤 ≈ 33 → 因子取 top1，多样性充足。

## 3. 数据流（改后）

```
全部 live 永续  −canonical 去重  −黑名单(档0,无条件)  −24h成交额<$1M
   → fetch 每币 1h K线
   → 选币: 去NaN因子 → 成交额前55%(相对) → 极端崩盘过滤 → 因子加权排名 → top choose_symbols
```

## 4. 设计

### ① §1 — swap 过滤 + canonical 去重（`CcxtAdapter.list_instruments`）
- 遍历 `client.markets` 时**只收永续**：`m.get('swap') is True`（spot/其它类型丢弃）。
- **按 canonical 去重**：同一 `to_canonical(sym)` 只保留一个（keep first）。
- `Instrument` schema（symbol/tick/lot/min_size/state/list_ts）不变。
- 效果：universe = 去重后的永续集；白名单场景 26→26；全市场场景 = 干净的全部永续。

### ② §6 档0 — 黑名单无条件生效（`resolve_live_universe`）
现状（bug）：whitelist 分支不减 blacklist。改为**先无条件减 blacklist，再套 whitelist（若有）**：
```python
def resolve_live_universe(adapter, blacklist=(), whitelist=(), min_quote_volume=0.0):
    live = [i.symbol for i in adapter.list_instruments() if i.state == 'live']
    live = [s for s in live if s not in set(blacklist)]        # 档0：无条件硬禁
    if min_quote_volume and min_quote_volume > 0:              # ③ 绝对地板
        vol = adapter.fetch_24h_quote_volumes()               # {canonical: 24h quoteVolume}
        if vol:                                               # 空(无数据/未实现)→fail-open跳过、不清空票池
            live = [s for s in live if (vol.get(s) or 0.0) >= min_quote_volume]
    if whitelist:
        return [s for s in live if s in set(whitelist)]
    return live
```
- 缺失/None 成交额 → 视为 0 → 被剔除（安全失败）。
- 具体禁哪些币是**策略/config**（`BLACKLIST_SYMBOLS` env）；文档那 25 个是 OKX 符号，HL 用自己的名单，机制到位即可。

### ③ 绝对流动性地板 — 新 adapter 方法 + config
- **config**：新增 `min_quote_volume_24h: float`（env `MIN_QUOTE_VOLUME_24H`，**code 默认 `0.0`=停用**——避免改变现有无该 env 部署（如 testnet，成交额是假的）的行为；**$1M 只在 `fly.prod.toml` 显式设**）。
- **adapter 接口**：`ExchangeAdapter.fetch_24h_quote_volumes() -> Dict[str, float]`（canonical symbol → 24h 计价币成交额）。
  - `CcxtAdapter` 实现：`client.fetch_tickers()` → 逐 ticker 取 `quoteVolume`，`to_canonical` 归一，同 canonical 取已见的最大值（去重时保守取活跃者）。
  - HL 无需覆写（ccxt 直传 `dayNtlVlm`，已验证）。
- **接线**：`scheduler.run_scheduler_once` 调 `resolve_live_universe(rt.adapter, rt.config.blacklist, rt.config.whitelist, rt.config.min_quote_volume_24h)`。

### ④ prod config（`deploy/fly.prod.toml`）
- **删** `UNIVERSE_WHITELIST`（切全市场）。
- **设** `MIN_QUOTE_VOLUME_24H = "1000000"`。
- **设** `BLACKLIST_SYMBOLS`（档0 HL 硬禁名单，内容由用户定；可留空=不禁）。

## 5. 测试

- **§1**（`tests/exchanges/`）：fake ccxt markets 含 spot + swap + 重复 canonical → 断言 `list_instruments` 只出去重后的 swap。
- **§6**（`tests/runtime/test_universe.py`）：whitelist + blacklist 同设 → 断言被禁币**即使在白名单里也被剔除**（复现"白名单模式黑名单失效"→RED→GREEN）。
- **③ 地板**：fake adapter 的 `fetch_24h_quote_volumes` 返回构造的 volume_map → 断言 `< 门槛`剔除、`None/缺失`剔除、`门槛=0`不调用该方法且全保留、`>=门槛`保留。
- **config**：`MIN_QUOTE_VOLUME_24H` code 默认 `0.0`（停用）+ env 覆写解析（prod 显式设 1e6）。
- `resolve_live_universe` 现有测试联动（新增 `min_quote_volume` 默认 0 保持向后兼容，旧测试不改）。
- 双 TZ（沿用现约定）全套绿。

## 6. 风险 / 注意

- **拉 K 线量**：universe 176→~60（$1M 地板），`fetch_universe_candles` 逐个拉的耗时下降，但仍比 26 多。评估 scheduler 单轮耗时 + HL 限频（坏币 try/except 已有）。若过长 → 后续可并发拉/缓存（**本次不做，留观察点**）。
- **exchange-agnostic**：`fetch_24h_quote_volumes` 用 ccxt `fetch_tickers`（OKX 亦支持 `quoteVolume`）；HL 已验证 = `dayNtlVlm` 直传。
- **上线**：先 **testnet 验证**（universe 变全市场 + 地板 + 黑名单生效 + scheduler 耗时），再 merge 进 production。testnet whitelist 可保留或同样切全市场验证。

## 7. 不在本次范围

- 不改 offset / 选币因子集与阈值 / SymbolLockGate / 记账。
- 不实现档1/档2（SymbolLockGate 已覆盖）。
- 不做并发拉 K 线（留观察点，若 scheduler 耗时超标再开）。
