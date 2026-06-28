import time

import numpy as np
import pandas as pd

from gridtrade.core.factors import cal_factor, cal_cross_factor


def trans_period_for_grid(data, period, exg_dict=None, offset=0):
    """
    周期转换函数，网格策略要用到的
    :param data: K线数据
    :param period: 数据转换周期
    :param exg_dict: 转换规则
    :param offset: 偏移量
    :return:
    """
    data = data.copy()
    data['time'] = data['candle_begin_time']
    data.set_index('candle_begin_time', inplace=True)
    agg_dict = {
        'time': 'first',
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'symbol': 'last',
        'vol': 'sum',
        'volCcy': 'sum',
        'quote_volume': 'sum',
    }
    if exg_dict:
        agg_dict = dict(agg_dict, **exg_dict)
    period_df = data.resample(rule=period, base=offset).agg(agg_dict)

    return period_df


# 对k线数据进行周期转换
def proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset):
    period_df_list = []
    exg_dict = {}
    for symbol, df in symbol_candle_data.items():
        if df is None or df.empty:
            print('no data', symbol)
            continue
        # 我们请求的是1h的k线，计算因子最低要求k线数量是周期的2倍，12H周期需要24根k线
        if len(df) < 24:
            print('no enough data', symbol)
            continue
        df = trans_period_for_grid(df, period, exg_dict=exg_dict, offset=offset)

        # 计算因子
        df = cal_factor(df)

        period_df_list.append(df)

    if not period_df_list:
        print(f'[警告] 所有币种K线数据为空或不足24根，无法进行因子计算。'
              f'请检查: 1)run_time是否正确 2)网络/API是否正常 3)max_candle_num是否足够')
        return pd.DataFrame()

    all_data_df = pd.concat(period_df_list, ignore_index=True)
    all_data_df.sort_values('time', inplace=True)
    all_data_df.reset_index(inplace=True, drop=True)

    # 兼容时区
    utc_offset = int(time.localtime().tm_gmtoff / 60 / 60)
    # 时间转化为东八区
    all_data_df['time'] = pd.to_datetime(all_data_df['time'], unit='ms') + pd.Timedelta(hours=utc_offset)
    # 删除runtime那行的数据，如果有的话
    all_data_df = all_data_df[all_data_df['time'] < run_time]
    # 计算截面因子
    all_data_df = cal_cross_factor(all_data_df)

    return all_data_df


def select_grid_coin(data, factor_info, weight_list, choose_symbols, run_time):
    """
    选择网格币种，可以加入择时的方法
    :param data:
    :param factor_info:
    :return:
    """
    # # 由于增加多因子攒数，这里给因子组合默认值
    # factor_info = {"Reg_v2_2": True, "Sgcz_2": True, "db_volume_v1_2": False}

    # 删除选币因子为空的数据
    data.dropna(subset=list(factor_info.keys()), inplace=True)

    # 交易额过滤
    data = data[data['交易额分位占比'] <= 0.55]

    # 均线死叉过滤
    # data['标识信号'] = True
    # s_1 = data['ma_2'] < data['ma_13']
    # s_2 = data['ma_2'].shift() >= data['ma_13'].shift()
    # s_3 = data['ma_5'] < data['ma_13']
    # s_4 = data['ma_5'].shift() >= data['ma_13'].shift()
    # data.loc[s_1 & s_2 & s_3 & s_4, '标识信号'] = False
    # data = data.loc[data['标识信号'] == True]

    # 因子值筛选
    # data = data[
    # (data['Reg_v2_2'].between(-0.5, 0.2, inclusive='both')) &
    # (data['Sgcz_2'].between(-0.06, -0.02, inclusive='both')) &
    # (data['db_volume_v1_2'].between(0.8, 1.8, inclusive='both'))]

    # 过滤v1.0
    data = data[
        ~(
            (data['Reg_v2_2'] < -2) &
            (data['Sgcz_2'] < -0.12) &
            (data['db_volume_v1_2'] < 0.5)
        )
    ]

    # 多因子名次排序
    rank_col = []
    for factor in factor_info:
        data['rank_%s' % factor] = data.groupby('time')[factor].rank(method='first', ascending=factor_info[factor])
        rank_col.append('rank_%s' % factor)

    # 排序相加
    # data['rank_sum'] = data[rank_col].sum(axis=1)  # 因子排名
    data['rank_sum'] = (data[rank_col] * weight_list).sum(axis=1)  # 因子排名加权重
    data['rank'] = data.groupby('time')['rank_sum'].rank(method='first', ascending=True)

    # 交易额分位排序
    # data['rank'] = data.groupby('time')['交易额分位占比'].rank(method='first',ascending=True)  # 注意这里设置为升序

    # 测试用：打印当前周期的全集排序
    pdata = data[(data['time'] + pd.to_timedelta('12H')) >= run_time]
    pdata.sort_values(by='rank', inplace=True)
    pdata["time"] = pdata["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    print("当前周期的全集选币排序")
    print(pdata.head(10))
    # exit()

    # 选币
    data = data[data['rank'] <= choose_symbols]

    # （大盘择时）筛选条件：8%<上涨比例<92%
    # con1 = data['上涨比例'] > 0.08
    # con2 = data['上涨比例'] < 0.92
    # data = data[con1 & con2]

    data.sort_values(by='time', inplace=True)

    return data


def compute_offset(run_time, period, utc_offset):
    """复刻 functions.get_order_offset_tag 的 offset 计算。"""
    utc_run_time = run_time - pd.Timedelta(hours=utc_offset)
    return int(((utc_run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % int(period[:-1]))
