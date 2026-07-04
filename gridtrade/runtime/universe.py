"""币池解析：HL 全部永续中保留 state=='live' 的符号。

黑名单无条件生效（不论是否设置 whitelist）；whitelist 非空时进一步只取其中已上市的
（testnet 聚焦真实币、避开成百上千的垃圾币），否则取全部 live（mainnet 默认）。
可选 min_quote_volume>0 时按 24h 成交额过滤（数据缺失则跳过）。"""
from typing import List


def resolve_live_universe(adapter, blacklist=(), whitelist=(),
                          min_quote_volume=0.0) -> List[str]:
    live = [i.symbol for i in adapter.list_instruments() if i.state == 'live']
    live = [s for s in live if s not in set(blacklist)]        # 档0：无条件硬禁
    if min_quote_volume and min_quote_volume > 0:              # ③ 绝对成交额地板
        vol = adapter.fetch_24h_quote_volumes()
        if vol:                                                # 空(无数据)→fail-open 跳过、不清空票池
            live = [s for s in live if (vol.get(s) or 0.0) >= min_quote_volume]
    if whitelist:
        return [s for s in live if s in set(whitelist)]
    return live
