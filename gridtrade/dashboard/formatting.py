"""Jinja2 展示格式化：时间/数字/百分比/盈亏着色。纯函数，无副作用。"""
import math
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def to_display_dt(ts_ms, tz_name: str = 'UTC') -> datetime:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    if not tz_name or tz_name == 'UTC':
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:        # 非法/缺 tzdata → 回退 UTC，绝不崩
        return dt


def ms_to_human(ts: Optional[int], tz_name: str = 'UTC') -> str:
    if ts is None:
        return '-'
    return to_display_dt(ts, tz_name).strftime('%Y-%m-%d %H:%M:%S')


def age_human(sec: Optional[float]) -> str:
    if sec is None:
        return '-'
    sec = int(sec)
    if sec < 0:                 # 负龄（now 早于 last_beat，时钟漂移）当作不可用
        return '-'
    if sec < 60:
        return '%ds' % sec
    if sec < 3600:
        return '%dm' % (sec // 60)
    return '%dh' % (sec // 3600)


def fmt_num(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return '-'
    return f'{x:.{digits}f}'


def fmt_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return '-'
    return f'{x * 100:.{digits}f}%'


def fmt_size(x: Optional[float], digits: int = 8) -> str:
    """持仓/挂单数量：保留较多小数（默认 8 位），并去掉尾部多余的 0 与小数点，
    使 0.001 正常显示、26 仍显示为 26 而非 26.00000000。"""
    if x is None:
        return '-'
    s = f'{x:.{digits}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def fmt_fee(x: Optional[float], digits: int = 8) -> str:
    """手续费：maker 单笔费 ~0.002 USDC，不能用 fmt_num(2 位) 否则截成 0.00。
    保留高精度并去掉尾部多余的 0 与小数点（0.001955→0.001955、0→0）。"""
    if x is None:
        return '-'
    s = f'{x:.{digits}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def fmt_price(x: Optional[float], sig: int = 6) -> str:
    """价格：币价跨度大（ARB 0.077 ~ BTC 60949），固定 2 位会把低价币塌成
    0.08/0.00。按有效数字自适应小数位（默认 6 sig figs）并去尾零：
    0.07768→0.07768、1.78601414→1.78601、561.84→561.84、60949→60949。"""
    if x is None:
        return '-'
    if x == 0:
        return '0'
    digits = max(0, sig - 1 - int(math.floor(math.log10(abs(x)))))
    s = f'{x:.{digits}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def pnl_class(x: Optional[float]) -> str:
    if x is None or x == 0:
        return 'zero'
    return 'pos' if x > 0 else 'neg'


def fill_line(f) -> str:
    """成交行的 line 列:普通网格线成交显示 line_index;line_index=-1 的两类特殊行
    (spec 2026-07-08-position-ledger)按 trade_id 区分——'ledger:' 前缀=内部转仓
    (同币双格净额化的合成行,fee=0/mark 价),其余=保险丝成交(真实成交,不属于网格线)。"""
    li = getattr(f, 'line_index', None)
    if li is None:
        return '-'
    if int(li) >= 0:
        return str(int(li))
    tid = str(getattr(f, 'trade_id', '') or '')
    return '内部转仓' if tid.startswith('ledger:') else '保险丝'
