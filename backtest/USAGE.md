# backtest prewarm 使用说明

回测数据预热程序：**一次性、并发、幂等**地把回测需要的数据缓存到本地，之后所有回测 /
参数扫描都跑在热缓存上，不再反复打网络。对应《可复用回测架构设计》支柱五。

> 一句话：`prewarm.py` 先确定票池(S3) → 拉全市场 1H K线(S0) → 回放实盘选币得到候选并产出
> tick 下载清单(S1)。它**不修改 `account_0` 任何代码**，只复用其选币逻辑保证 parity。

---

## 1. 前置条件

### 1.1 运行环境

必须在装好依赖的环境里运行（本地已建好 `.venv/`）：

| 依赖 | 用途 |
|---|---|
| pandas (<2.0)、numpy | 因子计算（复用 account_0），需 `<2.0` 以兼容 `resample(base=)` |
| TA-Lib | 选币因子 `Reg_v2` 用到 `LINEARREG` |
| ccxt | S1 import `account_0` 管线时需要 |
| pyarrow | 按天 parquet 缓存读写 |
| requests | 直连 OKX 公共端点取数 |

本地 venv 实测版本：pandas 1.5.3 / numpy 1.24.4 / TA-Lib 0.6.8 / pyarrow 21 / ccxt 2.0.58。

### 1.2 ⚠️ 时区（最容易踩的坑）

`account_0` 的选币函数内部读机器时区（`time.localtime()`）。**必须用与实盘服务器一致的时区运行**，
否则因子时间轴会漂移、与实盘选币结果不一致（parity 失效）。实盘服务器通常是 UTC，所以：

```bash
TZ=UTC <python> prewarm.py ...
```

文档下面所有命令都带 `TZ=UTC`，请勿省略。

---

## 2. 快速开始

```bash
cd backtest

# 用 1 周的小窗口先跑通整条流程（强烈建议首次这么做）
TZ=UTC ../.venv/bin/python prewarm.py --stage all \
    --start "2024-01-01" --end "2024-01-08"
```

跑完后检查产物（见 §5）。确认无误后再放大窗口。

---

## 3. 命令行参数

```
prewarm.py [--stage {all,s0,s1,s3}]
           [--start START] [--end END]
           [--bar BAR]
           [--cache-dir CACHE_DIR]
           [--manifest-dir MANIFEST_DIR]
           [--workers WORKERS]
           [--refresh-instruments]
```

| 参数 | 默认值（见 `bt_config.py`） | 说明 |
|---|---|---|
| `--stage` | `all` | 跑哪个阶段：`s3`/`s0`/`s1`/`all`。注意：任何 stage 都会**先跑 S3** 以确定票池 |
| `--start` | `2024-01-01 00:00:00` | 回测窗口起（UTC）。S0 会自动往前多取 `WARMUP_DAYS` 天暖机 |
| `--end` | `2024-01-08 00:00:00` | 回测窗口止（UTC） |
| `--bar` | `1H` | K线周期 |
| `--cache-dir` | `data/bt_cache` | 按天 parquet 缓存根目录 |
| `--manifest-dir` | `data/bt_manifest` | 候选与 tick 清单输出目录 |
| `--workers` | `8` | S0 并发取数线程数（OKX 公共端点限频，别设太大） |
| `--refresh-instruments` | 关 | 强制重新拉合约规格（否则复用已冻结的） |

窗口 / 票池 / 因子 / 时区 / 暖机天数 / 代理等默认值集中在 [bt_config.py](bt_config.py)。

---

## 4. 三个阶段

阶段间有依赖序（S2 概念上依赖 S1；本版 S0→S1），**阶段内并发**。

### S3 — 合约规格 / 票池（冻结）
拉 OKX `public/instruments`，过滤出 **live 的 USDT 永续**作为票池，并冻结到磁盘。
冻结后重复运行直接复用（除非 `--refresh-instruments`）。

```bash
TZ=UTC ../.venv/bin/python prewarm.py --stage s3
```

### S0 — 全票池 1H K线
对票池每个币拉 `[start - WARMUP_DAYS, end]` 的 1H K线，按天落 parquet。
幂等：已缓存的天直接跳过；单个币失败只告警+计数，**不中断整次预热**。

```bash
TZ=UTC ../.venv/bin/python prewarm.py --stage s0 --start "2024-01-01" --end "2024-02-01"
```

### S1 — 候选发现 + tick 清单
按小时游标回放实盘 `proceed_calc_symbol_factor` + `select_grid_coin`，
得到每个 run_time 选中的币，写 `candidates.csv`；再派生 tick 下载清单 `tick_manifest.csv`。
幂等：已回放过的 run_time 跳过，可断点续跑。

```bash
TZ=UTC ../.venv/bin/python prewarm.py --stage s1
```

> S1 是纯本地计算（用 S0 的缓存），不打网络。实盘每小时触发一次选币，回放一年 ≈ 8760 次
> 全市场因子计算，单进程约 1~数小时；中途可 Ctrl-C，重跑会从断点继续。

---

## 5. 产物

```
data/bt_cache/
  instruments/SWAP/frozen.parquet        # S3：冻结的合约规格
  1H/<symbol>/<YYYY-MM-DD>.parquet        # S0：按天 1H K线（无数据的天为 0 行空哨兵）
data/bt_manifest/
  candidates.csv                          # S1：选币候选
  tick_manifest.csv                       # S1：tick 下载清单
```

**candidates.csv** 列：`run_time, offset, symbol, rank`
（某 run_time 没选中任何币时，写一行 `symbol` 为空的标记行，用于幂等续跑。）

**tick_manifest.csv** 列：`symbol, day`
= 选中币在持仓周期 `[run_time, run_time + period]` 覆盖到的每一天，去重后的清单。
这就是后续从 OKX 官方下载页要下载的 tick 文件清单。

---

## 6. 幂等与断点续跑

- **S0**：每天先 `cache.exists()`（廉价 stat）短路，重跑已预热窗口近乎零成本。
- **S1**：从 `candidates.csv` 读出已完成的 run_time 跳过。
- **原子写**：临时文件 + `os.replace`，中断不会留下半截脏文件。
- **空哨兵**：无数据的天落 0 行空 parquet，区分「没取过」与「取过=空」，不会反复重取。

所以任何阶段中断后直接重跑同一条命令即可，不会重复劳动。

---

## 7. ⚠️ 一致性陷阱

预热填的是「**这一组配置 + 窗口 + 票池**会读的缓存」。后续真正跑回测时的
**窗口 / 票池 / period / factors 必须与预热完全一致**，否则引擎会去读没预热的数据
——这是「预热后还在打 API」的头号原因。改了任一项，就要对新配置重跑预热。

---

## 8. 单元测试

```bash
cd backtest
TZ=UTC ../.venv/bin/python -W ignore -m unittest discover -s tests -v
```

20 个用例，覆盖 cache / okx_history / prewarm / selection_replay 全部流程，
含 parity 端到端（合成数据跑通实盘选币管线）与 point-in-time 不读未来的回归。

---

## 9. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 选币结果与实盘对不上 | 八成是没设 `TZ=UTC`（或与实盘服务器时区不一致）。务必加 `TZ=UTC` |
| `ModuleNotFoundError: talib / pyarrow / ccxt` | 没用 venv 跑。用 `../.venv/bin/python`，或在实盘环境安装依赖 |
| `resample(base=)` 报 TypeError | pandas ≥ 2.0。需降到 `<2.0`（本地用 1.5.3） |
| S0 大量 `[WARN] xxx 取数失败` | OKX 限频或网络抖动。调小 `--workers`，或重跑（幂等，只补失败的） |
| 预热后跑回测仍打 API | 窗口/票池/period/factors 与预热不一致（见 §7） |
| 本地需要代理 | 在 `bt_config.py` 设 `PROXIES`；服务器上置 `None` |

---

## 10. 已知边界

- **Survivorship bias**：`instruments` 只含当前存活的币，历史上被选过但已退市的币缺失，回测票池偏乐观（v1 接受的妥协）。
- **tick 历史下界 2021-09**：OKX 官方下载页 tick 起始时间，早于此的窗口做不了高保真成交回放。
- **本版范围**：只做 API 部分（S0/S1/S3）+ 产出 tick 清单。**资金费 / 标记价**取数函数已在
  `okx_history.py` 备好但默认未接；**逐笔 tick 实际下载**（来自官方下载页）与**网格成交仿真器**尚未实现。
