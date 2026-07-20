"""资金费率止损:回测须按「最后已结算费率」持续判,对齐实盘(2026-07-18)。

分歧:
- **实盘**:`signals._funding_rate` 在 **9h 回看窗**(结算周期 8h + 1h)里取**最后已结算**费率,
  `core/stop_rules.py:22-25` 每 tick 判 `abs(funding_rate) > fr_thr`。故**开格瞬间**就能读到开格
  **之前**那次结算的费率 —— 若超阈值,立刻关格。
- **回测(旧)**:`cal_equity_curve` 用精确时间戳 left-merge,`fundingRate` 只在与结算 ts 相等的那根
  1m bar 上非 0;12h 窗内只有 1-2 根 bar 有机会触发。且 `backtest_run`/`sweep` 把 funding **预切到
  持仓窗** `[lo, hi]` → **窗前那次结算被切掉,回测永远看不到**。

实测:四窗 6493 格中 141 格(2.2%)会在实盘开格瞬间即被资金费率止损,而回测让它们跑满。
(经济影响小 —— 这批格回测均值仅 −0.0006 —— 但口径要对,否则实盘 funding 退出笔数会比回测多 ~2.4x。)

注意 `fundingRate` 一列此前**身兼两职**:收费(只该在结算时刻收)与止损判定(该持续用最后已结算
费率)。故新增 `fr_last` 专供判定,收费仍只在结算时刻 —— 直接 ffill 会变成每分钟都收资金费。
"""
import pandas as pd

from gridtrade.core.grid_engine import simulate_grid_engine

_GP = {'low_price': 100.0, 'high_price': 200.0, 'grid_count': 10,
       'stop_low_price': 90.0, 'stop_high_price': 210.0}
_STOP = {'stop_loss': 0.5, 'trailing_k': None, 'trailing_floor': None,
         'fundingRate_stop_loss': 0.0015}          # 阈值 0.0015
_OPEN = pd.Timestamp('2026-01-01 10:00:00')


def _bars(n=30):
    """开格 10:00 @150,价格上穿最近线 151.57 → 有成交(否则判'未触网',测不到东西)。"""
    rows = [{'candle_begin_time': _OPEN, 'open': 150.0, 'high': 152.0,
             'low': 149.9, 'close': 152.0}]
    for i in range(1, n):
        rows.append({'candle_begin_time': _OPEN + pd.Timedelta(minutes=i),
                     'open': 152.0, 'high': 152.1, 'low': 151.9, 'close': 152.0})
    return pd.DataFrame(rows)


def _fund(ts, rate):
    return pd.DataFrame([{'ts': int(pd.Timestamp(ts).value // 1_000_000), 'fundingRate': rate}])


def _run(funding_df):
    return simulate_grid_engine(_bars(), _GP, cap=1000.0, leverage=5.0,
                                stop_cfg=_STOP, funding_df=funding_df,
                                neutral_init=False, active_stop_mode='none')


def test_pre_window_settlement_triggers_stop_like_live():
    """窗前 1h 结算的费率超阈值 → 实盘开格瞬间就关格,回测必须同样关。
    旧口径:该结算不在窗内任何 bar 上 → 精确 merge 落空 → 回测让它跑满。"""
    res = _run(_fund('2026-01-01 09:00:00', 0.002))       # 开格前 1h 结算,0.002 > 0.0015
    assert res['exit_reason'] == '资金费率止损'


def test_pre_window_settlement_below_threshold_does_not_trigger():
    """窗前结算但未超阈值 → 不得误关(否则是把信号改成恒真)。"""
    res = _run(_fund('2026-01-01 09:00:00', 0.0005))      # 0.0005 < 0.0015
    assert res['exit_reason'] != '资金费率止损'


def test_pre_window_settlement_negative_rate_triggers_on_abs():
    """负费率同样按绝对值判(与实盘 abs(funding_rate) 一致)。"""
    res = _run(_fund('2026-01-01 09:00:00', -0.002))
    assert res['exit_reason'] == '资金费率止损'


def test_settlement_older_than_live_lookback_is_ignored():
    """超出实盘 9h 回看窗的陈旧结算不得触发 —— 实盘那侧根本取不到它,回测也不该看见。"""
    res = _run(_fund('2025-12-31 23:00:00', 0.002))       # 11h 前,超出 9h 窗
    assert res['exit_reason'] != '资金费率止损'


def test_in_window_settlement_still_triggers():
    """窗内结算照常触发(旧口径本就对,修复不得回归)。"""
    res = _run(_fund('2026-01-01 10:05:00', 0.002))
    assert res['exit_reason'] == '资金费率止损'


def test_no_funding_data_does_not_trigger():
    """无资金费数据 → 不触发(契约不变)。"""
    assert _run(None)['exit_reason'] != '资金费率止损'


# ---- B案(2026-07-20):窗口结束 maker 计费(费差上界,门控默认关) ----

def test_window_end_maker_fee_upper_bound():
    """B案:窗口结束平仓 maker 计费——同场景收益差=费率差;默认(0/缺省)与现状逐位一致。"""
    fd = _fund('2026-01-01 09:00:00', 0.0001)             # 不触发任何止损 → 窗口结束
    base = _run(fd)
    stop_m = dict(_STOP, window_end_maker=0.0002)
    res_m = simulate_grid_engine(_bars(), _GP, cap=1000.0, leverage=5.0,
                                 stop_cfg=stop_m, funding_df=fd,
                                 neutral_init=False, active_stop_mode='none')
    assert base['exit_reason'] == '窗口结束' and res_m['exit_reason'] == '窗口结束'
    assert res_m['pnl_ratio'] > base['pnl_ratio']          # maker 费更低 → 收益更高
