# 交易所解耦重构 P4i 实现计划（config.py：env 驱动部署配置 + 策略默认常量）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供 `gridtrade/config.py`：从环境变量解析**部署/风控配置**（交易所/凭证/testnet/Postgres URL/cap/杠杆/轮询间隔/准入门参数/utc_offset/scheduler 周期），并暴露**默认策略常量**（`DEFAULT_STRATEGY_CONFIG` / `DEFAULT_STOP_CFG`，镜像已验证的 legacy account_0 参数，可经 env 覆盖关键项）。守护进程据此构造 adapter/manager/trigger。env 可注入，纯函数式、确定性可测。

**Architecture:** `DeployConfig` dataclass 持所有部署字段；`load_deploy_config(env=os.environ)` 解析（bool/int/float 强制转换 + 默认值）。策略参数（period/factors/weight_list/price_limit/grid_v2_config 等）作为默认常量提供（镜像 account_0/config.py 的 strategy_config，OKX→HL 沿用同套已验证参数），关键风控项（cap/leverage/choose_symbols）经 env 覆盖。凭证按交易所：HL=钱包地址+私钥。

**Tech Stack:** Python 3.9、dataclasses、os.environ（注入式）、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（env key 命名、默认值、策略参数口径、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；只新增 `gridtrade/config.py` 及 `tests/test_config.py`；不改其它文件。
- `load_deploy_config(env)` 的 `env` 参数默认 `os.environ`，测试传 dict（不改真实环境）。
- 类型强制：bool 用 `{'1','true','yes','on'}`（小写）判真；int/float 用构造转换；缺失用默认。
- 凭证不写死代码，全从 env；默认空字符串（缺失不报错，由上线前 ops 步骤注入）。
- 策略默认常量镜像 `account_0/config.py` 已验证值：period='12H'、grid_version=2、price_limit=[0.25,0.25]、stop_limit=0.01、grid_v2_config（atr_range_multiplier=3 等）、stop_cfg（stop_loss=0.034/trailing_k=0.3/trailing_floor=0.00618）。**上线 mainnet 前用户须确认 live 策略参数**（factors/weight_list/cap/leverage/choose_symbols 是策略决策，本模块只给默认 + env 覆盖）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 190 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/
  config.py        # DeployConfig + load_deploy_config + DEFAULT_STRATEGY_CONFIG/DEFAULT_STOP_CFG
tests/
  test_config.py
```

公共接口：

```python
@dataclass
class DeployConfig:
    exchange: str
    wallet_address: str
    private_key: str
    testnet: bool
    database_url: str
    cap: float
    leverage: float
    monitor_interval_sec: float
    scheduler_period: str
    max_concurrent: int
    total_budget: float
    default_cap: float
    utc_offset: int

def load_deploy_config(env=None) -> DeployConfig: ...

DEFAULT_STRATEGY_CONFIG: dict   # period/strategy_tag/factors/weight_list/choose_symbols/
                                # price_limit/stop_limit/leverage/grid_version/grid_v2_config/max_candle_num
DEFAULT_STOP_CFG: dict          # stop_loss/trailing_k/trailing_floor
```

---

### Task 1: DeployConfig + load_deploy_config + 策略默认常量

**Files:**
- Create: `gridtrade/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `DeployConfig`、`load_deploy_config(env=None)`、`DEFAULT_STRATEGY_CONFIG`、`DEFAULT_STOP_CFG`。

- [ ] **Step 1: 写失败测试**

Create `tests/test_config.py`:

```python
from gridtrade.config import (load_deploy_config, DeployConfig,
                              DEFAULT_STRATEGY_CONFIG, DEFAULT_STOP_CFG)


def test_defaults_when_env_empty():
    cfg = load_deploy_config(env={})
    assert isinstance(cfg, DeployConfig)
    assert cfg.exchange == 'hyperliquid'
    assert cfg.testnet is False
    assert cfg.cap == 100.0
    assert cfg.leverage == 5.0
    assert cfg.monitor_interval_sec == 5.0
    assert cfg.scheduler_period == '12H'
    assert cfg.max_concurrent == 20
    assert cfg.utc_offset == 8
    assert cfg.wallet_address == '' and cfg.private_key == ''


def test_parses_env_with_type_coercion():
    env = {
        'EXCHANGE': 'okx',
        'HL_WALLET_ADDRESS': '0xabc',
        'HL_PRIVATE_KEY': 'deadbeef',
        'HL_TESTNET': 'true',
        'DATABASE_URL': 'postgresql+psycopg2://u:p@h/db',
        'CAP': '250.5',
        'LEVERAGE': '3',
        'MONITOR_INTERVAL_SEC': '3.5',
        'SCHEDULER_PERIOD': '6H',
        'MAX_CONCURRENT': '10',
        'TOTAL_BUDGET': '5000',
        'DEFAULT_CAP': '200',
        'UTC_OFFSET': '0',
    }
    cfg = load_deploy_config(env=env)
    assert cfg.exchange == 'okx'
    assert cfg.wallet_address == '0xabc' and cfg.private_key == 'deadbeef'
    assert cfg.testnet is True
    assert cfg.database_url == 'postgresql+psycopg2://u:p@h/db'
    assert cfg.cap == 250.5 and cfg.leverage == 3.0
    assert cfg.monitor_interval_sec == 3.5 and cfg.scheduler_period == '6H'
    assert cfg.max_concurrent == 10 and cfg.total_budget == 5000.0
    assert cfg.default_cap == 200.0 and cfg.utc_offset == 0


def test_bool_parsing_variants():
    assert load_deploy_config(env={'HL_TESTNET': 'YES'}).testnet is True
    assert load_deploy_config(env={'HL_TESTNET': '1'}).testnet is True
    assert load_deploy_config(env={'HL_TESTNET': 'off'}).testnet is False
    assert load_deploy_config(env={'HL_TESTNET': 'false'}).testnet is False


def test_default_cap_falls_back_to_cap_when_unset():
    cfg = load_deploy_config(env={'CAP': '300'})
    assert cfg.cap == 300.0 and cfg.default_cap == 300.0   # default_cap 未设 -> 用 cap


def test_strategy_defaults_mirror_legacy():
    assert DEFAULT_STRATEGY_CONFIG['period'] == '12H'
    assert DEFAULT_STRATEGY_CONFIG['grid_version'] == 2
    assert DEFAULT_STRATEGY_CONFIG['price_limit'] == [0.25, 0.25]
    assert DEFAULT_STRATEGY_CONFIG['stop_limit'] == 0.01
    assert DEFAULT_STRATEGY_CONFIG['grid_v2_config']['grid_count_max'] == 149
    assert DEFAULT_STOP_CFG == {'stop_loss': 0.034, 'trailing_k': 0.3,
                                'trailing_floor': 0.00618}
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.config`）。

- [ ] **Step 3: 实现 config.py**

Create `gridtrade/config.py`:

```python
"""部署配置：从环境变量解析 DeployConfig + 暴露默认策略常量（镜像 account_0 已验证参数）。

凭证全从 env、不写死。策略风控项（factors/weight_list/cap/leverage/choose_symbols）是
策略决策：本模块给 legacy 默认 + env 覆盖关键项，上线 mainnet 前由用户确认。
"""
import os
from dataclasses import dataclass

_TRUE = {'1', 'true', 'yes', 'on'}


def _b(env, key, default=False):
    v = env.get(key)
    if v is None:
        return default
    return v.strip().lower() in _TRUE


def _f(env, key, default):
    v = env.get(key)
    return float(v) if v not in (None, '') else float(default)


def _i(env, key, default):
    v = env.get(key)
    return int(v) if v not in (None, '') else int(default)


def _s(env, key, default=''):
    v = env.get(key)
    return v if v not in (None,) else default


@dataclass
class DeployConfig:
    exchange: str
    wallet_address: str
    private_key: str
    testnet: bool
    database_url: str
    cap: float
    leverage: float
    monitor_interval_sec: float
    scheduler_period: str
    max_concurrent: int
    total_budget: float
    default_cap: float
    utc_offset: int


def load_deploy_config(env=None) -> DeployConfig:
    env = os.environ if env is None else env
    cap = _f(env, 'CAP', 100.0)
    return DeployConfig(
        exchange=_s(env, 'EXCHANGE', 'hyperliquid'),
        wallet_address=_s(env, 'HL_WALLET_ADDRESS', ''),
        private_key=_s(env, 'HL_PRIVATE_KEY', ''),
        testnet=_b(env, 'HL_TESTNET', False),
        database_url=_s(env, 'DATABASE_URL', ''),
        cap=cap,
        leverage=_f(env, 'LEVERAGE', 5.0),
        monitor_interval_sec=_f(env, 'MONITOR_INTERVAL_SEC', 5.0),
        scheduler_period=_s(env, 'SCHEDULER_PERIOD', '12H'),
        max_concurrent=_i(env, 'MAX_CONCURRENT', 20),
        total_budget=_f(env, 'TOTAL_BUDGET', 1_000_000.0),
        default_cap=_f(env, 'DEFAULT_CAP', cap),   # 未设 -> 用 cap
        utc_offset=_i(env, 'UTC_OFFSET', 8),
    )


# ---- 默认策略常量（镜像 account_0/config.py 已验证参数；可在构造触发器/执行器时覆盖）----
DEFAULT_STRATEGY_CONFIG = {
    'strategy_name': 'gridtrade',
    'strategy_tag': 'gt0',          # 不含中文/下划线/特殊字符
    'period': '12H',
    'max_candle_num': 160,
    'factors': {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True},
    'weight_list': [1, 1, 1],
    'leverage': 5,
    'price_limit': [0.25, 0.25],
    'stop_limit': 0.01,
    'choose_symbols': 1,
    'grid_version': 2,
    'grid_v2_config': {
        'atr_range_multiplier': 3,
        'range_pct_min': 0.05,
        'range_pct_max': 0.25,
        'grid_spacing_atr_ratio': 0.5,
        'grid_spacing_min': 0.003,
        'grid_spacing_max': 0.02,
        'grid_count_min': 25,
        'grid_count_max': 149,
        'stop_buffer_ratio': 0.01,
    },
}

DEFAULT_STOP_CFG = {
    'stop_loss': 0.034,
    'trailing_k': 0.3,
    'trailing_floor': 0.00618,
}
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/test_config.py -q`
Expected: 全 PASS（5）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 190 + 新增）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/config.py tests/test_config.py
git commit -m "feat(config): env-driven DeployConfig + default strategy constants (P4i)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §2「config.py 统一配置（选交易所/凭证/策略/状态/运行时）」+ §8 部署配置 + P4-deploy 决策（HL 默认、testnet flag、Postgres URL、~5s 间隔、scheduler 周期）。
- **决策对齐**：默认 exchange='hyperliquid'、testnet 默认 False（先 testnet 上线时 env 置真）、monitor_interval=5.0、scheduler_period='12H'、HL 凭证=钱包+私钥。
- **策略参数边界**：策略口径（factors/weight/cap/leverage/choose_symbols）是用户决策；本模块给 legacy 默认 + env 覆盖，已在约束里标注上线前须确认。
- **可测性**：env 注入 dict，零真实环境依赖；bool/int/float 强制转换全覆盖。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`DeployConfig` 字段与 `load_deploy_config` 构造、测试断言三处一致；`default_cap` 未设回退 `cap` 的语义在实现与测试一致。
