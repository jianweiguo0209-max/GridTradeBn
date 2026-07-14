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
