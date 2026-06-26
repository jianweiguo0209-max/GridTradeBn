import os
import pandas as pd

pd.set_option('display.max_rows', 1000)
pd.set_option('expand_frame_repr', False)  # 当列太多时不换行
pd.set_option('display.unicode.ambiguous_as_wide', True)  # 设置命令行输出时的列对齐功能
pd.set_option('display.unicode.east_asian_width', True)

# 项目根目录
_ = os.path.abspath(os.path.dirname(__file__))  # 返回当前文件路径
root_path = os.path.abspath(os.path.join(_, '..'))  # 返回根目录文件夹

# 从 .env 文件加载敏感配置（API 密钥、webhook 等）。优先读取项目根目录下的 .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(root_path, '.env'))
except ImportError:
    # 未安装 python-dotenv 时，仍可通过系统环境变量读取配置
    pass
# 定义 kline 目录路径，用于存放主动止损的k线数据
kline_path = os.path.join(root_path, 'data', 'kline')
if not os.path.exists(kline_path):
    os.mkdir(kline_path)

# 定义 order 目录路径，用于存放半拉黑名单的网格下单数据
order_path = os.path.join(root_path, 'data', 'order')
if not os.path.exists(order_path):
    os.mkdir(order_path)

# 策略配置
strategy_config = {
    'strategy_name': 'gjw账户0',  # 用于企业微信推送，告知策略名称
    'strategy_tag': 'acc0at',  # 用于ok网格下单时传给ok的标记，用于区分此单属于哪个策略、哪个offset。不能有中文，特殊字符，下划线等
    'period': "12H",  # 网格的换仓周期
    'max_candle_num': 160,  # 参与选币时需要获取的K线数量。可以适量减少一点，足够选币使用即可
    'factors': {  # 网格的选币因子配置（因子已绑定offset，修改startup.py）
        '涨跌幅': True # 这是默认的因子，实际因子已startup.py里根据offset配置，默认因子是兜底配置
    },
    'weight_list' : [1, 1], # 因子权重,注意因子个数
    'offset': [0,1,2,3,4,5,6,7,8,9,10,11],  # 跑哪几个offset
    'leverage': 5,  # ok的杠杆倍数 * 0.65 = 币安的杠杆倍数
    'price_limit': [0.25, 0.25],  # 网格价格区间 (前面一个0.5，表示网格下限-50%，后面一个0.5，表示网格上限 +50%，意思就是当前币种的收盘价的0.5-1.5这个区间)
    'grid_num': 90,  # 网格的格子数量
    'stop_limit': 0.01,  # 超过最高或最低价一定比例，就强制全部平仓
    'choose_symbols': 1,  # 这里设置时，注意一个子账户不能同时开超过20个网格
    # ========== 网格布网版本控制（用于AB测试）==========
    'grid_version': 1,  # 1: 原始布网逻辑(固定1.4%格间距)  2: 优化布网逻辑(动态格间距+动态终止价)
    'grid_v2_config': {  # V2 优化布网参数（仅 grid_version=2 时生效）
        'atr_range_multiplier': 3,     # ATR倍数，用于计算网格区间宽度（与V1一致）
        'range_pct_min': 0.05,         # 网格区间最小百分比（单边），防止极低波动时区间过窄
        'range_pct_max': 0.25,         # 网格区间最大百分比（单边），与price_limit一致
        'grid_spacing_atr_ratio': 0.5, # 格间距 = ATR去量纲值 × 此系数，控制每格占波动的比例
        'grid_spacing_min': 0.003,     # 格间距最小比例(0.3%)，防止格距过小
        'grid_spacing_max': 0.02,      # 格间距最大比例(2%)，防止格距过大
        'grid_count_min': 25,          # 网格数量下限
        'grid_count_max': 149,         # 网格数量上限（OKX平台限制）
        'stop_buffer_ratio': 0.01,     # 终止价在网格上下限基础上额外扩展的比例
    },
    'stop_loss_config': {  # 止盈止损配置
        'stop_loss': 0.034,  # 网格止损比例
        'stop_profit': 0.05,  # 网格止盈比例
        'stop_risk_l1': 0.00618,  # 网格L1级回撤比例
        'stop_risk_l2': 0.01,  # 网格L2级回撤比例
        'fundingRate_stop_loss': 0.0015,  # 资金费率止损
        'active_loss_period': '15m',  # 主动止损需要获取k线的周期，OK接口支持 1m/3m/5m/15m/30m/1H/2H/4H
        'active_loss_candle_num': 600,  # 主动止损最少需要获取k线的数量
        'every_times_candle_num': 10,  # 到达时间点之后，获取的k线数量
    }
}

# 开启多进程获取k线数据（注意请求权重，不要设置太大）
njob = 1  # 1 表示循环获取k线  2：表示使用2个进程同时获取k线。建议使用1。

# 监控频率
stop_loss_period = '5s'  # 可以尝试更低

# 半拉黑名单，不理解的可以观看 花式网格第一期船队 回放：《1期花式网格船队直播2-神秘嘉宾：花式网格代码近期的若干更新》
black_dict = {
    # 0 配置币种直接拉黑，不参与交易
    "0": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "PI-USDT-SWAP", "DEGEN-USDT-SWAP", "VINE-USDT-SWAP", "ALCH-USDT-SWAP", "MAX-USDT-SWAP", "OL-USDT-SWAP", "MASK-USDT-SWAP", "ACT-USDT-SWAP", "NEO-USDT-SWAP", "SONIC-USDT-SWAP", "BR-USDT-SWAP", "PEOPLE-USDT-SWAP", "NEIRO-USDT-SWAP", "RDNT-USDT-SWAP", "MAGIC-USDT-SWAP", "CSPR-USDT-SWAP", "LOOKS-USDT-SWAP", "MEW-USDT-SWAP", "MOODENG-USDT-SWAP",
     "NEIROETH-USDT-SWAP", "FARTCOIN-USDT-SWAP", "IP-USDT-SWAP", "CFX-USDT-SWAP"],
    # 1 配置币种最多只能开1个offset
    "1": [],
    # 2 OTHERS 其他币种最多只能开2个offset
    "2": ["OTHERS"]
}

# 是否需要rebalance：某个offset平仓之后，是否将其利润或亏损，和其他offset共同承担
rebalance = True

# 交易所超时配置，与OK交互时这个时间扩大一点
EXCHANGE_TIMEOUT = 5000
# 代理配置，本地开翻墙工具测试时，打开。在服务器运行时注释掉
PROXIES = {
    # 'http': 'http://127.0.0.1:9090',  # 根据电脑本地信息自行配置
    # 'https': 'http://127.0.0.1:9090',  # 根据电脑本地信息自行配置
}  # 代理配置

# OK交易所配置（密钥从 .env 环境变量读取，不要直接写在代码里）
OK_CONFIG = {
    'apiKey': os.getenv('OK_API_KEY', ''),
    'secret': os.getenv('OK_API_SECRET', ''),
    'password': os.getenv('OK_API_PASSWORD', ''),  # apikey的密码，不是账号密码
    'timeout': EXCHANGE_TIMEOUT,
    # 'rateLimit': 10,
    'enableRateLimit': True,
    'proxies': PROXIES,
}

# 是否模拟盘(demo/simulated-trading)：由 .env 的 OK_SIMULATED 控制，模拟盘密钥必须置 1
OK_SIMULATED = os.getenv('OK_SIMULATED', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def apply_simulated_mode(exchange):
    """模拟盘：给该 exchange 的所有请求加 x-simulated-trading:1 头并返回它。
    注意：ccxt 2.0.58 的 set_sandbox_mode 不会加此头，故手动设置 exchange.headers。
    实盘(OK_SIMULATED=0)时为 no-op。"""
    if OK_SIMULATED:
        exchange.headers = dict(getattr(exchange, 'headers', None) or {}, **{'x-simulated-trading': '1'})
        print('[OK_SIMULATED] 已启用模拟盘模式 (x-simulated-trading:1)')
    return exchange

# 企业微信机器人
wechat_webhook_url = os.getenv('WECHAT_WEBHOOK_URL', '')
