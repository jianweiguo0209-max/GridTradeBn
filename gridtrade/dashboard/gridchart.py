"""单网格实时价格图：build_grid_chart(采集只读) + render(纯函数 SVG)。web 零写。"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import now_ms, TERMINAL_STATES
from gridtrade.state.orders import OrderRepository

_HOUR = 3600_000


@dataclass
class ChartDTO:
    symbol: str
    window: str
    timeframe: str
    start_ms: int
    end_ms: int
    price_series: List[Tuple[int, float]]
    ohlcv_ok: bool
    grid_lines: List[float]
    open_orders: List[Tuple[float, str]]
    fills: List[Tuple[int, float, str]]
    entry_price: Optional[float]
    stop_low: Optional[float]
    stop_high: Optional[float]
    current_price: Optional[float]


def _timeframe_for(span_ms: int) -> str:
    if span_ms <= 2 * _HOUR:
        return '1m'
    if span_ms <= 12 * _HOUR:
        return '5m'
    if span_ms <= 2 * 24 * _HOUR:
        return '15m'
    return '1h'


def window_bounds(grid, window: str, *, now_ms_fn=now_ms) -> Tuple[int, int, str]:
    now = int(now_ms_fn())
    if window in ('1h', '6h', '24h'):
        hours = {'1h': 1, '6h': 6, '24h': 24}[window]
        start, end = now - hours * _HOUR, now
    else:                                    # life（含非法回退）
        start = int(grid.created_at or now)
        end = int(grid.updated_at) if grid.status in TERMINAL_STATES else now
    return start, end, _timeframe_for(end - start)


def _grid_lines(grid) -> List[float]:
    try:
        gi = grid_order_info(grid.cap, grid.leverage, grid.low_price, grid.high_price,
                             int(grid.grid_count), grid.stop_low_price, grid.stop_high_price)
    except Exception:
        return []
    if gi is None:
        return []
    seq = gi.get('价格序列')
    if seq is None:
        return []
    return [float(p) for p in seq]


def build_grid_chart(store, adapter, grid_id, window, *, now_ms_fn=now_ms) -> Optional[ChartDTO]:
    grid = GridRepository(store).get(grid_id)
    if grid is None:
        return None
    start_ms, end_ms, timeframe = window_bounds(grid, window, now_ms_fn=now_ms_fn)

    price_series: List[Tuple[int, float]] = []
    ohlcv_ok = True
    try:
        df = adapter.fetch_ohlcv(grid.symbol, timeframe, start_ms, end_ms)
        if df is not None and not df.empty:
            ts_ms = (pd.to_datetime(df['candle_begin_time']).view('int64') // 1_000_000)
            price_series = [(int(t), float(c)) for t, c in zip(ts_ms, df['close'])]
    except Exception:
        ohlcv_ok = False

    grid_lines = _grid_lines(grid)
    open_orders = [(float(o.price), o.side)
                   for o in OrderRepository(store).list_open_by_grid(grid_id)]
    fills = [(int(f.ts), float(f.price), f.side)
             for f in FillRepository(store).list_by_grid(grid_id)
             if start_ms <= f.ts <= end_ms]

    current_price = None
    try:
        current_price = float(adapter.fetch_price(grid.symbol))
    except Exception:
        current_price = None

    return ChartDTO(
        symbol=grid.symbol, window=window, timeframe=timeframe,
        start_ms=start_ms, end_ms=end_ms, price_series=price_series, ohlcv_ok=ohlcv_ok,
        grid_lines=grid_lines, open_orders=open_orders, fills=fills,
        entry_price=grid.entry_price, stop_low=grid.stop_low_price,
        stop_high=grid.stop_high_price, current_price=current_price)
