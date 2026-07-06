# 1m 缓存完整性闸 + 自愈重取 设计

日期：2026-07-07 ｜ 状态：已批准（用户，两节确认）｜ 范围：回测数据基础设施；不碰回测/选币/实盘

## 背景与动机

回测 1m 数据来自 Reservoir S3（1s→1m 重采样，按 (币,天) 缓存）。`warm_reservoir_ohlcv`
的跳过条件是**「文件存在即跳过」，不校验内容**——任何一次残缺/坏拉取被永久冻结，
再也不刷新、不校验。今日事故实证与诊断：

- **振幅错型（TRUMP 2026-03-15）**：坏缓存把平静的一天变成"假暴跌"，1m 高低幅和独立
  1h 完全不符 → 网格仿真吃到幻觉路径 → 样本内假 +47%（真实 ≈打平）。当前抽样 0%
  （被今天一次意外重写修掉了），但**无任何机制防复发**。
- **残缺拉取型（GMX 等）**：诊断 500 抽样 **6.4%**——1h 显示满 24h 交易，1m 却只有
  105-932 根（该 ~1440），部分小时零 bar → 成交笔数被低估 → pnl 偏小。
- **空哨兵（46%）**：币当天真无成交，`write_empty` 落空 → 合法。
- **真·低流动性稀疏**：0%（所有缺口都是坏拉取，不是市场稀疏）。

**连锁后果**：跨配置回测不可比——不同配置选不同币，撞不同的坏格/覆盖缺口，格被
静默丢弃（整条不进结果），格数与 pnl 都失真（今日实证 W_base 1590 格 vs W_dropsgcz
1278 格，同为 choose_symbols=1 无锁，本应相同）。

## 目标

- 清现存坏格（6.4% 残缺型 + 任何振幅错型）。
- 加一道**永久完整性闸**：warm 时坏格自动重取（自愈），防复发。
- 让回测 1m 数据可信、跨配置可比。

## 非目标（YAGNI）

- 不动 1h 缓存（它是校验基准，诊断证明一致）。
- 不改 API/Reservoir 源选择的 200 天阈值逻辑（另一回事，本次不碰）。
- 不改回测/选币/实盘任何代码——纯缓存基础设施。

## 设计

### 组件① `validate_1m_cell(m_df, h_df, *, range_tol=0.05) -> (ok: bool, reason: str)`

reservoir.py 新增纯函数（可单测，无 IO）。判定一个 (币,天) 的 1m 是否可信：

1. `h_df` 空或缺 → 1m 空/任意皆合法（真·不成交）→ `(True, 'no_1h_ref' or 'ok')`。
   （清库命令遇 1h 缺时另行标记 `no_1h_ref`、跳过，不误判为坏。）
2. **振幅校验**：`entry = h_df.close.iloc[0]`；若
   `|（1m 日高低幅）−（1h 日高低幅）| / entry > range_tol` → `(False, 'range_mismatch')`。
3. **逐小时完整性**：对 1h 有 bar 的每个整点小时，若 1m 在该小时窗内 **零 bar** →
   `(False, 'hour_gap')`。合法稀疏（分钟级无成交但每小时都有 bar）判 ok。
4. 全过 → `(True, 'ok')`。

默认阈值来自今日诊断（range 5% 分得开 TRUMP 型；逐小时零 bar 抓得住 GMX 型），
写死默认、留参数口子。

### 组件② warm 跳过逻辑自愈化（`warm_reservoir_ohlcv`）

跳过条件从「所有币所有 tf 文件存在」升级为「存在**且** 1m 过 `validate_1m_cell`」：

```
旧: if all(cache.exists(tf, s, day) for tf ... for s ...): skip
新: if all(cache.exists(...)) and _day_1m_all_valid(cache, universe, day): skip
    否则落进现成的「重下当天日文件→重采样→覆盖写」路径（覆盖写修正值）
```

`_day_1m_all_valid`：对该天每个币读缓存 1m + 1h、调 validate；任一坏 → 该天不跳过、
重下。校验读的 1h 同批已在缓存（1h/1m 同批下载）。重取失败（S3 未发布/报错）→
**保留旧坏格，不删不改，计入 `retry_later`**（宁留已知坏格待下次，不留空洞）。

### 组件③ 一次性清库命令 `validate-1m`（dbadmin.py 子命令，复用①）

```
用法: python -m gridtrade.runtime.dbadmin validate-1m [--dry-run] [--window START END]
```

流程：扫 1m 缓存全库（或指定窗口）→ 每格调 `validate_1m_cell` → 分类计数
（ok / range_mismatch / hour_gap / no_1h_ref）→ 坏格 (币,天) 按天聚合 → 对每个坏天调
`warm_reservoir_ohlcv`（因 validate 不过而重下）→ 复检 → 报告。
`--dry-run`：只扫描分类报告，不重取（先核对坏格数 ≈ 6.4% 再真修）。幂等：反复可跑，
已修好的第二遍 validate 通过跳过。

## 数据流

```
预热: warm → 每天: exists?+validate? → 不过 → 重下日文件 → 重采样覆盖写
清库: scan全库 → validate每格 → 坏格聚合成天 → warm那些天(自动重取) → 复检报告
```

## 错误处理与边界

- **重取失败不更坏**：保留旧坏格 + retry_later，清库报告列「重取仍坏」的格。
- **当天未过完**：沿用 `day_end_ms > now_ms` 跳过，不校验/不重取进行中的当天。
- **1h 缺**：无法校验 → 标 `no_1h_ref`、跳过，不误判。
- **幂等/断点续跑**：清库命令与 warm 自愈均可反复跑，只对仍坏的重取。

## 测试

- **validate_1m_cell 纯函数单测**（核心）：① 1h空+1m空→ok ② 振幅差>5%→range_mismatch
  （合成假崩 1m）③ 1h满24h+某小时1m零bar→hour_gap（合成 GMX 型）④ 合法稀疏（分钟无
  bar但每小时有）→ok ⑤ 1h缺→no_1h_ref。
- **warm 自愈集成测**：mock `_s3_cp` + 桩 cache，预置坏格→warm→断言重下被触发+格被覆盖；
  好格→断言跳过不重下；重取失败→断言保留旧格+retry_later。
- **validate-1m 命令**：桩缓存（3币×3天含1坏1空1好）→断言只重取坏格、报告数字对、二次跑幂等。
- **真实冒烟（不进 CI）**：对当前缓存跑 `validate-1m --dry-run`，核对坏格数 ≈ 6.4%。

## 改动面

`gridtrade/backtest/reservoir.py`（validate_1m_cell + warm 跳过自愈 + _day_1m_all_valid）；
`gridtrade/runtime/dbadmin.py`（validate-1m 子命令）；对应测试。回测/选币/实盘/1h 缓存
零改动。
