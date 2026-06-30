from gridtrade.dashboard.charts import line_chart, bar_chart, stacked_bar


def test_line_chart_maps_points():
    # 单线两点 (0,0),(10,10)；width=height=100
    # 绘图区 _L=34,_R=10,_T=16,_B=16 → pl=34,pr=90,pt=16,pb=84
    # sx(0)=34, sy(0)=84; sx(10)=90, sy(10)=16
    svg = line_chart([[(0, 0), (10, 10)]], width=100, height=100)
    assert '<svg' in svg and '<polyline' in svg
    assert '34.0,84.0' in svg and '90.0,16.0' in svg


def test_line_chart_empty_placeholder():
    svg = line_chart([], width=100, height=100)
    assert '暂无数据' in svg and '<polyline' not in svg


def test_bar_chart_rects():
    svg = bar_chart([('a', 5.0), ('b', 10.0)], width=100, height=100)
    assert svg.count('<rect') == 2
    # 新绘图区 _L=34,_R=10,_T=16,_B=16 → pl=34,pr=90,pt=16,pb=84 → ih=68
    # 最大值 10 → 满高(68)，5 → 半高(34)
    assert 'height="68.0"' in svg and 'height="34.0"' in svg


def test_bar_chart_empty():
    assert '暂无数据' in bar_chart([], width=100, height=100)


def test_stacked_bar_segments():
    svg = stacked_bar([('g1', [('buy', 3.0), ('sell', 1.0)])], width=100, height=100)
    assert svg.count('<rect') == 2     # 两段堆叠


# --- new tests for chart chrome (axes / legend / value labels) ---

def test_line_chart_has_axes_legend_value():
    svg = line_chart([[(0, 0.0), (3600_000, 10.0)]], x_is_time=True,
                     series_labels=[('#6cf', '权益')], value_labels=True)
    assert '<polyline' in svg                 # 几何仍在
    assert '00:00' in svg                      # x 时间刻度
    assert '权益' in svg                        # 图例
    assert '10.0' in svg or '10.00' in svg     # y 刻度/末值标注（数值出现）


def test_bar_chart_shows_category_labels_and_values():
    svg = bar_chart([('0', 5.0), ('1', 10.0)], value_labels=True)
    assert svg.count('<rect') >= 2             # 几何仍在
    assert '>0<' in svg or '>0</text>' in svg  # 类目标签 0
    assert '10' in svg                          # 顶值标注 / y 刻度


def test_stacked_bar_legend():
    svg = stacked_bar([('成交', [('buy', 3.0), ('sell', 1.0)])],
                      seg_labels=[('#4caf50', '买'), ('#e53935', '卖')])
    assert svg.count('<rect') >= 2 + 2          # 段 + 图例色块
    assert '买' in svg and '卖' in svg


def test_charts_empty_still_placeholder():
    assert '暂无数据' in line_chart([])
    assert '暂无数据' in bar_chart([])
    assert '暂无数据' in stacked_bar([])
