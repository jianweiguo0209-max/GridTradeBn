# MarginGate 交易所 IM 口径 + MaxConcurrentGate 并发对齐 N

2026-07-18。mainnet 首日(gridtrade-bi-prod,LIVE_OPEN_OFFSETS=2,4)02:00 首开实证定案。

## 背景(实证)

- N=2 → frac=AL/(N×gearing/2)=5.0/3.4≈**1.47>1** → cap=equity×1.47 恒 > cash。
- 02:00 选中 MET → 旧 MarginGate「cash≥cap」判:cash $510.95 < cap $751.39 → **结构性永拒**
  (加钱无效:cap 随 equity 等比涨)。grids 表零记录、无任何下单;scheduler 健康(门拒非异常)。
- 根因:**cap 是 sizing 基数,不是保证金**。交易所真实锁定 = 初始保证金 IM,MET 实测仅 ~$128。

## 口径(用户定,2026-07-18)

```
required = k × ( 整梯双侧名义/L + worst止损浮亏 + 整梯名义×fee_rate )
门:      availableBalance − 本批已预留 ≥ required
```

- **整梯名义/L = 轨迹最大 IM**。币安 USDM 保证金**挂单时刻锁定**(open order IM,one-way
  净额按最坏侧;-2019 在下单时报)。沿「挂满梯→吃满最坏侧」轨迹,回售补单使卖侧净额不降,
  总 IM(挂单+仓位)单调升:MET $64.5(t₀)→$96.5(半程)→**$127.7(=整梯/L,吃满侧)**。
- **worst浮亏显式化**:库存×|成交均价−止损价|,两侧取大(MET ~$105,与 IM 同量级),
  同吃 availableBalance,不埋进 k。
- **L 同源预演**:pick_leverage(order_num×grid_count×entry, tiers, gearing)——与
  executor.open 同式;门链在 set_leverage 之前跑,必须模拟将要设的 L。
- k=1.25(env `MARGIN_GATE_K`,<1 boot 响亮报错),fee_rate=0.0005(VIP0 taker 上界,ε 项)。
- **fail-closed 分层**:①余额读不到→本批全拒(原语义);②IM 算不出(tiers 空/取数抛错/
  executor 缺失)→**回退旧 cash≥cap 保守口径**并留 fallback 日志——宁可误拒不误放。
- 语义变更自觉:MarginGate 从「资金背书门」变纯「交易所可行性门」;风险约束由
  AL/frac(sizing,N 格最坏总名义=AL×equity,MET 实证 2×1277=5.0×510 恰合)+
  止损/保险丝(损失界)承担。

## 连带:MaxConcurrentGate 上限 = eff_concurrency

frac 按 N 放大单格 cap 后,并发上限仍为 12 会冲破 AL(N=2 时 cap 已 6×)。
`DeployConfig.eff_concurrency = min(启用offset数×choose_symbols, max_concurrent)`(空集=
max_concurrent,零行为变更),factory 的 MaxConcurrentGate 改用之——成为方案B 的真兜底。

## 实现

- `gridtrade/execution/margin_policy.py`(新):`ladder_margin_required(...)→(required,
  breakdown)|None` 纯函数;None=无法计算,调用方回退。
- `gates.py MarginGate`:新口径 + fallback;放行/拒绝都留 IM breakdown 日志(每小时数条)。
- `config.py`:`eff_concurrency`/`margin_gate_k` 字段 + `MARGIN_GATE_K` 解析(<1 fail-fast)。
- `factory.py`:MaxConcurrentGate(eff_concurrency) + MarginGate(k=margin_gate_k)。

## 测试(TDD,全量 999 passed/2 skip)

- `test_margin_policy.py`:干净数手算基准(low100/high400/count2→required=k×470.7)、
  k 缩放、entry 出带、tiers 空/建网 None→None、min_amount 截断。
- `test_margin_gate_im.py`:新旧口径分野双向证明(cash100≥cap70 旧过新拒;**MET 主网回归**
  cash510<cap751 旧拒新过 required≈$293)、批内累计预留、三类 fallback、放行留痕。
- 旧 `test_gates.py` 全数保留=回退口径回归(其桩无 tiers/price 能力,天然走 fallback)。
- config/factory:eff_concurrency 跟随启用集、k 透传、零行为变更护栏。

## 追加:票池杠杆过滤两侧同步(2026-07-18 当日)

- **实盘**:`UNIVERSE_MIN_LEVERAGE`(prod=10):scheduler 取数前剔 pick_L<阈值 币(04:00 MYX
  空转实证;05:00 实测 -44/286);tiers map 共享 fetch_max_leverages bulk 缓存;fail-open
  (MarginGate 兜底)。
- **回测**:`BT_MIN_LEVERAGE` **默认 10=实盘一致(用户定)**;`exclude_low_leverage` 复用同源
  eligible_min_leverage/normalize_tiers_map;**fail-loud**(回测无 MarginGate 兜底,静默跳过=
  静默背离,沿 exclude_non_coin 先例;tiers 私有端点需 env key;=0 显式停用回旧口径)。
  近似边界:当前档位快照(无历史档位)+ notional=回测 sizing 常量($3400,与实盘 $2555 同档界)。
  ⚠ 历史 sweep(候选A 等)均为未过滤票池所得,与新默认口径不可直接比;复现旧结果设
  BT_MIN_LEVERAGE=0。仅 canonical main 流程接线,scratchpad 自建票池的脚本不自动获得过滤。

## 非目标 / 后续

- 跨格同时最坏联合建模:靠每批实时 availableBalance 快照 + k 余量,不解析建模。
- demo 实证 openOrderInitialMargin 净额口径(挂对称梯逐状态读数):非阻塞——本口径按
  双侧总名义/L 计,已覆盖「净额=sum」的最坏解读;有空补做可再收紧 k。
- Balance.cash 映射已核:ccxt binance parseBalance `free=availableBalance`(:3755)。
