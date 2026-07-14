# 币安独立环境部署（取代同库延续） 设计

> 状态:**已获用户批准(2026-07-14)**。用户决策:币安 testnet/mainnet 部署到**全新虚拟环境**,
> 与 HL 的两个既有环境零共享、互不冲突。命名(用户定):testnet=`gridtrade-bi-test`、
> mainnet=`gridtrade-bi-prod`,PG 同后缀(`gridtrade-pg-bi-test`/`gridtrade-pg-bi-prod`)。

## 一、决策与取代关系

本决策**取代** 2026-07-14-binance-migration-design.md 的两处语义:
1. **④同库延续 → 新库重开**:币安时代从空库起步;HL 历史完整保留在旧环境各自的库
   (gridtrade-pg / gridtrade-pg-prod)随时可查,只是不再与币安记录同库、盈亏曲线不跨所拼接。
2. **§7.2 阶段 2 与上线解耦**:HL 生产退场不再是币安上线的前置硬门槛(不共库,无"币安适配器
   撞 HL 残留网格"问题)——HL 环境冻结续跑,退场时机独立决定;硬门槛 SQL 降级为退场时的
   对账清点。

旧环境处置:`gridtrade-hl`/`gridtrade-prod`(+各自 PG)为 **HL 遗留环境**,跑旧代码、冻结保留,
不再是任何部署目标;GitHub 侧 CI 变量/令牌全部指向新环境。

## 二、落地改动(全部依托 2026-07-14-fly-app-parameterization 的参数化机制)

- **fly.toml(testnet)发布钩子**:`migrate` → `create && migrate`——全新空库裸 migrate 会因
  表不存在失败 abort 发布(fly.prod.toml 既有同理注释);此坑仅在新环境暴露,故随本决策修。
- **两 toml/两工作流注释与报错文案**:主实例名 → gridtrade-bi-test / gridtrade-bi-prod。
- **GitHub 侧**(用户操作):Variables `FLY_APP_TESTNET=gridtrade-bi-test`、
  `FLY_APP_PROD=gridtrade-bi-prod`;Secrets `FLY_API_TOKEN`/`FLY_API_TOKEN_PROD` 按新 app
  重新签发替换(deploy token 按 app 绑定,旧值只能部署 HL app)。
- **DEPLOY.md**:部署目标全面转向新环境;§1 改 `fly apps create`;§8 明确 testnet app 永不切
  真钱 key(mainnet 走独立生产环境);Mainnet 章前置改为新建四命令;HL 环境标注冻结遗留。
- **runbook**:阶段 1/3 指向新环境(含新建步骤);阶段 2 重标「独立事项,时机自定」,
  增可选彻底收摊步骤;阶段 3 删除 HL_* unset(新 app 无包袱)。
- **scripts/testnet_status.sh**:默认 `FLY_APP=gridtrade-bi-test`(可 env 覆盖查 HL 遗留环境)。

## 三、验证

workflows YAML / tomls TOML 结构断言(无 app 键、守卫步在位、testnet 钩子含 create)+
文档中旧 app 名仅存于「HL 遗留环境」语境的 grep 检查。真实部署验证=按 DEPLOY.md §1-§7
首次建站流程(用户执行,引导进行中)。

## 四、明确不做

- 不动 HL 两环境的任何 fly 资源/secrets/数据;
- 不做 HL 历史向新库的数据迁移(留档查询即可,需要时另立项);
- 面板不做跨库聚合视图(币安面板只看币安库)。
