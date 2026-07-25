# RSP111 组合战役 · 交接 Brief（验证全部 t>1.5 幸存格）

**日期**：2026-07-25 · **前情**：p12 gross/变体线已按预注册全谱收官（第 9/10 斩，勿重跑）。
探针新候选 **RSP111 = rank(Reg_v2_5↑)+rank(Sgcz_5↑)+rank(p12_cross1↓) 等权，top-1**
——六窗合并（stride-5，n=1750）出现选币侧**历史首个过线读数**。你的任务：全轮全窗正式
回测验证全部 t>1.5 的格，并在新鲜留出 HOLD-E 上终裁。

## 1. 待验格（探针读数，预期有 stride-5 乐观收缩）

全部 alpha>0 且 t>1.5 的探针幸存臂,五臂平权全验(t 是探针层尺子,战役全样本用组合口径自证,不设 t 门):

| 臂 | 探针六窗合并读数(预期收缩) |
|---|---|
| RSP111×v2固3 | +9.4bp / t2.18 |
| RSP111×St5 | +9.0 / 1.78 |
| RSP111×F30 | +8.6 / 1.61 |
| RSP111×s030 现役链 | +8.1 / 1.59（分离选币/链贡献） |
| RSP111×St4 | +7.6 / 1.58 |
| 锚：现役 rank_sum×s030 | 必须先逐位复现历史锚 |

## 2. 冻结定义（禁止再调）

**选币器 RSP111**：候选集=与 p12 臂同口径（缺 p12 标签币不参选）；
`rs = rank(Reg_v2_5, asc)+rank(Sgcz_5, asc)+rank(p12_cross1, desc)`，等权，method='first'，
每轮取 rs 最小者。Reg/Sgcz 用生产 12H 因子现值（POOL 回放里已算或从面板 join）；
p12_cross1=标签 rt+12h=R 平移（p12_final_bt.py 现成注入路径）。

**链参数**（pv 均 mult5/n100/thr−1%；trailing 均 k0.15/floor1%；funding 除注明外 0.0015）：

| 链 | stop_loss | funding阈 |
|---|---|---|
| v2固3 | **0.03** | 0.0015 |
| St4 / St5 | 0.04 / 0.05 | 0.0015 |
| F30 / F99不跑 | 0.05 | 0.003 / — |
| s030 现役链 | 0.03 | 0.0015（trailing k0.3/floor2%、pv mult3——即生产现值全套） |

## 3. 流程（顺序执行，先报数再记账）

1. **补面板**：hold_factors_HOLD-C/D（holdout_gate stage F，标签已在）→ 八窗可用。
2. **判定段（全轮八窗）**：锚复现 → 六臂 × {W1,W2,OOS,IS,HOLD-A,B,C,D} 全轮。
   汇报：各臂各窗 ret/MDD/Calmar/退出构成 + 八窗合计对照表。
3. **预注册**（判定段报完、HOLD-E 数据构建**之前**写死并 commit）：建议模板——
   ①主臂选择规则(先声明后应用):主臂 = 判定段八窗合计 Calmar 最高的 RSP 臂;
   ②部署门(唯一裁决,组合口径):HOLD-E 上主臂 **Calmar ≥ 锚臂 且 MDD ≤ 锚臂×1.3
   且 ret > 锚臂**。其余臂 HOLD-E 同跑同报,只作参考。判据由你落笔定稿,写完不许挪。
4. **HOLD-E 构建**：**2025-06-01 ~ 2025-08-14**（全库唯一未触碰近代时段;先验归档,缺月
   vision 下载补 1m/1h/funding → stage L 标签 → stage F 因子）。
5. **HOLD-E 终裁**：锚臂+主臂（+副臂参考），按预注册判据出生死。

## 4. 纪律红线

- 八个判定窗**全部污染**（选币器与链参数皆探索于其上）——只做估计不做裁决；HOLD-E 是唯一裁决。
- 已裁决线勿碰：p12 gross/paired/r7f 各臂不重跑；F99 不进本战役（carry 定性）。
- 统计陷阱前科：stride-5 探针三次被全样本反证——**预期收缩,别被判定段的好看数字带跑**；
  t<1.5 的读数禁入叙事（研究方法论 memory 已固化此条）。
- 锚纪律：锚臂不逐位复现历史产物即停手。
- 机器：16GB,≤2 并发;原 session 已让路。**顺手把上一战未提交的
  p12_final_bt.py/p12_anchor_parity.py/p12_report.py 一并 commit**（预注册证据链闭合）。

## 5. 背景资产

探针全数据 `data/score_research_2026-07-21/ablation/`（p12_rsp_chains.log=本战候选出处;
p12_final_results.txt=上一战全记录）;台账 `.superpowers/sdd/progress.md`;
方法论 memory `research-methodology`（探针纪律新条款）。
