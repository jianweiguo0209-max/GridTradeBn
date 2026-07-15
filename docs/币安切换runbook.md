# 币安生产切换 Runbook（spec 2026-07-14 §7.2）

## 阶段 0：代码就绪
- [ ] main 分支 CI 全绿；本 runbook 前置 = 实施计划 Task 0-17 全部完成。

## 阶段 1：testnet 验证（≥3 天）
- [ ] 创建 Demo Trading API key（币安期货 testnet 已弃用）：https://demo.binance.com →
      API Management；`BINANCE_TESTNET=true` 语义=Demo Trading（demo-fapi.binance.com）。
- [ ] `.venv/bin/python scripts/binance_testnet_smoke.py` → SMOKE PASS
      （若 cloid 断言失败：启用 encode_cloid 替换编码，更新 spec §5.1 注记后重跑）。
- [ ] 新建 testnet 环境（用户定 2026-07-14：全新独立环境，与 HL 旧 app 零共享）：
      `fly apps create gridtrade-bi-test` + PG `gridtrade-pg-bi-test` 建库挂载
      ——完整命令见 deploy/DEPLOY.md §1-§2。
- [ ] testnet app：`fly secrets set BINANCE_API_KEY=... BINANCE_API_SECRET=... -a gridtrade-bi-test`
      + 面板凭证三件套（DEPLOY.md「Dashboard」节）。
- [ ] `fly deploy -c deploy/fly.toml -a gridtrade-bi-test`（toml 不含 app 名，必须 -a；CI 部署则读
      仓库变量 `FLY_APP_TESTNET`，见 deploy/DEPLOY.md §6b；空库由发布钩子 create && migrate
      自动建表）；观察 ≥3 天：开格/成交映射/补单/部分成交/
      对账自愈/保险丝挂撤/面板五视图/心跳，无人工干预。

## 阶段 2（独立事项，时机自定）：HL 遗留生产退场
> 币安生产部署到**全新环境**（gridtrade-bi-prod + 新库，用户定 2026-07-14），**不依赖本阶段**
> ——HL 环境（gridtrade-prod）可继续运行任意久，退场只关乎资金调度，不阻塞阶段 3。
- [ ] /controls 暂停 scheduler 开新格（或 fly scale count scheduler=0 -a gridtrade-prod）。
- [ ] 随 12H 换仓自然关格，或经 /controls 逐格手动关闭。
- [ ] **对账清点**：HL 生产库执行
      `SELECT id, symbol, status FROM grids WHERE status NOT IN ('CLOSED', 'FAILED');`
      （FAILED 为无害终态，与 CLOSED 同免检；须 0 行=无未平仓格），并核对交易所侧无残余
      持仓/挂单后再提资。
- [ ] HL 提资；HL 历史保留在其独立库（gridtrade-pg-prod）随时可查，与币安新库零耦合。
- [ ] 彻底收摊（可选）：`fly scale count monitor=0 scheduler=0 web=0 -a gridtrade-prod`；
      确认历史无需再查（或已导出）后可 `fly apps destroy gridtrade-prod`。

## 阶段 3：生产切换
- [ ] **前置门槛(代码):票池 COIN-only 过滤已落地**——`BinanceAdapter._include_market` 仅收
      `underlyingType=='COIN'`(实盘+回测同口径);切换后核对 mainnet 票池无 TradFi
      (`resolve_live_universe` 结果中 `underlyingType!='COIN'` 应为 0)。背景:币安 mainnet 上
      美股/韩股/商品代币化永续,非 7×24 跳空打穿网格+保险丝(spec 2026-07-15)。
- [ ] 币安主网 API key：只开合约交易、**禁提现**、不绑 IP 白名单
      （Fly 出口 IP 非静态；如启用 Fly static egress 再收紧，spec §7.3）。
- [ ] 新建生产环境：`fly apps create gridtrade-bi-prod` + PG `gridtrade-pg-bi-prod` 建库挂载
      + GitHub 侧 `FLY_APP_PROD=gridtrade-bi-prod` 变量与按新 app 签发的 `FLY_API_TOKEN_PROD`
      ——完整命令见 deploy/DEPLOY.md「Mainnet 生产环境」章。
- [ ] `fly secrets set BINANCE_API_KEY=... BINANCE_API_SECRET=... -a gridtrade-bi-prod`
      + 面板凭证三件套（全新 app 无旧 HL_* 密钥包袱，无需 unset）。
- [ ] `fly deploy -c deploy/fly.prod.toml -a gridtrade-bi-prod`（toml 不含 app 名，必须 -a；CI 部署
      则读仓库变量 `FLY_APP_PROD`；env 已是 EXCHANGE=binance/BINANCE_TESTNET=false，
      SCHEDULER_RUN_ON_START=false 保护在位；空库由发布钩子 create && migrate 自动建表）。
- [ ] 小资金试跑：临时 `fly secrets set TOTAL_BUDGET=500 MAX_CONCURRENT=3 -a gridtrade-bi-prod`，
      入金小额，观察 ≥1 个换仓周期：无 429/418、无 stuck OPENING、记录/盈亏诚实。
- [ ] 恢复正常参数，逐步加资金。

## 验收核对（spec §八）
- [ ] ① testnet ≥3 天无人工干预
- [ ] ② 全历史回测出报告：
      `.venv/bin/python -m gridtrade.backtest.vision_sync 2019-12-01 <昨日> --tf 1h`（约数 GB）
      `TZ=Asia/Shanghai BT_WORKERS=4 .venv/bin/python -m gridtrade.backtest.backtest_run 2020-01-01 <昨日> 1m`
- [ ] ③ CI 全绿  ④ 生产小资金 ≥1 周期  ⑤ 快照契约测试在位
