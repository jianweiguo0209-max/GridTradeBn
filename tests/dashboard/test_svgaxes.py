from gridtrade.dashboard.svgaxes import (svg_escape, nice_ticks, y_axis, axis_digits,
                                         x_time_axis, x_cat_axis, legend, value_label)


def test_axis_digits_adapts_to_magnitude():
    # 高价币轴：2 位够（下限）
    assert axis_digits([500.0, 561.84, 600.0]) == 2
    assert axis_digits([50000.0, 60949.0]) == 2
    # 低价币轴：加位数使刻度可分辨（fmt_num 2 位会塌成同值）
    assert axis_digits([1.48, 1.78, 2.11]) >= 4
    assert axis_digits([0.0625, 0.077, 0.106]) >= 5
    assert axis_digits([0.001, 0.0012, 0.0015]) >= 7
    # 空/全零回退到下限 2
    assert axis_digits([0.0, 0.0]) == 2


def test_y_axis_adaptive_digits_for_low_priced():
    # 未显式给 digits 时按量级自适应：低价币刻度带足够小数、不塌成 2 位
    svg = y_axis(nice_ticks(0.0625, 0.106, 4), sy=lambda p: 0.0, x_left=0, x_right=100)
    assert '0.0625' in svg or '0.06251' in svg


def test_svg_escape():
    assert svg_escape('<script>&"\'') == '&lt;script&gt;&amp;&quot;&#39;'
    assert svg_escape(5) == '5'


def test_nice_ticks():
    assert nice_ticks(0.0, 100.0, 4) == [0.0, 25.0, 50.0, 75.0, 100.0]
    assert nice_ticks(5.0, 5.0) == [5.0]              # lo==hi 退化


def test_y_axis_has_lines_and_number_labels():
    svg = y_axis([0.0, 50.0, 100.0], sy=lambda v: 100 - v, x_left=20, x_right=200, digits=1)
    assert svg.count('<line') == 3
    assert '0.0' in svg and '50.0' in svg and '100.0' in svg


def test_x_time_axis_hhmm():
    # 0 ms = 1970-01-01 00:00 UTC；≤24h 跨度维持纯 HH:MM（实时图现状）
    svg = x_time_axis(0, 3600_000, sx=lambda t: t / 3600_000 * 100, y_base=120)
    assert '00:00' in svg and '01:00' in svg          # 起/现
    assert '00:30' in svg                             # 5 刻度加密后含中点
    assert '<text' in svg
    assert '<line' not in svg                         # 不传 y_top 无纵向网格线（gridchart 口径）


def test_x_time_axis_multiday_has_dates():
    day = 86400_000
    sx = lambda t: t / (30 * day) * 700
    # >7d 跨度 → 只留日期 MM-DD（修跨天曲线 HH:MM 不可读）
    svg = x_time_axis(0, 30 * day, sx=sx, y_base=120)
    assert '01-01' in svg and '01-31' in svg          # 起/止日期
    import re
    labels = re.findall(r'>([^<]+)</text>', svg)
    assert len(labels) == 5 and all(':' not in lab for lab in labels)
    # 1d<跨度≤7d → 日期+时刻
    svg3 = x_time_axis(0, 3 * day, sx=lambda t: t / (3 * day) * 700, y_base=120)
    labels3 = re.findall(r'>([^<]+)</text>', svg3)
    assert all('-' in lab and ':' in lab for lab in labels3)


def test_x_time_axis_vertical_gridlines_with_y_top():
    svg = x_time_axis(0, 3600_000, sx=lambda t: t / 3600_000 * 100, y_base=120, y_top=18)
    assert svg.count('<line') == 5                    # 每刻度一条纵向网格线
    assert 'y1="18.0"' in svg and 'y2="120.0"' in svg


def test_x_time_axis_zero_span_single_tick():
    svg = x_time_axis(1000, 1000, sx=lambda t: 50.0, y_base=120)
    import re
    assert len(re.findall(r'<text', svg)) == 1


def test_x_cat_axis_escapes():
    svg = x_cat_axis(['<b>', 'sell'], [10.0, 50.0], y_base=120)
    assert '&lt;b&gt;' in svg and 'sell' in svg and '<b>' not in svg


def test_legend_swatches_and_text():
    svg = legend([('#4caf50', '买'), ('#e53935', '卖')], x=10, y=8)
    assert svg.count('<rect') == 2 and '买' in svg and '卖' in svg


def test_legend_escapes_text_and_color():
    svg = legend([('"><script>', '<b>label')], x=0, y=0)
    assert '<script>' not in svg and '<b>label' not in svg
    assert '&lt;script&gt;' in svg and '&lt;b&gt;label' in svg


def test_value_label_escapes():
    assert '<text' in value_label(10, 10, '1.5') and '1.5' in value_label(10, 10, '1.5')
