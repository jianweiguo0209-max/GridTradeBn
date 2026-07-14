"""币池解析：全部 live 永续符号 − 黑名单 −（可选）成交额过滤。

黑名单无条件生效（不论是否设置 whitelist）；whitelist 非空时进一步只取其中已上市的
（testnet 聚焦真实币、避开成百上千的垃圾币），否则取全部 live（mainnet 默认）。
成交额过滤两种口径可叠加（先地板后相对，spec 2026-07-14-universe-top-volume-pct）：
- min_quote_volume>0：24h 成交额绝对地板；
- top_volume_pct>0：按 24h 成交额降序保留前 ceil(pct×N)（相对口径，自适应市场总量）；
  量缺失的币按 0 垫底，量并列按 symbol 字典序（确定性，与回测复现同口径）。
两者在 24h ticker 数据缺失时一律 fail-open 跳过（不清空票池）。"""
import math
from typing import List


def resolve_live_universe(adapter, blacklist=(), whitelist=(),
                          min_quote_volume=0.0, top_volume_pct=0.0) -> List[str]:
    live = [i.symbol for i in adapter.list_instruments() if i.state == 'live']
    live = [s for s in live if s not in set(blacklist)]        # 档0：无条件硬禁
    need_vol = ((min_quote_volume and min_quote_volume > 0)
                or (top_volume_pct and top_volume_pct > 0))
    if need_vol:
        vol = adapter.fetch_24h_quote_volumes()
        if vol:                                                # 空(无数据)→fail-open 跳过、不清空票池
            if min_quote_volume and min_quote_volume > 0:      # ③a 绝对成交额地板
                live = [s for s in live if (vol.get(s) or 0.0) >= min_quote_volume]
            if top_volume_pct and top_volume_pct > 0 and live:  # ③b 相对口径：前 ceil(pct×N)
                keep_n = max(1, math.ceil(float(top_volume_pct) * len(live)))
                ranked = sorted(live, key=lambda s: (-float(vol.get(s) or 0.0), s))
                live = ranked[:keep_n]
    if whitelist:
        return [s for s in live if s in set(whitelist)]
    return live
