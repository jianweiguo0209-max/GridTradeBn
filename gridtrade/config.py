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


def _csv(env, key):
    v = env.get(key)
    if not v:
        return ()
    return tuple(s.strip() for s in v.split(',') if s.strip())


@dataclass
class DeployConfig:
    exchange: str
    wallet_address: str
    private_key: str
    testnet: bool
    quote_currency: str  # 计价/结算币覆写；'' -> 用适配器类默认（HL=USDC / OKX=USDT）
    database_url: str
    cap: float
    leverage: float
    monitor_interval_sec: float
    scheduler_period: str
    max_concurrent: int
    total_budget: float
    default_cap: float
    utc_offset: int
    blacklist: tuple = ()
    whitelist: tuple = ()
    scheduler_run_on_start: bool = False
    equity_snapshot_interval_sec: float = 300.0
    dashboard_user: str = 'admin'
    dashboard_password_hash: str = ''
    dashboard_session_secret: str = ''
    dashboard_port: int = 8080
    stop_orders_enabled: bool = True
    stop_slippage: float = 0.15


def load_deploy_config(env=None) -> DeployConfig:
    env = os.environ if env is None else env
    cap = _f(env, 'CAP', 100.0)
    return DeployConfig(
        exchange=_s(env, 'EXCHANGE', 'hyperliquid'),
        wallet_address=_s(env, 'HL_WALLET_ADDRESS', ''),
        private_key=_s(env, 'HL_PRIVATE_KEY', ''),
        testnet=_b(env, 'HL_TESTNET', False),
        quote_currency=_s(env, 'QUOTE_CURRENCY', ''),
        database_url=_s(env, 'DATABASE_URL', ''),
        cap=cap,
        leverage=_f(env, 'LEVERAGE', 5.0),
        monitor_interval_sec=_f(env, 'MONITOR_INTERVAL_SEC', 5.0),
        scheduler_period=_s(env, 'SCHEDULER_PERIOD', '12H'),
        max_concurrent=_i(env, 'MAX_CONCURRENT', 20),
        total_budget=_f(env, 'TOTAL_BUDGET', 1_000_000.0),
        default_cap=_f(env, 'DEFAULT_CAP', cap),   # 未设 -> 用 cap
        utc_offset=_i(env, 'UTC_OFFSET', 8),
        blacklist=_csv(env, 'BLACKLIST_SYMBOLS'),
        whitelist=_csv(env, 'UNIVERSE_WHITELIST'),
        scheduler_run_on_start=_b(env, 'SCHEDULER_RUN_ON_START', False),
        dashboard_user=_s(env, 'DASHBOARD_USER', 'admin'),
        dashboard_password_hash=_s(env, 'DASHBOARD_PASSWORD_HASH', ''),
        dashboard_session_secret=_s(env, 'DASHBOARD_SESSION_SECRET', ''),
        dashboard_port=_i(env, 'PORT', 8080),
        equity_snapshot_interval_sec=_f(env, 'EQUITY_SNAPSHOT_INTERVAL_SEC', 300.0),
        stop_orders_enabled=_b(env, 'STOP_ORDERS_ENABLED', True),
        stop_slippage=_f(env, 'STOP_SLIPPAGE', 0.15),
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
    'fundingRate_stop_loss': 0.0015,   # 资金费率止损（HL 真实 fundingRate）
    # pv 主动止损（量能尖峰 + 亏损门槛）；参数由 2026-03~06 回测扫描调优（mult3/thr-0.02 最优）
    'pv_pnl_thr': -0.02,               # pv 止损的亏损门槛（evaluate_exit 读此值）
    'pv_mult': 3,                      # 量能尖峰倍数（LiveSignalProvider 算 pv_spike 用）
    'pv_period': '15min',              # 量能重采样周期（'15min' 非 '15m'——后者被 pandas 当月）
    'pv_n': 233,                       # 量能基线滚动窗口（持仓窗内实为 expanding，对齐回测）
}
