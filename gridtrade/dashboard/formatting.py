"""Jinja2 展示格式化：时间/数字/百分比/盈亏着色。纯函数，无副作用。"""
from datetime import datetime, timezone
from typing import Optional


def ms_to_human(ts: Optional[int]) -> str:
    if ts is None:
        return '-'
    return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S')


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


def pnl_class(x: Optional[float]) -> str:
    if x is None or x == 0:
        return 'zero'
    return 'pos' if x > 0 else 'neg'
