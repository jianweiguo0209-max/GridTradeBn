"""SVG 图表 chrome 共享纯函数：转义 / 刻度 / 坐标轴 / 图例 / 数值标注。
文本只用数值+时间+固定词；svg_escape 对字符串标签兜底，守 |safe 边界。"""
from datetime import datetime, timezone
from typing import List, Tuple


def svg_escape(s) -> str:
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def nice_ticks(lo: float, hi: float, n: int = 4) -> List[float]:
    if hi == lo:
        return [float(lo)]
    step = (hi - lo) / n
    return [round(lo + i * step, 10) for i in range(n + 1)]


def y_axis(ticks, sy, x_left, x_right, *, digits: int = 2) -> str:
    out = []
    for t in ticks:
        y = sy(t)
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#222" '
                   'stroke-width="0.5"/>' % (x_left, y, x_right, y))
        out.append('<text x="%.1f" y="%.1f" text-anchor="end" font-size="9" '
                   'fill="#999">%s</text>' % (x_left - 2, y + 3, ('%%.%df' % digits) % t))
    return ''.join(out)


def _hhmm(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).strftime('%H:%M')


def x_time_axis(xmin, xmax, sx, y_base) -> str:
    mid = (int(xmin) + int(xmax)) // 2
    out = []
    for t in (xmin, mid, xmax):
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" '
                   'fill="#999">%s</text>' % (sx(t), y_base + 10, _hhmm(t)))
    return ''.join(out)


def x_cat_axis(labels, centers, y_base) -> str:
    out = []
    for lab, cx in zip(labels, centers):
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" '
                   'fill="#999">%s</text>' % (cx, y_base + 10, svg_escape(lab)))
    return ''.join(out)


def legend(items: List[Tuple[str, str]], x, y) -> str:
    out = []
    cx = x
    for color, text in items:
        out.append('<rect x="%.1f" y="%.1f" width="8" height="8" fill="%s"/>'
                   % (cx, y, color))
        out.append('<text x="%.1f" y="%.1f" font-size="9" fill="#ccc">%s</text>'
                   % (cx + 10, y + 8, svg_escape(text)))
        cx += 60
    return ''.join(out)


def value_label(x, y, text) -> str:
    return ('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="9" fill="#ccc">%s</text>'
            % (x, y, svg_escape(text)))
