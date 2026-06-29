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

## 5. scheduler（无需单独操作）
scheduler 现为 `deploy/fly.toml` 的 process group（常驻，自己睡到整点跑一遍），与 monitor
**同一镜像、同一 `fly deploy` 一起部署/更新**——无需建定时机。
- testnet 调试想让它启动即跑一遍：把 fly.toml `[env]` 的 `SCHEDULER_RUN_ON_START = "true"`
  取消注释（或 `fly secrets set SCHEDULER_RUN_ON_START=true`）；稳定后置回 false（仅整点跑，
  避免部署 mid-hour 把当前 offset 的网格关掉重开）。

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

---
## 本地 Postgres 测试（双后端）

默认 `pytest` 走内存 SQLite（快、离线）。要在真 Postgres 上跑全套（抓 PG-only bug，如 INT4 溢出）
和 PG-only 并发 TOCTOU 测试：

```bash
docker run -d --name gridpg -e POSTGRES_PASSWORD=grid -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16
export TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade
TZ=Asia/Shanghai .venv/bin/python -m pytest -q      # 全量走 PG（含并发 TOCTOU）
unset TEST_DATABASE_URL                               # 回到默认 SQLite
```

- DB 测试由 `tests/conftest.py` 的 `store`（双模式）/ `pg_store`（PG-only，无 env 则 skip）fixture 驱动。
- 并发测试 `tests/state/test_transition_concurrency.py` 仅在设了 `TEST_DATABASE_URL` 时运行。
- CI 仍跑 SQLite（不依赖 PG）；CI PG job 待多监控机阶段。

---

## Dashboard（web 进程，只读监控）

设置登录凭据（密码用本地生成的 pbkdf2 哈希，不在仓库存明文）。
**把下面的 `你的密码` 换成你自己的真实密码再执行**：

    HASH=$(.venv/bin/python -c "from gridtrade.dashboard.auth import hash_password; print(hash_password('你的密码'))")
    fly secrets set -a gridtrade-hl DASHBOARD_USER=admin DASHBOARD_PASSWORD_HASH="$HASH" DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)"

访问：`fly open -a gridtrade-hl`（web 进程 scale-to-zero，首次访问有数秒冷启动）。
登录失败 5 次锁定 ≥ 1 小时（内存态，机器重启后清零）。
