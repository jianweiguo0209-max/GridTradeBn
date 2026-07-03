# Mainnet 生产环境 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 GridTradeGP 建立独立的 Hyperliquid **mainnet 小额实盘环境**（fly app `gridtrade-prod` + PG `gridtrade-pg-prod`，已由前置步骤 2 建好），并接上 `production` 分支触发的全自动 CI/CD——**在用户完成手动准备并明确授权前，绝不 push `production`（= 绝不触发真钱部署）**。

**Architecture:** 与 testnet 共用同一 Docker 镜像/全部业务代码；差异只在一个新配置文件 `deploy/fly.prod.toml`（app 名、`HL_TESTNET=false`、`SCHEDULER_RUN_ON_START=false`、`release_command=create && migrate`）和一个新 workflow `deploy-prod.yml`（`test` → `deploy` 两 job，push `production` 触发）。代码产物全部先落 `main`，再从 `main` 切 `production`。

**Tech Stack:** fly.io（flyctl v0.4.61）、Fly Postgres（flex/repmgr）、GitHub Actions、Python 3.9 / pytest / SQLAlchemy 2.0。

## Global Constraints

- App = `gridtrade-prod`；PG = `gridtrade-pg-prod`（region `nrt`，均已创建 + attach，`DATABASE_URL` 已 Staged）。
- 新配置文件路径 = `deploy/fly.prod.toml`；不改 `deploy/fly.toml`（testnet 专用）。
- `HL_TESTNET = "false"`（真钱）、`SCHEDULER_RUN_ON_START = "false"`（防自动部署 churn）。
- prod `release_command = "sh -c 'python -m gridtrade.runtime.dbadmin create && python -m gridtrade.runtime.dbadmin migrate'"`（空库首部署必需）。
- 部署 = 全自动、无人工审批门；CI 门天然内建（`deploy` 有 `needs: test`）。
- 上线姿势 = 把 `main` merge 进 `production` 再 push；**不**直接往 `production` 提交。
- 测试环境：`TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- **硬性闸门：Task 8（首次部署）不由实现方自动执行**——必须停下等用户完成手动步骤 1/3/4 并显式授权后才 push。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `tests/runtime/test_dbadmin_fresh_db.py` | Create | 回归测试：锁定"空库裸 migrate 抛错、`create` 后 migrate no-op"（守 release_command 设计） |
| `deploy/fly.prod.toml` | Create | mainnet fly 配置（唯一与 testnet 的差异载体） |
| `.github/workflows/deploy-prod.yml` | Create | mainnet CD：`production` push → test → deploy |
| `.github/workflows/ci.yml` | Modify | 加 `branches-ignore: [production]`，避免 production push 双跑测试 |
| `deploy/DEPLOY.md` | Modify | 补 mainnet ops 清单（手动步骤 + 资源名） |
| `docs/STATUS.md` | Modify | 登记 mainnet 环境拓扑与状态 |
| （git） | — | feat 分支实现 → merge main → 切 `production`（不 push） |

---

## Setup: 建工作分支

- [ ] **Step 1: 从 main 切 feat 分支**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP checkout main
git -C /Users/thomaschang/Projects/GridTradeGP checkout -b feat/mainnet-prod-env
```

- [ ] **Step 2: 确认干净**

Run: `git -C /Users/thomaschang/Projects/GridTradeGP status`
Expected: `On branch feat/mainnet-prod-env` / working tree clean（spec 文件若未提交会显示为 untracked，正常，将随 Task 6 提交）。

---

### Task 1: 回归测试锁定空库 `create && migrate` 行为

**Files:**
- Create: `tests/runtime/test_dbadmin_fresh_db.py`

**Interfaces:**
- Consumes: `gridtrade.runtime.dbadmin.migrate(store) -> list`、`gridtrade.runtime.dbadmin.run(action, *, store_factory=None)`、`gridtrade.state.store.StateStore.in_memory()`。
- Produces: 无（纯测试；守护 prod `release_command` 采用 `create && migrate` 而非裸 `migrate` 的依据）。

- [ ] **Step 1: 写测试**

```python
# tests/runtime/test_dbadmin_fresh_db.py
"""锁定 mainnet 首部署所依赖的空库行为：裸 migrate 在空库上抛错（NoSuchTableError），
先 create（create_all，按当前模型建含 fee/fuse 列的全表）后 migrate 变幂等 no-op。
这是 deploy/fly.prod.toml 用 `create && migrate` 而非裸 `migrate` 的根据。"""
import pytest
import sqlalchemy as sa

from gridtrade.runtime.dbadmin import migrate, run
from gridtrade.state.store import StateStore


def test_bare_migrate_on_empty_db_raises():
    st = StateStore.in_memory()          # 全新空库，未 create_all
    with pytest.raises(sa.exc.NoSuchTableError):
        migrate(st)


def test_create_then_migrate_on_empty_db_is_clean():
    st = StateStore.in_memory()
    assert run('create', store_factory=lambda: st) == 'create'
    # create_all 已按当前模型建好含 fee/fuse 列的表 → migrate 全部 skipped、不抛错
    results = run('migrate', store_factory=lambda: st)
    assert results == [('add_grid_fills_fee', 'skipped'),
                       ('add_grids_fuse_oids', 'skipped')]
```

- [ ] **Step 2: 跑测试确认通过（守护现有行为）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_dbadmin_fresh_db.py -v`
Expected: 2 passed。（这是刻画/回归测试，守护既有 dbadmin 行为，故一次通过；若 `test_bare_migrate_on_empty_db_raises` 未抛错，说明 dbadmin 行为已变，需回看 release_command 假设。）

- [ ] **Step 3: 提交**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP add tests/runtime/test_dbadmin_fresh_db.py
git -C /Users/thomaschang/Projects/GridTradeGP commit -m "test(dbadmin): 锁定空库 create&&migrate 行为（mainnet 首部署依据）"
```

---

### Task 2: 创建 `deploy/fly.prod.toml`

**Files:**
- Create: `deploy/fly.prod.toml`

**Interfaces:**
- Consumes: 已存在的 fly app `gridtrade-prod`（供 `fly config validate` 校验）、镜像 `deploy/Dockerfile`、进程入口 `gridtrade.runtime.{monitor,scheduler,web}`。
- Produces: `deploy-prod.yml` 部署时引用的 `--config deploy/fly.prod.toml`。

- [ ] **Step 1: 写文件**

```toml
# Fly.io 部署配置 —— GridTradeGP Mainnet 生产环境（Hyperliquid mainnet）。
# 与 testnet deploy/fly.toml 共用同一镜像与全部业务代码；差异见
# docs/superpowers/specs/2026-07-03-mainnet-production-environment-design.md。
# 独立 app（gridtrade-prod）+ 独立 Postgres（gridtrade-pg-prod），与 testnet 完全隔离。

app = "gridtrade-prod"
primary_region = "nrt"

[build]
  dockerfile = "Dockerfile"

# 发布前一次性钩子：先 create（空库按当前模型建全表，幂等）再 migrate（增量加列，幂等）。
# mainnet 首次部署面对全新空库，裸 migrate 会因表不存在（NoSuchTableError）失败 abort 发布，
# 故用 create && migrate：空库时 create 建全表、migrate no-op；后续 create no-op、migrate 负责加列。
[deploy]
  release_command = "sh -c 'python -m gridtrade.runtime.dbadmin create && python -m gridtrade.runtime.dbadmin migrate'"

# 非敏感默认配置（敏感项走 `fly secrets set`：HL_WALLET_ADDRESS / HL_PRIVATE_KEY /
# DATABASE_URL（attach 已设）/ DASHBOARD_* / UNIVERSE_WHITELIST）。
[env]
  EXCHANGE = "hyperliquid"
  HL_TESTNET = "false"              # ← mainnet，真金白银
  MONITOR_INTERVAL_SEC = "5"
  SCHEDULER_PERIOD = "12H"
  UTC_OFFSET = "8"
  MAX_CONCURRENT = "20"
  SCHEDULER_RUN_ON_START = "false"  # ← 生产：避免每次自动部署把网格中途关开、烧真手续费
  # 风控项（CAP / CAP_EQUITY_FRAC / CAP_MIN / CAP_MAX / LEVERAGE）沿用 config.py 默认或按需
  # fly secrets set 覆盖；单网格 cap = clamp(equity×0.10, CAP_MIN, CAP_MAX)，小额注资即小 cap。

[processes]
  monitor = "python -m gridtrade.runtime.monitor"
  scheduler = "python -m gridtrade.runtime.scheduler"
  web = "python -m gridtrade.runtime.web"

# web dashboard：常驻 ≥1 台（min_machines_running=1，与 testnet 同理，见 deploy/DEPLOY.md）。
[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 1
  processes = ["web"]

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
  processes = ["monitor", "scheduler", "web"]
```

- [ ] **Step 2: 语法/平台校验**

Run: `fly config validate --config deploy/fly.prod.toml -a gridtrade-prod`
Expected: `Configuration is valid`（app 已存在，能通过平台校验）。

- [ ] **Step 3: 提交**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP add deploy/fly.prod.toml
git -C /Users/thomaschang/Projects/GridTradeGP commit -m "feat(deploy): 新增 mainnet fly 配置 deploy/fly.prod.toml"
```

---

### Task 3: 创建 `.github/workflows/deploy-prod.yml`

**Files:**
- Create: `.github/workflows/deploy-prod.yml`

**Interfaces:**
- Consumes: GitHub repo secret `FLY_API_TOKEN_PROD`（app 级 deploy token，由用户在手动步骤 4 添加）、`deploy/fly.prod.toml`、`deploy/Dockerfile`、`requirements.txt`。
- Produces: `production` 分支 push 时的自动 CI/CD（test → deploy）。

- [ ] **Step 1: 写文件**

```yaml
name: Deploy Mainnet

# CD（mainnet）：push 到 production 分支 → 跑测试 → 通过后自动 flyctl deploy 到 gridtrade-prod。
# 全自动、无人工审批门（用户决策）；CI 门天然内建（deploy 有 needs: test）。
# 需 GitHub repo secret FLY_API_TOKEN_PROD（`fly tokens create deploy -a gridtrade-prod` 生成）。
on:
  push:
    branches: [production]

jobs:
  test:
    name: pytest (py3.9)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python 3.9
        uses: actions/setup-python@v5
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          # TA-Lib 0.6.8 提供 manylinux bundled wheel，含 C 库，无需系统 ta-lib（与 ci.yml 一致）
          pip install TA-Lib==0.6.8
          pip install pytest

      - name: Run tests
        env:
          TZ: Asia/Shanghai
        run: python -m pytest -q

  deploy:
    name: flyctl deploy (mainnet)
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup flyctl
        uses: superfly/flyctl-actions/setup-flyctl@master

      - name: Deploy to Fly (mainnet)
        run: flyctl deploy --config deploy/fly.prod.toml --dockerfile deploy/Dockerfile --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN_PROD }}
```

- [ ] **Step 2: YAML 校验**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy-prod.yml')); print('ok')"`
Expected: `ok`。（若 `ModuleNotFoundError: yaml`：`.venv/bin/pip install pyyaml` 后重试，或跳过——GitHub 会在 push 时校验。）

- [ ] **Step 3: 提交**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP add .github/workflows/deploy-prod.yml
git -C /Users/thomaschang/Projects/GridTradeGP commit -m "ci(cd): 新增 mainnet 全自动部署 workflow deploy-prod.yml（production push 触发）"
```

---

### Task 4: 修改 `.github/workflows/ci.yml`（避免 production push 双跑测试）

**Files:**
- Modify: `.github/workflows/ci.yml:3-6`

**Interfaces:**
- Consumes: 无。
- Produces: CI 仍在 main/feature 分支 push + 对 main 的 PR 上跑；`production` push 交给 `deploy-prod.yml` 内的 test job。

- [ ] **Step 1: 改触发段**

把开头的：

```yaml
on:
  push:
  pull_request:
    branches: [main]
```

改为：

```yaml
on:
  push:
    branches-ignore: [production]
  pull_request:
    branches: [main]
```

- [ ] **Step 2: YAML 校验**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"`
Expected: `ok`。

- [ ] **Step 3: 提交**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP add .github/workflows/ci.yml
git -C /Users/thomaschang/Projects/GridTradeGP commit -m "ci: ci.yml 忽略 production 分支（由 deploy-prod.yml 内 test job 覆盖）"
```

---

### Task 5: 更新 `deploy/DEPLOY.md`（补 mainnet ops 清单）

**Files:**
- Modify: `deploy/DEPLOY.md`（在文件末尾追加一节；不改现有 testnet 内容）

**Interfaces:**
- Consumes: 无。
- Produces: 用户执行手动步骤 1/3/4 + 首部署的可复制命令清单。

- [ ] **Step 1: 在 `deploy/DEPLOY.md` 末尾追加以下整节**

````markdown
---

## Mainnet 生产环境（app `gridtrade-prod` / PG `gridtrade-pg-prod`）

> 独立 app + 独立 PG，与 testnet 完全隔离。设计见
> `docs/superpowers/specs/2026-07-03-mainnet-production-environment-design.md`。
> **全自动 CD：push `production` 分支即触发真钱部署——手动准备未就位前不要 push。**

### 已就位（前置步骤 2，2026-07-03 建好）
- `fly apps create gridtrade-prod --org personal`
- `fly postgres create --name gridtrade-pg-prod --org personal --region nrt --vm-size shared-cpu-1x --volume-size 1 --initial-cluster-size 1`
- `fly postgres attach gridtrade-pg-prod --app gridtrade-prod`（已设 `DATABASE_URL` secret）

### 手动步骤 1 — HL mainnet 钱包（真钱）
- 给 HL **mainnet** 主账户注资小额测试 USDC；创建并批准 **mainnet agent/API 钱包**（testnet agent 不通用）。
- 记下 `HL_WALLET_ADDRESS`=有钱主账户地址、`HL_PRIVATE_KEY`=agent 私钥（66 字符 `0x`+64hex）。

### 手动步骤 3 — secrets（面板凭证全新生成，勿复用 testnet）
```bash
fly secrets set --app gridtrade-prod HL_WALLET_ADDRESS=0x... HL_PRIVATE_KEY=0x...
HASH=$(.venv/bin/python -c "from gridtrade.dashboard.auth import hash_password; print(hash_password('<强密码>'))")
fly secrets set --app gridtrade-prod DASHBOARD_USER=admin \
  DASHBOARD_PASSWORD_HASH="$HASH" DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)"
# 镜像 testnet 的币池（确认这些币在 mainnet 有效；testnet 值可能含 testnet 专属币）：
fly secrets set --app gridtrade-prod UNIVERSE_WHITELIST="<与 testnet 同口径的 mainnet 币池>"
```
> 注意：`SCHEDULER_RUN_ON_START` **不要**设成 secret（会盖过 fly.prod.toml 的 `[env]=false`）；prod 靠 [env] 保持 false。
> DATABASE_URL 已由 attach 设置，勿手动改。

### 手动步骤 4 — GitHub 部署 token（app 级、与 testnet 隔离）
```bash
fly tokens create deploy -a gridtrade-prod        # 复制输出
gh secret set FLY_API_TOKEN_PROD --repo rockingchang/GridTradeGP   # 粘贴 token
```

### 手动步骤 5 — 首次上线（全部就位后）
```bash
# 可选：本地干跑一次验证（不经 CI）
fly deploy --config deploy/fly.prod.toml --dockerfile deploy/Dockerfile --remote-only
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
````

- [ ] **Step 2: 提交**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP add deploy/DEPLOY.md
git -C /Users/thomaschang/Projects/GridTradeGP commit -m "docs(deploy): 补 mainnet 生产环境 ops 清单（gridtrade-prod）"
```

---

### Task 6: 更新 `docs/STATUS.md` + 提交 spec

**Files:**
- Modify: `docs/STATUS.md`（§5 或 §7 附近登记 mainnet 环境）
- Add: `docs/superpowers/specs/2026-07-03-mainnet-production-environment-design.md`（已写好，随本 task 入库）

**Interfaces:**
- Consumes: 无。
- Produces: 单一事实源登记 mainnet 拓扑与状态。

- [ ] **Step 1: 在 `docs/STATUS.md` §7 末尾（`⏳ mainnet 小额` 那条）改写为已搭好环境的状态**

把 §7 的：
```markdown
- ⏳ **mainnet 小额**（需求 3 收尾）：testnet 稳定后 `HL_TESTNET=false` + 确认 live 策略参数 + 切主账户凭证。
```
改为：
```markdown
- ⏳ **mainnet 小额**（需求 3 收尾）：**独立环境已搭建**（app `gridtrade-prod` + PG `gridtrade-pg-prod`，
  region nrt；`deploy/fly.prod.toml` `HL_TESTNET=false`/`SCHEDULER_RUN_ON_START=false`/`release_command=create&&migrate`；
  `deploy-prod.yml` 全自动 CD：push `production` 分支触发 test→deploy）。设计/计划见
  `docs/superpowers/{specs,plans}/2026-07-03-mainnet-production-environment*`。**待用户完成手动步骤 1/3/4
  （HL mainnet 钱包/secrets/GH token FLY_API_TOKEN_PROD）并授权后 push `production` 触发首部署**（ops 见 deploy/DEPLOY.md）。
```

- [ ] **Step 2: 提交（含 spec + plan）**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP add docs/STATUS.md \
  docs/superpowers/specs/2026-07-03-mainnet-production-environment-design.md \
  docs/superpowers/plans/2026-07-03-mainnet-production-environment.md
git -C /Users/thomaschang/Projects/GridTradeGP commit -m "docs(status): 登记 mainnet 生产环境 + 收录设计/计划"
```

---

### Task 7: 合并到 main + 切 `production` 分支（**不 push**）

**Files:**
- （git 操作，无文件改动）

**Interfaces:**
- Consumes: feat 分支 `feat/mainnet-prod-env` 的全部提交。
- Produces: 本地 `main` 含全部产物；本地 `production` 分支（从 main），已含 `deploy-prod.yml` + `fly.prod.toml`，**未 push**。

- [ ] **Step 1: 跑全量测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（原 456 passed + 新增 2 = 458 passed，SQLite 后端）。

- [ ] **Step 2: merge 到本地 main**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP checkout main
git -C /Users/thomaschang/Projects/GridTradeGP merge --no-ff feat/mainnet-prod-env -m "Merge: mainnet 生产环境（fly.prod.toml + deploy-prod.yml + ops）"
```

- [ ] **Step 3: 从 main 切 production（不 push）**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP checkout -b production
git -C /Users/thomaschang/Projects/GridTradeGP checkout main
```

- [ ] **Step 4: 确认分支存在且不含未授权 push**

Run: `git -C /Users/thomaschang/Projects/GridTradeGP branch -vv`
Expected: 出现 `production`（本地，无上游追踪）；`main` 领先 `origin/main`（尚未 push）。**到此停止，进入 Task 8 前的用户闸门。**

---

### Task 8: 🚦 GATED — 首次部署（**实现方不自动执行；停下等用户**）

> **本 task 不由 agent 自动跑。** 到这里必须停下，逐条提示用户完成手动步骤，核验就位后由用户明确授权，才 push。
> push `production` = 触发真钱部署。

- [ ] **Step 1: 提示并等待用户完成手动步骤 1/3/4**（DEPLOY.md 的 mainnet 节）：HL mainnet 钱包注资+agent、`fly secrets set`、`gh secret set FLY_API_TOKEN_PROD`。

- [ ] **Step 2: 只读核验就位**

```bash
fly secrets list -a gridtrade-prod          # 期望含 DATABASE_URL/HL_WALLET_ADDRESS/HL_PRIVATE_KEY/DASHBOARD_*/UNIVERSE_WHITELIST
gh secret list --repo rockingchang/GridTradeGP   # 期望含 FLY_API_TOKEN_PROD
```

- [ ] **Step 3: 用户显式授权后，push（先 main 同步，再 production 触发部署）**

```bash
git -C /Users/thomaschang/Projects/GridTradeGP push origin main         # 同步仓库（仅触发 testnet CI，无害）
git -C /Users/thomaschang/Projects/GridTradeGP push origin production   # ← 真钱触发：deploy-prod.yml test→deploy
```

- [ ] **Step 4: 验收**（DEPLOY.md「上线验收」）：`fly logs`/心跳/面板登录/**实测 panic 熔断**/首个小网格开仓记账正确。

---

## Self-Review

**1. Spec coverage：**
- §1 拓扑 → Task 2（fly.prod.toml）、前置步骤 2（已建 app/PG）、Task 7（分支）。✅
- §2 分支/部署流 → Task 3（deploy-prod.yml）、Task 4（ci.yml）、Task 7/8（分支+push）。✅
- §3 fly.prod.toml 两处偏离 → Task 2（release_command、SCHEDULER_RUN_ON_START）+ Task 1（守护空库行为）。✅
- §4 安全边界 → Task 8 GATED + 验收测熔断。✅
- §5 手动步骤 → Task 5（DEPLOY.md）+ Task 8。✅
- §6 验收 → Task 5/8。✅
- §7 代码产物 1~6 → Tasks 1~7 一一对应。✅

**2. Placeholder scan：** 仅保留用户须自填的 `<强密码>`/`0x...`/`<...mainnet 币池>`（属手动步骤，非计划缺口）。无 TBD/TODO。✅

**3. Type consistency：** `migrate(store)->list`、`run(action,*,store_factory=)`、`StateStore.in_memory()`、`add_grid_fills_fee/add_grids_fuse_oids` 返回 `'added'/'skipped'`——均与 `gridtrade/runtime/dbadmin.py` 现有签名一致；测试断言 `[('add_grid_fills_fee','skipped'),('add_grids_fuse_oids','skipped')]` 对齐 `migrate()` 返回结构。✅
