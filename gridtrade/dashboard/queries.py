"""只读查询层：复用现有仓储 + 直读表做 dashboard 聚合，绝不写库/写交易所。"""
from dataclasses import dataclass
from typing import List, Optional

from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.models import now_ms
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository


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
            stop_low_dist_pct=low_dist, stop_high_dist_pct=high_dist))
    return rows
