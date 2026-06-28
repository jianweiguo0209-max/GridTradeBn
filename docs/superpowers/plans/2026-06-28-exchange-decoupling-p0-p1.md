# 交易所解耦重构 P0–P1 实现计划（脚手架 + core 搬运 + 金标 + 适配器层）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把策略核心从 OKX 强绑定中剥离为交易所无关的 `gridtrade/core/` 包（因子/选币/网格参数逻辑零漂移，金标测试锁定），并落地统一 `ExchangeAdapter` 抽象层（ccxt 通用实现 + OKX/Hyperliquid 适配器 + FakeExchange 测试替身）。

**Architecture:** 端口与适配器（Ports & Adapters）。`core/` 只吃 DataFrame/参数、吐 DataFrame/决策，不 import 任何交易所库。`exchanges/` 是唯一含交易所差异的地方：一个 ABC 接口 + ccxt 通用实现 + 各所差异覆写 + 内存 FakeExchange。本计划不改动现有 `account_0/` 与 `backtest/` 的运行行为（它们继续可用），只新增 `gridtrade/` 包与测试；后续阶段（P2+）再把运行时切换过去。

**Tech Stack:** Python 3.9、pandas 1.3.5、numpy 1.22.4、TA-Lib、ccxt(升级到支持 hyperliquid 的 4.x)、pytest、pyarrow。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现过程中遇到任何不确定（接口语义、交易所行为、字段含义、版本兼容、本计划未写清的细节），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

> 以下为全项目级约束，每个任务都隐含适用。值均照抄自现有代码/spec。

- Python 3.9；`pandas==1.3.5`；`numpy==1.22.4`（不得升级，因子逻辑依赖其行为，如 `DataFrame.append`、`resample(base=...)`）。
- TA-Lib 为因子计算硬依赖（`import talib as ta`），开发/CI 环境须预装系统库 `ta-lib` + `pip install TA-Lib`。
- ccxt 当前为 `2.0.58`，无 hyperliquid；本计划升级到支持 hyperliquid 统一市场的版本（目标 4.x），并必须验证其在 py3.9 + pandas 1.3.5 下可用（Task 6 钉死具体版本）。
- 规范符号 = ccxt 统一符号，永续格式 `"BTC/USDT:USDT"`；系统内部一律用规范符号，不得出现 `-USDT-SWAP` 字符串判断（OKX 原生格式仅存在于 `exchanges/okx.py` 内部映射）。
- K线 DataFrame 统一列：`['symbol','candle_begin_time','open','high','low','close','vol','volCcy','quote_volume']`（与现有 `okx_history.CANDLE_COLS` 一致）。
- 资金费 DataFrame 统一列：`['ts','symbol','fundingRate','realizedRate']`。
- 类型注解用 `typing.List/Optional/Dict`（py3.9 不支持 `list[...]` 内置泛型下标在某些位置；统一用 typing）。
- 因子保真：任何 `core/` 函数的数值输出必须与现 `account_0/` 实现逐字段一致（金标测试护栏）。
- 因子/选币函数内部读机器时区（`time.localtime().tm_gmtoff`）。所有相关测试必须在固定时区下运行（conftest 设 `TZ=Asia/Shanghai` 并 `time.tzset()`）。

---

## 文件结构（本计划新建/修改）

新建包 `gridtrade/`（与 `account_0/`、`backtest/` 平级）：

```
gridtrade/
  __init__.py
  core/
    __init__.py
    factors.py        # 全部因子函数 + cal_factor + cal_cross_factor（从 fancy_grid_function.py 逐字搬运）
    selection.py      # trans_period_for_grid + proceed_calc_symbol_factor + compute_offset + select_grid_coin
    grid_params.py    # calc_grid_params_v1/v2 + _format_price
  exchanges/
    __init__.py
    base.py           # ExchangeAdapter ABC + 数据类(Instrument/Balance/Position/Order/Trade) + 规范符号常量
    fake.py           # FakeExchange 内存实现
    ccxt_adapter.py   # CcxtAdapter 通用实现（unified ccxt → 适配器 schema）
    okx.py            # OkxAdapter（passphrase/sandbox header/funding 8h/符号映射）
    hyperliquid.py    # HyperliquidAdapter（钱包凭证/funding 1h/符号=币名）
    registry.py       # build_adapter(config) 工厂
tests/
  conftest.py         # 固定 TZ；pytest 路径
  golden/
    gen_golden.py     # 一次性：从原 account_0 代码生成金标 fixture
    factors_golden.parquet
    cross_select_golden.parquet
    grid_params_golden.json
  core/
    test_factors_parity.py
    test_selection_parity.py
    test_grid_params_parity.py
  exchanges/
    test_ccxt_smoke.py        # ccxt 升级 + hyperliquid 可用性验证
    test_base_contract.py
    test_fake.py
    test_ccxt_adapter.py
    test_okx_adapter.py
    test_hyperliquid_adapter.py
    test_registry.py
pyproject.toml        # 修改/新建：pytest 配置 + 包发现
requirements.txt      # 修改：ccxt 版本
```

---

## P0：脚手架 + core 搬运 + 金标

### Task 1: 包脚手架与 pytest 基础设施

**Files:**
- Create: `gridtrade/__init__.py`, `gridtrade/core/__init__.py`, `gridtrade/exchanges/__init__.py`
- Create: `tests/conftest.py`, `tests/__init__.py`
- Create/Modify: `pyproject.toml`

**Interfaces:**
- Produces: 可被 `import gridtrade.core` / `import gridtrade.exchanges` 的包；`pytest` 可在仓库根目录运行；测试在 `TZ=Asia/Shanghai` 下运行。

- [ ] **Step 1: 写一个会失败的探针测试**

Create `tests/test_scaffold.py`:

```python
import time


def test_timezone_is_pinned():
    # conftest 应把进程时区钉到东八区（UTC+8 => +28800 秒）
    assert time.localtime().tm_gmtoff == 8 * 3600


def test_packages_importable():
    import gridtrade.core  # noqa: F401
    import gridtrade.exchanges  # noqa: F401
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_scaffold.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade` 或时区断言失败）。

- [ ] **Step 3: 建包与 conftest**

Create empty `gridtrade/__init__.py`, `gridtrade/core/__init__.py`, `gridtrade/exchanges/__init__.py`, `tests/__init__.py`.

Create `tests/conftest.py`:

```python
import os
import time

# 因子/选币函数内部读机器时区；测试统一钉到东八区，保证金标确定性。
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()
```

Create `pyproject.toml`（若已存在则合并 `[tool.pytest.ini_options]` 段）:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
addopts = "-q"
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_scaffold.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade tests pyproject.toml
git commit -m "chore: scaffold gridtrade package + pinned-TZ pytest infra"
```

---

### Task 2: 金标 fixture 生成（来自原 account_0 代码）

生成"重构前"的权威输出快照，作为 core 搬运后的零漂移护栏。该脚本 import 原始 `account_0` 因子代码。

**Files:**
- Create: `tests/golden/__init__.py`
- Create: `tests/golden/gen_golden.py`
- Create (产物，提交进仓库): `tests/golden/factors_golden.parquet`, `tests/golden/cross_select_golden.parquet`, `tests/golden/grid_params_golden.json`

**Interfaces:**
- Produces: 三个金标 fixture 文件；以及可复用的确定性造数函数 `make_symbol_df(symbol, n, seed)`（后续 parity 测试 import 它构造相同输入）。

- [ ] **Step 1: 写造数 + 生成脚本**

Create `tests/golden/__init__.py`（空）。

Create `tests/golden/gen_golden.py`:

```python
"""一次性脚本：用原始 account_0 因子代码生成金标 fixture。
运行：TZ=Asia/Shanghai python tests/golden/gen_golden.py
依赖 talib。重构后由 parity 测试用相同输入比对新 core 输出。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
# 注入原始 account_0 到 sys.path（复刻 selection_replay 的做法）
for _p in (os.path.join(_ROOT, 'account_0'),
           os.path.join(_ROOT, 'account_0', 'utils'),
           os.path.join(_ROOT, 'account_0', 'api')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def make_symbol_df(symbol, n=240, seed=0):
    """确定性合成 1H OHLCV（列与实盘 CANDLE_COLS 一致）。"""
    rng = np.random.RandomState(seed)
    rets = rng.normal(0, 0.01, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, size=n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, size=n)))
    vol = rng.uniform(1e3, 1e4, size=n)
    volccy = vol * close
    quote_volume = volccy * close
    t0 = pd.Timestamp('2024-01-01 00:00:00')
    cbt = pd.date_range(t0, periods=n, freq='1H')
    return pd.DataFrame({
        'symbol': symbol,
        'candle_begin_time': cbt,
        'open': open_, 'high': high, 'low': low, 'close': close,
        'vol': vol, 'volCcy': volccy, 'quote_volume': quote_volume,
    })


def main():
    from utils.fancy_grid_function import cal_factor  # 原始实现
    from utils.functions import (proceed_calc_symbol_factor,
                                 calc_grid_params_v1, calc_grid_params_v2)
    from utils.fancy_grid_function import select_grid_coin

    # ---- 1) 单币因子金标 ----
    df = make_symbol_df('BTC/USDT:USDT', n=240, seed=1)
    fac = cal_factor(df.copy())
    factor_cols = ['Reg_v2_2', 'Sgcz_2', 'Reg_v2_5', 'Sgcz_5', 'Er_2',
                   'db_volume_v1_2', 'Atr_5', 'middle_5', 'ma_2', 'ma_5', 'ma_13', '涨跌幅']
    fac[['candle_begin_time'] + factor_cols].to_parquet(
        os.path.join(_HERE, 'factors_golden.parquet'), index=False)

    # ---- 2) 截面因子 + 选币金标 ----
    period = '12H'
    offset = 0
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
    factors = {"Reg_v2_5": True, "Sgcz_5": True, "Er_2": True}
    sel = select_grid_coin(all_df.copy(), factors, [1, 1, 1], 2, run_time)
    keep = ['symbol', 'time', 'rank', 'rank_sum', 'close', 'Atr_5', 'middle_5'] + list(factors.keys())
    sel = sel[[c for c in keep if c in sel.columns]].reset_index(drop=True)
    sel.to_parquet(os.path.join(_HERE, 'cross_select_golden.parquet'), index=False)

    # ---- 3) 网格参数金标（v1 + v2）----
    v2_config = {
        'atr_range_multiplier': 3, 'range_pct_min': 0.05, 'range_pct_max': 0.25,
        'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.003, 'grid_spacing_max': 0.02,
        'grid_count_min': 25, 'grid_count_max': 149, 'stop_buffer_ratio': 0.01,
    }
    row = {'close': 123.45, 'Atr_5': 0.04, 'middle_5': 122.0}
    out = {
        'v1': calc_grid_params_v1(row, price_limit=[0.25, 0.25], stop_limit=0.01),
        'v2': calc_grid_params_v2(row, price_limit=[0.25, 0.25], stop_limit=0.01, v2_config=v2_config),
    }
    with open(os.path.join(_HERE, 'grid_params_golden.json'), 'w') as f:
        json.dump(out, f, indent=2)

    print('golden fixtures written to', _HERE)


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 运行生成脚本**

Run: `TZ=Asia/Shanghai python tests/golden/gen_golden.py`
Expected: 打印 `golden fixtures written to ...`，生成 3 个 fixture 文件。

- [ ] **Step 3: 校验 fixture 非空**

Run:
```bash
python -c "import pandas as pd; print(pd.read_parquet('tests/golden/factors_golden.parquet').shape); print(pd.read_parquet('tests/golden/cross_select_golden.parquet').shape)"
```
Expected: 打印两个非零形状（如 `(240, 13)` 和 `(N, ...)`，N≥1）。

- [ ] **Step 4: 提交 fixture（含生成脚本）**

```bash
git add tests/golden
git commit -m "test: generate golden fixtures from original account_0 factor code"
```

---

### Task 3: 搬运因子到 `core/factors.py`（金标 parity）

**Files:**
- Create: `gridtrade/core/factors.py`
- Create: `tests/core/__init__.py`, `tests/core/test_factors_parity.py`

**Interfaces:**
- Consumes: `tests/golden/factors_golden.parquet`, `tests.golden.gen_golden.make_symbol_df`
- Produces: `gridtrade.core.factors` 暴露 `cal_factor(df)->df`、`cal_cross_factor(all_coin_data)->df`，以及全部底层因子函数（`Reg_v2_signal`、`Sgcz_signal`、`Er_signal`、`Atr_signal`、`db_volume_v1_signal` 等），签名与原 `fancy_grid_function.py` 完全一致。

- [ ] **Step 1: 写 parity 失败测试**

Create `tests/core/__init__.py`（空）。

Create `tests/core/test_factors_parity.py`:

```python
import os

import numpy as np
import pandas as pd

from tests.golden.gen_golden import make_symbol_df

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'factors_golden.parquet')
FACTOR_COLS = ['Reg_v2_2', 'Sgcz_2', 'Reg_v2_5', 'Sgcz_5', 'Er_2',
               'db_volume_v1_2', 'Atr_5', 'middle_5', 'ma_2', 'ma_5', 'ma_13', '涨跌幅']


def test_cal_factor_matches_golden():
    from gridtrade.core.factors import cal_factor
    df = make_symbol_df('BTC/USDT:USDT', n=240, seed=1)
    got = cal_factor(df.copy())
    golden = pd.read_parquet(_GOLDEN)
    for col in FACTOR_COLS:
        np.testing.assert_allclose(
            got[col].to_numpy(dtype='float64'),
            golden[col].to_numpy(dtype='float64'),
            rtol=1e-9, atol=1e-12, equal_nan=True, err_msg=f'factor {col} drifted',
        )
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_factors_parity.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.core.factors`）。

- [ ] **Step 3: 逐字搬运因子文件**

把 `account_0/utils/fancy_grid_function.py` 的**全部内容**复制到 `gridtrade/core/factors.py`，逐字不改公式。仅保留到 `cal_cross_factor`（含）为止的内容 + 顶部 `import talib as ta / numpy / pandas / eps`。把 `select_grid_coin`（文件末尾那个函数）**不要**放进 factors.py（它属于 selection，Task 4 处理）。

执行：
```bash
cp account_0/utils/fancy_grid_function.py gridtrade/core/factors.py
```
然后编辑 `gridtrade/core/factors.py`，删除文件末尾的 `def select_grid_coin(...)` 整个函数（其余因子函数 + `cal_factor` + `cal_cross_factor` 全部保留，不改动）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_factors_parity.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/core/factors.py tests/core
git commit -m "feat(core): migrate factor functions to core.factors (golden parity)"
```

---

### Task 4: 搬运选币/周期转换到 `core/selection.py`（金标 parity）

**Files:**
- Create: `gridtrade/core/selection.py`
- Create: `tests/core/test_selection_parity.py`

**Interfaces:**
- Consumes: `gridtrade.core.factors.cal_factor / cal_cross_factor`；`tests/golden/cross_select_golden.parquet`
- Produces: `gridtrade.core.selection` 暴露：
  - `trans_period_for_grid(data, period, exg_dict=None, offset=0) -> df`
  - `proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset) -> df`
  - `select_grid_coin(data, factor_info, weight_list, choose_symbols, run_time) -> df`
  - `compute_offset(run_time, period, utc_offset) -> int`
  签名与原 `account_0/utils/functions.py` / `fancy_grid_function.py` / `selection_replay.py` 一致。

- [ ] **Step 1: 写 parity 失败测试**

Create `tests/core/test_selection_parity.py`:

```python
import os

import numpy as np
import pandas as pd

from tests.golden.gen_golden import make_symbol_df

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'cross_select_golden.parquet')


def _run_new():
    from gridtrade.core.selection import proceed_calc_symbol_factor, select_grid_coin
    period, offset = '12H', 0
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
    factors = {"Reg_v2_5": True, "Sgcz_5": True, "Er_2": True}
    return select_grid_coin(all_df.copy(), factors, [1, 1, 1], 2, run_time)


def test_selection_matches_golden():
    got = _run_new().reset_index(drop=True)
    golden = pd.read_parquet(_GOLDEN)
    # 选中的币集合与排序一致
    assert list(got.sort_values('rank')['symbol']) == list(golden.sort_values('rank')['symbol'])
    # 关键数值列一致
    g = got.set_index('symbol'); e = golden.set_index('symbol')
    for col in ['rank', 'rank_sum', 'close', 'Atr_5', 'middle_5']:
        np.testing.assert_allclose(
            g.loc[e.index, col].to_numpy('float64'),
            e[col].to_numpy('float64'),
            rtol=1e-9, atol=1e-12, equal_nan=True, err_msg=f'{col} drifted')


def test_compute_offset_matches_legacy_formula():
    from gridtrade.core.selection import compute_offset
    run_time = pd.Timestamp('2024-01-09 05:00:00')
    # 复刻 functions.get_order_offset_tag 的口径
    utc_run = run_time - pd.Timedelta(hours=8)
    expected = int(((utc_run - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % 12)
    assert compute_offset(run_time, '12H', 8) == expected
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_selection_parity.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.core.selection`）。

- [ ] **Step 3: 搬运选币逻辑**

Create `gridtrade/core/selection.py`，把以下函数逐字搬入（不改逻辑）：
- 从 `account_0/utils/functions.py` 复制 `trans_period_for_grid`（53–80 行）与 `proceed_calc_symbol_factor`（84–120 行）。
- 从 `account_0/utils/fancy_grid_function.py` 复制 `select_grid_coin`（566–638 行）。
- 从 `backtest/selection_replay.py` 复制 `compute_offset`（36–39 行）。

文件头部 import 改为：

```python
import time

import numpy as np
import pandas as pd

from gridtrade.core.factors import cal_factor, cal_cross_factor
```

注意：`proceed_calc_symbol_factor` 内部用到 `time.localtime().tm_gmtoff` 与 `cal_factor`/`cal_cross_factor`——保持原样。`select_grid_coin` 内部的 `print(...)` 保留（与原行为一致；调用方可重定向）。`np` 仅 select_grid_coin/其它若用到则保留，否则可删；保留不影响。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_selection_parity.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/core/selection.py tests/core/test_selection_parity.py
git commit -m "feat(core): migrate coin-selection pipeline to core.selection (golden parity)"
```

---

### Task 5: 搬运网格参数到 `core/grid_params.py`（金标 parity）

**Files:**
- Create: `gridtrade/core/grid_params.py`
- Create: `tests/core/test_grid_params_parity.py`

**Interfaces:**
- Consumes: `tests/golden/grid_params_golden.json`
- Produces: `gridtrade.core.grid_params` 暴露：
  - `calc_grid_params_v1(row, price_limit, stop_limit, **kwargs) -> dict`（键: high_price/low_price/stop_high_price/stop_low_price/grid_count）
  - `calc_grid_params_v2(row, price_limit, stop_limit, v2_config, **kwargs) -> dict`（同上键）
  - `_format_price(price, accuracy) -> str`
  逻辑与原 `account_0/utils/functions.py:236-331` 完全一致。

- [ ] **Step 1: 写 parity 失败测试**

Create `tests/core/test_grid_params_parity.py`:

```python
import json
import os

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'grid_params_golden.json')
V2_CONFIG = {
    'atr_range_multiplier': 3, 'range_pct_min': 0.05, 'range_pct_max': 0.25,
    'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.003, 'grid_spacing_max': 0.02,
    'grid_count_min': 25, 'grid_count_max': 149, 'stop_buffer_ratio': 0.01,
}
ROW = {'close': 123.45, 'Atr_5': 0.04, 'middle_5': 122.0}
KEYS = ['high_price', 'low_price', 'stop_high_price', 'stop_low_price', 'grid_count']


def test_grid_params_match_golden():
    from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2
    with open(_GOLDEN) as f:
        golden = json.load(f)
    v1 = calc_grid_params_v1(ROW, price_limit=[0.25, 0.25], stop_limit=0.01)
    v2 = calc_grid_params_v2(ROW, price_limit=[0.25, 0.25], stop_limit=0.01, v2_config=V2_CONFIG)
    for k in KEYS:
        assert abs(float(v1[k]) - float(golden['v1'][k])) < 1e-9, f'v1 {k}'
        assert abs(float(v2[k]) - float(golden['v2'][k])) < 1e-9, f'v2 {k}'


def test_format_price_no_scientific_notation():
    from gridtrade.core.grid_params import _format_price
    assert _format_price(0.000012345, 8) == '0.00001234' or _format_price(0.000012345, 8).startswith('0.0000123')
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_grid_params_parity.py -v`
Expected: FAIL（`ModuleNotFoundError`）。

- [ ] **Step 3: 搬运网格参数函数**

Create `gridtrade/core/grid_params.py`，从 `account_0/utils/functions.py` 逐字复制 `calc_grid_params_v1`（236–270）、`calc_grid_params_v2`（274–323）、`_format_price`（327–331），不改逻辑。文件头部：

```python
import math

import numpy as np
```

（`math` 供 `_format_price` 周边/后续；`np` 供 `_format_price` 的 `np.format_float_positional`。）

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_grid_params_parity.py -v`
Expected: PASS。

- [ ] **Step 5: 全量回归 + 提交**

Run: `pytest tests/core tests/test_scaffold.py -v`
Expected: 全 PASS。

```bash
git add gridtrade/core/grid_params.py tests/core/test_grid_params_parity.py
git commit -m "feat(core): migrate grid-param calc to core.grid_params (golden parity)"
```

---

## P1：ccxt 升级验证 + 适配器层 + FakeExchange

### Task 6: ccxt 升级 + Hyperliquid 可用性验证（spike，钉版本）

**Files:**
- Modify: `requirements.txt:1`（`ccxt==2.0.58` → 目标版本）
- Create: `tests/exchanges/__init__.py`, `tests/exchanges/test_ccxt_smoke.py`

**Interfaces:**
- Produces: 仓库锁定一个支持 hyperliquid、且在 py3.9 + pandas 1.3.5 下可 import 的 ccxt 版本；冒烟测试断言 okx 与 hyperliquid 类存在且具备统一方法。

- [ ] **Step 1: 写冒烟测试（不联网）**

Create `tests/exchanges/__init__.py`（空）。

Create `tests/exchanges/test_ccxt_smoke.py`:

```python
import ccxt


def test_ccxt_has_okx_and_hyperliquid():
    assert hasattr(ccxt, 'okx'), 'ccxt 缺少 okx 类'
    assert hasattr(ccxt, 'hyperliquid'), 'ccxt 版本过低，无 hyperliquid（需升级）'


def test_unified_methods_present():
    for name in ('okx', 'hyperliquid'):
        cls = getattr(ccxt, name)
        ex = cls({'enableRateLimit': True})
        for m in ('fetch_ohlcv', 'create_order', 'cancel_order',
                  'fetch_open_orders', 'fetch_balance', 'fetch_positions',
                  'set_leverage', 'load_markets', 'fetch_funding_rate_history'):
            assert hasattr(ex, m), f'{name} 缺少统一方法 {m}'
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_ccxt_smoke.py -v`
Expected: FAIL（`ccxt 2.0.58` 无 `hyperliquid`）。

- [ ] **Step 3: 升级 ccxt 并验证 py3.9 兼容**

升级到支持 hyperliquid 的最新 4.x（先试最新，按需回退）：
```bash
pip install 'ccxt>=4.2,<5' && python -c "import ccxt, pandas; print('ccxt', ccxt.__version__, '| pandas', pandas.__version__)"
```
确认：①能 import 不报错；②pandas 仍为 1.3.5；③`ccxt.hyperliquid` 存在。把实测可用的确切版本写回 `requirements.txt`（例如 `ccxt==4.4.x`，用实测通过的版本号替换 `ccxt==2.0.58`）。

> 若最新 4.x 在 py3.9 报错，逐步回退到仍含 hyperliquid 的较低 4.x，直到 import 通过；将通过的版本钉死。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/exchanges/test_ccxt_smoke.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add requirements.txt tests/exchanges/test_ccxt_smoke.py
git commit -m "build: upgrade ccxt to hyperliquid-capable version (py3.9 verified)"
```

---

### Task 7: ExchangeAdapter 抽象接口 + 数据类

**Files:**
- Create: `gridtrade/exchanges/base.py`
- Create: `tests/exchanges/test_base_contract.py`

**Interfaces:**
- Produces: `gridtrade.exchanges.base` 暴露规范符号常量与数据类 + ABC：
  - 数据类：`Instrument(symbol,tick,lot,min_size,state,list_ts)`、`Balance(equity,cash)`、`Position(symbol,net_size,avg_price)`、`Order(id,client_oid,symbol,side,price,size,filled,status,reduce_only)`、`Trade(id,client_oid,symbol,side,price,size,fee,ts)`
  - ABC `ExchangeAdapter`，抽象方法见下。`name: str` 属性。
  - 列常量 `CANDLE_COLS`、`FUNDING_COLS`。

- [ ] **Step 1: 写接口契约测试**

Create `tests/exchanges/test_base_contract.py`:

```python
import inspect

import pytest


def test_dataclasses_fields():
    from gridtrade.exchanges.base import Instrument, Balance, Position, Order, Trade
    inst = Instrument(symbol='BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                      state='live', list_ts=0)
    assert inst.symbol == 'BTC/USDT:USDT'
    assert Balance(equity=1.0, cash=0.5).cash == 0.5
    assert Position(symbol='BTC/USDT:USDT', net_size=-1.0, avg_price=100.0).net_size == -1.0
    o = Order(id='1', client_oid='g:0', symbol='BTC/USDT:USDT', side='buy',
              price=1.0, size=2.0, filled=0.0, status='open', reduce_only=False)
    assert o.client_oid == 'g:0'
    assert Trade(id='t', client_oid='g:0', symbol='X', side='buy', price=1.0,
                 size=1.0, fee=0.1, ts=0).fee == 0.1


def test_adapter_is_abstract():
    from gridtrade.exchanges.base import ExchangeAdapter
    with pytest.raises(TypeError):
        ExchangeAdapter()  # 抽象类不能实例化


def test_adapter_declares_required_methods():
    from gridtrade.exchanges.base import ExchangeAdapter
    required = {'list_instruments', 'fetch_ohlcv', 'fetch_funding_history',
               'fetch_price', 'fetch_balance', 'fetch_positions',
               'create_limit_order', 'create_market_order', 'cancel_order',
               'cancel_all', 'fetch_open_orders', 'fetch_my_trades',
               'set_leverage', 'exchange_status'}
    abstract = ExchangeAdapter.__abstractmethods__
    assert required.issubset(abstract), f'缺少抽象方法: {required - abstract}'


def test_column_constants():
    from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS
    assert CANDLE_COLS == ['symbol', 'candle_begin_time', 'open', 'high', 'low',
                           'close', 'vol', 'volCcy', 'quote_volume']
    assert FUNDING_COLS == ['ts', 'symbol', 'fundingRate', 'realizedRate']
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_base_contract.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.exchanges.base`）。

- [ ] **Step 3: 实现 base.py**

Create `gridtrade/exchanges/base.py`:

```python
"""交易所抽象层（Ports & Adapters 的端口）。
规范符号 = ccxt 统一符号，永续如 'BTC/USDT:USDT'。各所原生格式仅在各自适配器内部映射。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

CANDLE_COLS = ['symbol', 'candle_begin_time', 'open', 'high', 'low',
               'close', 'vol', 'volCcy', 'quote_volume']
FUNDING_COLS = ['ts', 'symbol', 'fundingRate', 'realizedRate']


@dataclass
class Instrument:
    symbol: str
    tick: float
    lot: float
    min_size: float
    state: str
    list_ts: int  # 上市时间，毫秒


@dataclass
class Balance:
    equity: float
    cash: float


@dataclass
class Position:
    symbol: str
    net_size: float   # 带符号：+多 / -空
    avg_price: float


@dataclass
class Order:
    id: str
    client_oid: str
    symbol: str
    side: str         # 'buy' / 'sell'
    price: float
    size: float
    filled: float
    status: str       # 'open' / 'closed' / 'canceled'
    reduce_only: bool


@dataclass
class Trade:
    id: str
    client_oid: str
    symbol: str
    side: str
    price: float
    size: float
    fee: float
    ts: int           # 毫秒


class ExchangeAdapter(ABC):
    """所有交易所适配器的统一端口。规范符号入参，统一 schema 出参。"""

    name: str = 'base'

    # ---- 行情（公共）----
    @abstractmethod
    def list_instruments(self) -> List[Instrument]: ...

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    start_ms: int, end_ms: int) -> pd.DataFrame:
        """返回列为 CANDLE_COLS、按 candle_begin_time 升序的 DataFrame。"""

    @abstractmethod
    def fetch_funding_history(self, symbol: str,
                             start_ms: int, end_ms: int) -> pd.DataFrame:
        """返回列为 FUNDING_COLS、按 ts 升序的 DataFrame。"""

    @abstractmethod
    def fetch_price(self, symbol: str) -> float: ...

    # ---- 账户/交易（私有）----
    @abstractmethod
    def fetch_balance(self) -> Balance: ...

    @abstractmethod
    def fetch_positions(self, symbol: str) -> Position: ...

    @abstractmethod
    def create_limit_order(self, symbol: str, side: str, price: float, size: float,
                           *, post_only: bool = False, reduce_only: bool = False,
                           client_oid: Optional[str] = None) -> Order: ...

    @abstractmethod
    def create_market_order(self, symbol: str, side: str, size: float,
                            *, reduce_only: bool = False,
                            client_oid: Optional[str] = None) -> Order: ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> None: ...

    @abstractmethod
    def cancel_all(self, symbol: str) -> None: ...

    @abstractmethod
    def fetch_open_orders(self, symbol: str) -> List[Order]: ...

    @abstractmethod
    def fetch_my_trades(self, symbol: str,
                        since_ms: Optional[int] = None) -> List[Trade]: ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: float) -> None: ...

    @abstractmethod
    def exchange_status(self) -> str:
        """'ok' 或 'maintenance'。"""

    # ---- 可选：标记价 K线（默认未实现）----
    def fetch_mark_ohlcv(self, symbol: str, timeframe: str,
                         start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/exchanges/test_base_contract.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/base.py tests/exchanges/test_base_contract.py
git commit -m "feat(exchanges): add ExchangeAdapter ABC + domain dataclasses"
```

---

### Task 8: FakeExchange 内存实现（测试/回测替身）

**Files:**
- Create: `gridtrade/exchanges/fake.py`
- Create: `tests/exchanges/test_fake.py`

**Interfaces:**
- Consumes: `gridtrade.exchanges.base`（ExchangeAdapter + 数据类）
- Produces: `gridtrade.exchanges.fake.FakeExchange(ExchangeAdapter)`，额外暴露测试钩子：
  - `__init__(self, instruments=None, price=100.0)`
  - `set_price(self, symbol, price)`：设置当前价并撮合穿越价位的挂单
  - `seed_ohlcv(self, symbol, df)` / `seed_funding(self, symbol, df)`：注入历史数据
  - 限价撮合规则：buy 当 price>=order.price 成交；sell 当 price<=order.price 成交。成交写入 my_trades 并更新 position（净额、加权均价）。

- [ ] **Step 1: 写 FakeExchange 行为测试**

Create `tests/exchanges/test_fake.py`:

```python
import pandas as pd

from gridtrade.exchanges.base import Instrument


def _fake():
    from gridtrade.exchanges.fake import FakeExchange
    insts = [Instrument('BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                        state='live', list_ts=0)]
    return FakeExchange(instruments=insts, price=100.0)


def test_place_and_list_open_orders():
    ex = _fake()
    o = ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=1.0,
                              client_oid='g1:0')
    assert o.status == 'open'
    opens = ex.fetch_open_orders('BTC/USDT:USDT')
    assert len(opens) == 1 and opens[0].client_oid == 'g1:0'


def test_buy_limit_fills_when_price_drops():
    ex = _fake()
    ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=2.0, client_oid='g1:0')
    ex.set_price('BTC/USDT:USDT', 94.0)  # 穿越买单价
    assert ex.fetch_open_orders('BTC/USDT:USDT') == []
    trades = ex.fetch_my_trades('BTC/USDT:USDT')
    assert len(trades) == 1 and trades[0].side == 'buy' and trades[0].size == 2.0
    pos = ex.fetch_positions('BTC/USDT:USDT')
    assert pos.net_size == 2.0 and pos.avg_price == 95.0


def test_sell_reduces_position_and_cancel_works():
    ex = _fake()
    ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=2.0, client_oid='g1:0')
    ex.set_price('BTC/USDT:USDT', 94.0)
    ex.create_limit_order('BTC/USDT:USDT', 'sell', price=105.0, size=1.0, client_oid='g1:1')
    cid = ex.fetch_open_orders('BTC/USDT:USDT')[0].id
    ex.cancel_order('BTC/USDT:USDT', cid)
    assert ex.fetch_open_orders('BTC/USDT:USDT') == []


def test_market_order_fills_immediately():
    ex = _fake()
    ex.set_price('BTC/USDT:USDT', 100.0)
    ex.create_market_order('BTC/USDT:USDT', 'buy', size=3.0, client_oid='init')
    assert ex.fetch_positions('BTC/USDT:USDT').net_size == 3.0


def test_seeded_ohlcv_and_funding_roundtrip():
    ex = _fake()
    df = pd.DataFrame({'symbol': ['BTC/USDT:USDT'], 'candle_begin_time': [pd.Timestamp('2024-01-01')],
                       'open': [1.0], 'high': [2.0], 'low': [0.5], 'close': [1.5],
                       'vol': [10.0], 'volCcy': [15.0], 'quote_volume': [22.5]})
    ex.seed_ohlcv('BTC/USDT:USDT', df)
    got = ex.fetch_ohlcv('BTC/USDT:USDT', '1H', 0, 10**13)
    assert list(got['close']) == [1.5]
    assert ex.exchange_status() == 'ok'
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_fake.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.exchanges.fake`）。

- [ ] **Step 3: 实现 FakeExchange**

Create `gridtrade/exchanges/fake.py`:

```python
"""内存交易所模拟器：实现 ExchangeAdapter，供执行/对账/止损离线 TDD，并与回测填单同源。
撮合规则：buy 当现价<=买单价成交；sell 当现价>=卖单价成交（限价单被价格穿越即成交）。
"""
import itertools
from typing import Dict, List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, ExchangeAdapter, Instrument,
                                      Order, Position, Trade)


class FakeExchange(ExchangeAdapter):
    name = 'fake'

    def __init__(self, instruments: Optional[List[Instrument]] = None, price: float = 100.0):
        self._instruments = list(instruments or [])
        self._price: Dict[str, float] = {}
        self._open: Dict[str, Order] = {}          # order_id -> Order
        self._trades: List[Trade] = []
        self._pos: Dict[str, Position] = {}
        self._ohlcv: Dict[str, pd.DataFrame] = {}
        self._funding: Dict[str, pd.DataFrame] = {}
        self._ids = itertools.count(1)
        self._ts = itertools.count(1)
        self._fee_rate = 0.0005
        self._default_price = price

    # ---- 测试钩子 ----
    def set_price(self, symbol: str, price: float) -> None:
        self._price[symbol] = price
        self._match(symbol, price)

    def seed_ohlcv(self, symbol: str, df: pd.DataFrame) -> None:
        self._ohlcv[symbol] = df.copy()

    def seed_funding(self, symbol: str, df: pd.DataFrame) -> None:
        self._funding[symbol] = df.copy()

    def _price_of(self, symbol: str) -> float:
        return self._price.get(symbol, self._default_price)

    # ---- 撮合 ----
    def _match(self, symbol: str, price: float) -> None:
        for oid in list(self._open.keys()):
            o = self._open[oid]
            if o.symbol != symbol:
                continue
            hit = (o.side == 'buy' and price <= o.price) or (o.side == 'sell' and price >= o.price)
            if hit:
                self._fill(o, o.price)
                del self._open[oid]

    def _fill(self, o: Order, fill_price: float) -> None:
        signed = o.size if o.side == 'buy' else -o.size
        pos = self._pos.get(o.symbol, Position(o.symbol, 0.0, 0.0))
        new_net = pos.net_size + signed
        # 同向加仓更新加权均价；反向或反手时简单处理（净仓符号不翻转的减仓保留均价）
        if pos.net_size == 0 or (pos.net_size > 0) == (signed > 0):
            denom = abs(new_net) if new_net != 0 else 1.0
            avg = (abs(pos.net_size) * pos.avg_price + abs(signed) * fill_price) / denom
        else:
            avg = pos.avg_price if (pos.net_size > 0) == (new_net >= 0) else fill_price
        self._pos[o.symbol] = Position(o.symbol, new_net, avg)
        self._trades.append(Trade(
            id=str(next(self._ts)), client_oid=o.client_oid, symbol=o.symbol,
            side=o.side, price=fill_price, size=o.size,
            fee=o.size * fill_price * self._fee_rate, ts=next(self._ts)))

    # ---- 行情 ----
    def list_instruments(self) -> List[Instrument]:
        return list(self._instruments)

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        return self._ohlcv.get(symbol, pd.DataFrame()).copy()

    def fetch_funding_history(self, symbol, start_ms, end_ms):
        return self._funding.get(symbol, pd.DataFrame()).copy()

    def fetch_price(self, symbol) -> float:
        return self._price_of(symbol)

    # ---- 账户/交易 ----
    def fetch_balance(self) -> Balance:
        return Balance(equity=1_000_000.0, cash=1_000_000.0)

    def fetch_positions(self, symbol) -> Position:
        return self._pos.get(symbol, Position(symbol, 0.0, 0.0))

    def create_limit_order(self, symbol, side, price, size, *,
                           post_only=False, reduce_only=False, client_oid=None) -> Order:
        oid = str(next(self._ids))
        o = Order(id=oid, client_oid=client_oid or oid, symbol=symbol, side=side,
                  price=price, size=size, filled=0.0, status='open', reduce_only=reduce_only)
        self._open[oid] = o
        # 下单即按当前价检查是否立即成交
        self._match(symbol, self._price_of(symbol))
        return o if oid in self._open else Order(
            id=oid, client_oid=o.client_oid, symbol=symbol, side=side, price=price,
            size=size, filled=size, status='closed', reduce_only=reduce_only)

    def create_market_order(self, symbol, side, size, *,
                            reduce_only=False, client_oid=None) -> Order:
        oid = str(next(self._ids))
        o = Order(id=oid, client_oid=client_oid or oid, symbol=symbol, side=side,
                  price=self._price_of(symbol), size=size, filled=size,
                  status='closed', reduce_only=reduce_only)
        self._fill(o, self._price_of(symbol))
        return o

    def cancel_order(self, symbol, order_id) -> None:
        self._open.pop(order_id, None)

    def cancel_all(self, symbol) -> None:
        for oid in [k for k, v in self._open.items() if v.symbol == symbol]:
            del self._open[oid]

    def fetch_open_orders(self, symbol) -> List[Order]:
        return [o for o in self._open.values() if o.symbol == symbol]

    def fetch_my_trades(self, symbol, since_ms=None) -> List[Trade]:
        return [t for t in self._trades if t.symbol == symbol
                and (since_ms is None or t.ts >= since_ms)]

    def set_leverage(self, symbol, leverage) -> None:
        pass

    def exchange_status(self) -> str:
        return 'ok'
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/exchanges/test_fake.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/fake.py tests/exchanges/test_fake.py
git commit -m "feat(exchanges): add in-memory FakeExchange test double"
```

---

### Task 9: CcxtAdapter 通用实现

**Files:**
- Create: `gridtrade/exchanges/ccxt_adapter.py`
- Create: `tests/exchanges/test_ccxt_adapter.py`

**Interfaces:**
- Consumes: `gridtrade.exchanges.base`；一个 ccxt-like client 对象（注入，便于 mock）
- Produces: `gridtrade.exchanges.ccxt_adapter.CcxtAdapter(ExchangeAdapter)`：
  - `__init__(self, client, name='ccxt')`：`client` 为 ccxt 交易所实例（或 mock）
  - 将统一 ccxt 返回映射到适配器数据类/列。
  - `fetch_ohlcv` 把 ccxt 的 `[ts,o,h,l,c,v]` 列表映射到 CANDLE_COLS（`volCcy`=v、`quote_volume`=v*close 兜底）。

- [ ] **Step 1: 写基于 mock client 的测试**

Create `tests/exchanges/test_ccxt_adapter.py`:

```python
import pandas as pd


class FakeCcxtClient:
    """最小 ccxt-like 桩：只实现 CcxtAdapter 用到的方法。"""
    def __init__(self):
        self.created = []
        self.canceled = []
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
        # ccxt: [ms, open, high, low, close, volume]
        return [[1704067200000, 1.0, 2.0, 0.5, 1.5, 10.0],
                [1704070800000, 1.5, 2.5, 1.0, 2.0, 20.0]]
    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        return [{'timestamp': 1704067200000, 'fundingRate': 0.0001},
                {'timestamp': 1704070800000, 'fundingRate': -0.0002}]
    def fetch_ticker(self, symbol):
        return {'last': 2.0}
    def fetch_balance(self, params=None):
        return {'USDT': {'total': 1000.0, 'free': 800.0}}
    def fetch_positions(self, symbols=None, params=None):
        return [{'symbol': 'BTC/USDT:USDT', 'contracts': 3.0, 'side': 'long',
                 'entryPrice': 100.0}]
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        oid = str(len(self.created) + 1)
        self.created.append((symbol, type, side, amount, price, params))
        return {'id': oid, 'clientOrderId': (params or {}).get('clientOrderId', oid),
                'symbol': symbol, 'side': side, 'price': price or 0.0, 'amount': amount,
                'filled': 0.0, 'status': 'open'}
    def cancel_order(self, id, symbol=None, params=None):
        self.canceled.append((id, symbol))
    def cancel_all_orders(self, symbol=None, params=None):
        self.canceled.append(('ALL', symbol))
    def fetch_open_orders(self, symbol=None, params=None):
        return [{'id': '7', 'clientOrderId': 'g:0', 'symbol': symbol, 'side': 'buy',
                 'price': 1.0, 'amount': 2.0, 'filled': 0.0, 'status': 'open'}]
    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        return [{'id': 't1', 'order': 'o1', 'symbol': symbol, 'side': 'buy',
                 'price': 1.0, 'amount': 2.0, 'timestamp': 1704067200000,
                 'fee': {'cost': 0.1}, 'info': {'clOrdId': 'g:0'}}]
    def set_leverage(self, leverage, symbol=None, params=None):
        self._lev = (leverage, symbol)
    def load_markets(self):
        return {'BTC/USDT:USDT': {}}
    markets = {'BTC/USDT:USDT': {'precision': {'price': 0.1, 'amount': 0.001},
                                 'limits': {'amount': {'min': 0.001}},
                                 'active': True, 'info': {'listTime': '0'}}}


def _adapter():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    return CcxtAdapter(FakeCcxtClient(), name='ccxt')


def test_fetch_ohlcv_maps_to_candle_cols():
    from gridtrade.exchanges.base import CANDLE_COLS
    df = _adapter().fetch_ohlcv('BTC/USDT:USDT', '1H', 0, 10**13)
    assert list(df.columns) == CANDLE_COLS
    assert df['close'].tolist() == [1.5, 2.0]
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2024-01-01 00:00:00')


def test_fetch_funding_history_maps_cols():
    from gridtrade.exchanges.base import FUNDING_COLS
    df = _adapter().fetch_funding_history('BTC/USDT:USDT', 0, 10**13)
    assert list(df.columns) == FUNDING_COLS
    assert df['fundingRate'].tolist() == [0.0001, -0.0002]


def test_balance_and_position_mapping():
    a = _adapter()
    bal = a.fetch_balance()
    assert bal.equity == 1000.0 and bal.cash == 800.0
    pos = a.fetch_positions('BTC/USDT:USDT')
    assert pos.net_size == 3.0 and pos.avg_price == 100.0


def test_create_limit_order_passes_client_oid():
    a = _adapter()
    o = a.create_limit_order('BTC/USDT:USDT', 'buy', 1.0, 2.0, client_oid='g:0')
    assert o.client_oid == 'g:0' and o.status == 'open'
    # client.created 最后一项的 params 应带 clientOrderId
    _, type_, side, amount, price, params = a.client.created[-1]
    assert type_ == 'limit' and params.get('clientOrderId') == 'g:0'


def test_open_orders_and_trades_mapping():
    a = _adapter()
    orders = a.fetch_open_orders('BTC/USDT:USDT')
    assert orders[0].client_oid == 'g:0'
    trades = a.fetch_my_trades('BTC/USDT:USDT')
    assert trades[0].client_oid == 'g:0' and trades[0].fee == 0.1


def test_instruments_mapping():
    a = _adapter()
    insts = a.list_instruments()
    assert insts[0].symbol == 'BTC/USDT:USDT' and insts[0].state == 'live'
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_ccxt_adapter.py -v`
Expected: FAIL（`ModuleNotFoundError`）。

- [ ] **Step 3: 实现 CcxtAdapter**

Create `gridtrade/exchanges/ccxt_adapter.py`:

```python
"""基于 ccxt 统一接口的通用适配器。client 注入以便 mock。
各所差异（凭证/资金费周期/沙盒/符号映射）由子类覆写。"""
from typing import List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, CANDLE_COLS, ExchangeAdapter,
                                      FUNDING_COLS, Instrument, Order, Position, Trade)


class CcxtAdapter(ExchangeAdapter):
    name = 'ccxt'

    def __init__(self, client, name: Optional[str] = None):
        self.client = client
        if name:
            self.name = name

    # ---- 符号映射：默认规范符号即 ccxt 统一符号，原样透传 ----
    def to_native(self, symbol: str) -> str:
        return symbol

    def to_canonical(self, native: str) -> str:
        return native

    # ---- 行情 ----
    def list_instruments(self) -> List[Instrument]:
        self.client.load_markets()
        out = []
        for sym, m in self.client.markets.items():
            info = m.get('info', {}) or {}
            out.append(Instrument(
                symbol=self.to_canonical(sym),
                tick=float(m.get('precision', {}).get('price') or 0.0),
                lot=float(m.get('precision', {}).get('amount') or 0.0),
                min_size=float(m.get('limits', {}).get('amount', {}).get('min') or 0.0),
                state='live' if m.get('active', True) else 'expired',
                list_ts=int(info.get('listTime') or 0),
            ))
        return out

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms) -> pd.DataFrame:
        rows = self.client.fetch_ohlcv(self.to_native(symbol), timeframe,
                                       since=start_ms, limit=None)
        if not rows:
            return pd.DataFrame(columns=CANDLE_COLS)
        df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
        df['symbol'] = symbol
        df['volCcy'] = df['vol']
        df['quote_volume'] = df['vol'] * df['close']
        df = df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)
        return df

    def fetch_funding_history(self, symbol, start_ms, end_ms) -> pd.DataFrame:
        rows = self.client.fetch_funding_rate_history(self.to_native(symbol),
                                                      since=start_ms, limit=None)
        if not rows:
            return pd.DataFrame(columns=FUNDING_COLS)
        df = pd.DataFrame([{
            'ts': int(r['timestamp']), 'symbol': symbol,
            'fundingRate': float(r['fundingRate']),
            'realizedRate': float(r['fundingRate']),
        } for r in rows])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        return df[FUNDING_COLS].sort_values('ts').reset_index(drop=True)

    def fetch_price(self, symbol) -> float:
        return float(self.client.fetch_ticker(self.to_native(symbol))['last'])

    # ---- 账户/交易 ----
    def fetch_balance(self) -> Balance:
        b = self.client.fetch_balance()
        u = b.get('USDT', {})
        return Balance(equity=float(u.get('total') or 0.0), cash=float(u.get('free') or 0.0))

    def fetch_positions(self, symbol) -> Position:
        for p in self.client.fetch_positions([self.to_native(symbol)]):
            if self.to_canonical(p['symbol']) == symbol:
                contracts = float(p.get('contracts') or 0.0)
                net = contracts if p.get('side') == 'long' else -contracts
                return Position(symbol, net, float(p.get('entryPrice') or 0.0))
        return Position(symbol, 0.0, 0.0)

    def _to_order(self, r) -> Order:
        return Order(
            id=str(r['id']),
            client_oid=str(r.get('clientOrderId') or (r.get('info', {}) or {}).get('clOrdId') or r['id']),
            symbol=self.to_canonical(r['symbol']), side=r['side'],
            price=float(r.get('price') or 0.0), size=float(r.get('amount') or 0.0),
            filled=float(r.get('filled') or 0.0), status=r.get('status', 'open'),
            reduce_only=bool((r.get('info', {}) or {}).get('reduceOnly', False)))

    def _params(self, reduce_only, client_oid, post_only=False):
        p = {}
        if client_oid:
            p['clientOrderId'] = client_oid
        if reduce_only:
            p['reduceOnly'] = True
        if post_only:
            p['postOnly'] = True
        return p

    def create_limit_order(self, symbol, side, price, size, *,
                           post_only=False, reduce_only=False, client_oid=None) -> Order:
        r = self.client.create_order(self.to_native(symbol), 'limit', side, size, price,
                                     self._params(reduce_only, client_oid, post_only))
        return self._to_order(r)

    def create_market_order(self, symbol, side, size, *,
                            reduce_only=False, client_oid=None) -> Order:
        r = self.client.create_order(self.to_native(symbol), 'market', side, size, None,
                                     self._params(reduce_only, client_oid))
        return self._to_order(r)

    def cancel_order(self, symbol, order_id) -> None:
        self.client.cancel_order(order_id, self.to_native(symbol))

    def cancel_all(self, symbol) -> None:
        self.client.cancel_all_orders(self.to_native(symbol))

    def fetch_open_orders(self, symbol) -> List[Order]:
        return [self._to_order(r) for r in self.client.fetch_open_orders(self.to_native(symbol))]

    def fetch_my_trades(self, symbol, since_ms=None) -> List[Trade]:
        out = []
        for r in self.client.fetch_my_trades(self.to_native(symbol), since=since_ms):
            out.append(Trade(
                id=str(r['id']),
                client_oid=str((r.get('info', {}) or {}).get('clOrdId') or r.get('order') or r['id']),
                symbol=self.to_canonical(r['symbol']), side=r['side'],
                price=float(r['price']), size=float(r['amount']),
                fee=float((r.get('fee') or {}).get('cost') or 0.0), ts=int(r['timestamp'])))
        return out

    def set_leverage(self, symbol, leverage) -> None:
        self.client.set_leverage(leverage, self.to_native(symbol))

    def exchange_status(self) -> str:
        return 'ok'
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/exchanges/test_ccxt_adapter.py -v`
Expected: PASS（6 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_ccxt_adapter.py
git commit -m "feat(exchanges): add generic CcxtAdapter mapping unified ccxt to ports"
```

---

### Task 10: OkxAdapter（凭证/沙盒/资金费 8h/符号映射）

**Files:**
- Create: `gridtrade/exchanges/okx.py`
- Create: `tests/exchanges/test_okx_adapter.py`

**Interfaces:**
- Consumes: `gridtrade.exchanges.ccxt_adapter.CcxtAdapter`
- Produces: `gridtrade.exchanges.okx.OkxAdapter(CcxtAdapter)`：
  - `name = 'okx'`、`FUNDING_INTERVAL_HOURS = 8`
  - 符号映射：规范 `'BTC/USDT:USDT'` ↔ 原生 `'BTC-USDT-SWAP'`
  - 类方法 `from_credentials(api_key, secret, password, *, simulated=False, proxies=None)` 构造内部 ccxt okx client（模拟盘加 `x-simulated-trading:1` 头）。

- [ ] **Step 1: 写符号映射 + 资金费周期测试**

Create `tests/exchanges/test_okx_adapter.py`:

```python
from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


def _okx():
    from gridtrade.exchanges.okx import OkxAdapter
    return OkxAdapter(FakeCcxtClient())


def test_symbol_mapping_roundtrip():
    a = _okx()
    assert a.to_native('BTC/USDT:USDT') == 'BTC-USDT-SWAP'
    assert a.to_canonical('BTC-USDT-SWAP') == 'BTC/USDT:USDT'
    assert a.to_native('ETH/USDT:USDT') == 'ETH-USDT-SWAP'


def test_funding_interval():
    assert _okx().FUNDING_INTERVAL_HOURS == 8
    assert _okx().name == 'okx'


def test_simulated_header_applied():
    import ccxt
    from gridtrade.exchanges.okx import OkxAdapter
    a = OkxAdapter.from_credentials('k', 's', 'p', simulated=True)
    assert isinstance(a.client, ccxt.okx)
    assert a.client.headers.get('x-simulated-trading') == '1'
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_okx_adapter.py -v`
Expected: FAIL（`ModuleNotFoundError`）。

- [ ] **Step 3: 实现 OkxAdapter**

Create `gridtrade/exchanges/okx.py`:

```python
"""OKX 适配器：凭证(passphrase)/模拟盘头/资金费 8h/符号映射。"""
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class OkxAdapter(CcxtAdapter):
    name = 'okx'
    FUNDING_INTERVAL_HOURS = 8

    def __init__(self, client):
        super().__init__(client, name='okx')

    # 规范 'BTC/USDT:USDT' <-> 原生 'BTC-USDT-SWAP'
    def to_native(self, symbol: str) -> str:
        base = symbol.split('/')[0]
        return f'{base}-USDT-SWAP'

    def to_canonical(self, native: str) -> str:
        if native.endswith('-USDT-SWAP'):
            return f'{native[:-len("-USDT-SWAP")]}/USDT:USDT'
        return native

    @classmethod
    def from_credentials(cls, api_key, secret, password, *,
                         simulated=False, proxies=None, timeout=5000):
        import ccxt
        client = ccxt.okx({
            'apiKey': api_key, 'secret': secret, 'password': password,
            'timeout': timeout, 'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if simulated:
            client.headers = dict(getattr(client, 'headers', None) or {},
                                  **{'x-simulated-trading': '1'})
        return cls(client)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/exchanges/test_okx_adapter.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/okx.py tests/exchanges/test_okx_adapter.py
git commit -m "feat(exchanges): add OkxAdapter (symbol map, sandbox header, 8h funding)"
```

---

### Task 11: HyperliquidAdapter（钱包凭证/资金费 1h/符号=币名）

**Files:**
- Create: `gridtrade/exchanges/hyperliquid.py`
- Create: `tests/exchanges/test_hyperliquid_adapter.py`

**Interfaces:**
- Consumes: `gridtrade.exchanges.ccxt_adapter.CcxtAdapter`
- Produces: `gridtrade.exchanges.hyperliquid.HyperliquidAdapter(CcxtAdapter)`：
  - `name = 'hyperliquid'`、`FUNDING_INTERVAL_HOURS = 1`
  - 符号映射：规范 `'BTC/USDT:USDT'` ↔ 原生 `'BTC/USDC:USDC'`（HL 用 USDC 计价；ccxt 统一符号）
  - 类方法 `from_credentials(wallet_address, private_key, *, proxies=None)` 构造 ccxt hyperliquid client。

- [ ] **Step 1: 写符号映射 + 资金费周期测试**

Create `tests/exchanges/test_hyperliquid_adapter.py`:

```python
from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


def _hl():
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    return HyperliquidAdapter(FakeCcxtClient())


def test_symbol_mapping_roundtrip():
    a = _hl()
    assert a.to_native('BTC/USDT:USDT') == 'BTC/USDC:USDC'
    assert a.to_canonical('BTC/USDC:USDC') == 'BTC/USDT:USDT'


def test_funding_interval_and_name():
    assert _hl().FUNDING_INTERVAL_HOURS == 1
    assert _hl().name == 'hyperliquid'


def test_from_credentials_builds_ccxt_client():
    import ccxt
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    a = HyperliquidAdapter.from_credentials('0xWALLET', '0xKEY')
    assert isinstance(a.client, ccxt.hyperliquid)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_hyperliquid_adapter.py -v`
Expected: FAIL（`ModuleNotFoundError`）。

- [ ] **Step 3: 实现 HyperliquidAdapter**

Create `gridtrade/exchanges/hyperliquid.py`:

```python
"""Hyperliquid 适配器：钱包凭证/资金费 1h/USDC 计价符号映射。"""
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class HyperliquidAdapter(CcxtAdapter):
    name = 'hyperliquid'
    FUNDING_INTERVAL_HOURS = 1

    def __init__(self, client):
        super().__init__(client, name='hyperliquid')

    # 规范 'BTC/USDT:USDT' <-> HL 原生 'BTC/USDC:USDC'
    def to_native(self, symbol: str) -> str:
        base = symbol.split('/')[0]
        return f'{base}/USDC:USDC'

    def to_canonical(self, native: str) -> str:
        base = native.split('/')[0]
        return f'{base}/USDT:USDT'

    @classmethod
    def from_credentials(cls, wallet_address, private_key, *, proxies=None):
        import ccxt
        client = ccxt.hyperliquid({
            'walletAddress': wallet_address,
            'privateKey': private_key,
            'enableRateLimit': True,
            'proxies': proxies or {},
        })
        return cls(client)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/exchanges/test_hyperliquid_adapter.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/hyperliquid.py tests/exchanges/test_hyperliquid_adapter.py
git commit -m "feat(exchanges): add HyperliquidAdapter (wallet creds, 1h funding, USDC symbols)"
```

---

### Task 12: ExchangeRegistry 工厂（按配置构造适配器）

**Files:**
- Create: `gridtrade/exchanges/registry.py`
- Create: `tests/exchanges/test_registry.py`

**Interfaces:**
- Consumes: `OkxAdapter`、`HyperliquidAdapter`、`FakeExchange`
- Produces: `gridtrade.exchanges.registry.build_adapter(config) -> ExchangeAdapter`：
  - `config` 为 dict，含 `exchange`（'okx'/'hyperliquid'/'fake'）与对应凭证。
  - 未知 exchange 抛 `ValueError`。

- [ ] **Step 1: 写工厂测试**

Create `tests/exchanges/test_registry.py`:

```python
import pytest


def test_build_fake():
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.fake import FakeExchange
    a = build_adapter({'exchange': 'fake'})
    assert isinstance(a, FakeExchange)


def test_build_okx():
    import ccxt
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.okx import OkxAdapter
    a = build_adapter({'exchange': 'okx', 'api_key': 'k', 'secret': 's',
                       'password': 'p', 'simulated': True})
    assert isinstance(a, OkxAdapter) and isinstance(a.client, ccxt.okx)


def test_build_hyperliquid():
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    a = build_adapter({'exchange': 'hyperliquid', 'wallet_address': '0xW',
                       'private_key': '0xK'})
    assert isinstance(a, HyperliquidAdapter)


def test_unknown_raises():
    from gridtrade.exchanges.registry import build_adapter
    with pytest.raises(ValueError):
        build_adapter({'exchange': 'nope'})
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/exchanges/test_registry.py -v`
Expected: FAIL（`ModuleNotFoundError`）。

- [ ] **Step 3: 实现 registry**

Create `gridtrade/exchanges/registry.py`:

```python
"""按配置构造交易所适配器（Factory）。"""
from gridtrade.exchanges.base import ExchangeAdapter
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.exchanges.okx import OkxAdapter


def build_adapter(config: dict) -> ExchangeAdapter:
    name = (config.get('exchange') or '').lower()
    if name == 'fake':
        return FakeExchange()
    if name == 'okx':
        return OkxAdapter.from_credentials(
            config.get('api_key', ''), config.get('secret', ''),
            config.get('password', ''),
            simulated=bool(config.get('simulated', False)),
            proxies=config.get('proxies'))
    if name == 'hyperliquid':
        return HyperliquidAdapter.from_credentials(
            config.get('wallet_address', ''), config.get('private_key', ''),
            proxies=config.get('proxies'))
    raise ValueError(f'未知交易所: {name!r}（支持: okx/hyperliquid/fake）')
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `pytest tests/exchanges/test_registry.py -v`
Expected: PASS（4 passed）。

Run: `pytest -q`
Expected: 全 PASS（P0 core + P1 exchanges 全绿）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/registry.py tests/exchanges/test_registry.py
git commit -m "feat(exchanges): add build_adapter factory (okx/hyperliquid/fake)"
```

---

## 完成判定（P0–P1）

- `pytest -q` 全绿：core 金标 parity（因子/选币/网格参数零漂移）+ 适配器层（base/fake/ccxt/okx/hyperliquid/registry）+ ccxt 升级冒烟。
- `gridtrade/core/` 不 import 任何交易所库（可用 `grep -rn "ccxt\|okx\|hyperliquid\|requests" gridtrade/core` 确认无匹配）。
- `requirements.txt` 中 ccxt 钉死为实测支持 hyperliquid 且 py3.9 可用的版本。
- 现有 `account_0/`、`backtest/` 未被修改，行为不变。

## 后续（不在本计划内）

P2 状态层（Postgres + Repository）、P3 执行器（挂单网格状态机 + live_equity + reconciler）、P4 运行时（triggers/gates/manager + scheduler/monitor + fly.io）、P5 回测数据层（datasource + 泛化 prewarm + HL 验证）、P6 加固、P7 同币种多网格。每阶段单独出计划。
