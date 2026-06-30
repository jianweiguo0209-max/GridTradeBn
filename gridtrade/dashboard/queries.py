"""只读查询层：复用现有仓储 + 直读表做 dashboard 聚合，绝不写库/写交易所。"""
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select

from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.models import (Accounting, Fill, Grid, GridOrder,
                                    grid_fills, now_ms, order_records)
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.fills import FillRepository


@dataclass
class MachineHealth:
    machine: str
    last_beat_ts: int
    age_sec: float
    stale: bool


@dataclass
class HealthDTO:
    machines: List[MachineHealth]
    endpoint: str
    equity: Optional[float]
    cash: Optional[float]
    balance_error: Optional[str]
    db_ok: bool


def build_health(store, adapter, *, now_ms_fn=now_ms,
                 stale_threshold_sec: float = 30.0) -> HealthDTO:
    db_ok = True
    machines: List[MachineHealth] = []
    try:
        beats = HeartbeatRepository(store).list_all()
        now = now_ms_fn()
        for hb in sorted(beats, key=lambda b: b.machine):
            age = (now - hb.last_beat_ts) / 1000.0
            machines.append(MachineHealth(
                machine=hb.machine, last_beat_ts=hb.last_beat_ts,
                age_sec=age, stale=age > stale_threshold_sec))
    except Exception:
        db_ok = False

    equity = cash = None
    balance_error = None
    try:
        bal = adapter.fetch_balance()
        equity, cash = bal.equity, bal.cash
    except Exception as exc:
        balance_error = repr(exc)

    try:
        endpoint = adapter_endpoint(adapter)
    except Exception:
        endpoint = 'n/a'

    return HealthDTO(machines=machines, endpoint=endpoint, equity=equity,
                     cash=cash, balance_error=balance_error, db_ok=db_ok)


@dataclass
class GridOverviewRow:
    grid_id: str
    symbol: str
    status: str
    direction: str
    low_price: Optional[float]
    high_price: Optional[float]
    open_order_count: int
    net_position: float
    avg_price: float
    realized_pnl: float
    current_price: Optional[float]
    unrealized_pnl: Optional[float]
    price_error: Optional[str]
    stop_low_price: Optional[float]
    stop_high_price: Optional[float]
    stop_low_dist_pct: Optional[float]
    stop_high_dist_pct: Optional[float]
    fee_paid: float = 0.0


def _unrealized(net_position: float, avg_price: float, price: float) -> float:
    return net_position * (price - avg_price)


def build_overview(store, adapter) -> List[GridOverviewRow]:
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    orders = OrderRepository(store)
    rows: List[GridOverviewRow] = []
    for g in sorted(grids.list_active(), key=lambda x: x.symbol):
        acc = accs.get(g.id)
        net = acc.net_position if acc else 0.0
        avg = acc.avg_price if acc else 0.0
        realized = acc.realized_pnl if acc else 0.0
        fee = acc.fee_paid if acc else 0.0
        open_n = len(orders.list_open_by_grid(g.id))

        price = unreal = None
        price_error = None
        low_dist = high_dist = None
        try:
            fetched = adapter.fetch_price(g.symbol)
            if fetched is None or fetched <= 0:
                price_error = 'non-positive price: %r' % (fetched,)
            else:
                price = fetched
                unreal = _unrealized(net, avg, price)
                if g.stop_low_price is not None:
                    low_dist = (price - g.stop_low_price) / price
                if g.stop_high_price is not None:
                    high_dist = (g.stop_high_price - price) / price
        except Exception as exc:
            price_error = repr(exc)

        rows.append(GridOverviewRow(
            grid_id=g.id, symbol=g.symbol, status=g.status, direction=g.direction,
            low_price=g.low_price, high_price=g.high_price, open_order_count=open_n,
            net_position=net, avg_price=avg, realized_pnl=realized,
            current_price=price, unrealized_pnl=unreal, price_error=price_error,
            stop_low_price=g.stop_low_price, stop_high_price=g.stop_high_price,
            stop_low_dist_pct=low_dist, stop_high_dist_pct=high_dist, fee_paid=fee))
    return rows


@dataclass
class GridDetailDTO:
    grid: Grid
    orders: List[GridOrder]
    fills: List[Fill]
    accounting: Optional[Accounting]


def build_grid_detail(store, grid_id: str, *,
                      fills_limit: int = 50) -> Optional[GridDetailDTO]:
    grid = GridRepository(store).get(grid_id)
    if grid is None:
        return None
    orders = sorted(OrderRepository(store).list_by_grid(grid_id),
                    key=lambda o: o.line_index)
    fills = sorted(FillRepository(store).list_by_grid(grid_id),
                   key=lambda f: f.ts, reverse=True)[:fills_limit]
    acc = AccountingRepository(store).get(grid_id)
    return GridDetailDTO(grid=grid, orders=orders, fills=fills, accounting=acc)


@dataclass
class TagSummary:
    tag: str
    count: int
    total_pnl: float
    win_count: int
    win_rate: float


@dataclass
class RecordRow:
    id: str
    symbol: str
    tag: str
    total_pnl: Optional[float]
    pnl_ratio: Optional[float]
    exit_reason: Optional[str]
    closed_at: Optional[int]


@dataclass
class RecentFill:
    grid_id: str
    line_index: int
    side: str
    price: float
    size: float
    ts: int
    fee: float = 0.0


@dataclass
class RecordsDTO:
    records: List[RecordRow]
    tag_summaries: List[TagSummary]
    recent_fills: List[RecentFill]


def build_records(store, *, records_limit: int = 200,
                  fills_limit: int = 50) -> RecordsDTO:
    with store.engine.connect() as c:
        rows = c.execute(
            select(order_records)
            .where(order_records.c.closed_at.isnot(None))
            .order_by(order_records.c.closed_at.desc())
            .limit(records_limit)
        ).all()
        fill_rows = c.execute(
            select(grid_fills).order_by(grid_fills.c.ts.desc()).limit(fills_limit)
        ).all()

    records = [RecordRow(id=r._mapping['id'], symbol=r._mapping['symbol'],
                         tag=r._mapping['tag'], total_pnl=r._mapping['total_pnl'],
                         pnl_ratio=r._mapping['pnl_ratio'],
                         exit_reason=r._mapping['exit_reason'],
                         closed_at=r._mapping['closed_at']) for r in rows]

    agg = {}
    for r in records:
        s = agg.setdefault(r.tag, {'count': 0, 'total': 0.0, 'win': 0})
        pnl = r.total_pnl if r.total_pnl is not None else 0.0
        s['count'] += 1
        s['total'] += pnl
        if pnl > 0:
            s['win'] += 1
    tag_summaries = [
        TagSummary(tag=t, count=v['count'], total_pnl=v['total'],
                   win_count=v['win'],
                   win_rate=(v['win'] / v['count'] if v['count'] else 0.0))
        for t, v in sorted(agg.items())]

    recent_fills = [RecentFill(grid_id=f._mapping['grid_id'],
                               line_index=f._mapping['line_index'],
                               side=f._mapping['side'], price=f._mapping['price'],
                               size=f._mapping['size'], ts=f._mapping['ts'],
                               fee=f._mapping['fee'])
                    for f in fill_rows]
    return RecordsDTO(records=records, tag_summaries=tag_summaries,
                      recent_fills=recent_fills)
