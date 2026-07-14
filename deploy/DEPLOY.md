# GridTradeBi 部署 ops 清单（Binance USDT-M / fly.io / nrt）

> 代码侧（Dockerfile / fly.toml / CI / CD / 守护进程 / 工厂）已就绪。下面是**只能你执行**的运维步骤。
> 决策依据见会话记忆 `p4-deploy-decisions`。先 **testnet 跑通** → 再 **mainnet 小额**。
> 生产切换完整流程见 `docs/币安切换runbook.md`（本文件是常规运维手册，切换期以 runbook 为准）。

## 0. 前置
- 安装 fly CLI：`brew install flyctl`（或 https://fly.io/docs/flyctl/install/）。
- `fly auth login`。
- 准备 **Binance testnet** API 凭证：到 https://testnet.binancefuture.com 注册并创建 API Key/Secret
  （测试网自带测试金，无需单独领水）。记下 **API Key** 与 **API Secret**。

## 1. 创建 app（不立即部署）
```bash
cd <repo 根>
fly launch --no-deploy --copy-config --name gridtrade-hl --region nrt \
  --dockerfile deploy/Dockerfile
# 若 fly launch 生成了根目录 fly.toml，确保用的是 deploy/fly.toml 的内容（app=gridtrade-hl, region=nrt, processes.monitor）。
```
> app 名 `gridtrade-hl`/`gridtrade-prod` 沿用历史命名（现已跑 Binance）；改名=换 app 属独立基础设施操作，不在本次范围。
> **app 名已参数化**：fly toml 不再写死 app 名，CI 从仓库变量读取、手动部署必须 `-a`——多实例部署见 §6b。

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
  BINANCE_API_KEY=YourTestnetApiKey \
  BINANCE_API_SECRET=YourTestnetApiSecret
# BINANCE_TESTNET=true 已在 deploy/fly.toml 的 [env]（静态配置，不走 secrets）；其余风控项
# （CAP/TOTAL_BUDGET/BLACKLIST_SYMBOLS）按需：fly secrets set CAP=50 BLACKLIST_SYMBOLS="BTC,ETH"
```
> 旧 HL_* 键（`HL_WALLET_ADDRESS`/`HL_PRIVATE_KEY`/`HL_TESTNET`）与 `LEVERAGE`/`CAP_EQUITY_FRAC`
> 已退役——设置任一将在 boot 时 `RuntimeError`（刻意 fail-fast，见 `gridtrade/config.py`）。
> 迁移自旧部署先执行：`fly secrets unset HL_WALLET_ADDRESS HL_PRIVATE_KEY HL_TESTNET --app gridtrade-hl`。

## 4. 首次部署（monitor 常驻机）
```bash
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile --remote-only --app gridtrade-hl
# toml 不含 app 名（多实例防冲突，见 §6b）——手动部署必须 -a/--app，漏了会被 flyctl 拒绝。
# 这会建一台跑 `python -m gridtrade.runtime.monitor` 的常驻机（启动即 restore_all 自愈 +
# 进入 ~5s 循环）。进程崩溃由 fly 自动重启。
```

## 5. scheduler（无需单独操作）
scheduler 现为 `deploy/fly.toml` 的 process group（常驻，自己睡到整点跑一遍），与 monitor
**同一镜像、同一 `fly deploy` 一起部署/更新**——无需建定时机。
- testnet 调试想让它启动即跑一遍：把 fly.toml `[env]` 的 `SCHEDULER_RUN_ON_START = "true"`
  取消注释（或 `fly secrets set SCHEDULER_RUN_ON_START=true`）；稳定后置回 false（仅整点跑，
  避免部署 mid-hour 把当前 offset 的网格关掉重开）。

## 5b. 灾难止损保险丝（交易所原生触发单）
软止损（monitor 5s 轮询）之外，开网时挂两张**交易所原生 reduce-only 止损**作硬兜底（破网价触发，
连续盯价、不依赖本进程在线）。开关与滑点：
- `STOP_ORDERS_ENABLED`（默认 `true`）：`false` 一键回退纯软止损、零行为变化。
- `STOP_SLIPPAGE`（默认 `0.15`）：触发市价单成交底线 = 破网价×(1∓slippage)、下单时锁死，越宽越保成交。
**上线对已有库跑一次幂等迁移**（加 `grids.fuse_low_oid/fuse_high_oid` 列，与 fee 列同一条命令）：
```bash
fly machine run <image> python -m gridtrade.runtime.dbadmin migrate
```
> mainnet 前 testnet 需实测：Binance reduce-only 超额 size 封顶到持仓、触发对 mark/last、端到端破网触发→撑网全拆、`cancel_all` 是否覆盖触发单。

## 6. CD（可选，自动部署）
GitHub → Settings → Secrets and variables → Actions：
- **Secrets** 加 `FLY_API_TOKEN`（`fly tokens create deploy -a gridtrade-hl` 生成，token 按 app 签发）；
- **Variables** 加 `FLY_APP_TESTNET=gridtrade-hl`（**必填**——未设置部署工作流直接报错退出，见 §6b）。

之后 push 到 main → CI 通过 → `.github/workflows/deploy.yml` 自动 `flyctl deploy --app $FLY_APP_TESTNET`。

## 6b. 多实例部署（app 名参数化）

fly toml **不写死 app 名**（spec `2026-07-14-fly-app-parameterization`）：CI 从 GitHub 仓库
Variables 读取，未设置即 fail-fast 报错；手动部署必须 `-a`。同一项目部署第二套实例（fork /
双生产）互不冲突。

| 隔离资产 | testnet | mainnet | 说明 |
|---|---|---|---|
| 仓库变量（Variables） | `FLY_APP_TESTNET` | `FLY_APP_PROD` | 主实例值 `gridtrade-hl` / `gridtrade-prod` |
| 部署令牌（Secrets） | `FLY_API_TOKEN` | `FLY_API_TOKEN_PROD` | `fly tokens create deploy -a <app>` 按 app 签发 |
| fly app + Postgres | 各建各的 | 各建各的 | §1-§2 / Mainnet 前置步骤 |
| fly secrets | 各设各的 | 各设各的 | §3 / Mainnet 手动步骤 3 |

**新实例五步**：① `fly apps create <名>` + PG 建库挂载 → ② `fly secrets set`（凭证/面板）→
③ GitHub Variables 设 app 名 → ④ `fly tokens create deploy -a <名>` 进 Secrets → ⑤ 触发部署。

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
fly secrets set --app gridtrade-hl BINANCE_API_KEY=MainnetApiKey BINANCE_API_SECRET=MainnetApiSecret CAP=30
fly deploy --config deploy/fly.toml --dockerfile deploy/Dockerfile --remote-only --app gridtrade-hl
```
用极小额跑一次真实端到端，核对盈亏/记账与回测口径一致。

---
### 注意
- **mainnet 上线前确认 live 策略参数**：factors / weight_list / cap / leverage / choose_symbols（`gridtrade/config.py` 的 `DEFAULT_STRATEGY_CONFIG` 是 legacy 默认 + env 覆盖）。
- 全程 UTC 存储与计算；换仓 offset 相位纯 UTC（已移除 UTC_OFFSET）；显示时区由 `DISPLAY_TZ`（IANA，默认 UTC）控制。
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

访问：`fly open -a gridtrade-hl`（web 进程常驻 ≥1 台，long-live，无冷启动）。
登录失败 5 次锁定 ≥ 1 小时（内存态，机器重启后清零）。

> 部署机制注意：web 是 scale-to-zero 之外的常驻进程组（`min_machines_running = 1`）。
> 早期用 `min=0` 时 CI 滚动部署不会为空的新进程组创建首台机器（且空组无法 `fly scale count`），
> 故改 `min=1` 让 `fly deploy` 直接建/留 web 机器。若哪天想回 scale-to-zero，需先有 1 台 web 机器存在，
> 再 `fly scale count web=1` 后改 min=0。

---

### 控制台（P2）
- 控制动作 = web 写 DB（control_flags/control_commands/control_audit），monitor 每 ~5s 消费执行，web 永不下单。
- halt：冻结补单/开仓/选币，止损与记账照常。panic：置 halt + 入队全平（需输入 PANIC 确认）。
- 关/开网格、暂停 scheduler 同走指令/标志；审计与队列状态在 /controls 页可查。

---

### 复盘分析（P3）
- /analytics：权益/已实现盈亏曲线 + tag 盈亏归因 + 成交分布（时间/买卖/line/累计费）+ 退出原因，全部服务端内联 SVG（零 JS）。范围过滤 all/7d/30d。
- equity_snapshots 表随 create_all 自动建（无需 migrate）；monitor 每 EQUITY_SNAPSHOT_INTERVAL_SEC（默认 300s）节流写一行真权益（fetch_balance().equity，含未实现），取余额失败跳过不崩。
- 真实手续费（grid_fills.fee）已铺进成交流水表 / 总览 / tag 归因。

---

### 实时网格价格图（P1 明细页）
- /grid/{id}/chart 返回 SVG 片段；明细页内联 JS 每 5s fetch 局部刷新（document.hidden 暂停）。
- 走势 fetch_ohlcv 按需拉（timeframe 按窗口自适应 1m/5m/15m/1h）；网格挂点由 grid_order_info 纯函数重算；挂单/成交读 DB；当前价 fetch_price。
- web 零写；fetch_ohlcv/fetch_price 失败 try/except 降级（画 DB 层 + 「行情暂不可用」），端点永不 500。

---

## Mainnet 生产环境（app `gridtrade-prod` / PG `gridtrade-pg-prod`）

> 独立 app + 独立 PG，与 testnet 完全隔离。设计见
> `docs/superpowers/specs/2026-07-03-mainnet-production-environment-design.md`。
> **全自动 CD：push `production` 分支即触发真钱部署——手动准备未就位前不要 push。**

### 已就位（前置步骤 2，2026-07-03 建好）
- `fly apps create gridtrade-prod --org personal`
- `fly postgres create --name gridtrade-pg-prod --org personal --region nrt --vm-size shared-cpu-1x --volume-size 1 --initial-cluster-size 1`
- `fly postgres attach gridtrade-pg-prod --app gridtrade-prod`（已设 `DATABASE_URL` secret）

### 手动步骤 1 — Binance mainnet API key（真钱）
- 币安主站创建 API key——只开合约交易权限、禁提现、不绑 IP 白名单（Fly 出口 IP 非静态；如启用
  Fly static egress 可再收紧）。语义对齐 `docs/币安切换runbook.md` 阶段 3。
- 记下 `BINANCE_API_KEY`=API Key、`BINANCE_API_SECRET`=API Secret（与 testnet 的 key 分开管理，勿复用）。

### 手动步骤 3 — secrets（面板凭证全新生成，勿复用 testnet）
```bash
fly secrets set --app gridtrade-prod BINANCE_API_KEY=... BINANCE_API_SECRET=...
HASH=$(.venv/bin/python -c "from gridtrade.dashboard.auth import hash_password; print(hash_password('<强密码>'))")
fly secrets set --app gridtrade-prod DASHBOARD_USER=admin \
  DASHBOARD_PASSWORD_HASH="$HASH" DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)"
```
> 旧 HL_* 键已退役，设置任一将在 boot 时 `RuntimeError`（刻意 fail-fast）；若此 app 曾按本文件旧版
> 设过 `HL_WALLET_ADDRESS`/`HL_PRIVATE_KEY`，先 `fly secrets unset HL_WALLET_ADDRESS HL_PRIVATE_KEY HL_TESTNET --app gridtrade-prod`。

> 注意：
> - `UNIVERSE_WHITELIST` **`deploy/fly.prod.toml` [env] 未设置该键**——2026-07-04 起票池已改为
>   全市场动态：全部 live 永续 swap − 黑名单（`BLACKLIST_SYMBOLS`/tier0）− 24h 成交额地板
>   （`MIN_QUOTE_VOLUME_24H`），见 fly.prod.toml `[env]` 注释与
>   docs/superpowers/specs/2026-07-04-candidate-universe-port-design.md；如需收窄回白名单，
>   在 `[env]` 加回该键并重部署。
> - `SCHEDULER_RUN_ON_START` **不要**设成 secret（会盖过 fly.prod.toml 的 `[env]=false`）；prod 靠 [env] 保持 false。
> - `DATABASE_URL` 已由 attach 设置，勿手动改。

### 手动步骤 4 — GitHub 部署 token + app 名变量（app 级、与 testnet 隔离）
```bash
fly tokens create deploy -a gridtrade-prod        # 复制输出
gh secret set FLY_API_TOKEN_PROD --repo rockingchang/GrideTradeBi   # 粘贴 token
gh variable set FLY_APP_PROD --body gridtrade-prod --repo rockingchang/GrideTradeBi
# FLY_APP_PROD 必填：未设置 deploy-prod.yml 直接报错退出（多实例防冲突，见 §6b）
```

### 手动步骤 5 — 首次上线（全部就位后）
```bash
# 可选：本地干跑一次验证（不经 CI；toml 无 app 名，必须 -a）
fly deploy --config deploy/fly.prod.toml --dockerfile deploy/Dockerfile --remote-only --app gridtrade-prod
# 正式：把 main 合进 production 再 push → 自动 test→deploy
git checkout production && git merge --no-ff main && git push origin production
```

### 上线验收
```bash
fly logs -a gridtrade-prod                 # monitor/scheduler 心跳更新
fly pg connect -a gridtrade-pg-prod        # SELECT * FROM heartbeats; SELECT id,symbol,status,tag FROM grids;
fly open -a gridtrade-prod                 # 面板可登录；实测 panic/halt 熔断可用
```

### 后续上线（改动 mainnet）
`main` 验证 → merge 进 `production` → `git push origin production`（自动 test→deploy）。**不要直接往 production 提交。**
