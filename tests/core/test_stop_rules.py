import numpy as np
import pandas as pd


STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
            'fundingRate_stop_loss': 0.0015}
MARGIN = 0.05
CAP = 1000.0
C_RATE = 0.0005


def _make_df(net_values, funding=None, pv=None):
    n = len(net_values)
    t = pd.date_range('2024-03-01', periods=n, freq='1min')
    df = pd.DataFrame({
        'candle_begin_time': t,
        'net_value': np.asarray(net_values, dtype='float64'),
        'hold_num': np.ones(n),       # _apply_exit 平仓扣费用得到，等价比对不依赖其值
        'close': np.full(n, 100.0),
    })
    if funding is not None:
        df['fundingRate'] = np.asarray(funding, dtype='float64')
    pv_df = None
    if pv is not None:
        pv_df = pd.DataFrame({'candle_begin_time': t, 'pv_spike': np.asarray(pv, dtype='int64')})
    return df, pv_df


def _scalar_first(df, pv_df, stop_cfg):
    """逐行扫描 evaluate_exit，返回首个触发的 (reason, idx) 或 (None, None)。"""
    from gridtrade.core.stop_rules import evaluate_exit
    pr = (df['net_value'] - 1.0).values
    pr_max = np.maximum.accumulate(pr)
    pv_map = {}
    if pv_df is not None:
        pv_map = dict(zip(pv_df['candle_begin_time'], pv_df['pv_spike']))
    for i in range(len(df)):
        fr = float(df['fundingRate'].iloc[i]) if 'fundingRate' in df.columns else None
        pv = int(pv_map.get(df['candle_begin_time'].iloc[i], 0))
        r = evaluate_exit(float(pr[i]), float(pr_max[i]),
                          net_value=float(df['net_value'].iloc[i]),
                          stop_cfg=stop_cfg, margin_rate=MARGIN, funding_rate=fr, pv_spike=pv)
        if r is not None:
            return r, i
    return None, None


def _assert_equiv(net_values, funding=None, pv=None, stop_cfg=STOP_CFG):
    from gridtrade.core.grid_engine import _apply_exit
    df, pv_df = _make_df(net_values, funding, pv)
    truncated, reason, blown = _apply_exit(df.copy(), CAP, C_RATE, stop_cfg, MARGIN, pv_df)
    s_reason, s_idx = _scalar_first(df, pv_df, stop_cfg)
    assert s_reason == reason, f'reason mismatch: scalar={s_reason} apply_exit={reason}'
    if reason is None:
        assert s_idx is None
    else:
        assert s_idx == len(truncated) - 1, f'idx mismatch: scalar={s_idx} apply_exit={len(truncated)-1}'


def test_no_trigger_runs_to_end():
    _assert_equiv([1.0, 1.002, 1.001, 1.003, 1.002])


def test_fixed_stop_loss():
    _assert_equiv([1.0, 0.99, 0.97, 0.96, 0.95])  # 跌破 -3.4%


def test_chandelier_trailing():
    # 先冲高再回撤：峰值 +5%，回撤超过 max(0.618%, 30%×5%)=1.5%
    _assert_equiv([1.0, 1.02, 1.05, 1.045, 1.03])


def test_funding_rate_stop():
    _assert_equiv([1.0, 1.001, 1.002, 1.001],
                  funding=[0.0, 0.0, 0.002, 0.0])  # |0.002| > 0.0015


def test_pv_active_stop():
    _assert_equiv([1.0, 0.99, 0.98, 0.985],
                  pv=[0, 0, 1, 0])  # pv_spike 且 pnl<-0.015


def test_liquidation():
    _assert_equiv([1.0, 0.5, 0.04, 0.03])  # net_value < 0.05


def test_priority_fixed_over_chandelier_same_bar():
    # bar2 同时满足固定止损(-4%<-3.4%)与回撤止盈；固定止损优先；前两 bar 不触发
    _assert_equiv([1.0, 1.007, 0.96])


def test_liquidation_no_stopcfg_equivalence():
    # stop_cfg=None disables fixed/trailing/funding/pv; only 爆仓 (net_value<0.05) can fire,
    # so this is the scenario that actually exercises the 爆仓 equivalence branch.
    _assert_equiv([1.0, 0.8, 0.04], stop_cfg=None)


def test_pv_threshold_read_from_stop_cfg():
    """evaluate_exit 的 pv 阈值改读 stop_cfg['pv_pnl_thr']；缺省回退 -0.015。"""
    from gridtrade.core.stop_rules import evaluate_exit
    cfg = {'stop_loss': 0.034, 'pv_pnl_thr': -0.02}
    # pnl=-0.017 未到 -0.02 → 不触发；-0.025 触发
    assert evaluate_exit(-0.017, 0.0, net_value=0.983, stop_cfg=cfg, pv_spike=1) is None
    assert evaluate_exit(-0.025, 0.0, net_value=0.975, stop_cfg=cfg, pv_spike=1) == 'pv主动止损'
    # 无 pv_pnl_thr → 回退 -0.015（-0.017 即触发）
    assert evaluate_exit(-0.017, 0.0, net_value=0.983,
                         stop_cfg={'stop_loss': 0.034}, pv_spike=1) == 'pv主动止损'


def test_pv_equiv_with_apply_exit_at_tuned_thr():
    """阈值设 -0.02 时 evaluate_exit 与 _apply_exit 仍逐位等价（两边同传 -0.02）。"""
    from gridtrade.core.grid_engine import _apply_exit
    cfg = {'stop_loss': 0.034, 'pv_pnl_thr': -0.02}
    net = [1.0, 0.99, 0.982, 0.975]            # 逐步下探破 -0.02
    pv = [0, 0, 1, 1]
    df, pv_df = _make_df(net, pv=pv)
    trunc, reason, _ = _apply_exit(df.copy(), CAP, C_RATE, cfg, MARGIN, pv_df, pv_pnl_thr=-0.02)
    s_reason, s_idx = _scalar_first(df, pv_df, cfg)
    assert s_reason == reason and s_idx == len(trunc) - 1
