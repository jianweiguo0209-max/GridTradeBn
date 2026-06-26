# 回测数据预热（backtest/）

把回测要用的数据**一次性并发、幂等地**缓存好，之后所有回测/参数扫描都跑在热缓存上。
对应《可复用回测架构设计》支柱五（Cache + Prewarm）。本目录**不修改 `account_0` 任何代码**，
只复用其选币逻辑以保证 live/backtest parity。

## 当前实现范围（v1）

| 阶段 | 内容 | 数据源 | 产物 |
|---|---|---|---|
| S3 | 合约规格 / 票池（冻结） | OKX `public/instruments` | `data/bt_cache/instruments/SWAP/frozen.parquet` |
| S0 | 全票池 1H K线（选币+布网用） | OKX `market/history-candles` | `data/bt_cache/1H/<symbol>/<day>.parquet` |
| S1 | 按小时回放实盘选币 → 候选 + tick 下载清单 | 纯本地计算（用 S0 缓存） | `data/bt_manifest/candidates.csv`、`tick_manifest.csv` |

**尚未做（下一步）**：S2 条件取数（资金费 / 标记价，取数函数已在 `okx_history.py` 备好）、
逐笔 tick 下载（来自官方下载页，S1 已产出 `tick_manifest.csv` 作为下载清单）、网格成交仿真器。

## 运行

> ⚠️ **时区**：`account_0` 选币函数内部读机器时区（`time.localtime()`）。
> 必须用与**实盘服务器一致的 TZ** 运行，通常服务器是 UTC：

```bash
cd backtest
TZ=UTC python prewarm.py --stage all --start "2024-01-01" --end "2024-01-08"
```

分阶段 / 断点续跑：

```bash
TZ=UTC python prewarm.py --stage s3                       # 只刷新票池
TZ=UTC python prewarm.py --stage s0 --start ... --end ... # 只补 K线（幂等，已存在的天跳过）
TZ=UTC python prewarm.py --stage s1                       # 只回放选币（跳过已回放的 run_time）
```

窗口/票池/因子默认值在 `bt_config.py`。**首次先用很短的窗口验证流程**，再放大。

## 依赖

实盘同款环境：`pandas`(<2.0)、`numpy`、`requests`、`TA-Lib`、`ccxt`(S1 import 需要)，外加 `pyarrow`（按天 parquet）。
取数用 `requests` 直连 OKX 公共端点（免鉴权），不经过 ccxt。

> 本地 venv（Apple Silicon）实测用 `pandas==1.5.3` / `numpy==1.24.4` / `TA-Lib==0.6.8`(bundled wheel)。
> `pandas==1.3.5` 在 arm64 无 wheel 且源码编译失败；1.5.3 与 1.3.5 的 `resample(base=)` 行为一致。
> 生产服务器仍以 requirements.txt 的 1.3.5 为准。

## 单元测试

```bash
cd backtest
TZ=UTC ../.venv/bin/python -W ignore -m unittest discover -s tests -v
```

覆盖 cache / okx_history / prewarm / selection_replay 全部新流程（20 个用例），
含 parity 端到端（合成数据跑通实盘选币管线）与 point-in-time 不读未来的回归。

## 设计约束（照搬文档）

- **有界并发**：S0 用 `ThreadPoolExecutor` + `as_completed` 流式回收。
- **幂等短路**：每天先 `cache.exists()` 跳过；S1 跳过已写入 `candidates.csv` 的 run_time，可断点续跑。
- **原子写 + 空哨兵**：临时文件 + `os.replace`；无数据的天落 schema-only 空 parquet，区分「没取过」与「取过=空」。
- **回放真实读路径**：S1 直接调实盘 `proceed_calc_symbol_factor` + `select_grid_coin`，只为真正选中的币派生 tick 清单，绝不盲扫全市场。

## 已知边界

- **Survivorship bias**：`instruments` 只含当前存活的币，历史上被选过但已退市的币缺失，回测票池偏乐观（v1 接受的妥协）。
- **tick 历史下界 2021-09**：官方下载页 tick 起始时间，早于此的窗口做不了高保真成交回放。
- **一致性陷阱**：后续回测的窗口/票池/period/factors 必须与预热完全一致，否则会读到未预热的数据。
- **S1 算力**：实盘每小时触发一次选币，回放一年 ≈ 8760 次全市场因子计算，单进程约 1~数小时（可断点续跑）；
  v1 单进程内存载入全序列后逐小时切片，后续可并行化。
