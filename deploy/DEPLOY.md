# GridTradeGP 部署 ops 清单（Hyperliquid / fly.io / nrt）

> 代码侧（Dockerfile / fly.toml / CI / CD / 守护进程 / 工厂）已就绪。下面是**只能你执行**的运维步骤。
> 决策依据见会话记忆 `p4-deploy-decisions`。先 **testnet 跑通** → 再 **mainnet 小额**。

## 0. 前置
- 安装 fly CLI：`brew install flyctl`（或 https://fly.io/docs/flyctl/install/）。
- `fly auth login`。
- 准备一个 **HL testnet** 钱包：到 https://app.hyperliquid-testnet.xyz 用钱包登录、领测试金（faucet）。记下**钱包地址**与**API 私钥**（HL 的 API wallet 私钥，非主钱包助记词）。

## 1. 创建 app（不立即部署）
```bash
cd <repo 根>
fly launch --no-deploy --copy-config --name gridtrade-hl --region nrt \
  --dockerfile deploy/Dockerfile
# 若 fly launch 生成了根目录 fly.toml，确保用的是 deploy/fly.toml 的内容（app=gridtrade-hl, region=nrt, processes.monitor）。
```

## 2. 开 Postgres（同区 nrt）并挂载
```bash
fly postgres create --name gridtrade-pg --region nrt --vm-size shared-cpu-1x --volume-size 1
fly postgres attach gridtrade-pg --app gridtrade-hl
# attach 会自动给 app 设 DATABASE_URL（postgres://…）。代码 StateStore.from_url 会把
# postgres:// 规范成 postgresql://，无需手改。
```

## 3. 注入 secrets（testnet 凭证）
```bash
fly secrets set --app gridtrade-hl \
  HL_WALLET_ADDRESS=0xYourTestnetWallet \
  HL_PRIVATE_KEY=YourTestnetApiPrivateKey
# HL_TESTNET=true 已在 deploy/fly.toml 的 [env]；其余风控项（CAP/LEVERAGE/TOTAL_BUDGET/
# BLACKLIST_SYMBOLS）按需：fly secrets set CAP=50 LEVERAGE=3 BLACKLIST_SYMBOLS="BTC,ETH"
```

## 4. 首次部署（monitor 常驻机）
```bash
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile --remote-only
# 这会建一台跑 `python -m gridtrade.runtime.monitor` 的常驻机（启动即 restore_all 自愈 +
# 进入 ~5s 循环）。进程崩溃由 fly 自动重启。
```

## 5. 建 scheduler 定时机（scale-to-zero，每小时唤醒一次）
fly.toml 的 process group 不带 cron，scheduler 用 **scheduled machine** 单独建：
```bash
fly machine run . --app gridtrade-hl --region nrt --schedule hourly \
  --dockerfile deploy/Dockerfile \
  --entrypoint "python -m gridtrade.runtime.scheduler"
# 每小时唤醒跑一遍 run_scheduler_once（关旧 tag→选币→准入→开新→心跳）后退出（scale-to-zero）。
# 注：scheduled machine 用与 app 相同的 secrets/env。
```

## 6. CD（可选，自动部署）
GitHub → Settings → Secrets → Actions 加 `FLY_API_TOKEN`（`fly tokens create deploy` 生成）。
之后 push 到 main → CI 通过 → `.github/workflows/deploy.yml` 自动 `flyctl deploy`。

## 7. 验证 testnet 跑通
```bash
fly logs --app gridtrade-hl                 # 看 monitor/scheduler 日志
fly pg connect -a gridtrade-pg              # 连库
  SELECT * FROM heartbeats;                 #   两机心跳 last_beat_ts 在更新 = 存活
  SELECT id,symbol,status,tag FROM grids;   #   开/平网格记录
  SELECT * FROM order_records ORDER BY closed_at DESC LIMIT 10;
```
确认全链路：开网格 → 补单 → 止盈止损平仓 → 杀进程/重启后对账自愈续跑、无重复单/孤儿单。

## 8. 切 mainnet 小额（需求 3）
testnet 稳定后：
```bash
fly secrets set --app gridtrade-hl HL_WALLET_ADDRESS=0xMainnetWallet HL_PRIVATE_KEY=MainnetApiKey CAP=30
fly secrets set --app gridtrade-hl HL_TESTNET=false      # 切主网
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile --remote-only
```
用极小额跑一次真实端到端，核对盈亏/记账与回测口径一致。

---
### 注意
- **mainnet 上线前确认 live 策略参数**：factors / weight_list / cap / leverage / choose_symbols（`gridtrade/config.py` 的 `DEFAULT_STRATEGY_CONFIG` 是 legacy 默认 + env 覆盖）。
- 全程 UTC 存储；offset 由 `UTC_OFFSET` 驱动（默认 8）。
- 健壮性：交易所调用已自带退避重试 + 熔断，失败降级续跑不退出；monitor 崩溃 fly 自动重启 + 重启对账自愈。
