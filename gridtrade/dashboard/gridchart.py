"""单网格实时价格图：build_grid_chart(采集只读) + render(纯函数 SVG)。web 零写。"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from gridtrade.state.models import now_ms, TERMINAL_STATES

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
