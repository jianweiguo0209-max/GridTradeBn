# gridtrade/dashboard/charts.py
"""服务端内联 SVG 图表：纯函数，确定坐标映射，可单测。空数据返回占位。
chrome（刻度/类目/图例/标注）由 svgaxes 提供；文本只用数值+时间+固定词。"""
from typing import List, Tuple

from gridtrade.dashboard import svgaxes as ax

_L, _R, _T, _B = 34, 10, 16, 16     # 绘图区边距：左(y标签)/右/上(图例)/下(x标签)
_SERIES_COLORS = ['#6cf', '#fb0', '#4caf50', '#e53935']


def _placeholder(width: int, height: int) -> str:
    return ('<svg viewBox="0 0 %d %d" class="chart">'
            '<text x="%d" y="%d" text-anchor="middle" fill="#999">暂无数据</text>'
            '</svg>' % (width, height, width // 2, height // 2))


def _frame(width, height):
    return _L, width - _R, _T, height - _B     # plot_left, plot_right, plot_top, plot_bottom


def line_chart(series, *, width: int = 720, height: int = 240,
               x_is_time: bool = False, series_labels=None, value_labels: bool = False) -> str:
    pts = [p for s in series for p in s]
    if not pts:
        return _placeholder(width, height)
    pl, pr, pt, pb = _frame(width, height)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    dx = (xmax - xmin) or 1.0
    dy = (ymax - ymin) or 1.0

    def sx(x): return pl + (x - xmin) / dx * (pr - pl)
    def sy(y): return pt + (ymax - y) / dy * (pb - pt)

    parts = [ax.y_axis(ax.nice_ticks(ymin, ymax), sy, pl, pr)]
    if x_is_time:
        parts.append(ax.x_time_axis(xmin, xmax, sx, pb))
    else:
        cs = [sx(v) for v in (xmin, (xmin + xmax) / 2, xmax)]
        parts.append(ax.x_cat_axis(['%.0f' % v for v in (xmin, (xmin + xmax) / 2, xmax)], cs, pb))
    for i, s in enumerate(series):
        if not s:
            continue
        color = (series_labels[i][0] if series_labels and i < len(series_labels)
                 else _SERIES_COLORS[i % len(_SERIES_COLORS)])
        coords = ' '.join('%.1f,%.1f' % (sx(x), sy(y)) for x, y in s)
        parts.append('<polyline fill="none" stroke="%s" stroke-width="1.5" points="%s"/>'
                     % (color, coords))
        if value_labels and s:
            lx, ly = sx(s[-1][0]), sy(s[-1][1])
            parts.append(ax.value_label(lx, ly - 3, '%.2f' % s[-1][1]))
    if series_labels:
        parts.append(ax.legend(series_labels, pl, 8))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))


def bar_chart(bars, *, width: int = 720, height: int = 240,
              value_labels: bool = False) -> str:
    if not bars:
        return _placeholder(width, height)
    pl, pr, pt, pb = _frame(width, height)
    vmax = max(abs(v) for _, v in bars) or 1.0
    iw, ih = pr - pl, pb - pt
    n = len(bars)
    bw = iw / n * 0.7
    gap = iw / n

    def sy(v): return pt + (1 - v / vmax) * ih

    parts = [ax.y_axis(ax.nice_ticks(0.0, vmax), sy, pl, pr)]
    centers = []
    for i, (label, v) in enumerate(bars):
        h = abs(v) / vmax * ih
        x = pl + i * gap + (gap - bw) / 2
        y = pt + (ih - h)
        centers.append(x + bw / 2)
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#4caf50"/>'
                     % (x, y, bw, h))
        if value_labels:
            parts.append(ax.value_label(x + bw / 2, y - 2, '%g' % v))
    parts.append(ax.x_cat_axis([lab for lab, _ in bars], centers, pb))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))


def stacked_bar(groups, *, width: int = 720, height: int = 240,
                seg_labels=None) -> str:
    if not groups:
        return _placeholder(width, height)
    pl, pr, pt, pb = _frame(width, height)
    totals = [sum(abs(v) for _, v in segs) for _, segs in groups]
    vmax = max(totals) or 1.0
    iw, ih = pr - pl, pb - pt
    n = len(groups)
    bw = iw / n * 0.7
    gap = iw / n
    colors = [c for c, _ in seg_labels] if seg_labels else ['#4caf50', '#e53935', '#6cf', '#fb0']

    def sy(v): return pt + (1 - v / vmax) * ih

    parts = [ax.y_axis(ax.nice_ticks(0.0, vmax), sy, pl, pr)]
    centers = []
    for i, (label, segs) in enumerate(groups):
        x = pl + i * gap + (gap - bw) / 2
        centers.append(x + bw / 2)
        y_bottom = pt + ih
        for j, (_seg, v) in enumerate(segs):
            h = abs(v) / vmax * ih
            y_bottom -= h
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                         % (x, y_bottom, bw, h, colors[j % len(colors)]))
    parts.append(ax.x_cat_axis([lab for lab, _ in groups], centers, pb))
    if seg_labels:
        parts.append(ax.legend(seg_labels, pl, 8))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (width, height, ''.join(parts))
