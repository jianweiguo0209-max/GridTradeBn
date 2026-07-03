# Mainnet 生产环境设计（Hyperliquid / fly.io）

> 目标：为 GridTradeGP 建立**独立的 mainnet 小额实盘环境**，与现有 testnet（`gridtrade-hl`）完全隔离，
> 走 `production` 分支触发的自动 CI/CD。设计经用户确认（2026-07-03）。
>
> 关键决策（用户拍板）：
> - 部署触发 = **全自动**（push `production` → CI 通过 → 自动 `flyctl deploy`，无人工审批门）。
> - App/PG 命名 = **`gridtrade-prod`** / **`gridtrade-pg-prod`**。
> - 配置布局 = **独立提交的配置文件** `deploy/fly.prod.toml`（两分支共存，避免 per-branch 改同一行的合并冲突）。
> - 风控/策略参数 = **与 testnet 完全一致**（小额由"往 mainnet 钱包注资多少"天然控制——cap 是权益分数
>   `clamp(equity×0.10, CAP_MIN, CAP_MAX)`，小额注资即小 cap）。

---

## 1. 拓扑

| 资源 | Testnet（已有） | Mainnet（本设计） |
|---|---|---|
| Git 分支 | `main` | `production`（长期分支，从 `main` 切出） |
| Fly app | `gridtrade-hl` | `gridtrade-prod` |
| Fly Postgres | `gridtrade-pg` | `gridtrade-pg-prod`（专用，nrt，单节点） |
| Fly 配置 | `deploy/fly.toml` | `deploy/fly.prod.toml`（新建、入库） |
| 部署 workflow | `.github/workflows/deploy.yml`（手动 `workflow_dispatch`） | `.github/workflows/deploy-prod.yml`（`production` push 自动） |
| HL 凭证 | testnet 钱包 | mainnet 主账户 + mainnet agent 私钥 |
| GH 部署密钥 | `FLY_API_TOKEN` | `FLY_API_TOKEN_PROD`（app 级 deploy token，隔离） |

两套环境**共用同一 Docker 镜像与全部业务代码**。唯一差异：fly 配置（app 名、`HL_TESTNET=false`、
`SCHEDULER_RUN_ON_START=false`、`release_command`）与 secrets（mainnet 钱包 + 独立 DB + 独立面板凭证）。

---

## 2. 分支与部署流

```
main (testnet/dev)  ──merge──▶  production (mainnet)
                                     │ push
                                     ▼
                          .github/workflows/deploy-prod.yml
                          ┌─────────────────────────────────┐
                          │ job: test   (pytest, py3.9)      │
                          │      │ 通过                       │
                          │      ▼                            │
                          │ job: deploy (needs: test)         │
                          │   flyctl deploy \                 │
                          │     --config deploy/fly.prod.toml │
                          │     --dockerfile deploy/Dockerfile│
                          │     --remote-only                 │
                          └─────────────────────────────────┘
```

- **上线动作 = 把 `main` merge 进 `production` 再 push**（不直接往 `production` 提交），保证 mainnet 只跑
  已在 `main`/testnet 验证过的代码。
- `deploy-prod.yml` 单 workflow 内两个 job：`test` → `deploy(needs: test)`。测试红即挡部署——CI 门天然内建。
- 触发器：`on: push: branches: [production]`。
- 现有 `ci.yml`（`on: push:` 全分支）加 `branches-ignore: [production]`，避免 production push 时测试跑两遍。
- workflow 与 `fly.prod.toml` 先加在 `main`，`production` 从 `main` 切出即已包含，无"文件缺失不触发"问题。

---

## 3. `deploy/fly.prod.toml`（与 testnet 的差异）

进程组（monitor / scheduler / web）、`[http_service]`、VM 规格、`primary_region = "nrt"` 均与
`deploy/fly.toml` 一致。差异项：

```toml
app = "gridtrade-prod"

[deploy]
  # 处理全新空库：create 按当前模型建全部表（已含 fee/fuse 列），migrate 变 no-op；
  # 后续重新部署 create 是 no-op、migrate 负责将来加列。二者皆幂等。
  release_command = "sh -c 'python -m gridtrade.runtime.dbadmin create && python -m gridtrade.runtime.dbadmin migrate'"

[env]
  HL_TESTNET = "false"              # 真金白银
  SCHEDULER_RUN_ON_START = "false"  # 避免每次自动部署 churn 关开网格烧真手续费
  # 其余 EXCHANGE / MONITOR_INTERVAL_SEC / SCHEDULER_PERIOD / UTC_OFFSET / MAX_CONCURRENT
  # 及风控项（CAP / CAP_EQUITY_FRAC / CAP_MIN / CAP_MAX / LEVERAGE / UNIVERSE_WHITELIST 等）
  # = 与 testnet fly.toml 完全一致。
```

### 两处故意偏离"与 testnet 完全一致"（均因真钱）

1. **`release_command` 用 `create && migrate` 而非裸 `migrate`。**
   *根因*：`dbadmin migrate`（`gridtrade/runtime/dbadmin.py`）对空库调用 `sa.inspect(engine).get_columns('grid_fills')`
   会抛 `NoSuchTableError`。testnet 因库里已有表侥幸不炸；mainnet 是**全新空库**，裸 `migrate` 会在
   **首次部署的 release 阶段直接失败、abort 整个发布**。`create`（= `create_all`，`checkfirst=True`）先建齐所有表，
   之后 `migrate` 幂等 no-op。纯配置解，无需改代码。

2. **`SCHEDULER_RUN_ON_START = "false"`。** testnet 为 `"true"`（调试便利）。在全自动部署下 `"true"` 意味着
   **每次 push production 都会把当前网格中途关掉重开**、churn 白烧真手续费。prod 用 `false`（scheduler 只在整点动作）。

---

## 4. 全自动 mainnet 部署的安全边界

用户选全自动、不加人工审批门。据此实现，并记录残留风险与既有缓解：
- 残留风险：(1) 任何通过测试的 `production` push 都会重启实盘交易进程、可能动到资金；(2) 测试通过 ≠ 策略安全。
- 缓解：`test` 门控 `deploy`（红测挡部署）；紧急手段 = 面板 **panic/halt 一键熔断**（同一镜像内，写
  `control_commands`/`control_flags`，monitor 消费执行清仓）。
- **上线硬性验收**：认真注资前，先在 prod 上实测面板可登录且 panic/halt 熔断可用（见 §6 验收）。

---

## 5. 需用户手动执行的步骤

app + PG + secrets 必须在**首次 push `production` 之前**就位，否则首次自动部署失败。

### ✅ 步骤 2 已由本 session 代为执行（2026-07-03）
```bash
fly apps create gridtrade-prod --org personal
fly postgres create --name gridtrade-pg-prod --org personal --region nrt \
  --vm-size shared-cpu-1x --volume-size 1 --initial-cluster-size 1
fly postgres attach gridtrade-pg-prod --app gridtrade-prod   # 已设 DATABASE_URL secret（Staged）
```
结果：app `gridtrade-prod`（pending，无机器属正常）；PG `gridtrade-pg-prod` 健康；`DATABASE_URL` 已 Staged。
> PG superuser 密码在 create 输出里一次性显示，用户须自行安全留存；app 用 attach 生成的独立库/用户，密码不入库。

### ⬜ 步骤 1 — HL mainnet 钱包（真钱步骤，只有你能做）
- 给 HL **mainnet** 主账户注资小额测试 USDC。
- 创建并批准 **mainnet agent/API 钱包**（testnet 的 agent 不通用）。
- 记下：`HL_WALLET_ADDRESS` = 有钱的主账户地址；`HL_PRIVATE_KEY` = agent 私钥（66 字符 `0x`+64hex）。

### ⬜ 步骤 3 — 给 `gridtrade-prod` 设 secrets（面板凭证**重新生成**，勿复用 testnet）
```bash
fly secrets set --app gridtrade-prod HL_WALLET_ADDRESS=0x... HL_PRIVATE_KEY=0x...
HASH=$(.venv/bin/python -c "from gridtrade.dashboard.auth import hash_password; print(hash_password('<强密码>'))")
fly secrets set --app gridtrade-prod DASHBOARD_USER=admin \
  DASHBOARD_PASSWORD_HASH="$HASH" DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)"
```

### ⬜ 步骤 4 — GitHub 部署 token（app 级、与 testnet 隔离）
```bash
fly tokens create deploy -a gridtrade-prod    # → 粘进 GH 仓库 secret FLY_API_TOKEN_PROD
```

### ⬜ 步骤 5 — 首次上线
- （可选）先本地干跑一次验证：`fly deploy --config deploy/fly.prod.toml --dockerfile deploy/Dockerfile --remote-only`
- 然后 merge `main → production` 并 push → 自动 CI/CD。

---

## 6. 上线验收

```bash
fly logs -a gridtrade-prod                 # monitor/scheduler 心跳在更新
fly pg connect -a gridtrade-pg-prod        # SELECT * FROM heartbeats; SELECT id,symbol,status,tag FROM grids;
```
- 面板可登录（`fly open -a gridtrade-prod`）。
- **实测 panic/halt 熔断**在 prod 生效。
- 确认第一个小网格能正常开仓、记账口径与回测一致。

---

## 7. 本设计要创建/改动的代码产物（实施范围）

1. 新建长期分支 `production`（从 `main`）。
2. 新建 `deploy/fly.prod.toml`（§3）。
3. 新建 `.github/workflows/deploy-prod.yml`（§2，test→deploy 两 job）。
4. 改 `.github/workflows/ci.yml`：加 `branches-ignore: [production]`。
5. 更新 `deploy/DEPLOY.md`：补 mainnet 环境 ops 清单（引用本 spec 的手动步骤 + 资源名）。
6. 更新 `docs/STATUS.md`：登记 mainnet 环境拓扑与状态。

> 顺序：产物 1~6 先在 `main` 上完成并合并；再从 `main` 切 `production` 并 push 触发首次自动部署
> （前提：步骤 1/3/4 的手动 secrets/token 已就位）。

---

## 8. 不在本设计范围（out of scope）
- 策略/风控参数调优（沿用 testnet）。
- 人工审批门 / GitHub Environment 保护规则（用户选全自动、明确不要）。
- 告警/监控外接（沿用面板只读观测）。
- mainnet 特有的多监控机 / CI PG job（仍延后）。
