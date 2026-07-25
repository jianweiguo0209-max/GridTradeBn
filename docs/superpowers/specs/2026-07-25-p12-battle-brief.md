# p12_cross1 × St4/St5 组合战役 · 交接 Brief

**日期**：2026-07-25 · **发起**：反事实评价器 session（探针全图谱已完成）
**你的任务**：把探针候选放进**组合级正式回测**（真选币回放+12offset+同币cap+资金分配+Calmar 裁判），按三道门+留出纪律裁决。探针只证明了"单格配对口径有肉"，组合级能不能兑现（尤其同币 cap 折扣与并发结构）只有这场仗能回答。

---

## 1. 候选定义（全部冻结，禁止再调参）

### 选币器 p12_cross1（燃料 top-1）
- 因子 = 选币时刻 R 往回看 12h 的 1% 对数阶梯跨越数：
  `step=floor(ln(close_1m)/ln(1.01)); cross1=Σ|Δstep| over [R−12h, R)`
- 权威实现：`data/score_research_2026-07-21/holdout_gate.py:59-72`（`_label_one`）；
  PIT 语义与 join（标签 rt+12h=R）见 `p12_probe.py:31-34`，已过 250/250 逐位 PIT 审计管线同构。
- 战役接法（推荐）：**预计算因子表注入**——用现成 `sc_labels_*` / `hold_labels_*`
  （rt/symbol/cross1）平移 +12h 得 (run_time,symbol,p12)，选币时对票池 join 后按 p12 降序取 top-1。
  ⚠不要往 `cal_factor` 里塞（它吃 1h→12H 重采样，算不了 1m 阶梯）；自定义 select_fn 或
  改 replay 的排序输入均可，**但现役 rank_sum 臂必须走原路不动**（锚）。
- 开放问题（不阻塞战役，记录即可）：实盘化时 1m×全池每小时取数的权重成本，及 15m 近似
  cross1 的保真度——战役用回测 1m 缓存，无此问题。

### 执行链 St4 / St5（"定制链 v2"家族，仅对 p12 臂使用）
| 参数 | St4 | St5 | s030 现役（对照/锚） |
|---|---|---|---|
| stop_loss 固损 | **0.04** | **0.05** | 0.03 |
| trailing_floor | **0.01** | **0.01** | 0.02 |
| trailing_k | **0.15** | **0.15** | 0.3 |
| pv_mult | **5** | **5** | 3 |
| pv_pnl_thr | −0.01 | −0.01 | −0.01 |
| pv_n / pv_period | 100 / 15min | 同 | 同 |
| fundingRate_stop_loss | 0.0015 | 0.0015 | 0.0015 |
| 建网几何 | V2 现值（band3×ATR/cmin16）不动 | 同 | 同 |

（F30/F99 费率变体已定性为资金费 carry，**不进本战役**；带符号费率止损是引擎改动，另立项。）

## 2. 臂位（组合级，全 12 offset）

1. **锚臂**：现役 rank_sum × s030 —— 必须先逐位复现历史战役锚（geo_final 系产物/pdetail_*），锚不平停手查保真度。
2. p12top1 × s030 原链（选币器单变量）
3. p12top1 × St5（候选主臂）
4. p12top1 × St4（家族兄弟）
5. （可选，若 harness 支持自定义 select_fn 分 offset）6+6 混合：偶 offset rank_sum×s030、奇 offset p12×St5 —— 探针显示两选币器逐轮零相关、混合无亏损窗。

同币 cap（tier2_cap=2）、资金分配、并发结构一律用生产现值——**这正是探针算不了、战役要验的东西**（探针 cap 模拟显示 W2 惩罚、HOLD-B 无损，但那是采样近似）。

## 3. 纪律红线（读三遍）

- **⚠已污染声明**：St4/St5 参数是在 W1/W2/OOS/HOLD-A/HOLD-B **全部五窗**上坐标下降选出的（~40 探索臂）；p12 本身在 W1/HOLD-B 上探索、HOLD-A/W2/OOS 已读数。**这六个窗对本战役全部只算"判定窗"，没有一个能当留出。**
- **新鲜留出必须新开**：档案未被任何探针触碰的时段——建议
  **HOLD-C = 2025-04-01 ~ 2025-05-31**、**HOLD-D = 2024-12-01 ~ 2025-01-31**
  （先验 vision 归档覆盖，缺月用 vision 下载器补；标签/因子用 `holdout_gate.py` 的
  stage L/F 模式对新窗重建，~10min/窗/2workers）。
- **预注册判据写在跑留出之前**（建议模板，你落笔定稿）：
  主判据 = p12×St5 在 HOLD-C 与 HOLD-D **均** Calmar ≥ 锚臂 且 MDD ≤ 锚臂×1.3（最差窗规则）；
  副判据 = p12×s030 臂同报（分离选币器与链的贡献）。判定窗结果只做布线自检，不参与裁决。
- **本项目留出斩杀率 7/7**（pvloose/idioZ3/cmin20/p12_cross1-IC版/shock/offset/time_decay 全灭，多个反号）——默认预期是死，活着才是新闻。见 memory `research-methodology`、`inventory-deterioration-auc-ceiling` 元结论。
- IS（2026-03~06）的 p12 预注册裁决由原 session 出，别重复消费。

## 4. 工程与机器

- 战役 harness 先例：`data/score_research_2026-07-21/geo_final_bt.py`（BT_WINDOWS 窗级并行参数）；
  组合级产物先例 pdetail_*。选币回放瓶颈 ~90min/窗（memory `selection-replay-speedup-todo`，
  若要提速必须锚 diff==0 才可替换，勿顺手优化）。
- **16GB 机器**：全程 ≤2 并发重进程；多进程守 `__main__`；`BT_SEL_WORKERS=1` 是故意的。
- **协调**：本机另有 IS-b 反事实全量在跑（预计 2026-07-25 落地）+ 其后终裁批处理；
  开重仗前 `ps aux | grep cf_run` 确认让路，或从标签重建/归档补齐等轻活起手。
- 新增可用加速：`ParquetCache.read_days_range`（按窗读天，已锚验）。

## 5. 汇报格式

每完成一段先报数再记账：①锚复现结果 ②判定窗四臂 Calmar/MDD/收益表 ③（预注册后）留出裁决表。
探针全量数据与结论在 `data/score_research_2026-07-21/ablation/`（p12*_probe 系列 +
p12_probe_curves.md）与 `.superpowers/sdd/progress.md` 台账，可引用不必重跑。

---
## 终裁注记（2026-07-25，IS 落地后）
- **p12×s030 原链：预注册 FAIL**（四未见窗合并 +6.9bp/t1.06，符号 3/4 过、幅度不过）——该臂降级为"已死对照"。
- **p12×St5 在 IS（链参数干净窗）alpha +21.9bp/t2.38、St4 +18.3/t2.05**——战役核心问题=此边在 HOLD-C/D 新鲜留出与真 cap 下是否幸存。
- F30/F99 在 IS 无增益（+18.9/+20.8 < St5）——carry 定性获干净窗验证，维持排除。
- E0→s030 转化率 IS 实测 9%（原链）→ St5 切下 3.6×——"链参数人群条件化"论点成立。
