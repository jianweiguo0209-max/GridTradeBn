"""回测入口 .env 便利加载(2026-07-18)。

动机:BT 票池杠杆过滤(BT_MIN_LEVERAGE,默认=实盘)需私有档位端点凭证
BINANCE_API_KEY/SECRET,而代码库其余部分坚持纯 os.environ(12-factor)——本地跑回测
每次手动 export 太苦。边界:**仅回测 CLI 入口(__main__ 守卫)调用**,库导入不自动加载
(防测试环境污染);override=False → 显式 shell env 恒优先;runtime(fly)不受影响
(env 走 secrets/[env],.dockerignore 已排除 .env 不进镜像)。依赖 python-dotenv(已在
requirements)。"""
from pathlib import Path


def load_env_file(path=None, *, override=False) -> bool:
    """加载 .env(默认仓库根)。返回是否实际读到文件;不覆盖已存在的 env。"""
    from dotenv import load_dotenv
    p = Path(path) if path is not None else Path(__file__).resolve().parents[2] / '.env'
    if not p.is_file():
        return False
    return bool(load_dotenv(p, override=override))
