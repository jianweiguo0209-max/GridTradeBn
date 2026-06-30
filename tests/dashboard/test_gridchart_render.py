from gridtrade.dashboard.gridchart import render, ChartDTO


def _dto(**kw):
    base = dict(symbol='BTC', window='life', timeframe='1m', start_ms=1000, end_ms=2000,
                price_series=[(1000, 100.0), (2000, 110.0)], ohlcv_ok=True,
                grid_lines=[95.0, 105.0], open_orders=[(95.0, 'buy'), (105.0, 'sell')],
                fills=[(1500, 102.0, 'buy')], entry_price=100.0, stop_low=80.0,
                stop_high=120.0, current_price=108.0)
    base.update(kw)
    return ChartDTO(**base)


def test_render_full_chart():
    svg = render(_dto(), width=200, height=200, pad=20)
    assert svg.startswith('<svg') and svg.endswith('</svg>')
    assert '<polyline' in svg                      # 价格走势
    assert svg.count('<line') >= 2 + 1 + 2         # 2 网格线 + entry + 2 stop（至少）
    assert '#4caf50' in svg and '#e53935' in svg   # 买绿（grid 95/ fill）、卖红（grid 105）
    assert svg.count('<circle') >= 1               # 至少 1 个 fill 点（current 也可能是 circle）


def test_render_degrades_without_ohlcv():
    svg = render(_dto(price_series=[], ohlcv_ok=False), width=200, height=200, pad=20)
    assert '<polyline' not in svg                  # 无价格折线
    assert '行情暂不可用' in svg
    assert svg.count('<line') >= 2                 # 网格线仍在


def test_render_all_empty_placeholder():
    svg = render(_dto(price_series=[], ohlcv_ok=False, grid_lines=[], open_orders=[],
                      fills=[], entry_price=None, stop_low=None, stop_high=None,
                      current_price=None))
    assert '无数据' in svg and '<polyline' not in svg
