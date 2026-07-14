# fly 部署 app 名参数化（多实例防冲突） 设计

> 状态:**已获用户批准(2026-07-14)**。回退策略用户选**严格必填**(B):变量未设置=部署报错退出,
> 不回退默认名——忘设变量静默部署到共享名正是要防的冲突,fail-fast 沿仓库退役键守卫惯例。
> 实施方式:用户批准直接实施(小改动,不走子代理流程)。

## 一、动机

`deploy/fly.toml`/`fly.prod.toml` 写死 `app = "gridtrade-hl"/"gridtrade-prod"`,同一项目部署
多个实例(fork/第二套生产)时,所有实例都会打到同一对 fly app 上,互相覆盖。app 名应由每个
仓库实例自己的 GitHub Actions 配置提供。

## 二、设计

**变量**(GitHub 仓库 **Variables**,非 Secrets——app 名不敏感):
- `FLY_APP_TESTNET` → `.github/workflows/deploy.yml`(主实例值 `gridtrade-hl`)
- `FLY_APP_PROD` → `.github/workflows/deploy-prod.yml`(主实例值 `gridtrade-prod`)

每个实例的隔离资产各自独立:app 名(Variables)、部署令牌(`FLY_API_TOKEN`/`FLY_API_TOKEN_PROD`,
deploy token 本就按 app 签发)、Postgres、fly secrets。

**工作流改动**(两条各两处):
1. 部署 job 加「Resolve app name (fail-fast)」守卫步:`vars.FLY_APP_*` 为空 →
   `::error::`(指路 Settings → Secrets and variables → Actions → Variables)+ `exit 1`;
2. `flyctl deploy` 追加 `--app "${{ vars.FLY_APP_* }}"`(flag 优先级最高,显式可 grep)。

**toml 改动**(两文件):删除 `app = "..."` 行,原地注释说明 app 名由 CI 变量或手动 `-a` 提供。
副作用即语义:**手动 `fly deploy -c deploy/fly*.toml` 必须带 `-a <app名>`**,不带被 flyctl
拒绝——防误部署。`primary_region`/processes/env 等其余不动。

**脚本**:`scripts/testnet_status.sh` 已支持 `FLY_APP` 覆盖(只读快照,无冲突风险),仅注释
对齐,默认值保留。

**文档**:
- `deploy/DEPLOY.md`:新增「多实例部署(app 名参数化)」小节(变量清单+新实例五步清单);
  既有手动 `fly deploy` 示例补 `-a`;
- `docs/币安切换runbook.md`:阶段1/阶段3 的 `fly deploy -c ...` 命令补 `-a`(否则照 runbook
  执行会因 toml 无 app 名失败);
- 历史 specs/plans 内的 app 名引用是历史记录,不动。

## 三、验证

无 pytest 面(yaml/toml/docs)。验证:①workflows YAML 解析通过;②tomls TOML 解析通过且无
`app` 键;③全量 pytest 无回归;④真实 CI 部署验证留给用户下次触发(主实例需先在 GitHub 设两个
Variables,文档写明)。

## 四、明确不做

- 不给 `fly launch`/`pg attach` 等一次性建站命令做参数化(本就按实例手打);
- 不改 app 命名规范/不迁移现有 app;
- ci.yml(纯测试)不涉及。
