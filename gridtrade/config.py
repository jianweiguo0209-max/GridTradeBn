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
    blacklist: tuple = ()
    whitelist: tuple = ()
    scheduler_run_on_start: bool = False
    equity_snapshot_interval_sec: float = 300.0
    dashboard_user: str = 'admin'
    dashboard_password_hash: str = ''
    dashboard_session_secret: str = ''
    dashboard_port: int = 8080
    display_tz: str = 'UTC'   # IANA 时区名，仅影响面板显示；策略侧永远存/算 UTC
    stop_orders_enabled: bool = True
    stop_slippage: float = 0.15
    cap_equity_frac: float = 0.10   # >0 → 每网格 cap 按当前权益动态定 = clamp(equity×frac, min, max)；0=停用用固定 cap
    cap_min: float = 20.0
    cap_max: float = 100000.0
    min_quote_volume_24h: float = 0.0   # >0 → 24h 成交额绝对地板（0=停用）；生产由 fly.prod.toml 设 $1M
    min_order_notional: float = 0.0     # >0 → 开仓预检单笔名义额下限（HL cost.min=$10）；0=停用
    scheduler_fetch_pace_ms: float = 2000.0   # 选币取数币间间隔（HL 权重制推导，见 scheduler.py）；0=关
    monitor_parallel: int = 4           # monitor per-grid 并行 worker 数；1=退回全串行（保底开关）
    monitor_unit_warn_sec: float = 30.0  # 单网格监控单元耗时告警阈值（病态格日志指名道姓）


def compute_cap(equity, frac, cap_min, cap_max):
    """按权益动态定单网格 cap = clamp(equity×frac, cap_min, cap_max)。
    frac<=0 视为停用，返回 None（调用方回退固定 cap）。"""
    if frac is None or frac <= 0:
        return None
    return max(float(cap_min), min(float(cap_max), float(equity) * float(frac)))


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
        blacklist=_csv(env, 'BLACKLIST_SYMBOLS') or DEFAULT_TIER_POLICY.tier0,
        whitelist=_csv(env, 'UNIVERSE_WHITELIST'),
        scheduler_run_on_start=_b(env, 'SCHEDULER_RUN_ON_START', False),
        dashboard_user=_s(env, 'DASHBOARD_USER', 'admin'),
        dashboard_password_hash=_s(env, 'DASHBOARD_PASSWORD_HASH', ''),
        dashboard_session_secret=_s(env, 'DASHBOARD_SESSION_SECRET', ''),
        dashboard_port=_i(env, 'PORT', 8080),
        display_tz=_s(env, 'DISPLAY_TZ', 'UTC'),
        equity_snapshot_interval_sec=_f(env, 'EQUITY_SNAPSHOT_INTERVAL_SEC', 300.0),
        stop_orders_enabled=_b(env, 'STOP_ORDERS_ENABLED', True),
        stop_slippage=_f(env, 'STOP_SLIPPAGE', 0.15),
        cap_equity_frac=_f(env, 'CAP_EQUITY_FRAC', 0.10),
        cap_min=_f(env, 'CAP_MIN', 20.0),
        cap_max=_f(env, 'CAP_MAX', 100000.0),
        min_quote_volume_24h=_f(env, 'MIN_QUOTE_VOLUME_24H', 0.0),
        min_order_notional=_f(env, 'MIN_ORDER_NOTIONAL', 0.0),
        scheduler_fetch_pace_ms=_f(env, 'SCHEDULER_FETCH_PACE_MS', 2000.0),
        monitor_parallel=_i(env, 'MONITOR_PARALLEL', 4),
        monitor_unit_warn_sec=_f(env, 'MONITOR_UNIT_WARN_SEC', 30.0),
    )


# ---- 默认策略常量（镜像 account_0/config.py 已验证参数；可在构造触发器/执行器时覆盖）----
from gridtrade.core.tier_policy import TierPolicy

# 三档名单唯一事实源（spec 2026-07-06-tiered-*）：实盘默认与回测默认都取此处；
# env（实盘 BLACKLIST_SYMBOLS / 回测 BT_TIER0 等）只作覆盖（运维紧急面/扫参面）。
DEFAULT_TIER_POLICY = TierPolicy(
    tier0=('BTC/USDC:USDC', 'ETH/USDC:USDC', 'VINE/USDC:USDC', 'NEO/USDC:USDC',
           'PEOPLE/USDC:USDC', 'KNEIRO/USDC:USDC', 'MOODENG/USDC:USDC',
           'FARTCOIN/USDC:USDC', 'CFX/USDC:USDC'),
    # legacy black_dict["0"] 25 币中 HL 在市 9 个（NEIRO→k 前缀 KNEIRO）；未上市 16 币
    # 不猜译名（PI/DEGEN/ALCH/MAX/OL/MASK/ACT/SONIC/BR/RDNT/MAGIC/CSPR/LOOKS/MEW/
    # NEIROETH/IP），上市巡检再补。
    tier1=(),
    tier2_cap=1,   # 当前实盘现实（SymbolLockGate 每币≤1）；回测评估后另批准再调
)

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
        # 网格调优（2026-03~06 in-sample + 2025-12 OOS 双验证）：疏格+宽带的方向跨行情稳
        # （gcm 25→10 / mult 3→5：默认亏损 regime 里 −7.2%→+3.5% OOS）。见 docs 回测记录。
        'atr_range_multiplier': 5,     # ←3：带宽乘数，宽带少破网、已实现vs存货浮亏平衡更好
        'range_pct_min': 0.05,
        'range_pct_max': 0.50,         # ←0.25：放宽上限（in-sample +2.2pp；OOS 持平不 hurt）
        'grid_spacing_atr_ratio': 0.5,
        'grid_spacing_min': 0.003,
        'grid_spacing_max': 0.02,
        'grid_count_min': 10,          # ←25：疏格（核心 edge，跨行情稳）；动态格数由此松开接管
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
