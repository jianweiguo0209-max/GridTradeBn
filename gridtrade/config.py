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
    api_key: str
    api_secret: str
    testnet: bool
    quote_currency: str  # 计价/结算币覆写；'' -> 用适配器类默认（Binance=USDT）
    database_url: str
    cap: float
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
    # 仓位参数体系(spec 2026-07-07-account-leverage-gearing)：
    # gearing = 单格名义部署倍数(挂单总名义额 = gearing×cap)，吸收旧 leverage(5)×max_rate(0.68) 冗余对；
    # account_leverage = 账户最坏净敞口倍数(N 格同侧扫穿上限)；cap_equity_frac 为推导值(勿从 env 读)。
    grid_gearing: float = 3.4
    account_leverage: float = 3.5
    cap_equity_frac: float = 0.10   # 推导值 = derive_frac(account_leverage, max_concurrent, gearing)；>0 → cap=clamp(equity×frac,min,max)
    cap_min: float = 20.0
    cap_max: float = 100000.0
    min_quote_volume_24h: float = 0.0   # >0 → 24h 成交额绝对地板（0=停用）；与相对口径可叠加（先地板后相对）
    universe_top_volume_pct: float = 0.0  # >0 → 票池按 24h 成交额取前 ceil(pct×N)（相对口径，spec 2026-07-14-universe-top-volume-pct）；生产设 0.55
    min_order_notional: float = 0.0     # >0 → 开仓预检单笔名义额下限（币安按币 5/20/50，与 Instrument.min_cost 取 max）；0=停用
    scheduler_fetch_pace_ms: float = 2000.0   # 选币取数币间间隔（HL 权重制推导，见 scheduler.py）；0=关
    monitor_parallel: int = 4           # monitor per-grid 并行 worker 数；1=退回全串行（保底开关）
    monitor_unit_warn_sec: float = 30.0  # 单网格监控单元耗时告警阈值（病态格日志指名道姓）
    # MarketShockBrake(spec 2026-07-08)：|票池中位数 k 小时收益|≥thr → 暂停开格 pause 小时(只关不开)。
    # thr=0.025:新几何完整口径重跑(2026-07-11,sb2)支配解——Δ≈0/四窗MDD全改善/W1+4.07/捕获37/37;
    # 旧 GO 档 0.04 在 band2 下 Δ−1.95pp(W2 反弹被拦)。thr<=0=停用;约束 pause<=k(重启自愈依赖信号自持)。
    shock_thr: float = 0.025
    shock_k_hours: int = 4
    shock_pause_hours: int = 2


def compute_cap(equity, frac, cap_min, cap_max):
    """按权益动态定单网格 cap = clamp(equity×frac, cap_min, cap_max)。
    frac<=0 视为停用，返回 None（调用方回退固定 cap）。"""
    if frac is None or frac <= 0:
        return None
    return max(float(cap_min), min(float(cap_max), float(equity) * float(frac)))


def derive_frac(account_leverage, max_concurrent, gearing):
    """cap 占权益比例 = 账户杠杆 / (最大仓数 × 单格最坏净敞口倍数 gearing/2)。
    中性网格双侧梯子最坏只吃单侧,故 /2(spec 2026-07-07-account-leverage-gearing)。"""
    return float(account_leverage) / (int(max_concurrent) * float(gearing) / 2.0)


def load_deploy_config(env=None) -> DeployConfig:
    env = os.environ if env is None else env
    # 退役键守卫(spec 2026-07-07-account-leverage-gearing)：语义变更,禁止静默映射。
    for legacy, repl in (('LEVERAGE', 'GRID_GEARING(=旧LEVERAGE×0.68,默认3.4)'),
                         ('CAP_EQUITY_FRAC', 'ACCOUNT_LEVERAGE(frac=AL/(N×gearing/2))'),
                         # 币安迁移(spec 2026-07-14)：HL 键退役,语义变更禁静默映射
                         ('HL_WALLET_ADDRESS', 'BINANCE_API_KEY'),
                         ('HL_PRIVATE_KEY', 'BINANCE_API_SECRET'),
                         ('HL_TESTNET', 'BINANCE_TESTNET')):
        if legacy in env:
            raise RuntimeError('env %s 已退役,请改用 %s' % (legacy, repl))
    cap = _f(env, 'CAP', 100.0)
    return DeployConfig(
        exchange=_s(env, 'EXCHANGE', 'binance'),
        api_key=_s(env, 'BINANCE_API_KEY', ''),
        api_secret=_s(env, 'BINANCE_API_SECRET', ''),
        testnet=_b(env, 'BINANCE_TESTNET', False),
        quote_currency=_s(env, 'QUOTE_CURRENCY', ''),
        database_url=_s(env, 'DATABASE_URL', ''),
        cap=cap,
        monitor_interval_sec=_f(env, 'MONITOR_INTERVAL_SEC', 5.0),
        scheduler_period=_s(env, 'SCHEDULER_PERIOD', '12H'),
        max_concurrent=_i(env, 'MAX_CONCURRENT', 12),
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
        grid_gearing=_f(env, 'GRID_GEARING', 3.4),
        account_leverage=_f(env, 'ACCOUNT_LEVERAGE', 5.0),
        cap_equity_frac=derive_frac(_f(env, 'ACCOUNT_LEVERAGE', 5.0),
                                    _i(env, 'MAX_CONCURRENT', 12),
                                    _f(env, 'GRID_GEARING', 3.4)),
        cap_min=_f(env, 'CAP_MIN', 20.0),
        cap_max=_f(env, 'CAP_MAX', 100000.0),
        min_quote_volume_24h=_f(env, 'MIN_QUOTE_VOLUME_24H', 0.0),
        universe_top_volume_pct=_f(env, 'UNIVERSE_TOP_VOLUME_PCT', 0.0),
        min_order_notional=_f(env, 'MIN_ORDER_NOTIONAL', 0.0),
        scheduler_fetch_pace_ms=_f(env, 'SCHEDULER_FETCH_PACE_MS', 2000.0),
        monitor_parallel=_i(env, 'MONITOR_PARALLEL', 4),
        monitor_unit_warn_sec=_f(env, 'MONITOR_UNIT_WARN_SEC', 30.0),
        shock_thr=_f(env, 'SHOCK_THR', 0.025),
        shock_k_hours=_i(env, 'SHOCK_K_HOURS', 4),
        shock_pause_hours=_i(env, 'SHOCK_PAUSE_HOURS', 2),
    )


# ---- 默认策略常量（镜像 account_0/config.py 已验证参数；可在构造触发器/执行器时覆盖）----
from gridtrade.core.tier_policy import TierPolicy

# 三档名单唯一事实源（spec 2026-07-06-tiered-*）：实盘默认与回测默认都取此处；
# env（实盘 BLACKLIST_SYMBOLS / 回测 BT_TIER0 等）只作覆盖（运维紧急面/扫参面）。
DEFAULT_TIER_POLICY = TierPolicy(
    # 币安迁移映射(2026-07-14 fapi 实查,spec §5.5)：HL 9 币直译,KNEIRO(k 前缀千倍币)→
    # 币安 NEIRO(NEIROUSDT TRADING)；VINE 为 SETTLING(退市中)留名单无害(黑名单 fail-safe)。
    tier0=('BTC/USDT:USDT', 'ETH/USDT:USDT', 'VINE/USDT:USDT', 'NEO/USDT:USDT',
           'PEOPLE/USDT:USDT', 'NEIRO/USDT:USDT', 'MOODENG/USDT:USDT',
           'FARTCOIN/USDT:USDT', 'CFX/USDT:USDT'),
    # legacy black_dict["0"] 25 币中币安在市 9 个；其余 16 币未上币安永续，不猜译名
    # （PI/DEGEN/ALCH/MAX/OL/MASK/ACT/SONIC/BR/RDNT/MAGIC/CSPR/LOOKS/MEW/NEIROETH/IP），
    # 上市巡检再补。
    tier1=(),
    tier2_cap=2,   # 同币开仓上限(2026-07-12 用户定)
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
        # 建网参数四窗扫描(2026-07-09,实盘口径:legacy 满窗 PV×mr0.68×新费率,memory
        # grid-params-sweep-verdict):PV 换形(+0.005)接管防爆后(四窗 7180 格破网 0),
        # 带宽从"防破网保险"变"收益杠杆"——窄带高换手胜出,与旧调优(宽带)结论翻转。
        'atr_range_multiplier': 2,     # ←5:窄带;四窗均值 4.00→7.60%,唯一 W1 正收益组
        'range_pct_min': 0.05,
        'range_pct_max': 0.50,
        'grid_spacing_atr_ratio': 0.5,
        'grid_spacing_min': 0.003,
        'grid_spacing_max': 0.04,      # ←0.02:放宽间距上限,修 band2 的 OOS 软肋(3.46→5.05)
        'grid_count_min': 10,
        'grid_count_max': 149,
        'stop_buffer_ratio': 0.01,     # 回测零敏感(破网 0);纯实盘丝距旋钮,留观察
    },
}

DEFAULT_STOP_CFG = {
    # 固定止损放宽(2026-07-10 四窗实盘口径扫描 data/tiercmp/stop_results.csv,用户拍板取保守候选):
    # 收紧全线更差;放宽单调更好——0.045 均值 +0.91pp(10.02→10.93%/2mo)、最差窗 MDD 反而
    # −4.17→−3.43、触发 30→14 格(被砍格多为 V 形底部,止损=在底部实现亏损)。OFF 臂均值最高(11.31)
    # 但最差单格 −5.3%→−11.2%、黑天鹅日只剩保险丝,不采纳。丝(破网价挂单)与本参数无关、不动。
    'stop_loss': 0.045,
    # 连续回撤止盈换挡(2026-07-10 四窗实盘口径扫描 data/tiercmp/trail_results.csv,用户拍板):
    # floor 是唯一敏感自由度(单调,0.015 附近饱和),k 几乎不敏感取最优 0.15;现值 k0.3/fl0.00618
    # 被四窗全面支配(均值 7.88→10.02%/2mo)。floor 抬高后 trailing 触发占比 6-12%→~1%,
    # 只砍大回撤不再过早锁小利;代价=最差窗 MDD −3.44→−4.17%。OFF 臂 W1 垫底(4.18/MDD−4.42),
    # 保险价值在趋势崩盘窗兑现,故松而不删。
    'trailing_k': 0.15,
    'trailing_floor': 0.015,
    'fundingRate_stop_loss': 0.0015,   # 资金费率止损（交易所真实 fundingRate）
    # pv 主动止损（量能尖峰 + pnl 门槛）；2026-07-07 PV 研究终配置（干净数据+对齐费率四窗全正，
    # spec 2026-07-07-pv-legacy-semantics-live）：尖峰时浮盈不足 +0.5% 即撤（策略换形，~70% 格首尖峰退出）
    'pv_pnl_thr': 0.005,               # pv 触发门槛：pv_spike && pnlRatio<+0.005（evaluate_exit 读此值）
    'pv_mult': 3,                      # 量能尖峰倍数（LiveSignalProvider 算 pv_spike 用）
    'pv_period': '15min',              # 量能重采样周期（'15min' 非 '15m'——后者被 pandas 当月）
    'pv_n': 100,                       # 量能基线滚动窗口（15m×100≈25h 真滚动；signals 取 n+8 根前置历史）
}
