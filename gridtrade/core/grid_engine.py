"""
网格成交仿真引擎 v2 —— 移植自 grid_backtest/program/{Grid_function,Active_stop}.py，适配本项目。

比 grid_sim.py 成熟：等量挂单(cap*lev*max_rate/Σ价)、按净头寸算持仓均价、未实现盈亏、
破网截断、爆仓、资金费框架、固定止盈损。

三处适配（按既定方案）：
1. 吃**显式终止价** stop_high/stop_low（来自 account_0 calc_grid_params_v1/v2，读 grid_v2_config），
   不再用单一对称 limit 反推。
2. 吃本项目缓存的 1m bars（列 candle_begin_time/open/high/low/close/symbol/quote_volume）。
3. 基准/主动止损/固定止盈损均**可选**（None=关闭），便于校准时跑"纯网格到窗口末"。

布网参数（grid_v2_config 等）仍由上游 account_0 计算，本引擎只消费布网结果（low/high/grid_num/stop价）。
"""
import datetime

import numpy as np
import pandas as pd


def grid_order_info(cap, leverage, low, high, grid_num, stop_low, stop_high,
                    min_amount=0.0, max_rate=0.68):
    """等比网格 + 等量挂单。stop_low/stop_high 为显式终止价。返回 None 表示保证金太低无法建网。"""
    q = (high / low) ** (1.0 / grid_num)
    price_array = np.array([low * (q ** i) for i in range(grid_num + 1)]).round(8)
    order_num = cap * leverage * max_rate / price_array.sum()
    if min_amount and min_amount > 0:
        order_num = order_num - order_num % min_amount
    if order_num <= 0:
        return None
    return {'价格序列': price_array, '每笔数量': order_num,
            '终止最低价': float(stop_low), '终止最高价': float(stop_high)}


def trans_candle_to_tick(df, grid_info):
    """分钟 K线→近似逐笔（开→低→高→收 / 开→高→低→收，4 点/分钟）；破网后截断。"""
    data = df[['candle_begin_time', 'open', 'high', 'low', 'close']].copy()
    data.loc[data['close'] >= data['open'], 'mode'] = 1
    data.loc[data['close'] < data['open'], 'mode'] = -1
    data['p1'] = data['open']
    data.loc[data['mode'] == 1, 'p2'] = data['low']
    data.loc[data['mode'] == -1, 'p2'] = data['high']
    data.loc[data['mode'] == 1, 'p3'] = data['high']
    data.loc[data['mode'] == -1, 'p3'] = data['low']
    data['p4'] = data['close']

    _dict = {'p1': 0, 'p2': 15, 'p3': 30, 'p4': 45}
    ticks = []
    for key in _dict.keys():
        _ = data[['candle_begin_time', key]].copy()
        _['candle_begin_time'] = _['candle_begin_time'] + datetime.timedelta(seconds=_dict[key])
        _.rename(columns={key: 'tick_price'}, inplace=True)
        ticks.append(_)
    tick_df = pd.concat(ticks, ignore_index=True)
    tick_df.sort_values(by='candle_begin_time', inplace=True)
    tick_df.reset_index(drop=True, inplace=True)

    # 破网：触及终止价即截断（含该点）
    tick_df['stop'] = np.nan
    tick_df.loc[tick_df['tick_price'] > grid_info['终止最高价'], 'stop'] = 1
    tick_df.loc[tick_df['tick_price'] < grid_info['终止最低价'], 'stop'] = 1
    stop = tick_df[tick_df['stop'] == 1]
    broke = not stop.empty
    if broke:
        tick_df = tick_df[:stop.index[0] + 1]
    del tick_df['stop']
    return tick_df, broke


def grid_touch_info(df, grid_info):
    """逐笔→触网信息。"""
    touch_df = df.copy()
    price_array = grid_info['价格序列']
    for p in price_array:
        touch_df[p] = ''
        touch_df.loc[(touch_df['tick_price'].shift() < p) & (p <= touch_df['tick_price']), p] = '%s_' % p
        touch_df.loc[(touch_df['tick_price'].shift() > p) & (p >= touch_df['tick_price']), p] = '%s_' % p
    touch_df['touch'] = touch_df[list(price_array)].sum(axis=1, skipna=True)

    def wash_touch(x):
        if x == '':
            return np.nan
        t_list = [float(t) for t in x.split('_')[:-1]]
        return t_list

    touch_df['touch'] = touch_df['touch'].apply(wash_touch)
    touch_df.drop(columns=list(price_array), axis=1, inplace=True)
    touch_df['last_tick'] = touch_df['tick_price'].shift()
    touch_df = touch_df[touch_df['touch'].notnull()]
    touch_df.reset_index(drop=True, inplace=True)
    touch_df['touch_times'] = touch_df['touch'].apply(lambda x: len(x))
    con = (touch_df['tick_price'] < touch_df['last_tick']) & (touch_df['touch_times'] > 1)
    touch_df.loc[con, 'touch'] = touch_df['touch'].apply(lambda x: sorted(x, reverse=True))
    return touch_df[['candle_begin_time', 'tick_price', 'touch', 'touch_times']]


def get_trade_info(touch_df, open_price, grid_info):
    """触网→交易信息。"""
    if touch_df.empty:
        return pd.DataFrame()
    trade_df = pd.DataFrame()
    touch_df = touch_df.copy()
    touch_df['time_list'] = touch_df.apply(lambda r: [r['candle_begin_time']] * r['touch_times'], axis=1)
    trade_df['candle_begin_time'] = touch_df['time_list'].sum()
    trade_df['touch'] = touch_df['touch'].sum()
    con = trade_df['touch'] == trade_df['touch'].shift()
    trade_df = trade_df[~con]
    price_array = grid_info['价格序列']
    closest = price_array[np.argmin(abs(price_array - open_price))]
    if not trade_df.empty and trade_df['touch'].iloc[0] == closest:
        trade_df = trade_df[1:]
    trade_df.reset_index(drop=True, inplace=True)
    if trade_df.empty:
        return pd.DataFrame()
    trade_df['last_touch'] = trade_df['touch'].shift()
    trade_df['last_touch'].fillna(value=open_price, inplace=True)
    trade_df.loc[trade_df['last_touch'] > trade_df['touch'], 'order_dir'] = 1
    trade_df.loc[trade_df['last_touch'] < trade_df['touch'], 'order_dir'] = -1
    trade_df['order_num'] = grid_info['每笔数量']
    return trade_df[['candle_begin_time', 'last_touch', 'touch', 'order_dir', 'order_num']]


def calc_pv_spike(bars_df, active_period='15min', mult=3, n=233):
    """复刻 calc_active_loss_signal_pv 的量能部分：active_period 重采样后 quote_volume > mult×rolling(n).mean。
    返回 (candle_begin_time, pv_spike) 逐 1m 映射。需 bars 含 quote_volume。
    注：窗口内 1m 数据有限，rolling(n) 用 min_periods 近似，缺 n 根前置历史（fidelity 限制，已知）。"""
    if 'quote_volume' not in bars_df.columns:
        return None
    b = bars_df[['candle_begin_time', 'quote_volume']].copy()
    b = b.set_index('candle_begin_time').resample(active_period).agg({'quote_volume': 'sum'})
    b['mean_n'] = b['quote_volume'].rolling(n, min_periods=1).mean()
    b['pv_spike'] = (b['quote_volume'] > mult * b['mean_n']).astype(int)
    b = b.reset_index()[['candle_begin_time', 'pv_spike']]
    # 映射回 1m：用 merge_asof 取每个 1m bar 所属的 active_period 信号
    out = bars_df[['candle_begin_time']].copy().sort_values('candle_begin_time')
    out = pd.merge_asof(out, b.sort_values('candle_begin_time'), on='candle_begin_time', direction='backward')
    out['pv_spike'].fillna(value=0, inplace=True)
    return out[['candle_begin_time', 'pv_spike']]


# ==================== 新型主动止损信号函数 ====================
# 每个函数接收 bars_df（含 candle_begin_time/OHLCV），返回 (candle_begin_time, signal) 的 1m 映射。
# signal=1 表示该 bar 触发止损信号（最终是否退出还需结合 pnlRatio 门槛）。

def _compute_atr_breakdown(bars_df, short_period=14, long_period=233, mult=2.0):
    """S1: ATR 突变止损 —— 短期 ATR 超过长期 ATR 的 mult 倍。
    捕获场景：市场突然从低波动跳入高波动 regime，网格面临快速亏损风险。"""
    df = bars_df[['candle_begin_time', 'high', 'low', 'close']].copy()
    tr = np.maximum(df['high'] - df['low'],
           np.maximum(abs(df['high'] - df['close'].shift(1)),
                      abs(df['low'] - df['close'].shift(1))))
    atr_short = tr.rolling(short_period, min_periods=1).mean()
    atr_long = tr.rolling(long_period, min_periods=1).mean()
    df['signal'] = (atr_short > mult * atr_long).astype(int)
    return df[['candle_begin_time', 'signal']]


def _compute_trend_break(bars_df, fast_period=20, slow_period=60):
    """S2: 趋势破位止损 —— EMA(fast) < EMA(slow) 且 close < EMA(slow)。
    捕获场景：确认性下跌趋势形成，网格持续承受方向性压力（正是本项目主要亏损源）。"""
    df = bars_df[['candle_begin_time', 'close']].copy()
    ema_fast = df['close'].ewm(span=fast_period, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow_period, adjust=False).mean()
    df['signal'] = ((ema_fast < ema_slow) & (df['close'] < ema_slow)).astype(int)
    return df[['candle_begin_time', 'signal']]


def _compute_bb_breakdown(bars_df, period=20, n_std=2.0):
    """S4: 布林带下轨击穿 —— close < 下轨 且 带宽扩张（当前宽度 > 1.5×均值）。
    捕获场景：价格跌破统计支撑位 + 波动加剧，网格面临持续下行压力。"""
    df = bars_df[['candle_begin_time', 'close']].copy()
    ma = df['close'].rolling(period, min_periods=1).mean()
    std = df['close'].rolling(period, min_periods=1).std()
    lower = ma - n_std * std
    width = 2 * n_std * std / (ma + 1e-8)
    width_mean = width.rolling(period * 4, min_periods=1).mean()
    df['signal'] = ((df['close'] < lower) & (width > 1.5 * width_mean)).astype(int)
    return df[['candle_begin_time', 'signal']]


def _apply_exit(df, cap, c_rate_taker, stop_cfg=None, margin_rate=0.05, pv_spike_df=None,
                active_stop_mode='pv', bars_df=None, pv_pnl_thr=-0.015):
    """
    复刻实盘 calc_loss_or_profit 的退出优先级，对 net_value 序列逐 bar 取最早触发：
      1) 固定止损     pnlRatio < -stop_loss
      2) Chandelier   回撤 >= max(trailing_floor, trailing_k×峰值) 且 峰值 > floor
      3) 资金费率止损 |fundingRate| > fundingRate_stop_loss（需 df 有 fundingRate 列）
      4) 主动止损     由 active_stop_mode 指定（pv/atr/trend/time_decay/bb/loss_accel/none）
      5) 爆仓         net_value < margin_rate
    active_stop_mode='pv' 时门槛用 pv_pnl_thr（默认 -0.015，与历史一致）；新型模式统一 -0.01。
    返回 (截断后的 df, reason, blown)。stop_cfg=None 时仅查爆仓。
    """
    df = df.reset_index(drop=True)
    pr = (df['net_value'] - 1.0).values
    pr_max = np.maximum.accumulate(pr)
    n = len(df)
    reason_at = [None] * n

    def mark(mask, name):
        idx = np.where(mask)[0]
        for i in idx:
            if reason_at[i] is None:
                reason_at[i] = name

    # 按优先级从高到低标注（同 bar 高优先级覆盖）
    if stop_cfg is not None:
        mark(pr < -stop_cfg['stop_loss'], '固定止损')
        k = stop_cfg.get('trailing_k'); floor = stop_cfg.get('trailing_floor')
        if k is not None and floor is not None:
            allowed = np.maximum(floor, k * pr_max)
            mark((pr_max - pr >= allowed) & (pr_max > floor), '连续回撤止盈')
        fr_thr = stop_cfg.get('fundingRate_stop_loss')
        if fr_thr is not None and 'fundingRate' in df.columns:
            mark(np.abs(df['fundingRate'].values) > fr_thr, '资金费率止损')

        # ---- 主动止损（按 mode 分派）----
        pnl_thr = -0.01  # 新型主动止损的统一亏损门槛（1%）
        if active_stop_mode == 'pv' and pv_spike_df is not None:
            m = pd.merge(df[['candle_begin_time']], pv_spike_df, on='candle_begin_time', how='left')
            mark((m['pv_spike'].fillna(0).values == 1) & (pr < pv_pnl_thr), 'pv主动止损')
        elif active_stop_mode == 'atr' and bars_df is not None:
            sig = _compute_atr_breakdown(bars_df)
            m = pd.merge(df[['candle_begin_time']], sig, on='candle_begin_time', how='left')
            mark((m['signal'].fillna(0).values == 1) & (pr < pnl_thr), 'atr主动止损')
        elif active_stop_mode == 'trend' and bars_df is not None:
            sig = _compute_trend_break(bars_df)
            m = pd.merge(df[['candle_begin_time']], sig, on='candle_begin_time', how='left')
            mark((m['signal'].fillna(0).values == 1) & (pr < pnl_thr), '趋势破位止损')
        elif active_stop_mode == 'time_decay':
            t0 = df['candle_begin_time'].iloc[0]
            hours = (df['candle_begin_time'] - t0).dt.total_seconds() / 3600.0
            decay = 0.10  # 每过 1 小时阈值收紧 10%
            dynamic_thr = pnl_thr / (1.0 + decay * hours.values)
            mark(pr < dynamic_thr, '时间衰减止损')
        elif active_stop_mode == 'bb' and bars_df is not None:
            sig = _compute_bb_breakdown(bars_df)
            m = pd.merge(df[['candle_begin_time']], sig, on='candle_begin_time', how='left')
            mark((m['signal'].fillna(0).values == 1) & (pr < pnl_thr), '布林击穿止损')
        elif active_stop_mode == 'loss_accel':
            pr_series = pd.Series(pr, index=df['candle_begin_time'])
            pr_delta = pr_series.diff()
            dt_hours = df['candle_begin_time'].diff().dt.total_seconds() / 3600.0
            dt_hours.iloc[0] = 1.0
            rate = pr_delta.values / dt_hours.values
            mark((rate < -0.005) & (pr < pnl_thr), '亏损加速止损')
        # active_stop_mode == 'none' → 不启用任何主动止损

    # 爆仓（最低优先级，通常被固定止损先触发）
    mark(pr < margin_rate - 1.0, '爆仓')

    first = next((i for i in range(n) if reason_at[i] is not None), None)
    if first is None:
        # 窗口结束：无退出触发，但持仓也应按最后 close 扣一次平仓 taker 费（诚实持仓成本，
        # 否则持仓越大漏扣越多、系统性高估——尤其宽带/疏格累积大仓的配置）。
        row = df.iloc[-1]
        fee_rate = abs(row['hold_num']) * row['close'] * c_rate_taker / cap
        df.loc[row.name, 'net_value'] = row['net_value'] - fee_rate
        return df, None, False
    reason = reason_at[first]
    df = df[:first + 1].copy()
    row = df.iloc[-1]
    if reason == '爆仓':
        df.loc[row.name, 'net_value'] = 0.0
        return df, reason, True
    # 平仓扣 taker 手续费
    fee_rate = abs(row['hold_num']) * row['close'] * c_rate_taker / cap
    df.loc[row.name, 'net_value'] = row['net_value'] - fee_rate
    return df, reason, False


def cal_equity_curve(candle_df, trade_df, fee, cap, c_rate_taker=0.0005, funding_df=None):
    """计算资金曲线（不套退出，退出由 _apply_exit 负责）。funding_df(可选): 列 ts(ms,UTC)/fundingRate。"""
    trade_data = trade_df.copy()
    candle_data = candle_df.copy()

    trade_data['fee'] = trade_data['order_num'] * trade_data['touch'] * fee
    trade_data['net_dir'] = trade_data['order_dir'].expanding().sum()
    trade_data['grid_gap'] = abs(trade_data['last_touch'] - trade_data['touch'])
    con = (abs(trade_data['net_dir']) - abs(trade_data['net_dir'].shift())) < 0
    trade_data.loc[con, 'real_profit'] = trade_data['grid_gap'] * trade_data['order_num']
    del trade_data['grid_gap'], trade_data['last_touch']

    # 净持仓 = 累计带符号成交量 Σ(order_dir×order_num)。均匀 lot（回测）下
    # 恒等于 net_dir×order_num；实盘逐笔 size 非均匀（部分成交）时后者失效，故用累计量。
    trade_data['hold_num'] = (trade_data['order_dir'] * trade_data['order_num']).expanding().sum()
    price_df = trade_data[['touch', 'net_dir']].drop_duplicates(subset=['net_dir']).copy()
    pos = price_df[price_df['net_dir'] > 0].sort_values('net_dir', ascending=True)
    neg = price_df[price_df['net_dir'] < 0].sort_values('net_dir', ascending=False)
    if not pos.empty:
        pos['avg_price'] = pos['touch'].expanding().mean()
    if not neg.empty:
        neg['avg_price'] = neg['touch'].expanding().mean()
    price_df = pd.concat([pos, neg], ignore_index=True)
    trade_data = pd.merge(left=trade_data, right=price_df[['net_dir', 'avg_price']], on='net_dir', how='left')
    trade_data['avg_price'].fillna(value=0, inplace=True)
    del trade_data['touch'], trade_data['order_dir'], trade_data['order_num']

    df = pd.merge(left=candle_data, right=trade_data, on=['candle_begin_time'], how='outer', sort=True)
    for col in ['close', 'open', 'net_dir', 'hold_num', 'avg_price', 'symbol']:
        if col in df.columns:
            df[col].fillna(method='ffill', inplace=True)
    for col in ['fee', 'real_profit']:
        df[col].fillna(value=0.0, inplace=True)
    df['net_dir'].fillna(value=0.0, inplace=True)
    df['hold_num'].fillna(value=0.0, inplace=True)
    df['avg_price'].fillna(value=0.0, inplace=True)

    df['unreal_profit'] = df['hold_num'] * (df['close'] - df['avg_price'])

    # 资金费：+给出/-收回 = hold_num * close * fundingRate（用 close 近似 mark，微小误差）
    df['fr_fee'] = 0.0
    df['fundingRate'] = 0.0
    if funding_df is not None and not funding_df.empty:
        fr = funding_df.copy()
        fr['candle_begin_time'] = pd.to_datetime(fr['ts'], unit='ms')  # UTC，与缓存 candle_begin_time 同口径
        fr = fr[['candle_begin_time', 'fundingRate']].rename(columns={'fundingRate': '_fr'})
        df = pd.merge(left=df, right=fr, on='candle_begin_time', how='left')
        df['fundingRate'] = df['_fr'].fillna(value=0.0)
        del df['_fr']
        df['fr_fee'] = df['hold_num'] * df['close'] * df['fundingRate']

    df['fee'] = df['fee'].expanding().sum()
    df['fr_fee'] = df['fr_fee'].expanding().sum()
    df['real_profit'] = df['real_profit'].expanding().sum()
    df['profit'] = df['real_profit'] - df['fr_fee'] - df['fee'] + df['unreal_profit']
    df['net_value'] = (df['profit'] + cap) / cap
    df['net_value'].fillna(value=1, inplace=True)
    return df


def simulate_grid_engine(bars_df, grid_params, cap=10000.0, leverage=5.0, fee=0.0002,
                         min_amount=0.0, max_rate=0.68, margin_rate=0.05,
                         stop_cfg=None, c_rate_taker=0.0005,
                         funding_df=None, neutral_init=False, pv_spike_df=None,
                         active_stop_mode='pv', pv_pnl_thr=-0.015,
                         pv_mult=3, pv_n=233, pv_period='15min'):
    """
    端到端封装：bars(本项目 1m df) + 布网参数 → 资金曲线终值。
    grid_params: dict(low_price, high_price, grid_count, stop_high_price, stop_low_price)
    neutral_init: 默认 False = 纯中性网格：入场净仓=0，上穿转空、下穿转多，仓位对称于 entry。
                  True 时模拟 OKX「做多式」底仓（开网即按 entry 预置 grids_above×每格量 多头，
                  仓位恒 ≥0、只多不空——顶部空仓、底部满多），供对照/校准用。
    stop_cfg: 实盘 stop_loss_config（stop_loss/trailing_k/trailing_floor/fundingRate_stop_loss）；
              None=不套退出(仅破网/爆仓)，跑到窗口末（校准手动停止网格用）。
    funding_df: 列 ts(ms,UTC)/fundingRate，用于资金费 PnL + 资金费率止损。
    active_stop_mode: 主动止损模式 (pv/atr/trend/time_decay/bb/loss_accel/none)；默认 'pv'（与历史一致）。
    pv_pnl_thr: pv 主动止损的亏损门槛（默认 -0.015）。
    pv_mult/pv_n/pv_period: pv 量能尖峰参数（quote_volume>mult×rolling(n).mean，period 重采样）；默认与历史一致。
    返回: dict(pnl_ratio, net_value_final, terminated, exit_reason, blown_up, n_trades, broke)
    """
    cols = ['candle_begin_time', 'open', 'high', 'low', 'close']
    if 'quote_volume' in bars_df.columns:
        cols = cols + ['quote_volume']
    bars = bars_df[cols].copy()
    if 'symbol' in bars_df.columns:
        bars['symbol'] = bars_df['symbol'].values
    gi = grid_order_info(cap, leverage, grid_params['low_price'], grid_params['high_price'],
                         int(grid_params['grid_count']), grid_params['stop_low_price'],
                         grid_params['stop_high_price'], min_amount=min_amount, max_rate=max_rate)
    if gi is None:
        return {'pnl_ratio': 0.0, 'net_value_final': 1.0, 'terminated': False,
                'exit_reason': '建网失败', 'blown_up': False, 'n_trades': 0, 'broke': False}
    tick_df, broke = trans_candle_to_tick(bars, gi)
    touch_df = grid_touch_info(tick_df, gi)
    entry = bars['open'].iloc[0]
    trade_df = get_trade_info(touch_df, entry, gi)

    # 做多式底仓（neutral_init=True，非默认）：开网即在 entry 预置 (entry 上方线数) 笔、每笔每格量 的多头，
    # 使仓位恒 ≥0（只多不空）。默认 False 时不注入 → 纯中性：上穿转空、下穿转多，仓位对称于 entry。
    # 用「逐格 +1 单位」注入（而非单行 bulk），以兼容引擎按净头寸算均价的逻辑（bulk 会污染均价）。
    if neutral_init:
        grids_above = int((gi['价格序列'] > entry).sum())
        if grids_above > 0:
            t0 = bars['candle_begin_time'].iloc[0]
            init_rows = pd.DataFrame([{'candle_begin_time': t0, 'last_touch': entry, 'touch': entry,
                                       'order_dir': 1.0, 'order_num': gi['每笔数量']}
                                      for _ in range(grids_above)])
            trade_df = init_rows if trade_df.empty else pd.concat([init_rows, trade_df], ignore_index=True)
    if trade_df.empty:
        return {'pnl_ratio': 0.0, 'net_value_final': 1.0, 'terminated': broke,
                'exit_reason': '破网' if broke else '未触网', 'blown_up': False, 'n_trades': 0, 'broke': broke}
    bars = bars[bars['candle_begin_time'] <= tick_df['candle_begin_time'].iloc[-1]]
    eq = cal_equity_curve(bars, trade_df, fee, cap, c_rate_taker, funding_df)

    # pv 量能信号：优先用外部传入(基于充分 15m 历史)；否则窗口内近似(缺前置历史，fidelity 限制)
    if active_stop_mode == 'pv' and pv_spike_df is None and stop_cfg is not None \
            and 'quote_volume' in bars.columns:
        pv_spike_df = calc_pv_spike(bars, active_period=pv_period, mult=pv_mult, n=pv_n)

    eq, stop_reason, blown = _apply_exit(eq, cap, c_rate_taker, stop_cfg, margin_rate, pv_spike_df,
                                         active_stop_mode=active_stop_mode, bars_df=bars,
                                         pv_pnl_thr=pv_pnl_thr)
    nv = float(eq['net_value'].iloc[-1])
    exit_reason = stop_reason or ('破网' if broke else '窗口结束')
    # 已实现 vs 未实现拆分（诊断/analytics）：unreal_pnl=最后一根浮盈/cap；real_pnl=其余(已实现净费)
    unreal_pnl = 0.0 if blown else float(eq['unreal_profit'].iloc[-1]) / cap
    return {'pnl_ratio': nv - 1.0, 'net_value_final': nv,
            'terminated': bool(stop_reason or broke or blown),
            'exit_reason': exit_reason, 'blown_up': blown, 'n_trades': int(len(trade_df)), 'broke': broke,
            'unreal_pnl': unreal_pnl, 'real_pnl': (nv - 1.0) - unreal_pnl}
