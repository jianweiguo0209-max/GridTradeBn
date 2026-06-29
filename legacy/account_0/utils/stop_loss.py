import os
import time
from datetime import datetime
from api.kline import fetch_ok_swap_candle_data
from utils.tools import get_before_period_time, retry_wrapper
from config import kline_path
import numpy as np
import pandas as pd
import talib as ta



def calc_loss_or_profit(exchange=None, total_df=None, stop_loss_info=None):
    """
    计算止盈止损
    :param exchange: 交易所对象
    :param total_df: 网格信息
    :param stop_loss_info: 止盈止损配置信息
    :return:
    """
    total_df['止盈止损'] = np.nan

    # 固定比例止损
    total_df = fixed_loss_and_profit(total_df, stop_loss_info)
    # 这里触发固定比列止损直接返回，减少后面的止损计算
    if not total_df[total_df['止盈止损'].notna()].empty:
        print('存在固定比例止损，不检测后续止损······')
        return total_df

    # 资金费率止损
    total_df = funding_rate_loss_and_profit(exchange, total_df, stop_loss_info)

    # 主动止盈止损
    total_df = activate_loss(exchange, total_df, stop_loss_info)
    return total_df


# 固定比例止损止盈
def fixed_loss_and_profit(total_df, stop_loss_info):
    data = total_df.copy()
    data['pnlRatio'] = data['pnlRatio'].astype(float)
    data['pnlRatio_max'] = data['pnlRatio_max'].astype(float)

    # 固定止损
    data.loc[data['pnlRatio'] < -stop_loss_info['stop_loss'], '止盈止损'] = '固定止损'

    # Chandelier 连续回撤止盈
    # allowed_drawback = max(地板, k × 峰值利润)
    # 盈利越高容忍越大，让利润奔跑
    k = stop_loss_info['trailing_k']
    floor = stop_loss_info['trailing_floor']
    allowed_drawback = data['pnlRatio_max'].apply(lambda x: max(floor, k * x))
    actual_drawback = data['pnlRatio_max'] - data['pnlRatio']

    # 触发条件：回撤超过允许值 且 峰值利润 > 地板（避免亏损时误触发）
    trigger = (actual_drawback >= allowed_drawback) & (data['pnlRatio_max'] > floor)
    data.loc[trigger, '止盈止损'] = '连续回撤止盈'

    return data


# 资金费率止损
def funding_rate_loss_and_profit(exchange, total_df, stop_loss_info):
    data = total_df.copy()
    data.reset_index(inplace=True, drop=True)

    # 循环获取 & 检查资金费率
    for index, row in data.iterrows():
        symbol = row['instId']
        request_json = retry_wrapper(func=exchange.public_get_public_funding_rate, params={'instId': symbol}, act_name='ok获取资金费率')
        funding_rate = float(request_json['data'][0]['fundingRate'])

        # print(funding_rate)
        if funding_rate > stop_loss_info['fundingRate_stop_loss'] or funding_rate < -stop_loss_info['fundingRate_stop_loss']:
            data.loc[index, '止盈止损'] = (
                '资金费率止盈' if float(row['pnlRatio']) > 0 else
                '资金费率止损' if float(row['pnlRatio']) < 0 else
                '资金费率关网'
            )
        time.sleep(0.5)

    return data


# 主动止损
def activate_loss(exchange, total_df, stop_loss_info):
    """
    这里使用放量主动止损来作为案例
    """
    # 遍历目前网格持仓的币种
    data = total_df.copy()
    data.reset_index(inplace=True, drop=True)
    for index, row in data.iterrows():
        # 加载币种数据
        df = load_data(exchange, row, index, stop_loss_info)
        # 计算主动止盈止损信号
        # signal1 = calc_active_loss_signal_ob(df)  # 波动率趋势轨道
        # signal2 = calc_active_loss_signal_3red(df)  # 连续3根阴线，主动止损
        # signal3 = calc_active_loss_signal_ema(df)  # 均线上穿，并且价格偏离度高

        # 备选：
        signal = calc_active_loss_signal_pv(df, float(row['pnlRatio']))  # 短期成交量爆增止损
        # signal = calc_active_loss_signal_rsi(df)  # deepseek 方案3 RSI主动止损

        if signal == 1:
            data.loc[index, '止盈止损'] =(
                'pv主动止盈' if float(row['pnlRatio']) > 0 else
                'pv主动止损' if float(row['pnlRatio']) < 0 else
                'pv主动关网'
            )
        # if signal2 == 1:
        #     data.loc[index, '止盈止损'] = (
        #         '3red主动止盈' if float(row['pnlRatio']) > 0 else
        #         '3red主动止损' if float(row['pnlRatio']) < 0 else
        #         '3red主动关网'
        #     )
        # elif signal3 == 1:
        #     data.loc[index, '止盈止损'] = (
        #         'ema主动止盈' if float(row['pnlRatio']) > 0 else
        #         'ema主动止损' if float(row['pnlRatio']) < 0 else
        #         'ema主动关网'
        #     )

    return data


# 计算主动止损的因子
def calc_active_loss_signal_3red(df):
    # 判断是否为阴线
    df['is_red'] = df['close'] < df['open']

    # 计算K线实体部分
    df['body_size'] = abs(df['open'] - df['close'])

    # 方案一：total_size = 整个k线
    # 计算整个K线的长度（最高价 - 最低价）
    # df['total_size'] = df['high'] - df['low']

    # 方案二：total_size = 实体+下影线
    # 计算下影线长度（阴线下影线 = close - low）
    df['lower_shadow'] = df['close'] - df['low']
    # 计算分母（实体+下影线）
    df['total_size'] = df['body_size'] + df['lower_shadow']

    # 计算实体部分占比
    df['body_ratio'] = df['body_size'] / df['total_size']

    # 统计连续阴线且实体比例大于50%的数量
    df['consecutive_red_count'] = ((df['is_red'] == True) & (df['body_ratio'] > 0.5)).astype(int).rolling(
        window=3).sum()

    # 计算10周期移动平均线
    df['ma10'] = df['close'].rolling(window=10).mean()
    # 判断移动平均线是否向下
    df['ma10_down'] = df['ma10'] < df['ma10'].shift(1)

    df['signal'] = 0
    # 设定触发止损的条件
    condition = (df['consecutive_red_count'] == 3) & (df['high'] < df['ma10']) & (
                df['high'].shift(1) < df['ma10'].shift(1)) & (df['high'].shift(2) < df['ma10'].shift(2)) & df[
                    'ma10_down']

    df.loc[condition, 'signal'] = 1
    df['signal'].fillna(method='ffill', inplace=True)

    return df.iloc[-1]['signal']


def calc_active_loss_signal_ema(df):
    # Factor3: 持续上涨趋势检测（EMA12上穿EMA36且偏离度>5%）
    df['ema12'] = ta.EMA(df['close'], timeperiod=12)
    df['ema36'] = ta.EMA(df['close'], timeperiod=36)
    trend_deviation = (df['close'] - df['ema36']) / df['ema36']
    # df['a'] = (df['close'] - df['ema36']) / df['ema36']

    # RSI背离检测（价格新高但RSI未新高）
    df['rsi'] = ta.RSI(df['close'], 14)
    df['price_high'] = df['close'] == df['close'].rolling(24).max()
    df['rsi_high'] = df['rsi'] == df['rsi'].rolling(24).max()
    df['divergence'] = df['price_high'] & ~df['rsi_high']

    df['signal'] = 0
    # 设定触发止损的条件
    condition = (df['ema12'] > df['ema36']) & (trend_deviation > 0.05) & ~df['divergence']

    df.loc[condition, 'signal'] = 1
    df['signal'].fillna(method='ffill', inplace=True)

    # GJW：用于测试
    # print("测试币：" + str(df.iloc[-1]['symbol']))
    # print("ema12："+str(df.iloc[-1]['ema12']))
    # print("ema36："+str(df.iloc[-1]['ema36']))
    # print("偏离度："+str(df.iloc[-1]['a']))
    # print("顶背离："+str(df.iloc[-1]['divergence']))

    return df.iloc[-1]['signal']


def calc_active_loss_signal_rsi(df):
    """
    通过RSI背离识别动量衰竭
    :param df: 包含ohlcv的DataFrame
    :return: 止损信号
    """
    # 计算RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / (avg_loss + 1e-8)
    df['RSI'] = 100 - (100 / (1 + rs))

    # 背离检测
    price_high = df['close'].rolling(5).max()
    rsi_high = df['RSI'].rolling(5).max()
    bear_divergence = (price_high.diff() > 0) & (rsi_high.diff() < 0)

    price_low = df['close'].rolling(5).min()
    rsi_low = df['RSI'].rolling(5).min()
    bull_divergence = (price_low.diff() < 0) & (rsi_low.diff() > 0)

    # 成交量确认
    # vol_confirm = df['volCcy'] > df['volCcy'].rolling(50).mean() * 2
    vol_confirm = df['volCcy'] > df['volCcy'].rolling(50).quantile(0.95)

    df['signal'] = np.where((bear_divergence | bull_divergence) & vol_confirm, 1, 0)
    df['signal'].ffill(inplace=True)

    return df.iloc[-1]['signal']


# 计算主动止损的因子
def calc_active_loss_signal_pv(df, pnlRatio):
    """
    计算主动止损的信号，基于瞬间爆量因子过滤
    :param df: 原始币种的1m全量数据，需要全量数据来计算，这样更加准确
    :return:
    """
    df['last_period_mean'] = df['quote_volume'].rolling(233).mean()
    df.fillna(method='ffill', inplace=True)

    # 计算止损信号
    df['signal'] = 0
    con1 = df['quote_volume'] > df['last_period_mean'] * 3
    con2 = (abs(df['close'] - df['open']) / (df['high'] - df['low'] + 1e-8)) > 0.1
    con3 = (pnlRatio < -0.015)
    condition = (con1 & con3)

    df.loc[condition, 'signal'] = 1
    df['signal'].fillna(method='ffill', inplace=True)

    # GJW：用于测试
    # print("测试币：" + str(df.iloc[-1]['symbol']))
    # print("测试币成交额："+str(df.iloc[-1]['quote_volume']))
    # print("测试币233成交额均值："+str(df.iloc[-1]['last_period_mean']))
    # print("测试币成交额比值："+str(df.iloc[-1]['quote_volume']/df.iloc[-1]['last_period_mean']))

    return df.iloc[-1]['signal']


# 计算主动止损的因子
def calc_active_loss_signal_kc(df):
    """
    计算主动止损的信号
    :param df: 原始币种的1m全量数据，需要全量数据来计算，这样更加准确
    :return:
    """
    # 计算主动止损因子
    tmp1_s = df['high'] - df['low']
    tmp2_s = (df['high'] - df['close'].shift(1)).abs()
    tmp3_s = (df['low'] - df['close'].shift(1)).abs()
    tr = np.max(np.array([tmp1_s, tmp2_s, tmp3_s]), axis=0)  # 三个数列取其大值
    atr = pd.Series(tr).rolling(233, min_periods=1).mean()
    middle = df['close'].ewm(span=233, adjust=False, min_periods=1).mean()
    df[f'kc_{233}'] = (df['close'] - middle + 2 * atr) / (4 * atr)
    df[f'kc_upper_{233}'] = middle + 2 * atr
    df[f'kc_lower_{233}'] = middle - 2 * atr

    df['quote_volume'] = abs(df['open'] + df['close']) / 2.0 * df['volCcy']  # 添加成交量
    df['last_period_mean_233'] = df['quote_volume'].rolling(233).mean()
    df.fillna(method='ffill', inplace=True)

    # 计算止损信号
    df['signal'] = 0
    condition1 = df['quote_volume'] > 2 * df['last_period_mean_233']
    con2 = (abs(df['close'] - df['open']) / (df['high'] - df['low'] + 1e-8)) > 0.1
    condition2 = df['close'] > df['kc_upper_233']
    condition3 = df['close'] < df['kc_lower_233']
    condition = (condition1 & con2) & (condition2 | condition3)
    df.loc[condition, 'signal'] = 1

    df['signal'].fillna(method='ffill', inplace=True)

    return df.iloc[-1]['signal']


# 计算主动止损的因子
def calc_active_loss_signal_ob(df):
    """
    计算主动止损的信号
    :param df: 原始币种的1m全量数据，需要全量数据来计算，这样更加准确
    :return:
    """
    # 计算主动止损因子
    for n in [55, 144]:
        df[f'BbwOri_{n}'] = df['close'].rolling(n).std(ddof=0) / df['close'].rolling(n, min_periods=1).mean()
        df[f'BbwOri_std_{n}'] = df[f'BbwOri_{n}'].rolling(n).std(ddof=0)
        df[f'BbwOri_mean_{n}'] = df[f'BbwOri_{n}'].rolling(n, min_periods=1).mean()

    df['last_period_mean'] = df['quote_volume'].rolling(55).mean()
    df.fillna(method='ffill', inplace=True)

    # 计算止损信号
    df['signal'] = 0
    condition0 = df['quote_volume'] > 1.5 * df['last_period_mean']
    condition2 = df[f'BbwOri_55'] > df['BbwOri_mean_55'] + 1.2 * df['BbwOri_std_55']
    condition3 = df[f'BbwOri_144'] < df['BbwOri_mean_144'] - 4 * df['BbwOri_std_144']
    condition = ((condition0 & condition2) | condition3)

    df.loc[condition, 'signal'] = 1
    df['signal'].fillna(method='ffill', inplace=True)

    return df.iloc[-1]['signal']


# 加载止损所需要的数据
def load_data_old(exchange, row, index, stop_loss_info):
    # 获取当前服务器所在时区
    utc_offset = int(time.localtime().tm_gmtoff / 60 / 60)
    # 获取当前运行时间，并多 +1 秒
    run_time = datetime.now().replace(second=1, microsecond=0)
    symbol = row['instId']
    # 拼接出当前监测的文件名称
    filename = f'{row["tag"]}_{index}_loss_data_{stop_loss_info["active_loss_period"]}.pkl'
    local_file_path = os.path.join(kline_path, filename)
    # 判断文件是否存在
    if os.path.exists(local_file_path):  # 若文件存在，直接读取
        try:
            df = pd.read_pickle(local_file_path)
        except:
            # 解决 ran out of input 错误
            df = pd.DataFrame()
    else:  # 若文件不存在，重新请求k线数据
        time.sleep(1)
        df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=stop_loss_info["active_loss_candle_num"],
                                       interval=stop_loss_info['active_loss_period'])[1]

    if df is None or df.empty:
        raise Exception('获取K线数据为空，请检查接口或者查看网络')
    # 过滤，只保留当前持仓的币种数据
    df = df[df['symbol'] == symbol]

    # 判断1：当前存储的数据不足配置的最小所需k线数量，需要重新全量获取
    condition1 = df.shape[0] < stop_loss_info['active_loss_candle_num'] - 1  # 获取比配置少一根（因为会包含一根未走完的k线）
    if condition1:
        time.sleep(1)
        limit = stop_loss_info['active_loss_candle_num']
        df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=limit,
                                       interval=stop_loss_info['active_loss_period'])[1]

    # 获取距离当前时间最近的上一个周期时间
    before_period_time = get_before_period_time(stop_loss_info['active_loss_period'])
    # 判断2:上一个获取K线的周期时间，大于当前存储k线的最后一根k线时间，补充一下新数据
    condition2 = before_period_time - pd.Timedelta(hours=utc_offset) > df.iloc[-1]['candle_begin_time']
    if condition2:
        time.sleep(1)
        # 当前时间与存储的数据最新时间，差多少根k线
        loss_candle_num = int(
            (run_time - pd.Timedelta(hours=utc_offset) - df.iloc[-1]['candle_begin_time']) / pd.to_timedelta(
                stop_loss_info['active_loss_period']))
        limit = max(loss_candle_num, stop_loss_info['active_loss_candle_num'])
        increment_df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=limit,
                                                 interval=stop_loss_info['active_loss_period'])[1]
        df = pd.concat([df, increment_df], ignore_index=True)

    # 判断最后一根k线是否走完，未走完就删除
    if pd.to_timedelta(0) < run_time - pd.Timedelta(hours=utc_offset) - df.iloc[-1][
        'candle_begin_time'] < pd.to_timedelta(stop_loss_info['active_loss_period']):
        df = df[:-1]

    df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
    df.sort_values('candle_begin_time', inplace=True)
    df.reset_index(inplace=True, drop=True)
    df.to_pickle(local_file_path)

    return df


# 加载止损所需要的数据
def load_data(exchange, row, index, stop_loss_info):
    # 获取当前运行时间，并多 +1 秒
    run_time = datetime.now().replace(second=1, microsecond=0)
    symbol = row['instId']

    # 拼接出当前监测的文件名称
    filename = f'{row["tag"]}_{index}_loss_data_{stop_loss_info["active_loss_period"]}.pkl'
    local_file_path = os.path.join(kline_path, filename)

    first_flag = False
    # 判断文件是否存在
    if os.path.exists(local_file_path):  # 若文件存在，直接读取
        try:
            df = pd.read_pickle(local_file_path)
        except:
            # 解决 ran out of input 错误
            df = pd.DataFrame()
    else:  # 若文件不存在，重新请求k线数据
        first_flag = True
        time.sleep(1)
        df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=stop_loss_info["active_loss_candle_num"],
                                       interval=stop_loss_info['active_loss_period'])[1]

        # GJW：首次获取K线时，需要删除最后一根未走完的K线，防止刚生成的新K线影响止损
        if pd.to_timedelta(0) < run_time - pd.to_timedelta('8h') - df.iloc[-1]['candle_begin_time'] < pd.to_timedelta(
                stop_loss_info['active_loss_period']):
            df = df[:-1]

    if df is None or df.empty:
        raise Exception('获取K线数据为空，请检查接口或者查看网络')
    # 过滤，只保留当前持仓的币种数据
    df = df[df['symbol'] == symbol]

    # 判断1：当前存储的数据不足配置的最小所需k线数量，需要重新全量获取
    condition1 = df.shape[0] < stop_loss_info['active_loss_candle_num']  # 获取比配置少一根（因为会包含一根未走完的k线）
    if condition1:
        time.sleep(1)
        # print("*******")
        # print("panduan 1")
        df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=stop_loss_info['active_loss_candle_num'],
                                       interval=stop_loss_info['active_loss_period'])[1]

    # 存储数据都是UTC时间，这里先转成UTC时间
    # 当前时间与存储的数据最新时间，差多少根k线
    loss_candle_num = int((run_time - pd.to_timedelta('8h') - df.iloc[-1]['candle_begin_time']) / pd.to_timedelta(
        stop_loss_info['active_loss_period']))
    # 判断2：判断一下缺失的k线数量是否超过每次获取的k数量，超过了就需要额外的补充一下
    condition2 = loss_candle_num > stop_loss_info['every_times_candle_num']
    if condition2:
        time.sleep(1)
        # print("*******")
        # print("panduan 2")
        limit = min(loss_candle_num, stop_loss_info['active_loss_candle_num'])
        increment_df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=limit,
                                                 interval=stop_loss_info['active_loss_period'])[1]
        df = pd.concat([df, increment_df], ignore_index=True)

    # 存储数据都是UTC时间，这里先转成UTC时间
    # 判断3：判断一下是否需要达到需要下载新数据的时间，从而进行补充最新数据
    # GJW：由于删掉了最后一根没走完的K线，所以这里是否更新，需要和30分钟比较，而不是和15分钟比较
    condition3 = run_time - pd.to_timedelta('8h') - df.iloc[-1]['candle_begin_time'] > 2 * pd.to_timedelta(
        stop_loss_info['active_loss_period'])
    if not first_flag:  # GJW:持续更新最新一条K线，以便通过最新一根K线做爆量止盈止损，其他止损则用condition3到时间才更新
        # if condition3:
        time.sleep(1)
        # print("*******")
        # print("panduan 3")
        limit = stop_loss_info['every_times_candle_num']
        increment_df = fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=limit,
                                                 interval=stop_loss_info['active_loss_period'])[1]
        df = pd.concat([df, increment_df], ignore_index=True)

    # 判断最后一根k线是否走完，未走完就删除
    # GJW:爆量主动止盈止损，希望实时性更高，所以不删除未走完的K线，会注销这里，只有首次获取数据才删除最后一根；
    # GJW:其他止损方案则希望删除未走完的K线，用完整K线做计算，所以会放开这里的注释
    # if pd.to_timedelta(0) < run_time - pd.to_timedelta('8h') - df.iloc[-1]['candle_begin_time'] < pd.to_timedelta(stop_loss_info['active_loss_period']):
    #     df = df[:-1]

    df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
    df.sort_values('candle_begin_time', inplace=True)
    df.reset_index(inplace=True, drop=True)
    df.to_pickle(local_file_path)

    return df
