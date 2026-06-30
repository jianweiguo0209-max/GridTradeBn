"""服务端内联 SVG 图表：纯函数，确定坐标映射，可单测。空数据返回占位。"""
from typing import List, Tuple


def _placeholder(width: int, height: int) -> str:
    return ('<svg viewBox="0 0 %d %d" class="chart">'
            '<text x="%d" y="%d" text-anchor="middle" fill="#999">暂无数据</text>'
            '</svg>' % (width, height, width // 2, height // 2))


def line_chart(series: List[List[Tuple]], *, width: int = 720, height: int = 200,
               pad: int = 10) -> str:
    pts = [p for s in series for p in s]
    if not pts:
        return _placeholder(width, height)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    dx = (xmax - xmin) or 1.0
    dy = (ymax - ymin) or 1.0
    iw = width - 2 * pad
    ih = height - 2 * pad

    def sx(x): return pad + (x - xmin) / dx * iw
    def sy(y): return pad + (ymax - y) / dy * ih    # 高值在上

    polylines = []
    for s in series:
        if not s:
            continue
        coords = ' '.join('%.1f,%.1f' % (sx(x), sy(y)) for x, y in s)
        polylines.append('<polyline fill="none" stroke="#6cf" stroke-width="1.5" '
                         'points="%s"/>' % coords)
    return ('<svg viewBox="0 0 %d %d" class="chart">%s</svg>'
            % (width, height, ''.join(polylines)))


def bar_chart(bars: List[Tuple], *, width: int = 720, height: int = 200,
              pad: int = 10) -> str:
    if not bars:
        return _placeholder(width, height)
    vmax = max(abs(v) for _, v in bars) or 1.0
    iw = width - 2 * pad
    ih = height - 2 * pad
    n = len(bars)
    bw = iw / n * 0.7
    gap = iw / n
    rects = []
    for i, (_label, v) in enumerate(bars):
        h = abs(v) / vmax * ih
        x = pad + i * gap + (gap - bw) / 2
        y = pad + (ih - h)
        rects.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#4caf50"/>'
                     % (x, y, bw, h))
    return ('<svg viewBox="0 0 %d %d" class="chart">%s</svg>'
            % (width, height, ''.join(rects)))


def stacked_bar(groups: List[Tuple], *, width: int = 720, height: int = 200,
                pad: int = 10) -> str:
    if not groups:
        return _placeholder(width, height)
    totals = [sum(abs(v) for _, v in segs) for _, segs in groups]
    vmax = max(totals) or 1.0
    iw = width - 2 * pad
    ih = height - 2 * pad
    n = len(groups)
    bw = iw / n * 0.7
    gap = iw / n
    colors = ['#4caf50', '#e53935', '#6cf', '#fb0']
    rects = []
    for i, (_label, segs) in enumerate(groups):
        x = pad + i * gap + (gap - bw) / 2
        y_bottom = pad + ih
        for j, (_seg, v) in enumerate(segs):
            h = abs(v) / vmax * ih
            y_bottom -= h
            rects.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                         % (x, y_bottom, bw, h, colors[j % len(colors)]))
    return ('<svg viewBox="0 0 %d %d" class="chart">%s</svg>'
            % (width, height, ''.join(rects)))
