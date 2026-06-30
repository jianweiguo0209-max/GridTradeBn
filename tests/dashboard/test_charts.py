from gridtrade.dashboard.charts import line_chart, bar_chart, stacked_bar


def test_line_chart_maps_points():
    # 单线两点 (0,0),(10,10)；width=height=100,pad=10 → x:0->10,10->90; y:0->90,10->10
    svg = line_chart([[(0, 0), (10, 10)]], width=100, height=100, pad=10)
    assert '<svg' in svg and '<polyline' in svg
    assert '10.0,90.0' in svg and '90.0,10.0' in svg


def test_line_chart_empty_placeholder():
    svg = line_chart([], width=100, height=100)
    assert '暂无数据' in svg and '<polyline' not in svg


def test_bar_chart_rects():
    svg = bar_chart([('a', 5.0), ('b', 10.0)], width=100, height=100, pad=10)
    assert svg.count('<rect') == 2
    # 最大值 10 → 满高(80)，5 → 半高(40)
    assert 'height="80.0"' in svg and 'height="40.0"' in svg


def test_bar_chart_empty():
    assert '暂无数据' in bar_chart([], width=100, height=100)


def test_stacked_bar_segments():
    svg = stacked_bar([('g1', [('buy', 3.0), ('sell', 1.0)])], width=100, height=100, pad=10)
    assert svg.count('<rect') == 2     # 两段堆叠
