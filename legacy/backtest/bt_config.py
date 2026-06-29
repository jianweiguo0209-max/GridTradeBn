"""
回测预热配置。窗口 / 票池 / 缓存路径等可变项集中在此（对应 支柱三 接缝注入）。

一致性陷阱：后续真正跑回测的窗口、票池、period、factors 必须与预热完全一致，
否则引擎会去读没预热的数据（"预热后还在打 API" 的头号原因）。
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)

# 缓存与产物目录
CACHE_DIR = os.path.join(ROOT, 'data', 'bt_cache')        # per-day parquet 缓存
MANIFEST_DIR = os.path.join(ROOT, 'data', 'bt_manifest')  # S1 候选 + tick 下载清单

# 回测窗口（UTC）。首次建议先用很短的窗口验证流程，再放大。
WINDOW_START = '2024-01-01 00:00:00'
WINDOW_END = '2024-01-08 00:00:00'

# K线周期
BAR = '1H'

# 选币因子：单一来源——直接读 account_0 strategy_config['factors']，自动跟随实盘 config 改动，
# 避免回测与实盘因子配置漂移。
import sys as _sys
_ACC = os.path.join(ROOT, 'account_0')
for _p in (_ACC, os.path.join(_ACC, 'utils'), os.path.join(_ACC, 'api')):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
from config import strategy_config as _SC  # noqa: E402
FACTORS = dict(_SC['factors'])

# 时区：必须与实盘服务器一致。经 orderInfo.pkl 实盘记录验证，本部署服务器跑在 UTC+8（北京时间）。
# account_0 选币函数内部读机器时区，运行本程序时必须用 `TZ=Asia/Shanghai python prewarm.py ...`，
# 且此处 UTC_OFFSET 必须与之一致（=8），否则 offset 与因子时间轴漂移、parity 失效。
UTC_OFFSET = 8

# S0 预热往前多取的暖机天数：选币需 max_candle_num 根 1H bar + 因子回看，留足缓冲。
WARMUP_DAYS = 12

# 并发（OKX 公共端点限频 ~20次/2s，worker 不宜过大；实测 8 偶发 50011，降到 5 更稳）
S0_WORKERS = 5

# 本地代理（服务器上跑置 None）
PROXIES = None
# PROXIES = {'http': 'http://127.0.0.1:9090', 'https': 'http://127.0.0.1:9090'}
