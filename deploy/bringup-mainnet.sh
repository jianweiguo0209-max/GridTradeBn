#!/usr/bin/env bash
# GridTradeBi — Mainnet 一键上线交互脚本（币安 USDT-M mainnet，真金白银）。
#
# 逐步提示输入并落地：① Fly app 名 ② Binance API 凭证 ③ Dashboard 登录
# ④ GitHub CD 部署 token+app 变量（自动）⑤ 启用 offset 数组（默认全开）；
# 全部就位后推 production 触发 GitHub Actions 的 mainnet CI/CD（.github/workflows/deploy-prod.yml）。
#
# 设计说明：
#  - 敏感/凭证项（②③④）→ fly secrets / gh secret+variable，不入库。
#  - 部署配置 offset（⑤）→ 写进版本控制的 deploy/fly.prod.toml [env]，随 CD 部署（避免 secret 与 toml 漂移）。
#  - 账户模式（单向持仓 + 单一资产）是引擎 boot 硬门，脚本无法代改币安 UI → 上来先确认。
#  - 幂等：app/PG 已存在则跳过创建，可安全重跑。
#
# 前置：本机已装并登录 flyctl（fly auth login）与 gh（gh auth login）；在仓库根目录运行；
#       已在币安把合约账户切【单向持仓】+【单一资产/关闭联合保证金】。
# 用法：bash deploy/bringup-mainnet.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PY="${PYTHON:-.venv/bin/python}"; [ -x "$PY" ] || PY="python3"
TOML="deploy/fly.prod.toml"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local p="$1" d="${2:-}" v; if [ -n "$d" ]; then read -r -p "$p [$d]: " v; printf '%s' "${v:-$d}"; else read -r -p "$p: " v; printf '%s' "$v"; fi; }
app_exists() { fly status -a "$1" >/dev/null 2>&1; }

# ---- 0. 环境检查 ----
command -v fly >/dev/null || die "未找到 flyctl（先装并 fly auth login）"
command -v gh  >/dev/null || die "未找到 gh（先装并 gh auth login）"
fly auth whoami >/dev/null 2>&1 || die "flyctl 未登录：fly auth login"
gh auth status  >/dev/null 2>&1 || die "gh 未登录：gh auth login"
[ -f "$TOML" ] || die "未找到 $TOML —— 请在仓库根目录运行"

say "GridTradeBi Mainnet 上线 —— 真金白银环境，逐步确认（Ctrl-C 可随时中止）"

# ---- 账户模式前置门（币安 UI，脚本无法代劳）----
warn "引擎 boot 硬门 assert_account_mode：币安 U 本位合约账户必须是"
warn "  1) 单向持仓（One-Way，非双向 / hedge）"
warn "  2) 单一资产（Single-Asset，关闭「联合保证金 Multi-Assets」）"
warn "否则 scheduler/monitor 一启动就崩溃循环到停机（拦在下单前，无资金风险，但不会交易）。"
[ "$(ask '已在币安设好这两项？输入 yes 继续')" = "yes" ] || die "请先到币安 U 本位合约→设置切好这两项再重跑。"

# ---- ① Fly app 名 + Postgres ----
say "步骤 1/5 — 自定义 Fly app 名（多实例防冲突，CD 从仓库变量 FLY_APP_PROD 读取）"
APP="$(ask 'Fly app 名' 'gridtrade-bi-prod')"
PG="$(ask 'Postgres 集群名' 'gridtrade-pg-bi-prod')"
REGION="$(ask '主区域' 'nrt')"

if app_exists "$APP"; then say "app $APP 已存在，跳过创建"; else
  say "创建 app $APP"; fly apps create "$APP" --org personal
fi
if app_exists "$PG"; then say "Postgres $PG 已存在，跳过创建"; else
  say "创建 Postgres $PG"
  fly postgres create --name "$PG" --org personal --region "$REGION" \
    --vm-size shared-cpu-1x --volume-size 1 --initial-cluster-size 1
fi
say "attach $PG → $APP（自动设 DATABASE_URL；已 attach 会报错，可忽略）"
fly postgres attach "$PG" --app "$APP" || warn "attach 返回非零（通常=已 attach），继续"

# ---- ② Binance mainnet API 凭证 ----
say "步骤 2/5 — Binance mainnet API 凭证（真钱；建议只开合约交易、禁提现）"
BKEY="$(ask 'BINANCE_API_KEY')"
read -r -s -p "BINANCE_API_SECRET（隐藏输入）: " BSECRET; echo
[ -n "$BKEY" ] && [ -n "$BSECRET" ] || die "API key/secret 不能为空"
fly secrets set --app "$APP" --stage BINANCE_API_KEY="$BKEY" BINANCE_API_SECRET="$BSECRET"

# ---- ③ Dashboard 登录 ----
say "步骤 3/5 — Dashboard 登录（密码转 pbkdf2 哈希，绝不存明文）"
DUSER="$(ask 'DASHBOARD_USER' 'admin')"
read -r -s -p "Dashboard 密码（隐藏输入）: " DPASS; echo
[ -n "$DPASS" ] || die "密码不能为空"
DHASH="$(DPASS="$DPASS" "$PY" -c 'import os;from gridtrade.dashboard.auth import hash_password;print(hash_password(os.environ["DPASS"]))')" \
  || die "生成密码 hash 失败（确认依赖已装 / .venv 就绪）"
fly secrets set --app "$APP" --stage \
  DASHBOARD_USER="$DUSER" DASHBOARD_PASSWORD_HASH="$DHASH" DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)"

# ---- ④ GitHub CD：部署 token（secret）+ app 名（variable），自动 ----
say "步骤 4/5 — GitHub CD 凭证（自动设置）"
REPO="$(ask 'GitHub 仓库 owner/name' 'rockingchang/GrideTradeBi')"
say "签发 fly deploy token（按 app 签发）→ GH secret FLY_API_TOKEN_PROD"
fly tokens create deploy -a "$APP" | gh secret set FLY_API_TOKEN_PROD --repo "$REPO"
gh variable set FLY_APP_PROD --body "$APP" --repo "$REPO"
say "已设 GH secret FLY_API_TOKEN_PROD + variable FLY_APP_PROD=$APP"

# ---- ⑤ 启用 offset 数组 → 写入 fly.prod.toml [env]（版本控制）----
say "步骤 5/5 — 启用 offset 数组"
say "  合法 offset ∈ [0, SCHEDULER_PERIOD 小时数)；直接回车 = 全 offset 开（默认，零行为变更）"
warn "  方案B：非空时 cap frac 分母改为启用数 N（满配达目标 AL，但 N 越小单币杠杆越集中，灰度慎用）"
OFFSETS="$(ask '启用 offset（CSV，如 2,4；空=全开）' '')"
if [ -n "$OFFSETS" ]; then
  LINE="  LIVE_OPEN_OFFSETS = \"$OFFSETS\""
else
  LINE="  # LIVE_OPEN_OFFSETS = \"0,6\"   # 空=全 offset 开（默认，零行为变更）"
fi
OFFSET_LINE="$LINE" "$PY" - "$TOML" <<'PYEOF'
import os, re, sys
path, new = sys.argv[1], os.environ['OFFSET_LINE']
src = open(path, encoding='utf-8').read()
pat = re.compile(r'^[ \t]*#?[ \t]*LIVE_OPEN_OFFSETS[ \t]*=.*$', re.M)
if not pat.search(src):
    raise SystemExit('未在 %s 找到 LIVE_OPEN_OFFSETS 行' % path)
open(path, 'w', encoding='utf-8').write(pat.sub(new.replace('\\', r'\\'), src, count=1))
print('offset 行已写入:', new.strip())
PYEOF

# ---- 触发 GitHub Actions mainnet CI/CD ----
say "最后一步 — 提交并推 production 触发 mainnet CI/CD（deploy-prod.yml）"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || warn "当前分支是 $BRANCH（通常从 main 上线）"
echo; echo "  app         = $APP"
echo "  postgres    = $PG"
echo "  offset      = ${OFFSETS:-全开}"
echo "  GH repo     = $REPO"
echo
[ "$(ask '以上确认无误、立即触发真钱 mainnet 部署？输入 deploy 继续')" = "deploy" ] || die "已中止（未推送；secrets 已 stage，可稍后手动部署）。"

git add "$TOML"
if git diff --cached --quiet; then
  git commit --allow-empty -m "deploy(prod): 触发 mainnet CD（offset=${OFFSETS:-全开}）"
else
  git commit -m "deploy(prod): LIVE_OPEN_OFFSETS=${OFFSETS:-全开} → mainnet 上线"
fi
git push origin "$BRANCH"
git push origin "HEAD:production"   # production=部署指针分支（勿直接提交）；推它触发 deploy-prod.yml

say "已推 production —— GitHub Actions 正在跑 test → deploy。"
RUN_ID="$(gh run list --workflow='Deploy Mainnet' -L1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
echo "  跟踪部署 : gh run watch ${RUN_ID:-<run-id>} --exit-status"
echo "  看日志   : fly logs -a $APP        （scheduler 应打印 open_offsets=${OFFSETS:-全开}）"
echo "  开面板   : fly open  -a $APP"
say "完成。首次空库由发布钩子 create && migrate 自动建表；下单前 assert_account_mode 会再核账户模式。"
