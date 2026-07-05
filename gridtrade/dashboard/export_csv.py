"""周期导出：闭网网格明细 / 逐笔成交 CSV。纯计算+纯读，不写库、不调行情。

口径：closed_at 落在 [start, end]（UTC 日历天、含两端）的网格各占一行；
成交明细导出同一网格集合的全部 fills（完整生命周期，与网格 CSV 用 grid_id 关联）。
"""
import csv
import io
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from sqlalchemy import select

from gridtrade.state.models import grid_accounting, grid_fills, grids, order_records

_DAY_MS = 86_400_000

GRID_HEADER = [
    'grid_id', 'tag', 'exchange', 'symbol', 'status', 'direction', 'offset',
    'opened_at_utc', 'closed_at_utc', 'duration_hours',
    'entry_price', 'low_price', 'high_price', 'grid_count', 'grid_step',
    'order_num', 'leverage', 'cap', 'stop_low_price', 'stop_high_price',
    'exit_reason', 'total_pnl', 'pnl_ratio', 'realized_pnl', 'fee_paid',
    'funding_paid', 'net_pnl', 'net_position', 'avg_price', 'pnl_ratio_max',
    'fills_count', 'buy_fills', 'sell_fills', 'filled_volume', 'filled_notional',
    'fills_per_hour', 'lines_touched', 'avg_buy_price', 'avg_sell_price',
    'first_fill_utc', 'last_fill_utc',
]

FILL_HEADER = ['grid_id', 'symbol', 'tag', 'trade_id', 'ts_utc',
               'line_index', 'side', 'price', 'size', 'notional', 'fee']


def parse_day_ms(s: str, *, end: bool = False) -> int:
    """YYYY-MM-DD（UTC）→ 毫秒；end=True 取当天最后一毫秒。非法输入抛 ValueError。"""
    dt = datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return ms + _DAY_MS - 1 if end else ms


def _iso(ms) -> str:
    if ms is None:
        return ''
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _num(v):
    return '' if v is None else round(v, 8)


def _closed_records(store, start_ms: int, end_ms: int) -> List:
    with store.engine.connect() as c:
        return c.execute(
            select(order_records)
            .where(order_records.c.closed_at.isnot(None),
                   order_records.c.closed_at >= start_ms,
                   order_records.c.closed_at <= end_ms,
                   order_records.c.grid_id.isnot(None))
            .order_by(order_records.c.closed_at)
        ).mappings().all()


def _fills_by_grid(store, grid_ids) -> Dict[str, List]:
    if not grid_ids:
        return {}
    with store.engine.connect() as c:
        rows = c.execute(
            select(grid_fills)
            .where(grid_fills.c.grid_id.in_(list(grid_ids)))
            .order_by(grid_fills.c.ts)
        ).mappings().all()
    out: Dict[str, List] = {}
    for f in rows:
        out.setdefault(f['grid_id'], []).append(f)
    return out


def _fill_stats(fills: List, duration_ms) -> Dict:
    buys = [f for f in fills if f['side'] == 'buy']
    sells = [f for f in fills if f['side'] == 'sell']
    buy_sz = sum(f['size'] for f in buys)
    sell_sz = sum(f['size'] for f in sells)
    hours = (duration_ms / 3_600_000) if duration_ms else None
    return {
        'fills_count': len(fills),
        'buy_fills': len(buys),
        'sell_fills': len(sells),
        'filled_volume': _num(sum(f['size'] for f in fills)),
        'filled_notional': _num(sum(f['price'] * f['size'] for f in fills)),
        'fills_per_hour': _num(len(fills) / hours) if hours else '',
        'lines_touched': len({f['line_index'] for f in fills}),
        'avg_buy_price': _num(sum(f['price'] * f['size'] for f in buys) / buy_sz) if buy_sz else '',
        'avg_sell_price': _num(sum(f['price'] * f['size'] for f in sells) / sell_sz) if sell_sz else '',
        'first_fill_utc': _iso(fills[0]['ts']) if fills else '',
        'last_fill_utc': _iso(fills[-1]['ts']) if fills else '',
    }


def grids_csv(store, start_ms: int, end_ms: int) -> str:
    recs = _closed_records(store, start_ms, end_ms)
    gids = [r['grid_id'] for r in recs]
    with store.engine.connect() as c:
        grows = c.execute(select(grids).where(grids.c.id.in_(gids))).mappings().all() if gids else []
        arows = c.execute(select(grid_accounting)
                          .where(grid_accounting.c.grid_id.in_(gids))).mappings().all() if gids else []
    gmap = {g['id']: g for g in grows}
    amap = {a['grid_id']: a for a in arows}
    fmap = _fills_by_grid(store, gids)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=GRID_HEADER, lineterminator='\n')
    w.writeheader()
    for r in recs:
        gid = r['grid_id']
        g = gmap.get(gid, {})
        a = amap.get(gid, {})
        duration_ms = (r['closed_at'] - r['opened_at']) \
            if (r['opened_at'] is not None and r['closed_at'] is not None) else None
        low, high, cnt = g.get('low_price'), g.get('high_price'), g.get('grid_count')
        net_pnl = ''
        if a:
            net_pnl = _num(a['realized_pnl'] - a['fee_paid'] - a['funding_paid'])
        row = {
            'grid_id': gid, 'tag': r['tag'], 'exchange': r['exchange'],
            'symbol': r['symbol'], 'status': g.get('status', ''),
            'direction': g.get('direction', ''), 'offset': r['offset'],
            'opened_at_utc': _iso(r['opened_at']), 'closed_at_utc': _iso(r['closed_at']),
            'duration_hours': _num(duration_ms / 3_600_000) if duration_ms is not None else '',
            'entry_price': _num(g.get('entry_price')), 'low_price': _num(low),
            'high_price': _num(high), 'grid_count': cnt if cnt is not None else '',
            'grid_step': _num((high - low) / cnt) if (low is not None and high is not None and cnt) else '',
            'order_num': _num(g.get('order_num')), 'leverage': _num(g.get('leverage')),
            'cap': _num(g.get('cap')), 'stop_low_price': _num(g.get('stop_low_price')),
            'stop_high_price': _num(g.get('stop_high_price')),
            'exit_reason': r['exit_reason'] or '', 'total_pnl': _num(r['total_pnl']),
            'pnl_ratio': _num(r['pnl_ratio']),
            'realized_pnl': _num(a['realized_pnl']) if a else '',
            'fee_paid': _num(a['fee_paid']) if a else '',
            'funding_paid': _num(a['funding_paid']) if a else '',
            'net_pnl': net_pnl,
            'net_position': _num(a['net_position']) if a else '',
            'avg_price': _num(a['avg_price']) if a else '',
            'pnl_ratio_max': _num(a['pnl_ratio_max']) if a else '',
        }
        row.update(_fill_stats(fmap.get(gid, []), duration_ms))
        w.writerow(row)
    return buf.getvalue()


def fills_csv(store, start_ms: int, end_ms: int) -> str:
    recs = _closed_records(store, start_ms, end_ms)
    meta = {r['grid_id']: (r['symbol'], r['tag']) for r in recs}
    fmap = _fills_by_grid(store, list(meta))

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FILL_HEADER, lineterminator='\n')
    w.writeheader()
    for gid in meta:
        symbol, tag = meta[gid]
        for f in fmap.get(gid, []):
            w.writerow({
                'grid_id': gid, 'symbol': symbol, 'tag': tag,
                'trade_id': f['trade_id'], 'ts_utc': _iso(f['ts']),
                'line_index': f['line_index'], 'side': f['side'],
                'price': _num(f['price']), 'size': _num(f['size']),
                'notional': _num(f['price'] * f['size']), 'fee': _num(f['fee']),
            })
    return buf.getvalue()
