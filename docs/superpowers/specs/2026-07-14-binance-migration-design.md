# 币安根本性迁移(USDT-M 永续 + Vision 全历史回测) 设计

> 状态:**已获用户批准(2026-07-14)**。四项用户决策:①市场=币安 USDT-M 永续;②形态=彻底替换,
> 删除 HL/OKX 适配器与 Reservoir 依赖;③回测深度=全历史(2019+);④数据库=同库延续(HL 历史留档)。
> 路线=A(适配器置换+Vision 归档数据层),另按用户要求**预留 WebSocket 数据接入接缝**(见 §四)。
> 实现前如遇本文未覆盖的分歧点,不确定就问,勿猜。
> **2026-07-14 追加**:部署拓扑改为全新独立环境(gridtrade-bi-test/gridtrade-bi-prod,用户定)——
> 本文 ④同库延续 与 §7.2 阶段 2 的上线耦合语义被取代,见
> 2026-07-14-binance-standalone-environments-design.md。

## 一、背景与决策

当前生产:Hyperliquid mainnet 永续中性网格(fly app `gridtrade-prod`,东京 nrt),OKX 适配器共存,
全部经 ccxt 统一接口。回测数据层深度绑定 HL:1h 选币线硬编码 `ccxt.hyperliquid`
(backtest_run.py::_hl_datasource_1h),1m 仿真线走 Reservoir S3 requester-pays 归档
(backtest/reservoir.py,HL 专属,因 HL 公共端点只留近 ~5000 根 1m)。

迁移动机:币安 USDT-M 流动性/币种覆盖/数据可得性全面优于 HL;官方公开归档
`data.binance.vision` 免费提供 2019 至今全量 K 线与资金费(含已退市合约),回测数据层反而变简单。

**架构前提(勘探已核实)**:`core/`(纯函数策略)、`execution/`(执行/对账)、`state/`(表结构)、
`dashboard/`、`ResilientAdapter`、`DataSource`/`ParquetCache` 均只依赖 `ExchangeAdapter` 端口
(base.py)与缓存契约,不含交易所特有假设——迁移改动面收敛在适配层与回测取数层。

## 二、总览

**目标**:项目唯一对接币安 USDT-M 永续(`ccxt.binanceusdm`);回测全链路(选币回放+网格仿真)
跑在币安数据上,深度 2019 至今。

**不变量(明确不改)**:`core/` 全部、`execution/` 执行引擎与 reconciler、`state/` 表结构(同库
延续,HL 历史行留档可查)、`dashboard/`、`ResilientAdapter`/resilience 框架、`DataSource` 与
`ParquetCache` 的 (namespace, symbol, day) 契约、5s 轮询+账户级快照架构(spec
2026-07-06-account-snapshot-batch-reads)、`fake`/`faulty` 测试桩、golden parity 测试。

**非目标**:WebSocket 客户端实现(只留接缝,§四)、现货支持、双向持仓模式、Python/pandas 栈升级、
grid_order_info 档数自适应封顶(既有已知局限维持现状,见 fly.prod.toml 注释)。

## 三、实盘适配层

### 3.1 新增 `gridtrade/exchanges/binance.py`

`BinanceAdapter(CcxtAdapter)`,参照 okx.py 模式但覆写点更多:

- `name='binance'`;`quote_currency` 沿用 CcxtAdapter 类默认 `'USDT'`;`FUNDING_INTERVAL_HOURS=8`
  (信息性常量;实际记账走真实流水,部分币 4h/1h 周期不受影响)。
- `from_credentials(api_key, secret, *, testnet=False, proxies=None, timeout)`:构造
  `ccxt.binanceusdm({apiKey, secret, enableRateLimit, timeout})`;`testnet=True` 走
  `client.enable_demo_trading(True)`——币安期货 testnet 已弃用,官方替代=Demo Trading
  (demo-fapi.binance.com,key 在 demo.binance.com 生成;2026-07-14 冒烟实测修正,原
  set_sandbox_mode 对 futures 直接 NotSupported)。
- 符号映射:ccxt 统一符号即规范符号(`BTC/USDT:USDT`),继承 CcxtAdapter 恒等 to_native/to_canonical。
- `_include_market` 覆写:**`m['settle'] == self.quote_currency`**——币安 fapi 同时挂 USDT-M 与
  USDC-M 合约,不按结算币过滤会把 USDC 合约混入票池(ccxt_adapter.py:38 只过滤了 swap)。
- **账户级批量读覆写**(monitor 快照权重预算核心,契约见 §四):
  - `fetch_open_orders_all` → 无 symbol `GET /fapi/v1/openOrders`(权重 40,一次全账户);
  - `fetch_positions_all` → `positionRisk`(权重 5);
  - `fetch_prices_all` → 全市场 `ticker/price`(权重 2);
  - `fetch_funding_payments_all` → `GET /fapi/v1/income?incomeType=FUNDING_FEE`(账户级单次,
    按 symbol 字段打标聚合——币安按 symbol 正确打标,走通用语义,无 HL 那种 delta.coin 例外);
  - `fetch_my_trades_all` 维持基类逐 symbol 合成(userTrades 必带 symbol,仅活跃网格币,权重 5/币)。
  - 权重估算（终审修正）:12 格满仓每 5s 周期 40+5+2+30+12×5+5(fetch_balance)≈142,每分钟
    ~1700(原估算"~60+12×5≈1400"漏计 fetch_balance 且四项批量调用合计被低估),低于 fapi
    IP 上限 2400/min,且与全市场票池规模解耦(选币轮独立限速 SCHEDULER_FETCH_PACE_MS 已有)。
- **启动断言 `assert_account_mode()`**(factory 组装时调用一次):
  - 持仓模式必须单向(one-way,`positionSide/dual == false`),双向直接 RuntimeError——
    执行引擎/PositionLedger 全部按净仓语义工作;
  - 联合保证金(multi-assets mode)必须关闭(权益口径=单一 USDT);
  - `set_leverage` 覆写:设杠杆同时确保 CROSSED 全仓 margin type(幂等,吞 -4046 "No need to
    change margin type"),对齐 账户杠杆/gearing 仓位体系(spec 2026-07-07)的全仓假设。
- `fetch_ohlcv` 覆写:走原生 klines 端点取**真实 quote_volume**(§5.4)。
- `exchange_status`:币安 fapi 无期货维护状态公共端点,以 `fapiPublicGetPing` 判定——
  成功 'ok',异常 'maintenance'(廉价,权重 1)。
- `encode_cloid`:见 §5.1。
- `create_stop_order`:见 §5.2。

### 3.2 registry / config

- `registry.py::build_adapter`:分支只认 `binance` / `fake`,其余名字报错(错误信息列出支持项)。
- `config.py::DeployConfig`:凭证字段改 `api_key`/`api_secret`;env 键
  `BINANCE_API_KEY`/`BINANCE_API_SECRET`/`BINANCE_TESTNET`;`EXCHANGE` 默认 `'binance'`。
- **退役键守卫**(沿用 load_deploy_config 既有惯例,config.py:103-107):设置
  `HL_WALLET_ADDRESS`/`HL_PRIVATE_KEY`/`HL_TESTNET` 任一即 boot RuntimeError,提示改用新键,
  禁止静默失效。
- `QUOTE_CURRENCY` 覆写机制保留(registry.py:25-29,日后 USDC-M 之门)。

### 3.3 删除清单

`gridtrade/exchanges/hyperliquid.py`、`gridtrade/exchanges/okx.py`、`gridtrade/backtest/reservoir.py`
及对应测试;backtest_run.py 的 `BT_BUILDER_DEXES` 旋钮(HL builder-dex 专属);requirements 中仅为
Reservoir 引入的依赖;`.env.example`/fly toml 的 HL 键与注释。`legacy/` 目录不动(惰性历史档案,
不在运行时,其中恰有旧版币安取数代码可作实现参考)。文档(docs/*.md)中 HL 相关描述随实现顺手更新
涉及处,不做全量考古。

## 四、WebSocket 预留接缝(本次不实现,只留门)

**核心:monitor 的唯一读取口就是接缝本身。** monitor 每周期经六个快照方法读市场/账户状态:
`fetch_prices_all` / `fetch_positions_all` / `fetch_open_orders_all` / `fetch_my_trades_all` /
`fetch_funding_payments_all` / `fetch_balance`。本次交付三件事:

1. **契约文档化**:在 base.py 六方法 docstring 写明快照契约——返回语义(canonical symbol 键、
   排序、单位)、时效性要求(调用时刻的最新已知状态,不要求强一致)、幂等只读;
2. **不泄漏 REST 假设**:BinanceAdapter 实现不得让上层感知分页游标/权重/调用时序;
3. **契约测试守卫**:同一套契约用例同时跑 fake 适配器与 BinanceAdapter(mock client),
   未来 WS 镜像实现对着同一套用例开发。

**升级路径(零上层改动)**:`WsFeedAdapter(BinanceAdapter)` 子类/装饰器——后台线程跑 WS 客户端
(user-data-stream:`ORDER_TRADE_UPDATE`/`ACCOUNT_UPDATE`;市场流:`bookTicker`/`markPrice@arr`),
内存维护实时镜像,覆写快照方法从镜像返回,断流/镜像失效回退 REST。组合顺序
`ResilientAdapter(WsFeedAdapter(BinanceAdapter))`,monitor/executor/reconciler 一行不改。
技术路径:ccxt 4.x 自带 pro(`watch_*`),无新增依赖(独立线程内跑 event loop,不动同步架构)。

**回测/数据侧同理**:ParquetCache 的 namespace 是开放集合,未来 WS 录制器(逐笔 aggTrades、
盘口快照)作为新数据生产者写新 namespace,DataSource/回测引擎无感。

**明确不做**:不实现 WS 客户端,不加空转配置键(如 MARKET_FEED)——避免死配置。

## 五、关键行为适配点(五个坑)

### 5.1 client order id

内部格式不变:`'{grid_id}:{line}:{seq}'` 及 `'{gid}:fuse:low|high'`/`'{gid}:close:{n}'`
(grid_executor.py:3,DB 键+对账依赖,绝不动)。币安 futures `newClientOrderId` 官方正则
`^[\.A-Z\:/a-z0-9_-]{1,36}$` 含 `:` 与 `.`,理论上恒等透传即可;`encode_cloid` 实现为
**校验+必要时确定性字符替换**(以 testnet 实测为准;若 `:` 被拒则映射 `:`→`-`,注入性保持)。
长度:gid 6 位+line 2 位+seq 3 位 ≈ 13 字符,远低于 36 上限,加断言防越界。

**风险已收敛**:成交→网格线映射走 exchange order id 而非 cloid(grid_executor.py:205、
reconciler.py:55 已核实"跨所通用"),编码是单向的,无需解码器;唯一依赖 cloid 的是交易所端
重复单去重,确定性注入编码保持该性质。
**2026-07-14 demo 实测通过**:cloid `999999:1:1` 含冒号直传,交易所原样回读——冒号合法性成立。
**2026-07-14 testnet 部署实证修正——长度模型错误**:grid_id 实为 **32-hex uuid**(本节原按
"gid 6 位整数"建模长度是错的):`{gid}:0:0` 恰 36 字符压线通过、`{gid}:fuse:low` 41 字符触
越界断言 → 保险丝下单失败、格卡 OPENING(SKL 首格实证)。修复:encode_cloid 压缩 gid 段到
前 12 hex(16^12≈2.8e14 同账户碰撞可忽略,确定性单向;成交/对账走 exchange order id 无回读
依赖),全格式 ≤22 字符。越界断言保留(fail-loud 正是本次抓到问题的功臣)。

### 5.2 止损保险丝语义差

币安 STOP_MARKET 触发后纯市价,**无 HL 的 slippage 成交底线参数**。`create_stop_order` 覆写:
ccxt `create_order(..., 'market', ..., {'stopLossPrice': trigger, 'reduceOnly': True})` →
STOP_MARKET;`slippage` 参数接受但忽略(接口签名不变)。语义差:灾难场景成交价无下限保护——
接受(灾难优先离场,软止损仍是主刹车),写入 runbook 与 .env.example 注释。workingType 用默认
CONTRACT_PRICE(最新价),与 HL 触发语义最接近。
**2026-07-14 demo 实测追加——algo 独立订单簿**:币安 USDT-M 已把触发单放入独立 algo 簿
(ccxt 4.5.61 stopLossPrice → fapiPrivatePostAlgoOrder,返回 algoId 独立号段)。三点适配已入
BinanceAdapter:①cancel_order 常规 -2011 后走 trigger 回退;②fetch_open_orders(_all) 两簿并读
(不并读→对账器误判保险丝丢失反复重挂,HL 孤儿触发单事故同型);③cancel_all 两簿齐清(防残留
丝关格后触发)。账户级挂单快照权重 40→80,5s 轮预算 ~2180/min(仍低于 2400;见 §3.1 修正)。

### 5.3 按币种最小名义额

币安各币 MIN_NOTIONAL 不同(多数 5 USDT,部分 20/100)。改造:

- `Instrument` dataclass(base.py:18)增加 `min_cost: float = 0.0`,CcxtAdapter.list_instruments
  从 `m['limits']['cost']['min']` 填充(缺失=0.0 fail-open);
- `MinNotionalGate`(gates.py:106)升级:每笔挂单名义额下限 = `max(全局 env MIN_ORDER_NOTIONAL,
  该币 Instrument.min_cost)`;env 语义从"HL 全市场 $10"变为"全局保底",fly.prod.toml 注释同步。

不做档数自适应封顶(非目标);高名义额币若够不着下限,门链拒绝=安全失败,现状语义。

### 5.4 quote_volume 诚实化

现状:CcxtAdapter.fetch_ohlcv 用 `(open+close)/2×vol` 估算报价成交额(ccxt_adapter.py:90-95,
HL 拿不到真值的 legacy 文档化回退)。币安原生 klines 第 8 列自带真实 quote_volume:

- **实盘**:BinanceAdapter 覆写 fetch_ohlcv 走原生 `fapiPublicGetKlines`(分页语义与基类一致,
  真实 quote_volume,volCcy=vol);
- **回测**:Vision 归档 CSV 同列直取;
- 两侧同为真值 → 选币因子(Vwapbias/MarketPl,依赖 vwap=quote_volume/volCcy)实盘-回测同分布。

副作用:因子输入相对 HL 时代分布漂移——由全历史回测重验参数吸收(本次目标之一,§八验收②)。

### 5.5 tier0 硬禁名单迁移

`DEFAULT_TIER_POLICY.tier0`(config.py:156)9 币字面量带 `/USDC:USDC` 后缀(core 视 symbol 为
不透明字符串,base.py:5),需一次性人工映射为币安 USDT 后缀并核对在市性:BTC/ETH 直改后缀;
HL `k` 前缀千倍币(KNEIRO)对应币安 `1000` 前缀体系,逐个查 fapi 在市名单定名;币安不在市的
名单项保留无害(黑名单 fail-safe,resolve_live_universe 剔除语义)。回测侧 tier 名单同源同改
(单一事实源,config.py 注释既有约定)。

## 六、回测数据层(Vision 归档)

### 6.1 新增 `gridtrade/backtest/vision.py`

币安官方公开归档批量取数器,填充**现有 ParquetCache**,回测引擎/DataSource 对来路零感知:

- 源:`https://data.binance.vision/data/futures/um/{monthly,daily}/klines/{SYMBOL}/{1m,1h}/
  {SYMBOL}-{tf}-{YYYY-MM[-DD]}.zip` 与 `.../fundingRate/...`;免费无鉴权,2019-09(fapi 上线)
  至今,**含已退市合约**;
- 流程:月度 zip 为主(当月尾部用日度)→ 校验 `.CHECKSUM`(sha256)→ 解析 CSV → 转换为
  `CANDLE_COLS`/`FUNDING_COLS` schema(open_time→candle_begin_time UTC、真实 quote_volume 直取、
  volCcy=volume)→ 按天切片写 ParquetCache(namespace `1m`/`1h`/`funding`,空哨兵/原子写沿用
  cache.py 契约);
- 线程池并行下载,幂等(exists 即跳过,与 DataSource._warm 同约定);月度文件不存在时(退市月/
  未来月)按日度回退再落空哨兵;
- **符号目录**:从归档 s3 风格 XML 列表(`?prefix=data/futures/um/monthly/klines/`)枚举全部
  历史合约(含退市)→ 回测票池来源,**无幸存者偏差**;canonical 化 = `{BASE}/USDT:USDT`
  (按 quote_currency 参数);
- 独立 CLI:`python -m gridtrade.backtest.vision_sync <start> <end> [--tf 1m,1h,funding]
  [--symbols ...]`,供预热/补拉/修复。

### 6.2 backtest_run 接缝替换

- `_hl_datasource_1h`(backtest_run.py:385)→ `_binance_datasource_1h`:公共
  `ccxt.binanceusdm`(无需 key)+ 既有 `_Retry` 包装模式;**尾部增量**(归档 1-2 天滞后)由
  DataSource._warm 原有缺失天逻辑经 adapter.fetch_ohlcv 补齐——两条路汇入同一缓存;
- `prewarm_1h`:先 vision 批量预热窗口所需天,再走 DataSource 兜底(网络端点只补尾部);
- `prewarm_sim_and_funding`(backtest_run.py:428):Reservoir 分支删除,1m 改 vision 预热
  (仅选中币,沿用两阶段分层设计:1h 全市场、1m 只拉选中币);funding 走 vision + API 尾补;
- 缓存根目录 `data/hl_validate/` → `data/binance/`(env BT_DATA_DIR 既有则沿用其机制);
- 选币/仿真/聚合/并行/缓存复用逻辑一行不动。

### 6.3 数据体量

全历史 1h 全市场(~500 合约含退市)≈ 数 GB;1m 仅选中币按窗口拉,全历史全币 1m 上限数十 GB——
分层预热天然控制实际落盘量。磁盘不足时按窗口分段回测(现有窗口参数已支持)。

## 七、配置、部署与切换 Runbook

### 7.1 env 变更(`.env.example` / `deploy/fly.toml` / `deploy/fly.prod.toml` 同步)

- `EXCHANGE=binance`;新增 `BINANCE_API_KEY`/`BINANCE_API_SECRET`(secrets)/`BINANCE_TESTNET`;
- 删除 `HL_*` 键(守卫报错兜底);`QUOTE_CURRENCY` 保留(默认空=USDT);
- `MIN_ORDER_NOTIONAL` 注释更新为"全局保底,与按币 min_cost 取 max"(§5.3);
- 部署区域 nrt(东京)不变——币安 API 可直连,无地域封锁问题。

### 7.2 切换步骤(顺序执行,同库延续)

1. 代码合入 main,CI 全绿(测试全离线:fake 适配器+SQLite,不依赖真实交易所);
2. **testnet 验证**:fly testnet app 切 `EXCHANGE=binance` + `BINANCE_TESTNET=true`,跑 ≥3 天:
   下单/撤单/cloid 实测(§5.1)/成交映射/补单/部分成交生命周期/对账自愈/保险丝挂撤/面板全链路;
3. **HL 生产有序退场**:控制面暂停 scheduler 开新格 → 随 12H 换仓自然关格或经 /controls 手动
   关格 → **前置校验:DB 无 OPEN/OPENING 网格**(runbook 附 SQL:
   `SELECT id,symbol,status FROM grids WHERE status NOT IN ('CLOSED', 'FAILED');` 须空;
   FAILED 为无害终态——2026-07-14 评审修正)→ HL 提资;
   此校验是硬门槛——残留 open 网格会让 monitor 拿币安适配器管 HL symbol,必然报错;
4. **生产切换**:fly secrets 换币安 key → env 切 binance → 部署(SCHEDULER_RUN_ON_START=false
   既有保护)→ 小资金试跑(临时调低 TOTAL_BUDGET/MAX_CONCURRENT)→ 观察数个换仓周期后恢复;
5. HL 历史行留库可查,面板盈亏曲线跨所连续(fills 直算口径与交易所无关)。

### 7.3 API key 安全

只开期货交易权限、**禁提现**;Fly 出口 IP 非静态,默认不做 IP 白名单(runbook 注明:如启用
Fly static egress 可再加白名单收紧)。

## 八、测试与验收

- **单元**:`tests/exchanges/test_binance.py`(mock ccxt client,既有 tests/exchanges 风格):
  settle 过滤/批量读六方法/精度量化/账户模式断言(双向持仓拒绝)/STOP_MARKET 参数/encode_cloid
  合法化与注入性/真实 quote_volume;`tests/backtest/test_vision.py`:fixture zip 解析、CHECKSUM、
  schema 转换、按天切片、空哨兵、退市月回退;MinNotionalGate 按币下限用例;
- **契约守卫**(§四):快照六方法契约用例,fake 与 BinanceAdapter(mock)共用;
- **golden parity**:既有引擎 golden 测试不动,确认逐位复现(引擎交易所无关);
- **testnet 冒烟**:`scripts/` 增端到端脚本(开小网格→触发成交→补单→关格→记录核对);
- **验收标准**:① testnet 全链路无人工干预 ≥3 天;② 全历史回测(2019→今,1h 选币+1m 仿真)
  在币安数据上完整跑完出报告;③ CI 全绿;④ 生产小资金 ≥1 个换仓周期,无 429/418、无 stuck
  OPENING;⑤ 快照契约测试守卫在位。

## 九、风险与已知语义差异

| 风险/差异 | 处置 |
|---|---|
| 保险丝无滑点底线(弱于 HL) | 接受并文档化;软止损仍是主刹车(§5.2) |
| quote_volume/数据分布漂移 → 因子行为变化 | 全历史回测重验参数后再上生产(§5.4,验收②) |
| 部分币 funding 周期 4h/1h 非固定 8h | 记账走真实流水(income/归档),常量仅信息性 |
| 429/418 IP 封禁 | ResilientAdapter 熔断已有;resilience 补币安错误分类,418/-1003 长冷却 |
| cloid 字符集实测与文档不符 | encode_cloid 校验+替换兜底,testnet 步骤前置实测(§5.1) |
| 全历史数据体量(数十 GB 上限) | 分层预热(1h 全市场/1m 仅选中币)+ 窗口分段(§6.3) |
| 残留 HL open 网格撞新适配器 | 切换前置校验硬门槛(§7.2 步骤 3) |
| 币安 fapi USDC 合约混入票池 | `_include_market` 按 settle 过滤(§3.1)+ 单测守卫 |
