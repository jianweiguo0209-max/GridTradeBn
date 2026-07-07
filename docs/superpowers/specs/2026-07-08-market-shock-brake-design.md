# MarketShockBrake(组合级急动刹车)设计

> 状态:设计=回测验证的接线草案(用户已批实现,2026-07-08)。依据:memory `shock-brake-verdict` +
> `data/tiercmp/sb_results.csv`(76 仿真)/`sb_pv_interact.csv`(24 仿真)。

## 背景

网格结构性逆势,全市场同向急动同时打满整队格(AL=3.5×12 仓下最坏日 ≈ 权益 13-18%)。
四窗双臂回测 GO:**信号 = 票池横截面中位数 4h 收益,|med|≥4% → 暂停开新格 2h**(存量不动)——
事件捕获 37/37、四窗 Δ收益合计 0.00(免费保险)、被拦格基线净亏、MDD 全面改善;
与 PV +0.005 互补非替代(交互扫描证实,无需重调 PV)。

## 机制与参数

- 每次 scheduler 整点运行:用**本轮已拉的 universe 1h K 线**(`fetch_universe_candles` 产物,零额外 API)
  计算 `med = median(close[-1]/close[-1-k] − 1)`(PIT:只用 candle_begin_time < run_time 的收盘 bar);
- `|med| ≥ SHOCK_THR` → 本轮**只关不开**(换仓关格照常,开格跳过)+ 记 `shock_until = run_time + SHOCK_PAUSE_HOURS`;
- 后续轮 `run_time < shock_until` 同样只关不开;
- 配置(env,DeployConfig):`SHOCK_THR=0.04`(**≤0 = 停用**)、`SHOCK_K_HOURS=4`、`SHOCK_PAUSE_HOURS=2`。
  代码默认即部署值(与回测推荐一致);两 toml 显式写入。

## 关键设计决策

1. **只暂停开格,换仓关格照常**:`run_scheduler_cycle` 加 `open_enabled=True` 参数,False 时跳过
   `trigger_engine.collect + open_proposals`(closed 流程原样)——过期 tag 不滞留。
2. **暂停状态进程内持有(`rt` 上挂 `_shock_until`),不落 DB**:信号自持——阶跃冲击后 med_4h
   在 ~k 小时内持续 ≥thr,重启丢状态由下轮信号重算自愈。**约束 X ≤ k**(2≤4 ✓);若 env 配出
   X>k 打 WARN(重启窗口可能漏暂停)。
3. **fail-open**:篮子有效币 < 5 或计算异常 → 不刹车 + 日志(与地板/信号降级同哲学:数据缺失不阻塞主流程)。
4. **篮子 = 本轮 candles dict 的币**(floor 过滤后 universe − held 预剔)——与回测 PIT $1M 池口径一致
   (差异仅 held ≤12 币,可忽略)。
5. 结构化日志:触发 `[shock] med_4h=-5.2% |≥4.0%| → 暂停开格至 HH:MM`;暂停中
   `[shock] braked until HH:MM(还剩 N 轮)`;monitor/止损/存量格路径零改动。

## 改动面

| 文件 | 改动 |
|---|---|
| `gridtrade/runtime/shock.py`(新) | `cross_median_k(candles, run_time, k_hours) -> float|None` 纯函数(PIT 截断、≥k+1 根、有效币<5 → None) |
| `gridtrade/config.py` | DeployConfig 三字段 + env 解析(SHOCK_THR/SHOCK_K_HOURS/SHOCK_PAUSE_HOURS) |
| `gridtrade/runtime/scheduler.py` | `run_scheduler_once`:fetch_candles 后算信号→定 braked→传 `open_enabled` + 记/读 `rt._shock_until` + 日志 |
| `gridtrade/runtime/cycles.py` | `run_scheduler_cycle(..., open_enabled=True)`:False 跳过 collect/open,result 加 `shock_braked` |
| `deploy/fly.toml`+`fly.prod.toml` | `SHOCK_THR="0.04"`/`SHOCK_K_HOURS="4"`/`SHOCK_PAUSE_HOURS="2"` |
| 测试 | shock 纯函数(触发/方向/数据不足 fail-open);scheduler 集成(冲击→关格照常+零开格+暂停窗持续+过窗恢复+停用旁路);config 解析;全套回归 |

## 上线路径

实现+全套绿 → 报用户批 push main → testnet(GH Actions)→ 观察(触发日志/无误拦)→ production 单独批。
回滚:env `SHOCK_THR=0` 即停用(零部署),或 revert。

## 非目标

- v1 不做主动减仓/平存量格;
- 不做多级阈值/自适应 thr;
- 信号不落库(不进 dashboard,v2 可加)。
