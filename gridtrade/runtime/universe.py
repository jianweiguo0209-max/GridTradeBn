"""币池解析：HL 全部永续中保留 state=='live' 的符号。

whitelist 非空 -> 只取其中已上市的（testnet 聚焦真实币、避开成百上千的垃圾币）；
否则取全部 live 减黑名单（mainnet 默认）。"""
from typing import List


def resolve_live_universe(adapter, blacklist=(), whitelist=()) -> List[str]:
    live = [i.symbol for i in adapter.list_instruments() if i.state == 'live']
    live = [s for s in live if s not in set(blacklist)]     # 档0：无条件硬禁（含 whitelist 模式）
    if whitelist:
        return [s for s in live if s in set(whitelist)]
    return live
