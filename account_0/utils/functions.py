import math
import os
import time
import traceback
from datetime import datetime
from multiprocessing import Pool

import numpy as np
import pandas as pd

from api.grid import stop_grid, create_grid, query_running_grid, query_grid_history
from api.kline import fetch_ok_swap_candle_data
from config import order_path
from utils.fancy_grid_function import cal_factor, cal_cross_factor
from utils.fancy_grid_function import select_grid_coin
from utils.notification import send_msg_q_wechat
from utils.stop_loss import calc_loss_or_profit
from utils.tools import retry_wrapper


# 并行获取所有币种永续合约数据的1小时K线数据
def fetch_all_binance_swap_candle_data(exchange, symbol_list, run_time, njob, max_candle_num=100):
    """
    获取所有币种的k线数据
    :param exchange: 交易所对象
    :param symbol_list: 币种信息
    :param run_time: 当前运行时间
    :param njob: 使用多少进程去执行
    :param max_candle_num: 每个币种最大获取k线数量
    :return:
    """
    start_time = datetime.now()

    if njob == 1:
        # 循环获取数据
        result = []
        for symbol in symbol_list:
            res = fetch_ok_swap_candle_data(exchange, symbol, run_time, max_candle_num)
            result.append(res)
    else:
        # 创建参数列表
        arg_list = [(exchange, symbol, run_time, max_candle_num) for symbol in symbol_list]
        # 多进程获取数据
        with Pool(processes=njob) as pl:
            # 利用starmap启用多进程信息
            result = pl.starmap(fetch_ok_swap_candle_data, arg_list)

    print('获取所有合约K线数据完成: ', datetime.now() - start_time)

    return dict(result)


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


# 获取策略信息中的tag
def get_order_offset_tag(strategy_config, run_time):
    """
    获取策略信息中的tag
    :param strategy_config: 策略配置
    :param run_time: 当前运行时间
    :return: 策略的tag 和 offset
    """
    order_offset_tag = None
    period = strategy_config['period']

    # 兼容时区
    utc_offset = int(time.localtime().tm_gmtoff / 60 / 60)
    utc_run_time = run_time - pd.Timedelta(hours=utc_offset)
    offset = int(((utc_run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % int(period[:-1]))

    # 当前offset在策略中，说明当前小时需要下单，直接结束循环
    if offset in strategy_config['offset']:
        order_offset_tag = f'{strategy_config["strategy_tag"]}{offset}'

    return order_offset_tag, offset


# 数据整理 & 选币 & 生成下单信息
def proceed_order_for_strategy_config(symbol_dict, symbol_candle_data, run_time, strategy_config, offset):
    """
    数据整理 & 选币 & 生成下单信息
    :param symbol_dict: 币种价格精度信息
    :param symbol_candle_data: 所有币种k线数据
    :param run_time: 当前运行时间
    :param strategy_config: 策略配置信息
    :param offset: 当前需要下单的offset
    :return:
    """
    start_time = datetime.now()

    period = strategy_config['period']
    factors = strategy_config['factors']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']

    # =计算因子
    all_data_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)
    if all_data_df.empty:
        print('[警告] 因子计算结果为空，跳过选币和下单')
        return pd.DataFrame()

    # =开始选币
    factor_data = select_grid_coin(all_data_df, factors, weight_list, choose_symbols, run_time)
    # print(factor_data.tail(10))

    # =只保存当前周期的数据
    factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]

    # =生成下单信息
    order_df = generate_order_info(factor_data, strategy_config, offset, symbol_dict)

    print('数据整理 & 选币 & 生成下单信息 完成:', datetime.now() - start_time)
    return order_df


# 计算下单金额
def calc_order_price(exchange, strategy_config, rebalance):
    """
    计算下单金额
    :param exchange: 交易所对象
    :param strategy_config: 策略配置信息
    :param rebalance: 是否需要rebalance
    :return:
    """
    # =====获取当前账户币种可用余额
    balance = retry_wrapper(exchange.private_get_account_balance, params={'ccy': 'USDT'}, act_name='获取账户资金')
    balance = balance['data'][0]['details']

    if not len(balance):
        print('当前账户里没有钱，程序退出')
        raise Exception('当前账户里没有钱')

    # 当前账户总资产(包含网格的已实现盈亏和当前持仓的未实现盈亏)
    total_balance = float(balance[0]['eq'])
    # 当前账户可用资产(未参与网格交易的资金)
    cash_balance = float(balance[0]['cashBal'])

    # ==获取当前策略需要开多少个网格
    offset_total = len(strategy_config['offset']) * strategy_config['choose_symbols']

    # 是否需要rebalance：某个offset平仓之后，是否将其利润或亏损，和其他offset共同承担
    if rebalance:
        # =====通过总资金进行rebalance(会带一点复利效应)
        order_price = round(min(total_balance * 0.99 / offset_total, cash_balance * 0.99), 1)  # 账户目前所有的钱 / 所有offset的数量
        # (已经开仓的offset初始投入的钱（不包含其当前交易盈亏）+  cash_balance) / 所有offset的数量
    else:
        # =====获取正在运行的offset数量
        running_offset_num = 0
        # 重新查询一下正在运行的网格(因为有些网格已经关闭了)
        grid_df = query_grid(exchange)
        if not grid_df.empty:
            grid_df = grid_df[grid_df['state'] == 'running']
            running_offset_num = grid_df[grid_df['tag'].notna()].shape[0]

        # 如果账号网格不是全部由本程序开启，会造成下单金额计算出现问题，判断一下是否满足价格计算
        if offset_total < running_offset_num:
            print('当前账号运行中网格数量超过offset数量，下单金额将会超过现有网格资金，导致网格失败，程序退出')
            raise Exception('运行中网格数量超过offset配置')

        # 使用99%的资金去开仓，预留1%预防计算下单资金不足导致网格开启失败
        order_price = round(cash_balance * 0.99 / (offset_total - running_offset_num), 1)

    print('账户总资金:', total_balance, '\t账户可用资金:', cash_balance, '\t每个offset下单金额:', order_price)
    return order_price


# ========== V1 原始布网逻辑 ==========
def calc_grid_params_v1(row, price_limit, stop_limit, **kwargs):
    """
    V1 原始布网逻辑（保持原有行为不变）
    - 网格区间: min(3 * ATR_5, price_limit)
    - 终止价:   基于固定 price_limit + stop_limit（不跟随动态区间）
    - 格间距:   固定 1.4%
    - 网格数:   区间宽度 / 格间距，上限 149，无下限保护
    """
    atr_5 = row['Atr_5']
    close = row['close']
    middle_5 = row['middle_5']

    # 网格区间: 基于ATR动态调整，上限为price_limit
    range_pct_up = min(3 * atr_5, price_limit[1])
    range_pct_down = min(3 * atr_5, price_limit[0])

    high_price = close * (1 + range_pct_up)
    low_price = close * (1 - range_pct_down)

    # 终止价: 基于固定price_limit（不跟随动态区间）
    stop_high_price = close * (1 + price_limit[1]) * (1 + stop_limit)
    stop_low_price = close * (1 - price_limit[0]) * (1 - stop_limit)

    # 网格数: 固定1.4%格间距
    grid_spacing = middle_5 * 0.014
    flex_grid_count = round((high_price - low_price) / grid_spacing) if grid_spacing > 0 else 25
    grid_count = min(flex_grid_count, 149)

    return {
        'high_price': high_price,
        'low_price': low_price,
        'stop_high_price': stop_high_price,
        'stop_low_price': stop_low_price,
        'grid_count': grid_count,
    }


# ========== V2 优化布网逻辑 ==========
def calc_grid_params_v2(row, price_limit, stop_limit, v2_config, **kwargs):
    """
    V2 优化布网逻辑
    相比V1的改进:
    1. 网格区间增加下限保护(range_pct_min)，防止极低波动时区间过窄
    2. 终止价跟随实际动态区间 + 缓冲，而非固定price_limit
    3. 格间距基于ATR动态计算，而非固定1.4%
    4. 网格数有上下限保护(grid_count_min ~ grid_count_max)
    """
    atr_5 = row['Atr_5']
    close = row['close']
    middle_5 = row['middle_5']

    atr_range_mult = v2_config['atr_range_multiplier']
    range_pct_min = v2_config['range_pct_min']
    range_pct_max = v2_config['range_pct_max']
    spacing_atr_ratio = v2_config['grid_spacing_atr_ratio']
    spacing_min = v2_config['grid_spacing_min']
    spacing_max = v2_config['grid_spacing_max']
    count_min = v2_config['grid_count_min']
    count_max = v2_config['grid_count_max']
    stop_buffer = v2_config['stop_buffer_ratio']

    # ---- 1. 网格区间（带下限保护）----
    range_pct = min(max(atr_5 * atr_range_mult, range_pct_min), range_pct_max)

    high_price = close * (1 + range_pct)
    low_price = close * (1 - range_pct)

    # ---- 2. 终止价跟随动态区间 + 缓冲 ----
    stop_high_price = close * (1 + range_pct) * (1 + stop_buffer)
    stop_low_price = close * (1 - range_pct) * (1 - stop_buffer)

    # ---- 3. 格间距基于ATR动态计算 ----
    # 高波动时格间距变大（避免噪音频繁触发），低波动时格间距变小（捕捉小震荡）
    grid_spacing_ratio = min(max(atr_5 * spacing_atr_ratio, spacing_min), spacing_max)
    grid_spacing = middle_5 * grid_spacing_ratio

    # ---- 4. 网格数（带上下限保护）----
    price_range = high_price - low_price
    flex_grid_count = round(price_range / grid_spacing) if grid_spacing > 0 else count_min
    grid_count = max(count_min, min(flex_grid_count, count_max))

    return {
        'high_price': high_price,
        'low_price': low_price,
        'stop_high_price': stop_high_price,
        'stop_low_price': stop_low_price,
        'grid_count': grid_count,
    }


# ========== 格式化辅助函数 ==========
def _format_price(price, accuracy):
    """根据精度格式化价格，解决科学计数法问题"""
    return np.format_float_positional(
        round(price, accuracy), precision=accuracy, unique=False
    )


# ========== 生成发送通知信息和订单信息 ==========
def generate_order_info(df, strategy, offset, tick_size_dict):
    strategy_name = strategy['strategy_name']
    strategy_tag = strategy['strategy_tag']
    price_limit = strategy['price_limit']
    stop_limit = strategy['stop_limit']
    leverage = strategy['leverage']
    grid_version = strategy.get('grid_version', 1)
    v2_config = strategy.get('grid_v2_config', {})

    # 根据 grid_version 选择布网函数
    calc_grid_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1
    print(f'[布网逻辑] 使用版本: V{grid_version}')

    if df.shape[0] > 0:
        df = df.copy()
        df.sort_values('rank', inplace=True)
        df.set_index('symbol', inplace=True)

        # 批量生成下单数据
        for symbol, row in df.iterrows():
            tick_size = float(tick_size_dict[symbol])
            accuracy = int(math.log(float(tick_size), 0.1))

            # 调用对应版本的布网函数，获取原始价格
            params = calc_grid_fn(
                row=row,
                price_limit=price_limit,
                stop_limit=stop_limit,
                v2_config=v2_config,
            )

            # 格式化价格
            high_price = _format_price(params['high_price'], accuracy)
            low_price = _format_price(params['low_price'], accuracy)
            stop_high_price = _format_price(params['stop_high_price'], accuracy)
            stop_low_price = _format_price(params['stop_low_price'], accuracy)

            # OK的下单精度较大，使用round四舍五入，存在网格上下限与终止网格上下限价格一致的问题
            if low_price == stop_low_price:
                stop_low_price = _format_price(
                    float(stop_low_price) - math.pow(10, -accuracy), accuracy
                )
            if high_price == stop_high_price:
                stop_high_price = _format_price(
                    float(stop_high_price) + math.pow(10, -accuracy), accuracy
                )

            # 下单信息拼接到df中
            df.loc[symbol, '当前价格'] = row['close']
            df.loc[symbol, '网格上限'] = high_price
            df.loc[symbol, '网格下限'] = low_price
            df.loc[symbol, '网格终止最高价'] = stop_high_price
            df.loc[symbol, '网格终止最低价'] = stop_low_price
            df.loc[symbol, '杠杆'] = int(leverage)
            df.loc[symbol, '网格数目'] = params['grid_count']
            df.loc[symbol, '时间'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            df.loc[symbol, 'tag'] = f'{strategy_tag}{offset}'
            df.loc[symbol, '策略名称'] = strategy_name
            df.loc[symbol, 'offset'] = offset

        df.reset_index(inplace=True, drop=False)
        df = df[['symbol', '时间', '当前价格', '网格上限', '网格下限', '网格终止最高价', '网格终止最低价', '杠杆', '网格数目', 'tag', '策略名称', 'offset', 'rank'] + list(strategy['factors'].keys())]

        # 记录网格下单信息
        save_grid_info(df)

    return df


# 查询网格历史
def query_grid_his(exchange):
    try:
        params = {
            'algoOrdType': 'contract_grid',
            'instType': 'SWAP'
        }
        grid = query_grid_history(exchange, params)
        # print('网格查询完成: ', grid)
        if grid is None:
            return pd.DataFrame()

        grid_df = pd.DataFrame(grid['data'], dtype=str)
        if grid_df.empty:
            return pd.DataFrame()

        return grid_df
    except BaseException as e:
        print('查询历史网格出现错误', str(e))
        print(traceback.format_exc())


# 发送上周期网格运行盈亏情况
def send_last_grid_info(exchange, grid_ids=[]):
    """
    发送上周期网格运行盈亏情况
    :param exchange: 交易所对象
    :param grid_ids: 网格id信息，用于匹配哪些网格需要推送
    :return:
    """
    if grid_ids is None or len(grid_ids) < 1:
        print('未传网格ID参数，无法推送上周期网格盈利情况')
        return

    # 查询历史网格信息，返回最近100条，实际可能更多
    grid_his_df = query_grid_his(exchange)
    if grid_his_df.empty:
        print('当前账号从未开过网格 或 网格还没有关闭成功')
        return

    # 从止盈止损的网格中拿到algoId，查询历史网格信息
    grid_his_df = grid_his_df[grid_his_df['algoId'].isin(grid_ids)]
    if grid_his_df.empty:
        return

    # ===== 保存网格结果数据（开仓参数 + 关仓盈亏），用于布网逻辑迭代分析 =====
    try:
        save_grid_result(grid_his_df)
    except Exception as e:
        print(f'[gridResult] 保存网格结果失败: {e}')
        print(traceback.format_exc())

    # 遍历上周期网格运行盈亏情况，并推送信息
    content = '上周期网格盈亏情况\n\n'
    for index, row in grid_his_df.iterrows():
        content += f'上周期网格净值: {"%.2f" % (float(row["totalPnl"]) + float(row["sz"]))}\n'
        content += f'上周期网格持仓: {row["instId"]}\n'
        content += f'上周期网格盈亏%: {"%.2f%%" % (float(row["pnlRatio"]) * 100)}\n'
        content += f'上周期网格盈亏金额: {"%.2f" % float(row["totalPnl"])}\n'
        content += f'上周期网格策略标识: {row["tag"]}\n'
        content += '-' * 10
        content += '\n'

    send_msg_q_wechat(content)


# 查询当前已经开仓的网格信息
def query_grid(exchange):
    """
    查询当前已经开仓的网格信息
    :param exchange: 交易所对象
    :return: 当前账号中开仓的网格信息
    """
    try:
        # 获取数据
        params = {
            'algoOrdType': 'contract_grid'
        }
        grid = query_running_grid(exchange, params)
        # print('网格查询完成: ', grid)
        if grid is None:
            return pd.DataFrame()

        # 整理数据
        grid_df = pd.DataFrame(grid['data'], dtype=str)
        if grid_df.empty:
            return pd.DataFrame()
        return grid_df

    except BaseException as e:
        print('查询网格出现错误', str(e))
        print(traceback.format_exc())
        raise


# 关闭网格
def close_grid(exchange, grid_df, tag=None):
    """
    关闭网格
    :param exchange: 交易所对象
    :param grid_df: 当前账号的网格信息
    :param tag: 需要关闭网格的tag信息
    :return:
    """
    # 防止修改元数据，造成后面操作出现混乱
    grid_df = grid_df.copy()

    # 没有网格
    if grid_df.empty:
        return

    # 筛选出正在运行的网格。running：正在运行，stopping：正在关闭。详见api文档
    grid_df = grid_df[grid_df['state'] == 'running']
    if grid_df.empty:
        return

    # 过滤出当前offset下的网格信息
    if tag is not None:
        grid_df = grid_df[grid_df['tag'] == tag]

    # 逐一关闭网格
    for index, row in grid_df.iterrows():
        try:
            params = {
                'algoId': row['algoId'],
                'instId': row['instId'],
                'algoOrdType': row['algoOrdType'],
                'stopType': '1'
            }
            print('关闭网格参数:', params)
            stop_order = stop_grid(exchange, [params])
            print('关闭网格完成，关闭网格信息：', stop_order)

            if '止盈止损' in row.keys():
                msg = f"{row['instId']}网格{row['tag']}已 <{row['止盈止损']}>"
            else:
                msg = f"{row['instId']}网格{row['tag']}已停止"
            send_msg_q_wechat(msg)
        except BaseException as e:
            print('关闭网格失败, 参数:', row, '\t错误信息:', str(e))
            print(traceback.format_exc())
            send_msg_q_wechat(f"{row['instId']}网格停止失败")
            continue

    # 返回关闭网格的algoId
    return grid_df['algoId'].to_list()


# 开启网格
def open_grid(exchange, order_df):
    """
    开启网格
    :param exchange: 交易所对象
    :param order_df: 下单信息
    :return:
    """
    for index, row in order_df.iterrows():
        try:
            params = {
                'instId': row['symbol'],  # 币种
                'algoOrdType': 'contract_grid',  # 合约网格
                'maxPx': row['网格上限'],  # 最高价
                'minPx': row['网格下限'],  # 最低价
                'gridNum': row['网格数目'],  # 网格数
                'runType': '2',  # 1等差 2等比
                'sz': row['下单金额'],  # 初始保证金
                'direction': 'neutral',  # 网格类型
                'lever': row['杠杆'],  # 杠杆
                'tpTriggerPx': row['网格终止最高价'],  # 止盈触发价
                'slTriggerPx': row['网格终止最低价'],  # 止损触发价
                'tag': row['tag'],  # 订单标签  官方说，不能有特殊字符，中文
            }
            print('开启网格参数:', params)
            open_order = create_grid(exchange, params)
            print('开启网格完成，开启网格信息：', open_order)
            send_msg_q_wechat(f"{row['symbol']}网格{row['tag']}已创建")
        except BaseException as e:
            print('开启网格失败, 参数:', row, '\t错误信息:', str(e))
            print(traceback.format_exc())
            send_msg_q_wechat(f"{row['策略名称']}\n{row['symbol']}网格创建失败")
            continue


# 计算需要停止的网格
def calc_stop_grid(exchange, grid_df, df_max, strategy_config):
    """
    计算需要停止的网格
    :param exchange: 交易所对象
    :param grid_df: 正在运行中网格
    :param strategy_config: 策略配置
    :return:
    """
    # 筛选出正在运行的网格
    grid_df = grid_df[grid_df['state'] == 'running']
    if grid_df.empty:
        return pd.DataFrame(), df_max

    # 计算需要监控的策略tags
    tags = [f'{ strategy_config["strategy_tag"]}{_}' for _ in strategy_config['offset']]

    # 筛选出属于当前策略的网格
    result_df = grid_df[grid_df['tag'].isin(tags)]
    if result_df.empty:
        return pd.DataFrame(), df_max

    # 更新最大收益率
    result_df, df_max = update_pnlRatio(result_df, df_max)

    # 计算止盈止损
    result_df = calc_loss_or_profit(exchange, result_df, strategy_config['stop_loss_config'])

    return result_df, df_max


# 发送网格下单信息
def send_order_info(order_df, strategy_config):
    """
    发送网格下单信息
    :param order_df: 下单的网格信息
    :return:
    """
    content = ''
    order_df = order_df.copy()
    order_df.sort_values('rank', inplace=True)
    _df = order_df[['symbol', 'rank'] + list(strategy_config['factors'].keys())]

    content += str(_df.to_markdown(index=False))
    content += '\n' * 2

    content += "当前下单信息\n\n"
    for name, group in order_df.groupby('策略名称'):
        content += f"策略名称： {name}\n"
        for index, row in group.iterrows():
            content += f"网格上限：{row['网格上限']} \n"
            content += f"网格下限：{row['网格下限']} \n"
            content += f"网格终止最高价：{row['网格终止最高价']} \n"
            content += f"网格终止最低价：{row['网格终止最低价']} \n"
            content += f"网格数目：{row['网格数目']} \n"
            content += f"杠杆：{row['杠杆']} \n"
            content += f"币种：{row['symbol']} \n"
            content += f"offset：{row['offset']} \n"
            content += f"下单金额：{row['下单金额']} \n\n"
            content += '-' * 10
            content += '\n'

    send_msg_q_wechat(content)


# # 保存下单信息
# def save_order_info(all_order_df):
#     """
#     保存下单信息
#     :param all_order_df: 需要保存的下单信息
#     :return:
#     """
#     file_path = os.path.join(order_path, 'orderInfo.pkl')
#     order_df = pd.read_pickle(file_path) if os.path.exists(file_path) else pd.DataFrame()
#     # 空df不需要操作
#     if not order_df.empty:
#         # 过滤掉之前开仓的tag数据
#         order_df = order_df[~order_df['tag'].isin(all_order_df['tag'])]
#     # 将最新的开仓数据合并起来
#     order_df = pd.concat([order_df, all_order_df], ignore_index=True)
#     order_df.reset_index(drop=True, inplace=True)
#     order_df.to_pickle(file_path)

def save_order_info(all_order_df, offset_order_tag):
    """
    保存下单信息
    :param all_order_df: 需要保存的下单信息
    :return:
    """
    file_path = os.path.join(order_path, 'orderInfo.pkl')
    order_df = pd.read_pickle(file_path) if os.path.exists(file_path) else pd.DataFrame()
    # 文件为空df不需要操作
    if not order_df.empty:
        # ==文件不为空
        # 过滤掉之前开仓的tag数据（给order_df过滤掉当前这个tag的旧记录，方便拼入新纪录）
        if all_order_df.empty:
            # ==没有下单数据，用tag过滤
            order_df = order_df[order_df['tag'] != offset_order_tag]
        else:
            # ==有下单数据，用 all_order_df['tag'] 进行过滤
            order_df = order_df[~order_df['tag'].isin(all_order_df['tag'])]

    # 将最新的开仓数据合并起来
    order_df = pd.concat([order_df, all_order_df], ignore_index=True)
    order_df.reset_index(drop=True, inplace=True)
    order_df.to_pickle(file_path)


# 记录网格开网信息，用于数据分析
def save_grid_info(df):
    """
    保存下单信息到CSV文件（追加模式）
    :param df: 需要保存的DataFrame
    """
    # 确保目录存在
    file_path = os.path.join(order_path, 'gridInfo.csv')
    
    # 处理首次保存：添加表头
    if not os.path.exists(file_path):
        df.to_csv(file_path, index=False)
        return
    
    # 追加模式写入（无表头/索引）
    df.to_csv(file_path, mode='a', header=False, index=False)


# ========== 记录网格关仓结果（开仓参数 + 关仓盈亏），用于布网逻辑迭代分析 ==========
# OKX 止盈止损类型映射
STOP_TYPE_MAP = {
    '1': '手动停止',
    '2': '止盈触发',
    '3': '止损触发',
}


def save_grid_result(grid_his_df):
    """
    将关仓网格的 OKX 结果数据 与 开仓时保存的 orderInfo 合并，写入 gridResult.csv
    :param grid_his_df: OKX 历史网格 DataFrame（已过滤出本次关仓的 algoId）
    """
    if grid_his_df is None or grid_his_df.empty:
        return

    # 读取开仓时保存的下单信息
    order_file_path = os.path.join(order_path, 'orderInfo.pkl')
    order_df = pd.read_pickle(order_file_path) if os.path.exists(order_file_path) else pd.DataFrame()

    result_rows = []
    for _, row in grid_his_df.iterrows():
        tag = row.get('tag', '')
        instId = row.get('instId', '')

        # ---- 关仓结果字段 ----
        total_pnl = float(row.get('totalPnl', 0))
        pnl_ratio = float(row.get('pnlRatio', 0))
        sz = float(row.get('sz', 0))
        algo_id = row.get('algoId', '')
        stop_type = STOP_TYPE_MAP.get(str(row.get('stopType', '')), str(row.get('stopType', '')))

        # 时间处理（OKX 返回毫秒时间戳）
        c_time_raw = row.get('cTime', '')
        trigger_time_raw = row.get('triggerTime', '')
        try:
            open_time = pd.to_datetime(int(c_time_raw), unit='ms') + pd.Timedelta(hours=8) if c_time_raw else pd.NaT
        except (ValueError, TypeError):
            open_time = pd.NaT
        try:
            close_time = pd.to_datetime(int(trigger_time_raw), unit='ms') + pd.Timedelta(hours=8) if trigger_time_raw else pd.NaT
        except (ValueError, TypeError):
            close_time = pd.NaT

        # 持仓时长（小时）
        duration_hours = ''
        if open_time is not pd.NaT and close_time is not pd.NaT:
            duration_hours = round((close_time - open_time).total_seconds() / 3600, 2)

        # ---- 开仓参数字段（从 orderInfo.pkl 中匹配） ----
        open_params = {}
        if not order_df.empty and 'tag' in order_df.columns:
            # 先按 tag 匹配，再按 symbol 匹配
            matched = order_df[(order_df['tag'] == tag) & (order_df['symbol'] == instId)]
            if not matched.empty:
                m = matched.iloc[0]
                open_params = {
                    '开仓时间': m.get('时间', ''),
                    '开仓价格': m.get('当前价格', ''),
                    'ATR_5': m.get('Atr_5', ''),
                    '网格上限': m.get('网格上限', ''),
                    '网格下限': m.get('网格下限', ''),
                    '网格终止最高价': m.get('网格终止最高价', ''),
                    '网格终止最低价': m.get('网格终止最低价', ''),
                    '网格数目': m.get('网格数目', ''),
                    '杠杆': m.get('杠杆', ''),
                    '下单金额': m.get('下单金额', ''),
                    'offset': m.get('offset', ''),
                    '策略名称': m.get('策略名称', ''),
                }

        result_row = {
            '币种': instId,
            'tag': tag,
            'algoId': algo_id,
            '关仓时间': close_time.strftime('%Y-%m-%d %H:%M:%S') if close_time is not pd.NaT else '',
            '开仓时间_okx': open_time.strftime('%Y-%m-%d %H:%M:%S') if open_time is not pd.NaT else '',
            '持仓时长_小时': duration_hours,
            '初始保证金': sz,
            '盈亏金额': total_pnl,
            '盈亏比例': pnl_ratio,
            '净值': sz + total_pnl,
            '关闭原因': stop_type,
            '是否盈利': 1 if total_pnl > 0 else 0,
        }
        result_row.update(open_params)
        result_rows.append(result_row)

    if not result_rows:
        return

    result_df = pd.DataFrame(result_rows)
    file_path = os.path.join(order_path, 'gridResult.csv')

    # 首次写入带表头，后续追加
    if not os.path.exists(file_path):
        result_df.to_csv(file_path, index=False, encoding='utf-8-sig')
    else:
        result_df.to_csv(file_path, mode='a', header=False, index=False, encoding='utf-8-sig')

    print(f'[gridResult] 已记录 {len(result_rows)} 条网格结果到 gridResult.csv')


# 更新最大收益率
def update_pnlRatio(grid_df, df_max):
    # 遍历 grid_df 的每一行
    for index, row in grid_df.iterrows():
        algoId = row['algoId']
        current_instId = row['instId']
        current_tag = row['tag']
        current_pnlRatio = float(row['pnlRatio'])

        # 查找匹配记录 
        condition = (df_max['algoId'] == algoId) & (df_max['instId'] == current_instId)
        matched_rows = df_max[condition]

        if not matched_rows.empty:
            # 比较 pnlRatio 大小并更新 
            max_pnl_ratio = float(matched_rows['pnlRatio'].max())
            if current_pnlRatio > max_pnl_ratio:
                df_max.loc[matched_rows.index, 'pnlRatio'] = current_pnlRatio
                # 更新 max_pnl_ratio 为最新值
                max_pnl_ratio = current_pnlRatio
        else:
            # 新增记录 
            new_row = {
                'algoId': algoId,
                'instId': current_instId,
                'tag': current_tag,
                'pnlRatio': current_pnlRatio
            }
            df_max = df_max.append(new_row, ignore_index=True)
            # 设置 max_pnl_ratio 为当前值
            max_pnl_ratio = current_pnlRatio

        # 更新 grid_df 中的 pnlRatio_max 列
        grid_df.loc[index, 'pnlRatio_max'] = max_pnl_ratio

    return grid_df, df_max