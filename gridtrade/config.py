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


def _offsets(env, key, period_hours):
    """实盘启用 offset 数组（int CSV）：空=停用；去空白→int→去重→排序。
    越界(∉[0,period_hours))/非整数 → 响亮报错（禁静默丢弃，沿退役键惯例）。"""
    v = env.get(key)
    if not v:
        return ()
    out = set()
    for tok in (t.strip() for t in v.split(',')):
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError:
            raise RuntimeError('%s=%r 含非整数项 %r；须为 [0, %d) 的整数 CSV'
                               % (key, v, tok, period_hours))
        if not (0 <= n < period_hours):
            raise RuntimeError('%s 的 offset %d 越界；合法区间 [0, %d)（由 SCHEDULER_PERIOD 定）'
                               % (key, n, period_hours))
        out.add(n)
    return tuple(sorted(out))


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
    account_leverage: float = 5.0   # 与 loader 默认一致（用户定 2026-07-09，实盘 AL=5.0）
    cap_equity_frac: float = 0.2451  # 推导值 = derive_frac(AL5.0, 12, 3.4)；>0 → cap=clamp(equity×frac,min,max)
    cap_min: float = 20.0
    cap_max: float = 100000.0
    min_quote_volume_24h: float = 0.0   # >0 → 24h 成交额绝对地板（0=停用）；与相对口径可叠加（先地板后相对）
    universe_top_volume_pct: float = 0.0  # >0 → 票池按 24h 成交额取前 ceil(pct×N)（相对口径，spec 2026-07-14-universe-top-volume-pct）；生产设 0.55
    fuse_min_coverage: float = 1.0  # 保险丝覆盖率门槛（spec 2026-07-15）：<该值即降 cap 护全额；0=停用（仅审计）。合法区间 (0, 1.0]——>1 无意义（覆盖率>1 只是余量，护栏已 clamp 成只降不升）
    min_order_notional: float = 0.0     # >0 → 开仓预检单笔名义额下限（币安按币 5/20/50，与 Instrument.min_cost 取 max）；0=停用
    scheduler_fetch_pace_ms: float = 250.0    # 选币取数币间间隔（币安权重实测重校，见 scheduler.py）；0=关
    monitor_parallel: int = 4           # monitor per-grid 并行 worker 数；1=退回全串行（保底开关）
    monitor_unit_warn_sec: float = 30.0  # 单网格监控单元耗时告警阈值（病态格日志指名道姓）
    # MarketShockBrake(spec 2026-07-08)：|票池中位数 k 小时收益|≥thr → 暂停开格 pause 小时(只关不开)。
    # thr=0.025:新几何完整口径重跑(2026-07-11,sb2)支配解——Δ≈0/四窗MDD全改善/W1+4.07/捕获37/37;
    # 旧 GO 档 0.04 在 band2 下 Δ−1.95pp(W2 反弹被拦)。thr<=0=停用;约束 pause<=k(重启自愈依赖信号自持)。
    shock_thr: float = 0.025
    shock_k_hours: int = 4
    shock_pause_hours: int = 2
    # 实盘 offset 启用数组(spec 2026-07-17)：空=停用=全 offset 开(默认零行为变更)。非空时
    # 当前 offset ∉ 集 → 本轮只关不开(灰度上量/减仓)；且 cap frac 分母按启用数 N 重算(方案B,
    # 满配仍达目标 AL)。合法值 ∈ [0, period_hours)，越界 boot 报错。
    live_open_offsets: tuple = ()
    # 实际可达并发 N（spec 2026-07-18-margin-gate-exchange-im）：=min(启用offset数×
    # choose_symbols, max_concurrent)，空集=max_concurrent。frac 分母与 MaxConcurrentGate
    # 上限共用此值——frac 按 N 放大 cap 后，并发上限必须同步收紧到 N（12 兜不住 AL）。
    eff_concurrency: int = 12
    # MarginGate IM 口径安全余量 k（≥1）：required=k×(整梯名义/L+worst浮亏+fee)。
    margin_gate_k: float = 1.25
    # 票池杠杆预过滤(2026-07-18)：pick_L<阈值 的币选币前剔除(低杠杆档币 IM 吃满余额、
    # 必被 MarginGate 拒,top-1 选中它=整轮空转,04:00 MYX 实证)。0=停用(默认,零行为变更)。
    universe_min_leverage: float = 0.0
    # 企业微信机器人：URL 是敏感项，本地走 .env，Fly 走 secret。空=完全禁用通知。
    wechat_webhook_url: str = ''
    wechat_timezone: str = 'Asia/Shanghai'
    maker_close_rebalance: bool = True    # B案:周期再平衡平仓 maker-first(2026-07-21 用户定默认开)
    strategy_name: str = 'gridtrade'


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
    # 保险丝覆盖率门槛（spec 2026-07-15）：接受 <=0（停用，仅审计）或 (0, 1.0]。
    # >1 无意义——coverage>1 只是余量（丝本就能全平最大持仓），设 1.2 这类"留余量"的自然
    # 误读只会把已足额的币白缩一个 lot 步。禁静默 clamp：配置错了要响亮（沿退役键守卫惯例）。
    _fmc = _f(env, 'FUSE_MIN_COVERAGE', 1.0)
    if _fmc > 1.0:
        raise RuntimeError('FUSE_MIN_COVERAGE=%s 无效：>1 无意义（覆盖率>1 只是余量，'
                           '丝本就能全平最大持仓）；取 (0, 1.0] 或 <=0（停用，仅审计）' % _fmc)
    cap = _f(env, 'CAP', 100.0)
    # 实盘 offset 启用数组(spec 2026-07-17)：越界校验用 SCHEDULER_PERIOD 定的相位数。
    _period_hours = int(_s(env, 'SCHEDULER_PERIOD', '12H')[:-1])
    _live_offsets = _offsets(env, 'LIVE_OPEN_OFFSETS', _period_hours)
    _max_conc = _i(env, 'MAX_CONCURRENT', 12)
    # cap frac 分母(方案B)：非空启用集时用实际可达并发 N=min(启用数×choose_symbols, max_concurrent)；
    # 空集回落到 max_concurrent(零行为变更)。choose_symbols=1 是"一 offset 一格"不变式的来源。
    _eff_concurrency = (min(len(_live_offsets) * DEFAULT_STRATEGY_CONFIG['choose_symbols'],
                            _max_conc)
                        if _live_offsets else _max_conc)
    # MarginGate 余量系数（spec 2026-07-18-margin-gate-exchange-im）：<1 = 余量为负、
    # 贴边开仓必吃 -2019/逼近强平，配置错了要响亮（沿 FUSE_MIN_COVERAGE 惯例）。
    _mgk = _f(env, 'MARGIN_GATE_K', 1.25)
    if _mgk < 1.0:
        raise RuntimeError('MARGIN_GATE_K=%s 无效：余量系数须 ≥1（=1 即零余量贴边）' % _mgk)
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
                                    _eff_concurrency,
                                    _f(env, 'GRID_GEARING', 3.4)),
        live_open_offsets=_live_offsets,
        eff_concurrency=_eff_concurrency,
        margin_gate_k=_mgk,
        universe_min_leverage=_f(env, 'UNIVERSE_MIN_LEVERAGE', 0.0),
        wechat_webhook_url=_s(env, 'WECHAT_WEBHOOK_URL', ''),
        wechat_timezone=_s(env, 'WECHAT_TIMEZONE', 'Asia/Shanghai'),
        maker_close_rebalance=_b(env, 'MAKER_CLOSE_REBALANCE', True),
        strategy_name=_s(env, 'STRATEGY_NAME', DEFAULT_STRATEGY_CONFIG['strategy_name']),
        cap_min=_f(env, 'CAP_MIN', 20.0),
        cap_max=_f(env, 'CAP_MAX', 100000.0),
        min_quote_volume_24h=_f(env, 'MIN_QUOTE_VOLUME_24H', 0.0),
        universe_top_volume_pct=_f(env, 'UNIVERSE_TOP_VOLUME_PCT', 0.0),
        fuse_min_coverage=_fmc,
        min_order_notional=_f(env, 'MIN_ORDER_NOTIONAL', 0.0),
        scheduler_fetch_pace_ms=_f(env, 'SCHEDULER_FETCH_PACE_MS', 250.0),
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
        # 2→3(2026-07-22 s030 冠军,geo_final 战役):宽带=便宜的尾部保险(消融实证止损链收
        # 71%期望税买尾部,3×ATR 带以库存∝漂移/带宽的几何学买到同款保护)。与 2026-07-09
        # "窄带胜出"不矛盾——当年是单改带宽于旧止损链底座;s030 是联动配置(宽带+密格+
        # pv让位+紧固损),六窗对锚Σ+7.1pp、双留出全过(HOLD-A 三指标胜/HOLD-B Calmar 4.1v3.0)。
        'atr_range_multiplier': 3,
        'range_pct_min': 0.05,
        'range_pct_max': 0.50,
        'grid_spacing_atr_ratio': 0.5,
        'grid_spacing_min': 0.003,
        'grid_spacing_max': 0.04,      # ←0.02:放宽间距上限,修 band2 的 OOS 软肋(3.46→5.05)
        # 回滚 20→10(2026-07-19 重扫v2):候选A 的 cmin=20 系带引擎保真度 bug(首触丢弃/pv前视等,
        # commits 69754cf..bd5f2ac)的回测选出;诚实引擎四窗实测 cmin20 在 IS 崩至 Calmar 2.1(基线
        # 14.4)、仅 W1/W2 占优,regime 依赖不稳。首触丢弃 bug 恰偏袒密网(线密→最近线离 entry 近→
        # 首笔亏损单更常被吞),cmin20 的当年优势主要是该幻觉。注:flex_count=4×band 恒等式(atr 约掉)
        # → cmin 几乎恒绑定,是事实上的格数旋钮。
        # 10→16(2026-07-22 s030):与 2026-07-19 回滚的 cmin20 本质不同——那次是 bug 引擎选出
        # 的单改(诚实引擎 IS 崩至 2.1);此次全程诚实引擎(69754cf 后)且为联动配置的一环
        # (带宽 3×ATR 下 flex≡12,cmin16 兜底≈恒 16 格,步距 0.375×ATR 与现役几乎同,
        # 实质是"同资金摊宽摊薄":单格 −37.5%、同漂移库存 ×0.62)。低波 clamp 区自动更密(至33格)。
        'grid_count_min': 16,
        'grid_count_max': 149,
        'stop_buffer_ratio': 0.01,     # 回测零敏感(破网 0);纯实盘丝距旋钮,留观察
    },
}

DEFAULT_STOP_CFG = {
    # 回滚 0.035→0.045(2026-07-19 重扫v2):候选A 的收紧依据("0.035 压 MDD 而收益不减")出自带
    # 保真度 bug 的引擎;诚实引擎实测 stop:0.035 四调参窗 3/4 胜但留出窗现形(HOLD-A −4.8 垫底
    # vs 基线 −4.5),收益边际、不值得动。0.045 = HL 时代原值(2026-07-10 口径)。
    # 0.045→0.03(2026-07-22 s030):pv 让位(−0.01)后固损从"最后防线"变"亏损主挡板",SWEEP4
    # 阶梯实测 0.03 为内点(0.025 起误杀弹回格,IS 固损 20→126);三窗 ret/Calmar/mdd 全升。
    # 与 2026-07-19 "0.035 留出现形"不矛盾——那是旧底座(pv+0.005 抢跑,固损没活干)的单改。
    'stop_loss': 0.03,
    # 连续回撤止盈恢复开启(2026-07-19 重扫v2,推翻 2026-07-15 候选A 的"关"):当年"关掉四窗更优"
    # 是 pv 前视幻觉下的适应——带前视的 pv 料事如神先砍亏损,trailing 显得多余。诚实引擎(pv 去前视,
    # 9199503)实测 trailing 是真保护:关掉则 W1 7.3→0.8、OOS −3.0→−4.2(少亏 3.5pp 没了)、
    # HOLD-A 崩盘窗它锁利数百笔;仅 IS 顺风窗付代价(14.4→20.5 的机会成本)。四窗 3/4 + 留出窗验证,
    # 0.3/0.00618 = HL 时代原值。收紧档(0.2/0.004)在震荡窗更强但 IS/HOLD-B 转负,不取。
    'trailing_k': 0.3,
    # 0.00618→0.02(2026-07-22 s030):锁盈门槛抬到 2%(峰值>2% 才武装),治"锁小利没收燃料
    # 溢价"(消融实证回撤止盈砍半燃料↔pnl 相关);SWEEP T 阶梯四窗 Σ 单峰于 2%。
    # trailing 本体保留(全关在每窗均非最优)。
    'trailing_floor': 0.02,
    'fundingRate_stop_loss': 0.0015,   # 资金费率止损（交易所真实 fundingRate）
    # pv 主动止损（量能尖峰 + pnl 门槛）；2026-07-07 PV 研究终配置（干净数据+对齐费率四窗全正，
    # spec 2026-07-07-pv-legacy-semantics-live）：尖峰时浮盈不足 +0.5% 即撤（策略换形，~70% 格首尖峰退出）
    # +0.005→−0.01(2026-07-22 s030,本轮最大单点发现):+0.005 在磨涨 regime 系统性自伤——
    # IS 窗 45% 格死于"浮盈<0.5%遇尖峰即砍",全是准赢家;−0.01=亏≥1%才认尖峰。SWEEP P 阶梯
    # 四窗 Σ 单峰于 −0.01(W1 单调向紧/IS 单调向关的 regime 镜像平衡点);pv 全关在每窗均非
    # 最优(崩盘窗保命价值真实,判别器两代参数化均未过线,见 memory grid-fitness-score-research)。
    'pv_pnl_thr': -0.01,               # pv 触发门槛：pv_spike && pnlRatio<thr（evaluate_exit 读此值）
    'pv_mult': 3,                      # 量能尖峰倍数（LiveSignalProvider 算 pv_spike 用）
    'pv_period': '15min',              # 量能重采样周期（'15min' 非 '15m'——后者被 pandas 当月）
    'pv_n': 100,                       # 量能基线滚动窗口（15m×100≈25h 真滚动；signals 取 n+8 根前置历史）
}
