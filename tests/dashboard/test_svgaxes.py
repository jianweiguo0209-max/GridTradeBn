from gridtrade.dashboard.svgaxes import (svg_escape, nice_ticks, y_axis,
                                         x_time_axis, x_cat_axis, legend, value_label)


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
    # 0 ms = 1970-01-01 00:00 UTC
    svg = x_time_axis(0, 3600_000, sx=lambda t: t / 3600_000 * 100, y_base=120)
    assert '00:00' in svg and '01:00' in svg          # 起/现
    assert '<text' in svg


def test_x_cat_axis_escapes():
    svg = x_cat_axis(['<b>', 'sell'], [10.0, 50.0], y_base=120)
    assert '&lt;b&gt;' in svg and 'sell' in svg and '<b>' not in svg


def test_legend_swatches_and_text():
    svg = legend([('#4caf50', '买'), ('#e53935', '卖')], x=10, y=8)
    assert svg.count('<rect') == 2 and '买' in svg and '卖' in svg


def test_value_label_escapes():
    assert '<text' in value_label(10, 10, '1.5') and '1.5' in value_label(10, 10, '1.5')
