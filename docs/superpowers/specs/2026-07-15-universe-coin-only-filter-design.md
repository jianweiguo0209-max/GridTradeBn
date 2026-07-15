# 票池 COIN-only 过滤（剔除 TradFi 代币化永续） 设计

> 状态:**已获用户批准(2026-07-15)**。三项用户决策:①过滤放 `BinanceAdapter._include_market`
> 适配器层(TradFi 处处隐形);②回测同口径用当前 exchangeInfo COIN 集交(零维护);③品类判定用
> **白名单 `underlyingType=='COIN'`**(实测 659 个 swap+USDT+active 全带该字段、0 缺失,白/黑名单
> 结果相同;白名单更稳健——未来新 TradFi 类型自动被挡)。**mainnet 上线一票否决级 blocker**,
> 作为 docs/币安切换runbook.md 阶段 3 生产切换前置门槛。实现前如遇本文未覆盖的分歧点,不确定
> 就问,勿猜。

## 一、问题与实证(2026-07-15 本地 ccxt 拉 mainnet 生产 exchangeInfo,独立复现)

选币池当前只按「永续 + settle=USDT + active」筛,不看标的品类。币安 mainnet 已上线大量 TradFi
代币化永续(美股/韩股/大宗商品/Pre-IPO),它们 `settle=USDT`、`swap=True`、`active=True`,照常进
候选池,再经「24h 成交额前 55%」相对口径筛选——而这些标的成交额很高、排名靠前,会被优先选中开
网格。

**实证(独立复现,与初报吻合)**:候选池(swap+USDT+active)=659;前 55% 票池=363;票池中非 COIN
的 TradFi **81 个(22%)**:EQUITY 68、COMMODITY 8、KR_EQUITY 3、INDEX 1、PREMARKET 1。成交额前 15
名里 **10 个是 TradFi**:#3 SKHYNIX(韩股)、#4 SNDK、#5 XAU(黄金)、#6 SOXL(半导体3倍ETF)、#7 SKHY、
#9 MU、#10 CL(WTI原油)、#11 SPCX、#13 XAG(白银)、#15 BZ(布伦特)。

**为何对网格致命**:①股票/商品/股指非 7×24,收盘停盘、隔夜/周末跳空可能一次跳过整个网格区间,
直接打穿软止损、甚至跳过交易所保险丝触发价;②杠杆/反向 ETF(SOXL/TQQQ/SQQQ/UVXY)有日内再平衡
衰减,网格必被磨干。

**为何 testnet 测不到**:demo(Demo Trading)币池与 mainnet 不同;mainnet 的 TradFi 大量在 demo 缺
席,上真钱那刻才第一次遇到。**2026-07-15 更新**:demo 现已有 50 个 TradFi(28 个与 mainnet 重叠)
——部分可观测,但不能依赖 testnet 覆盖,验证靠单测 + mainnet 上线核对票池无 TradFi。

## 二、根因(代码)

- `resolve_live_universe`(runtime/universe.py:16)取 `adapter.list_instruments()` 中 state=='live' 的符号。
- `list_instruments`(ccxt_adapter.py:33-56)过滤只有:`swap is True` + `_include_market(m)` + active;无品类过滤。
- `BinanceAdapter._include_market`(binance.py:25)仅 `return m.get('settle') == self.quote_currency`(只防 USDC 混入)。
- 品类字段在 ccxt `market['info']['underlyingType']`:COIN / EQUITY / KR_EQUITY / COMMODITY / INDEX / PREMARKET。

## 三、总览

**目标**:自动选币票池只含 COIN(加密)永续,TradFi 代币化永续处处隐形;回测同口径;mainnet
生效、testnet 为 no-op(demo 部分可观测)。

**不变量(明确不改)**:`core/`(纯函数策略)、`execution/`、回测几何(golden/core 逐位不变)、
现货/结算币过滤既有语义、tier 名单、grid 参数。

**非目标**:手动 OPEN_GRID 硬拦(见 §4.4)、TradFi 专用网格策略、demo 品类字段修复。

## 四、组件

### 4.1 单一事实源:品类判定谓词(binance.py 模块级)

```python
def is_coin_market(m) -> bool:
    """币安 market 是否为 COIN(加密)永续。TradFi 代币化永续(EQUITY/KR_EQUITY/COMMODITY/
    INDEX/PREMARKET)非 7×24、隔夜跳空打穿网格,一律剔除。白名单口径(fail-closed):
    underlyingType 缺失/未知也排除(安全优先;实测 659 个全带该字段,0 缺失)。"""
    return ((m.get('info') or {}).get('underlyingType')) == 'COIN'
```

实盘 `_include_market` 与回测票池解析**都调这一个谓词**——字面同口径,不会漂。

### 4.2 实盘过滤(`BinanceAdapter._include_market`)

`return m.get('settle') == self.quote_currency` → 增加 `and is_coin_market(m)`。这一处同时覆盖:
- `list_instruments`→`resolve_live_universe`(自动选币票池);
- `_id_map`(binance.py:196,账户级批量读 native id→canonical 映射,复用 `_include_market`)。

**连带语义(spec 明确)**:`_id_map` 加 COIN 过滤后不含 TradFi。mainnet 自动选币不会开 TradFi 仓
→ 正常无影响。但手动 `OPEN_GRID`(commands.py:24,直调 `ex.open`、**不经** `list_instruments`/
`_include_market`)若有人手动开 TradFi 仓,其账户快照映射(fetch_positions_all 等)会漏该仓——半碎
(能下单、快照不可见)。**这是有意的取舍(用户定)**:作为对"手动玩 TradFi"的隐性劝阻,不当 bug。

### 4.3 回测同口径(`backtest_run.py` 票池解析)

现(backtest_run.py:550)`universe = sorted(set(V.list_archive_symbols()) - set(bt_blacklist))`——
`list_archive_symbols` 返回 `canonical_of(native)` = 全 canonical `'BASE/USDT:USDT'`(vision.py:154、
canonical_of:37),**无品类字段**。

改:用**同一 `is_coin_market`** 从 `_adapter.client.markets` 算出「当前非 COIN 符号集」,从票池剔除。
**形态匹配已核**:ccxt binanceusdm 的 `m['symbol']` 与 `canonical_of` 同形(`'SOXL/USDT:USDT'`),
`to_canonical` 恒等透传(ccxt_adapter.py:25)→ 集合相减口径一致。

```python
_adapter.client.load_markets()          # 幂等;ccxt 缓存,紧随 prewarm 复用同缓存(全程一次 exchangeInfo)
non_coin = {_adapter.to_canonical(m['symbol']) for m in _adapter.client.markets.values()
            if m.get('swap') and m.get('settle') == _adapter.quote_currency and not is_coin_market(m)}
universe = sorted(set(V.list_archive_symbols()) - set(bt_blacklist) - non_coin)
```

**网络非额外**:票池解析(line 550)在 prewarm(552/557)之前,此刻 markets 可能未加载→显式
`load_markets()`;ccxt 缓存,后续 prewarm 复用,全程仅一次 exchangeInfo 拉取。**保留退市币**:
archive 全集**减** 当前非 COIN(而非交当前 COIN),退市 COIN 不在 non_coin 里→不被剔,无幸存者偏差。
**已知微小 gap**:已退市的 TradFi(在归档、不在当前 exchangeInfo)不在 non_coin→不会被剔——但
TradFi 全近期上市无退市,且历史回测窗多半无其归档数据,剔不剔无差;属可观测项,YAGNI 不解。

### 4.4 可观测性(fail-closed 配套护栏)

**分层洁净**:`list_instruments`(ccxt_adapter.py:33)是交易所无关通用层,`_include_market` 才是子类
专属谓词。故计数放通用层、措辞交易所无关——"我的 include 过滤拒了 N 个",不泄漏 COIN 概念。

- 实盘 `list_instruments`:统计通过 `swap` 但被 `_include_market` 剔除的合约数,一行日志(如
  `[universe] include 过滤剔除 N 个合约`,与既有选币轮审计同渠道)。币安下此数 ≈ 非 COIN TradFi
  (+ 少量 USDC-M 结算)。作用:underlyingType 字段格式若变、白名单误杀一片,此数跳升即可见。
- 回测票池解析:log `剔除 N 个非 COIN`(= archive ∩ non_coin 的量)。此处已在 binance 专属路径、
  直接用 COIN 措辞无分层顾虑。

**与既有护栏的关系**:白名单若因字段漂移误杀全部币→票池清空→选币轮审计报空池、monitor 无 active
——已是响亮失败;本计数补的是"部分漂移致票池缩水"这类不那么响亮的情形。

## 五、测试(TDD)

- **谓词单测**:`is_coin_market` 对各 underlyingType(COIN 真;EQUITY/KR_EQUITY/COMMODITY/INDEX/
  PREMARKET 假;缺失/None 假 fail-closed)。
- **`_include_market` + `list_instruments`**:mock client.markets 混入各品类 → 断言只有 COIN 进
  `list_instruments` 输出;USDC 结算 + 结算过滤既有语义不破。
- **`_id_map`**:mock 混品类 → 断言映射只含 COIN。
- **票池(resolve_live_universe)**:经过滤后的 list_instruments → 票池无 TradFi。
- **回测同口径**:mock client.markets(混品类)+ archive 符号集(含 COIN、TradFi、退市 COIN)→ 断言
  票池剔 TradFi、留 COIN(含退市)。
- **可观测性**:实盘——mock 混品类 markets,断言 `list_instruments` 打出 `_include_market` 剔除计数
  (捕获 stdout/日志);回测——断言票池解析打出非 COIN 剔除数。
- **golden/core parity**:回测几何逐位不变(本改动不碰 core/backtest 引擎,只碰票池解析)。
- **testnet 部署后核对项(非单测)**:demo 票池非空(demo 的 COIN 币 underlyingType 须='COIN',否则
  过滤误杀整个 demo 池;实测 demo 带该字段)——上 testnet 后确认 `resolve_live_universe` 返回非空。
- 全量 pytest 全绿;**不部署**(部署由主运维会话手动做;此改动 testnet 为 no-op、mainnet 上线生效)。

## 六、runbook 前置门槛 + 记忆

- 完成后在 docs/币安切换runbook.md 阶段 3 加前置勾选项:「票池 COIN-only 过滤已落地并核对
  mainnet 票池无 TradFi(underlyingType!='COIN' 应为 0)」。
- 会话记忆 `binance-migration-branch-status` 记一笔:runbook 阶段 3 前置门槛「票池 COIN-only」已落地。
