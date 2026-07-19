# 方向性 PV 止损(pv_directional)设计(2026-07-19)

## 动机

重扫 v2 实证 pv 的 regime 敏感性:pv 是第一大退出路径(~54%),但无差别开火——顺风窗把**恢复中的仓**
砍在 pnl 门槛下(IS 窗 pv:off Calmar 17.6 vs on 14.4,机会成本显著;mainnet 1000BONK 实证:net long
微亏 −0.078% 被砍,回测显示 ride 到 +0.96%)、把**零成交格**提前关掉(mainnet 0G 实证:零成交被 pv 关,
放弃剩余 10h 成交机会)。方向化 = 结构性对症:保留逆向保护,去掉顺向/零仓误伤。

## 语义(触发矩阵)

pv 尖峰按同窗价格方向分涨/跌;由当前净仓符号门控:

| 净仓 | 跌尖峰(dir<0) | 涨尖峰(dir>0) |
|---|---|---|
| 净多 hold>0 | **触发** | 不触发 |
| 净空 hold<0 | 不触发 | **触发** |
| 零仓 hold=0 | 不触发 | 不触发 |

pnl 门槛不变(触发后仍需 pnlRatio < pv_pnl_thr)。**资金费率止损同加零仓门控**(零仓关格无经济意义、
白弃剩余窗口;方向不判——funding 与仓位方向的交互不在本案)。

自洽性:中性网格逆势天然累积逆向仓(跌→接多、涨→转空),逆向尖峰恰在亏损扩大方向开火 → 趋势保护保留;
被去掉的只有顺向尖峰(回本中被砍)与零仓砍格两类误伤。

## 方向判据(用户定 2026-07-19)

`pv_dir(t) = sign(close_t − close_{t−active_period})`(1m 序列 shift win,win=period 内 bar 数)——
与方案C 量能滚动窗**同窗同源**,无新参数。窗口不足/平价 → dir=0 → 不触发(保守)。

## 改动点(两侧同源)

- **共享** `core/grid_engine.calc_pv_spike`:返回列 +`pv_dir`(+1/−1/0)。缺 close 列 → dir 恒 0(fail-soft)。
- **回测** `_apply_exit`:flag 开时 pv mask 叠 `(hold>0 & dir<0)|(hold<0 & dir>0)`(hold=df['hold_num']);
  funding mask 叠 `hold≠0`。零成交分支自然归零(hold≡0 恒不触发 → '未触网'),无需特判。
- **实盘** `signals.LiveSignalProvider`:`get` 返回 `(pv_spike, pv_dir, funding_rate)`(3 元组,
  dir 取 calc_pv_spike 最后一行,同源);调用方 manager/cycles 两处解包同步。
  `core/stop_rules.evaluate_exit`:+`pv_dir`/`net_position` 参数;flag 从 stop_cfg 读;
  `net_position=None` = 门控不可用 → fail-open 回旧行为(接线测试钉住 monitor 必传)。
  `monitor_grid`:传 `snap['net_position']`(live_equity snapshot 现成)+ pv_dir。
- **参数** `DEFAULT_STOP_CFG['pv_directional'] = False`(默认关,零行为变更);sweep `live_baseline`/
  `run_arm` stop_cfg 增该键(可开臂 A/B)。

## 与零成交修复(284fe1d)的关系

flag 开时零成交格回到"未触网/(资金费也不触发)"——语义升级非回退:两侧一起改,对齐性质保持。
flag 关 = 284fe1d 现状,金标/既有测试零漂移。

## 验证

1. TDD:方向门控矩阵(多/空/零 × 涨/跌尖峰)、flag=False 回归、标量↔向量等价、signals dir、monitor 接线。
2. 回测 A/B:directional on/off × 四调参窗 + 留出窗,最差窗主序。假说:IS/W2 回收机会成本,OOS/HOLD-A 保护基本保留。
3. 实盘案例复盘:1000BONK(不被砍)/0G(ride)。
4. 胜出才翻默认 → testnet → mainnet。

## 终判(2026-07-19 A/B 实测):不采纳,flag 恒 False

四窗(ON vs OFF Calmar):W1 **7.3→0.8(崩)**、W2 15.5→17.4(小胜)、OOS −3.0→−3.1(平)、
IS 14.4→12.1(差,+1破网)。1小胜:1平:2败,W1 毁灭性。

**机制结论:pv 的价值有一半在「无差别」本身。** 震荡窗(W1)价格来回穿越、仓位方向频繁翻转,
"顺向尖峰"多为假回本信号——旧 pv 无差别砍掉它们恰是在震荡市控频止损;方向化剪掉这层后 W1 崩。
1000BONK 单例("顺向尖峰砍在恢复中")是幸存者偏差,统计上顺向尖峰的退出平均是好退出。
IS 的 +1 破网亦证:保护松一分,尾部就冒头。

代码保留(默认 False、全量测试覆盖、零运行成本),作为已验证否定的假设归档,勿再重跑。
