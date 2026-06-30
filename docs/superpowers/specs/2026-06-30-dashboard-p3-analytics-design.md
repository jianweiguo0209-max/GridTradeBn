# GridTradeGP Dashboard 第三期（复盘分析）— 设计文档

> 日期：2026-06-30
> 状态：设计已确认，待转 writing-plans
> 前序：P1 只读监控（已上线）、P2 控制台（已上线 testnet app v33）。设计 `2026-06-29-dashboard-design.md`、`2026-06-30-dashboard-p2-control-design.md`
> 相关记忆：`dashboard-project`

## 1. 背景与目标

P1（只读监控）、P2（控制台）已上线 testnet。P3 给 Dashboard 加**复盘分析**：权益/盈亏曲线、tag 盈亏归因、成交分布、退出原因统计，并把刚落库的**真实平台手续费**铺进相关表格。图表全部**服务端渲染内联 SVG**（零新 JS，延续 P1/P2「无前端构建、可单测、不走 CDN」的栈）。

**核心约束（延续 P1/P2）**：web 进程**只读** bot 状态。P3 唯一的新写路径是 monitor 节流写 equity 快照（与 monitor 现有写记账/心跳一致，单一写者不变）。

## 2. 范围（已确认）

第一批：
1. **权益/盈亏曲线** —— (a) 已实现累计阶梯线（从 `order_records`）+ (b) 真权益曲线（从新表 `equity_snapshots`，含未实现 + 回撤）。
2. **tag 盈亏归因** —— 按 tag：总盈亏 / 总 fee / 净盈亏 / 胜率 / 笔数 / 平均持仓时长 / 最大回撤。
3. **成交分布**（四子图，全从 `grid_fills`）—— 按时间（活动）/ 买卖方向 / 网格线 line_index / 累计手续费。
4. **退出原因统计** —— 按 `exit_reason`：占比 + 各自平均盈亏 + 笔数。
5. **跨切面：真实手续费铺表** —— 成交流水表加 per-fill `fee` 列；活跃网格总览加每网格累计 fee；tag 表加总 fee + 净盈亏。
6. **时间范围过滤** —— 全部 / 近 7 天 / 近 30 天。

**不在本期（YAGNI）**：JS 交互图表（静态 SVG）；自定义日期选择器；CSV 导出；多账户；快照保留/清理策略（行小，日后再 prune）。

## 3. 架构

```
浏览器 ──登录会话──> web GET /analytics（只读）
   │  analytics.py 读 order_records/grid_fills/equity_snapshots → 算序列/聚合
   │  charts.py 把序列 → 内联 SVG → 模板渲染
   ▼
Fly Postgres
   ▲ 写 equity_snapshots（P3 唯一新写）
monitor 进程：run_monitor_cycle 末尾节流写一行 (ts, equity, cash)
            equity = executor.adapter.fetch_balance().equity（含未实现）
            try/except 包住：取余额失败跳过、记日志、绝不崩 cycle
```

**不变量**：web 只读三表（order_records/grid_fills/equity_snapshots）+ 只读现价/余额无需（analytics 不调行情）；唯一新写在 monitor（单一写者）；SVG 全服务端生成、零 JS、不走 CDN。

## 4. 数据模型

### 4.1 新表 `equity_snapshots`
| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String PK | uuid hex |
| `ts` | BigInteger | UTC ms，快照时刻 |
| `equity` | Float | `fetch_balance().equity`（账户权益，含未实现） |
| `cash` | Float nullable | `fetch_balance().cash`（可选，留作扩展） |
| Index | `ix_equity_snapshots_ts` on `ts` | 按时间范围查 |

随 `store.create_all()` 启动自动建（幂等，同 P2 三控制表）。**无需 ALTER/migrate**——全新表；`grid_fills.fee` 列已在 fee 落库阶段迁好。

### 4.2 `grid_fills.fee`（已存在）
fee 落库阶段已加 `grid_fills.fee` 列 + 真实费记账。P3 只是把它读出来铺进表格与累计费曲线——不改表结构。

## 5. 模块设计（延续分层）

```
gridtrade/
  state/
    equity.py            # ★ EquitySnapshotRepository：add_if_due(节流写) / list_range(读)
  dashboard/
    analytics.py         # ★ 只读聚合：累计已实现序列 / tag 归因 / 成交分布(四维) / 退出原因 / 真权益序列
    charts.py            # ★ 纯函数 SVG：line_chart / bar_chart / stacked_bar（空数据退化）
    queries.py           # 改：Fill/RecentFill DTO + 查询补 fee 字段（fee 铺表）
    app.py               # 改：GET /analytics 路由（登录门控）+ 范围过滤
    templates/
      analytics.html     # ★ 四区块 + 内联 SVG
      detail.html/history.html/overview.html  # 改：加 fee 列
  runtime/
    cycles.py            # 改：run_monitor_cycle 末尾节流写 equity 快照（可选 equity_repo 参）
    factory.py / monitor.py  # 改：接线 equity_repo（同 P2 控制仓储）
```

### 5.1 `EquitySnapshotRepository`
- `add_if_due(equity: float, cash: Optional[float], *, interval_sec: int, now_ms_fn=now_ms) -> bool`：查最新快照 ts；`now - latest >= interval*1000` 或无快照才 INSERT；返回是否写入。节流逻辑落 DB（查最新 ts），**重启安全、不依赖内存态**。
- `list_range(start_ms: int, end_ms: Optional[int] = None) -> List[EquitySnapshot]`：按 ts 升序。

### 5.2 `analytics.py`（只读，纯计算）
- `realized_curve(store, *, start_ms) -> List[(ts, cum_pnl)]`：order_records 按 closed_at 升序累加 total_pnl。
- `equity_curve(store, *, start_ms) -> List[(ts, equity)]`：equity_snapshots.list_range。
- `tag_attribution(store, *, start_ms) -> List[TagStat]`：按 tag 聚合 total_pnl/total_fee/net_pnl/win_rate/count/avg_hold_ms/max_drawdown。
- `fill_distribution(store, *, start_ms) -> FillDist`：四维——`by_time`（按小时/天分桶计数）、`by_side`（buy/sell 计数与量）、`by_line`（line_index 直方）、`fee_cum`（按 ts 累加 fee）。
- `exit_reason_stats(store, *, start_ms) -> List[ExitStat]`：按 exit_reason 计数 + 占比 + 平均 pnl。
- 跨切面：`total_fee` 来自 grid_fills.fee 之和（tag 维与全局）。

### 5.3 `charts.py`（纯函数，可单测）
- `line_chart(series: List[List[(ts,val)]], *, width=720, height=200, ...) -> str`：多序列归一化到 viewBox → `<polyline points=...>`；含轴/零线；可叠加（真权益 + 已实现）。
- `bar_chart(bars: List[(label, value)], ...) -> str`、`stacked_bar(groups, ...) -> str`：`<rect>` + 标签。
- 空序列 → 返回占位（含「暂无数据」文案）的小 SVG，不抛异常。
- 所有坐标计算纯函数，测试对已知输入断言关键 path/rect 数值。

## 6. 视图与手续费铺表

| 视图 | 数据来源 | 渲染 |
|---|---|---|
| ① 权益/盈亏曲线 | `realized_curve` + `equity_curve` | `line_chart`（两序列叠加 + 回撤） |
| ② tag 盈亏归因 | `tag_attribution` | 表格（总盈亏/总 fee/净盈亏/胜率/笔数/平均持仓/最大回撤） |
| ③ 成交分布 | `fill_distribution` 四维 | `bar_chart`（时间/line）+ `stacked_bar`（买卖）+ `line_chart`（累计费） |
| ④ 退出原因统计 | `exit_reason_stats` | `bar_chart` + 表格 |
| 跨切面 fee 铺表 | grid_fills.fee / accounting.fee_paid | detail+history 成交流水加 per-fill `fee` 列；overview 加每网格累计 fee；tag 表加总 fee/净盈亏 |

`/analytics` 顶部一个范围选择（全部 / 7d / 30d）→ 查询参 `?range=` → 各聚合按 `ts/closed_at >= cutoff` 过滤。

## 7. monitor 快照写入

`run_monitor_cycle` 末尾新增一步（在消费指令之后；新增可选 `equity_repo=None`，None 则跳过 → 向后兼容）：

```
if equity_repo is not None:
    try:
        bal = manager.executor.adapter.fetch_balance()
        equity_repo.add_if_due(bal.equity, getattr(bal, 'cash', None),
                               interval_sec=config.equity_snapshot_interval_sec)
    except Exception as exc:
        log('[monitor] equity snapshot skipped: %r' % exc)
```

- factory 构造 `EquitySnapshotRepository` 进 Runtime；monitor.py 把它传入 cycle（同 P2 控制仓储接线）。
- 间隔 `EQUITY_SNAPSHOT_INTERVAL_SEC`（DeployConfig 加字段，默认 300）。
- 取余额失败/限频不崩 cycle——节流写是「尽力而为」的旁路，不影响对账/补单/止损主链。

## 8. 鉴权 / 安全

- `/analytics` 在 P1 登录会话之后（沿用 `_user`，未登录 302 /login）。纯 GET、只读。
- web 仍**零写**：analytics 只读三表；唯一新写在 monitor。
- SVG 由服务端从数值生成；模板内任何文本经 Jinja autoescape，无 `|safe`。

## 9. 测试（双后端 TDD）

- `EquitySnapshotRepository`：add_if_due 节流（间隔内只写一次、跨重启按 DB 最新 ts）/ list_range 升序。
- `analytics.py`：store fixture 喂 records/fills/snapshots，断言累计已实现序列、tag 归因（含 fee/净盈亏/最大回撤）、四维分布、退出原因占比/均值、范围过滤。
- `charts.py`：纯 SVG 函数对已知输入断言 polyline/rect 坐标 + 空数据退化。
- web `/analytics`：TestClient 鉴权门控（未登录 302）、四区块渲染（关键 SVG/表格存在）、`?range=7d` 过滤。
- monitor 快照写：FakeExchange fetch_balance，断言节流（间隔内 1 行）+ 取余额抛错时跳过不崩 cycle。
- fee 铺表：`Fill`/`RecentFill` DTO 含 fee；detail/history/overview 模板显示 fee；既有 P1 测试不回归。

## 10. 风险与开放项（实现期定）

- 真权益曲线从现在起累积（testnet 早期点少）；已实现阶梯线立即可用，二者叠加互补。
- 快照间隔 300s → ~288 行/天，长期增长缓慢；保留策略留待后续。
- SVG 轴刻度/标签的具体取舍（点多时降采样）实现期按真实数据微调。
- `fetch_balance().cash` 字段在某些适配器可能缺省——用 `getattr(bal,'cash',None)` 容错，cash 仅作扩展列。
