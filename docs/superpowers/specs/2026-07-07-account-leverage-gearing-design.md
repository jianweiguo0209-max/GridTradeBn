# 仓位参数体系重构:GRID_GEARING + ACCOUNT_LEVERAGE 设计

> 状态:待用户 review。背景讨论 2026-07-07(HL 保证金语义实测 + frac 风险预算回测,见 memory `pv-thr-sweep-and-reservoir-loader`)。

## 背景与动机

1. **`leverage × max_rate` 是纯冗余参数对**:全系统唯一消费点是 `order_num = cap × leverage × max_rate / Σ价`(engine `grid_order_info` / executor / dashboard 图表三处同一公式)。`set_leverage` adapter 方法存在但零调用(HL 按资产 maxLeverage 收保证金,从未设置仓位杠杆);代码中无 maxLeverage 过滤。两个参数只以乘积 3.4(=5×0.68)起作用——这一冗余已经造成过一次真实校准事故(回测 max_rate=0.5 vs 实盘 0.68,pnlRatio 振幅失真 1.36×,commit d73f363 修复)。
2. **用户的风险预算需求缺一等公民表达**:"最坏净敞口 ≤ K×equity、最多 N 仓"目前要手工换算成 CAP_EQUITY_FRAC(如 5× → 0.245),换算依赖 gearing 且易错。
3. **HL 保证金实测(2026-07-07)**:`totalMarginUsed` 只计净持仓保证金,挂单预留仅 ~1.6% 名义额——中性网格保证金占用天然 10-20%,"保证金≤80%equity"永不绑定;**真实风险约束 = 最坏单侧净敞口**(单格 = gearing/2 × cap,12 格同侧扫穿 = N × gearing/2 × frac × equity)。

## 新参数体系

| 新参数 | 语义 | 默认 | 替代 |
|---|---|---|---|
| **`GRID_GEARING`** | 单格名义部署倍数:一格挂单总名义额 = gearing × cap(双侧各 ≈ gearing/2) | **3.4**(=5×0.68,行为不变) | `LEVERAGE`(5) 与 executor 内置 `max_rate`(0.68) |
| **`ACCOUNT_LEVERAGE`** | 账户最坏净敞口倍数:12 格全部同侧扫穿时 净敞口/equity 的上限 | **2.0**(≈现行 frac0.10@12仓,行为不变) | `CAP_EQUITY_FRAC`(降级为推导值) |

**推导公式**(取代手工 frac):

```
frac = ACCOUNT_LEVERAGE / (MAX_CONCURRENT × GRID_GEARING / 2)
cap  = clamp(equity × frac, CAP_MIN, CAP_MAX)          # compute_cap 不变
```

例:ACCOUNT_LEVERAGE=5, MAX_CONCURRENT=12, gearing=3.4 → frac = 5/20.4 = 0.245。

注:gearing/2 的 /2 = 中性网格双侧梯子、最坏只能吃到单侧(价格不能同时在带两端;来回震荡时反向成交互相抵消)。等比网格上侧名义额略大(~52-55%),/2 为近似,误差在锁定率不确定度内;止损体系(固定止损/PV/破网保险丝)会在扫穿前截断,该口径本身即保守上限。

## 安全不变量(不可妥协)

1. **绝不复用旧 env 键换语义**:启动时检测到 `LEVERAGE` 或 `CAP_EQUITY_FRAC` 已设置 → **响亮报错退出**,错误信息给出新键与换算公式(防旧部署静默变成 2.45× 加仓)。
2. **引擎 API 不动**:`simulate_grid_engine(leverage, max_rate)` / `grid_order_info` 签名与默认全保留(金标零风险);executor 内部调用改传 `(gearing, max_rate=1.0)`,数学恒等。
3. **回测管线不动**:backtest 侧 `leverage=5/max_rate=0.68` 显式传参已对齐(d73f363),本期不碰;两套参数化的等价性由换元测试钉住(见测试)。

## 改动面

| 文件 | 改动 |
|---|---|
| `gridtrade/config.py` | DeployConfig:去 `leverage` 字段、加 `grid_gearing`/`account_leverage`;`load_deploy_config` 读新键 + 旧键报错;新增 `derive_frac(account_leverage, max_concurrent, gearing)` 纯函数 |
| `gridtrade/execution/grid_executor.py` | `(leverage, max_rate)` 属性 → `gearing`;`grid_order_info(cap, self.gearing, ..., max_rate=1.0)`;`_resolve_cap` 用推导 frac;开格落库 `grids.leverage` 列**存 gearing**(列名不改,语义文档化) |
| `gridtrade/execution/gates.py` | MinNotionalGate 读 executor sizing 表面:`leverage×max_rate` → `gearing` |
| `gridtrade/dashboard/gridchart.py` | `grid_order_info(grid.cap, _effective_gearing(grid.leverage), ..., max_rate=1.0)`;`_effective_gearing(v) = v×0.68 if v>4.5 else v`(共享小助手,兜住未迁移的历史 CLOSED 行——迁移只动非 CLOSED,旧关闭格图表仍按原口径重算) |
| `gridtrade/runtime/dbadmin.py` | 一次性数据迁移 `normalize_grid_leverage_to_gearing`:`UPDATE grids SET leverage = leverage × 0.68 WHERE leverage > 4.5 AND status != 'CLOSED'`(幂等:gearing≈3.4<4.5、旧值恒 5.0>4.5;CLOSED 行只作历史展示不参与重算,不动) |
| `deploy/fly.toml` + `fly.prod.toml` | 去 `CAP_EQUITY_FRAC`;加 `GRID_GEARING="3.4"`、`ACCOUNT_LEVERAGE="5"`、`MAX_CONCURRENT="12"`(部署值=用户 5× 预算) |
| 测试 | ①换元等价:新 (gearing=3.4, max_rate=1.0) 与旧 (5, 0.68) 的 order_num 逐位相等;②derive_frac 纯函数(5/12/3.4→0.245);③旧 env 键报错;④迁移幂等差分(5.0→3.4,重跑不再动);⑤MinNotional/图表口径回归;全套 |

## 部署值与风险声明(用户 5× 预算)

`ACCOUNT_LEVERAGE=5 × MAX_CONCURRENT=12` → frac=0.245,cap≈$744(equity $3,035)。账户级回测(pv_frac_equity.csv,实盘口径格集):串联 10 月 +59.2%、最差窗已实现 MDD −5.5%、实测峰值敞口 5.13×。**风险如实**:已实现口径低估真 MDD(格内浮亏不可见);格集无币锁、绝对值偏乐观;深尾(满敞口+10% 跳空)理论 −50% equity;PV 换形前向验证尚未积累。执行者建议(留档):先 ACCOUNT_LEVERAGE=2.5~3 过前向验证再升 5;**部署值按用户预算 5 执行**。

## 上线路径

1. 实现+全套测试绿 → 用户批 push main → testnet 部署(release_command 跑迁移);
2. testnet 核验:启动无旧键报错、新格 cap≈frac×equity、存量格迁移后补单尺寸连续、MinNotional 正常;
3. 用户批 production push(真钱,5× 预算生效)。
4. 回滚:revert + toml 还原 + 迁移逆操作(leverage ÷0.68 WHERE <4.5,或等格自然轮换)。

## 非目标

- 引擎/回测参数化(保持 leverage×max_rate 显式传参,已对齐);
- 混合保底 `min(equity×frac, cash×0.95)`(保证金非瓶颈,YAGNI);
- `set_leverage` 调用(维持不设仓位杠杆的现状);
- funding/滑点建模改进。
