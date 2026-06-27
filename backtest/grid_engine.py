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


def _apply_stop(df, cap, stop_loss, stop_profit, c_rate_taker):
    """固定止盈损 + 主动止损信号(stop_signal 列，若有)。截断到首次触发并扣平仓手续费。"""
    df['stop'] = np.nan
    if 'stop_signal' in df.columns:
        df.loc[df['stop_signal'] == 1, 'stop'] = 1
    if stop_loss is not None:
        df.loc[df['net_value'] < 1 - stop_loss, 'stop'] = 1
    if stop_profit is not None:
        df.loc[df['net_value'] > 1 + stop_profit, 'stop'] = 1
    df['stop'].fillna(method='ffill', inplace=True)
    df['stop'].fillna(value=0, inplace=True)
    temp = df[df['stop'] == 1]
    reason = None
    if not temp.empty:
        inx = temp.index[0]
        df = df[:inx + 1]
        row = df.iloc[-1]
        fee_rate = abs(row['hold_num']) * row['close'] * c_rate_taker / cap
        df.loc[row.name, 'net_value'] = row['net_value'] - fee_rate
        reason = '止损/止盈触发'
    return df, reason


def cal_equity_curve(candle_df, trade_df, fee, cap, margin_rate=0.05,
                     stop_loss=None, stop_profit=None, c_rate_taker=0.0005,
                     funding_df=None):
    """计算资金曲线。funding_df(可选): 列 ts(ms,UTC)/fundingRate，按持仓收/扣资金费。"""
    trade_data = trade_df.copy()
    candle_data = candle_df.copy()

    trade_data['fee'] = trade_data['order_num'] * trade_data['touch'] * fee
    trade_data['net_dir'] = trade_data['order_dir'].expanding().sum()
    trade_data['grid_gap'] = abs(trade_data['last_touch'] - trade_data['touch'])
    con = (abs(trade_data['net_dir']) - abs(trade_data['net_dir'].shift())) < 0
    trade_data.loc[con, 'real_profit'] = trade_data['grid_gap'] * trade_data['order_num']
    del trade_data['grid_gap'], trade_data['last_touch']

    trade_data['hold_num'] = trade_data['net_dir'] * trade_data['order_num']
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
    if funding_df is not None and not funding_df.empty:
        fr = funding_df.copy()
        fr['candle_begin_time'] = pd.to_datetime(fr['ts'], unit='ms')  # UTC，与缓存 candle_begin_time 同口径
        fr = fr[['candle_begin_time', 'fundingRate']]
        df = pd.merge(left=df, right=fr, on='candle_begin_time', how='left')
        df['fundingRate'].fillna(value=0.0, inplace=True)
        df['fr_fee'] = df['hold_num'] * df['close'] * df['fundingRate']

    df['fee'] = df['fee'].expanding().sum()
    df['fr_fee'] = df['fr_fee'].expanding().sum()
    df['real_profit'] = df['real_profit'].expanding().sum()
    df['profit'] = df['real_profit'] - df['fr_fee'] - df['fee'] + df['unreal_profit']
    df['net_value'] = (df['profit'] + cap) / cap
    df['net_value'].fillna(value=1, inplace=True)

    df, stop_reason = _apply_stop(df, cap, stop_loss, stop_profit, c_rate_taker)

    blown = False
    df['爆仓'] = np.nan
    df.loc[df['net_value'] < margin_rate, '爆仓'] = 1
    df['爆仓'].fillna(method='ffill', inplace=True)
    if 1 in df['爆仓'].to_list():
        df.loc[df['爆仓'] == 1, 'net_value'] = 0.0
        blown = True
    return df, stop_reason, blown


def simulate_grid_engine(bars_df, grid_params, cap=10000.0, leverage=5.0, fee=0.0002,
                         min_amount=0.0, max_rate=0.68, margin_rate=0.05,
                         stop_loss=None, stop_profit=None, c_rate_taker=0.0005,
                         funding_df=None, neutral_init=True):
    """
    端到端封装：bars(本项目 1m df) + 布网参数 → 资金曲线终值。
    grid_params: dict(low_price, high_price, grid_count, stop_high_price, stop_low_price)
    neutral_init: True 时模拟 OKX 中性网格的初始仓位（开网即按 entry 预置 grids_above×每格量 多头）。
    返回: dict(pnl_ratio, net_value_final, terminated, exit_reason, blown_up, n_trades, broke)
    """
    bars = bars_df[['candle_begin_time', 'open', 'high', 'low', 'close']].copy()
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

    # OKX 中性网格初始仓位：开网即在 entry 预置 (entry 上方线数) 笔、每笔每格量 的多头。
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
    eq, stop_reason, blown = cal_equity_curve(bars, trade_df, fee, cap, margin_rate,
                                              stop_loss, stop_profit, c_rate_taker, funding_df)
    nv = float(eq['net_value'].iloc[-1])
    exit_reason = '爆仓' if blown else (stop_reason or ('破网' if broke else '窗口结束'))
    return {'pnl_ratio': nv - 1.0, 'net_value_final': nv, 'terminated': bool(stop_reason or broke or blown),
            'exit_reason': exit_reason, 'blown_up': blown, 'n_trades': int(len(trade_df)), 'broke': broke}
