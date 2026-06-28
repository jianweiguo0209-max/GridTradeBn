"""币池解析：HL 全部永续中保留 state=='live' 且不在黑名单的符号（用户决策）。"""
from typing import List


def resolve_live_universe(adapter, blacklist=()) -> List[str]:
    bl = set(blacklist)
    return [i.symbol for i in adapter.list_instruments()
            if i.state == 'live' and i.symbol not in bl]
