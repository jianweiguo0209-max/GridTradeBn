# 时区统一（内部 UTC + 显示可配）设计

> 日期：2026-07-04　状态：设计已确认，待写实现计划
> 目标：把全系统时间语义统一——**内部处理/存储/策略对齐全用 UTC（无机器 TZ 依赖）**，**外部显示时区由 config 配置**（IANA 名，默认 UTC）。

## 1. 背景与动机

选币路径存在两个不一致的时区来源：

- `core/selection.py:compute_offset` 用**显式 config `utc_offset`（=8）**算换仓 offset 相位；
- `core/selection.py:proceed_calc_symbol_factor` 的 point-in-time 时间对齐用**机器本地时区** `time.localtime().tm_gmtoff`。

部署容器写死 `TZ=UTC`（`deploy/Dockerfile`），金标 parity 测试却在 `TZ=Asia/Shanghai` 下跑。结果：

- 选币的换仓相位（offset）与因子时间对齐依赖两套不同的时区源，一半显式一半隐式；
- 依赖机器 TZ = 部署环境一换、选币行为可能悄悄漂移，且与金标验证环境不在同一时区假设下。

面板显示层当前把时间**硬编码为 UTC**（`dashboard/formatting.py`、`dashboard/svgaxes.py` 均 `tz=timezone.utc`），无法按需展示本地时间。

## 2. 已确认决策

| 决策点 | 选择 |
|---|---|
| 换仓 offset 相位 | **改纯 UTC**（不再 −8 对齐北京）——彻底铲平 `utc_offset`（方案 B） |
| 金标 | **重生成**（从 legacy 在 `TZ=UTC` 下重跑） |
| 显示时区 config 形式 | **IANA 名**（zoneinfo），env `DISPLAY_TZ`，**默认 UTC** |
| 上线节奏 | **实现后先在 testnet 验证**，再上 mainnet production |

这是一次**有意的策略变更**：换仓 12H 边界相位平移 8 小时，可能改变实盘选中的币与网格轮换时刻。

## 3. 先行验证结论（已实测）

- **回测缓存不受影响** ✅：`backtest/reservoir.py:42` 把 `candle_begin_time` 存为 tz-naive UTC（由绝对时间戳来、不含偏移）；`prewarm` 重拉也是 epoch。缓存**无需重生成**。`data/kline` 现为空。
- **回测本来就跑纯 UTC** ✅：`backtest/backtest_run.py` 主入口写死 `utc_offset=0`、窗口用 `utcnow`。**方案 B 把 live 拉齐到回测口径**（网格参数原就在 UTC 对齐下调优）——是利好，非风险。
- **Legacy 金标可重生成** ✅：`gen_golden.py` 依赖 legacy，legacy `config.py` import 时 `os.mkdir(legacy/data/{kline,order})`，需先 `mkdir -p legacy/data`；import 期无网络调用，可在 `.venv` 跑通。
- **金标重基线低风险** ✅：实测同一输入下 UTC 与 +8 选出的**币/rank/rank_sum 逐行完全相同，仅 `time` 列整体平移 8h**。重生成只改 time 标签，选币断言不变。
- **生产真实变化点** ⚠️：容器早已 `TZ=UTC`（factor-time 已是 UTC），Option B 对 live 的真正改变是 **`compute_offset` 相位**（offset 值 +8 mod 12 → resample 分箱与 tag 变）。这是 testnet 要盯的行为。

## 4. 设计

### 时间分层模型

1. **存储/计算（内部）**：一律 UTC（epoch ms / tz-naive UTC datetime）。无任何机器 TZ 依赖。
2. **策略对齐**：换仓 offset 相位、因子 point-in-time 截断 = 纯 UTC（相当于对齐偏移恒为 0）。
3. **显示（外部）**：`DISPLAY_TZ`（IANA，默认 UTC）驱动，**仅面板层**；日志/调试输出保持 UTC。

### ① 策略对齐 → 纯 UTC（铲平 `utc_offset`）

| 文件 | 改动 |
|---|---|
| `core/selection.py:proceed_calc_symbol_factor` (68-70) | 删 `tm_gmtoff` 三行；`time = pd.to_datetime(all_data_df['time'], unit='ms')`（纯 UTC，不 shift）。cutoff `time < run_time` 不变（两侧皆 UTC）。 |
| `core/selection.py:compute_offset` (154-157) | 去掉 `utc_offset` 参数：`compute_offset(run_time, period)`，`utc_run_time = run_time`（不减）。 |
| `execution/triggers.py:ScheduledSelectionTrigger` (66-79) | 删构造参数 `utc_offset` 与 `self.utc_offset`；调用 `compute_offset(run_time, period)`。 |
| `runtime/scheduler.py:56` | `compute_offset(run_time, period)`（不再传 `config.utc_offset`）。 |
| `runtime/factory.py:83` | 去掉 `utc_offset=config.utc_offset`。 |
| `config.py` (56, 97) | 删 `DeployConfig.utc_offset` 字段与 `UTC_OFFSET` env 解析。 |
| `backtest/selection_replay.py`、`backtest/backtest_run.py` | 删 `utc_offset` 参数穿线，cutoff 变 `candle_begin_time < run_time`。原本传 0，数值结果不变。 |
| `deploy/fly.toml`、`deploy/fly.prod.toml` | 删 `UTC_OFFSET` env（已无意义）。 |

### ② 显示时区 → 可配（新增，仅面板）

- `config.py`：新增 `display_tz: str = 'UTC'`，从 env `DISPLAY_TZ` 读（IANA 名）。
- 新 helper（放 `dashboard/formatting.py`）：`to_display_dt(ts_ms, tz_name) -> datetime`，用 `zoneinfo.ZoneInfo(tz_name)`；tz 非法或缺 tzdata 时**回退 UTC 且不崩**。
- 时间格式化点：`dashboard/formatting.py:ms_to_human`（当前 `tz=timezone.utc`）与 `dashboard/svgaxes.py` 的时间标签函数（当前 `tz=timezone.utc`，44 行）——二者是**纯函数/Jinja 过滤器、拿不到 config**。接线方式：给这两个函数加 `tz_name: str = 'UTC'` 参数（内部走 `to_display_dt`），在 **`dashboard/app.py` 启动组装时**把 `config.display_tz` 绑定进去——Jinja 过滤器用 `functools.partial` 绑定，图表构建器（`gridchart.py`/`charts.py` 经 `svgaxes`）在渲染调用处传入。默认仍 UTC，不设即与现状一致。
- `deploy/Dockerfile`：加 `tzdata`（slim 镜像 zoneinfo 需要）；`TZ` 可保留 `UTC`（现已与业务逻辑无关）。

### ③ 金标重基线

- 实现首步先 `mkdir -p legacy/data`，在 **`TZ=UTC`** 下跑 `python tests/golden/gen_golden.py` 重生成 `cross_select_golden.parquet`（legacy `tm_gmtoff=0` → UTC 对齐参考）。`factors_golden.parquet`、`grid_params_golden.json` 与时区无关、可一并重跑核对无变化。
- `tests/core/test_selection_parity.py`：
  - `_run_new`（offset=0、固定 run_time）调用不变——新 core 纯 UTC 后自动匹配新 UTC 金标；
  - `test_compute_offset_matches_legacy_formula`（40-45）改写为新签名 `compute_offset(run_time, '12H')` + 无 −8 的 UTC 公式。
- 确认 `tests/core/test_factors_parity.py` 不受影响（纯因子数学，无时区）。

### 数据流

```
candle (UTC epoch) → resample(base=offset, UTC) → factor time (UTC)
                   → cutoff time<run_time (UTC) → 截面/排名 → 选币
显示：ts_ms(UTC) → to_display_dt(DISPLAY_TZ) → 面板
```

## 5. 测试

- **金标**：重生成后 `test_selection_parity` / `test_factors_parity` / grid_params 金标全绿。
- **机器 TZ 独立性（新增）**：同一 `run_time` 下，分别在 `TZ=UTC` 与 `TZ=Asia/Shanghai` 进程内跑选币，断言 **offset 与选币结果完全一致**（证明彻底摆脱机器 TZ 依赖）。
- **显示（新增）**：`to_display_dt` 单测——UTC 默认、非 UTC IANA（如 `Asia/Shanghai` +8）、非法 tz 回退 UTC 三种。
- **回归**：全量 `pytest` 在 `TZ=UTC` 与 `TZ=Asia/Shanghai` 两个 env 下均绿（双后端 SQLite/PG 沿用现有约定）。

## 6. 上线（testnet 先行）与风险

- **顺序**：实现 → 全测过 → 部署 **testnet（`gridtrade-hl`）验证** → 观察首个换仓周期 offset/tag/选币与轮换正常 → 再 merge 进 `production` 上 mainnet。
- **风险 1（预期行为变更）**：live 换仓 12H 边界相位平移 8h，选中的币/轮换时刻会变。testnet 首个换仓要盯：offset tag 变化、旧 tag 网格与新 tag 网格的关旧/开新衔接。
- **风险 2**：slim 镜像 zoneinfo 需 `tzdata`；漏装则非 UTC 的 `DISPLAY_TZ` 回退 UTC（helper 已保证不崩）。
- **风险 3**：`UNIVERSE_WHITELIST`/其他 env 不动；仅删 `UTC_OFFSET`。部署前核对 fly secrets/env 无残留引用。

## 7. 同步更新

- 需求文档 §2「时区基准 UTC+8」→ 改为 UTC，并注明与本系统实现一致。
- `docs/STATUS.md`：记本次时区统一 + live 现与回测同口径。

## 8. 不在本次范围

- 不改因子数学、不改网格参数、不改选币因子集与阈值。
- 不引入"策略对齐时区可配"（用户要的是内部恒 UTC；如未来需要，可再加 config，扩展点为 `compute_offset` 与 `proceed_calc_symbol_factor` 的显式参数）。
