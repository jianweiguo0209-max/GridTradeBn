import talib as ta
import numpy as np
import pandas as pd
eps = 1e-8


def db_volume_v1_signal(*args):
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    # 计算成交量的滚动标准差
    df['volume_std'] = df['volCcy'].rolling(n).std()

    # 计算成交量的滚动均值
    df['volume_mean'] = df['volCcy'].rolling(n).mean()

    # 活跃度因子：标准差与均值的比值
    df[factor_name] = df['volume_std'] / (df['volume_mean'] + 1e-8)

    # 归一化处理
    df[factor_name] = (df[factor_name] - df[factor_name].mean()) / df[factor_name].std()

    del df['volume_std']
    del df['volume_mean']

    return df

def Erbull_signal(*args):
    """
    N=20
    BullPower=HIGH-EMA(CLOSE,N)
    BearPower=LOW-EMA(CLOSE,N)
    ER 为动量指标。用来衡量市场的多空力量对比。在多头市场，人们
    会更贪婪地在接近高价的地方买入，BullPower 越高则当前多头力量
    越强；而在空头市场，人们可能因为恐惧而在接近低价的地方卖出。
    BearPower 越低则当前空头力量越强。当两者都大于 0 时，反映当前
    多头力量占据主导地位；两者都小于0则反映空头力量占据主导地位。
    如果 BearPower 上穿 0，则产生买入信号；
    如果 BullPower 下穿 0，则产生卖出信号。
    """
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    ema = df['close'].ewm(n, adjust=False).mean()  # EMA(CLOSE,N)
    bull_power = df['high'] - ema  # 越高表示上涨 牛市 BullPower=HIGH-EMA(CLOSE,N)
    bear_power = df['low'] - ema  # 越低表示下降越厉害  熊市 BearPower=LOW-EMA(CLOSE,N)
    df[factor_name] = bull_power / (ema + eps)  # 去量纲

    return df

def Vwapbias_signal(*args):
    # WVAD 指标
    """
    将bias 的close替换成vwap


    N=20
    WVAD=SUM(((CLOSE-OPEN)/(HIGH-LOW)*VOLUME),N)
    WVAD 是用价格信息对成交量加权的价量指标，用来比较开盘到收盘
    期间多空双方的力量。WVAD 的构造与 CMF 类似，但是 CMF 的权
    值用的是 CLV(反映收盘价在最高价、最低价之间的位置)，而 WVAD
    用的是收盘价与开盘价的距离（即蜡烛图的实体部分的长度）占最高
    价与最低价的距离的比例，且没有再除以成交量之和。
    WVAD 上穿 0 线，代表买方力量强；
    WVAD 下穿 0 线，代表卖方力量强。

    """
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['vwap'] = df['quote_volume'] / df['volCcy']  # 在周期内成交额除以成交量等于成交均价
    ma = df['vwap'].rolling(n, min_periods=1).mean()  # 求移动平均线
    df[factor_name] = df['vwap'] / (ma + eps) - 1  # 去量纲

    del df['vwap']

    return df

def Er_signal(*args):
    # Er 指标（震荡度因子）
    # 输出 |BullPower + BearPower|，值越小越震荡，适合 ascending=True 排序
    # 值大 → 单边趋势（多头或空头主导），值小 → 多空均衡震荡
    df = args[0]
    n  = args[1]
    diff_num = args[2]
    factor_name = args[3]

    a = 2 / (n + 1)
    df['ema'] = df['close'].ewm(alpha=a, adjust=False).mean()
    df['BullPower'] = (df['high'] - df['ema']) / df['ema']
    df['BearPower'] = (df['low'] - df['ema']) / df['ema']
    df[factor_name] = (df['BullPower'] + df['BearPower']).abs()

    # 删除多余列
    del df['ema'], df['BullPower'], df['BearPower']

    return df

def Erbear_signal(*args):
    """
    N=20
    BullPower=HIGH-EMA(CLOSE,N)
    BearPower=LOW-EMA(CLOSE,N)
    ER 为动量指标。用来衡量市场的多空力量对比。在多头市场，人们
    会更贪婪地在接近高价的地方买入，BullPower 越高则当前多头力量
    越强；而在空头市场，人们可能因为恐惧而在接近低价的地方卖出。
    BearPower 越低则当前空头力量越强。当两者都大于 0 时，反映当前
    多头力量占据主导地位；两者都小于0则反映空头力量占据主导地位。
    如果 BearPower 上穿 0，则产生买入信号；
    如果 BullPower 下穿 0，则产生卖出信号。
    """
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    ema = df['close'].ewm(n, adjust=False).mean()  # EMA(CLOSE,N)
    bull_power = df['high'] - ema  # 越高表示上涨 牛市 BullPower=HIGH-EMA(CLOSE,N)
    bear_power = df['low'] - ema  # 越低表示下降越厉害  熊市 BearPower=LOW-EMA(CLOSE,N)
    df[factor_name] = bear_power / (ema + eps)  # 去量纲

    return df

def Bbi_signal(*args):
    # Bbi
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]
    
    """
    BBI=(MA(CLOSE,3)+MA(CLOSE,6)+MA(CLOSE,12)+MA(CLOSE,24))/4
    BBI 是对不同时间长度的移动平均线取平均，能够综合不同移动平均
    线的平滑性和滞后性。如果收盘价上穿/下穿 BBI 则产生买入/卖出信
    号。
    """
    # 将BBI指标计算出来求bias
    ma1 = df['close'].rolling(n, min_periods=1).mean()
    ma2 = df['close'].rolling(2 * n, min_periods=1).mean()
    ma3 = df['close'].rolling(4 * n, min_periods=1).mean()
    ma4 = df['close'].rolling(8 * n, min_periods=1).mean()
    # BBI=(MA(CLOSE,3)+MA(CLOSE,6)+MA(CLOSE,12)+MA(CLOSE,24))/4
    bbi = (ma1 + ma2 + ma3 + ma4) / 4
    df[factor_name] = bbi / df['close']

    return df

def Dbcd_signal(*args):
    # PMO 指标
    """

    """
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['ma'] = df['close'].rolling(n, min_periods=1).mean()
    df['BIAS'] = (df['close'] - df['ma']) / df['ma'] * 100
    df['BIAS_DIF'] = df['BIAS'] - df['BIAS'].shift(3 * n)
    df[factor_name] = df['BIAS_DIF'].rolling(3 * n + 2, min_periods=1).mean()

    del df['ma']
    del df['BIAS']
    del df['BIAS_DIF']

    return df

def Wad_signal(*args):
    df = args[0]
    n  = args[1]
    diff_num = args[2]
    factor_name = args[3]

    #  WAD 指标
    """
    TRH=MAX(HIGH,REF(CLOSE,1))
    TRL=MIN(LOW,REF(CLOSE,1))
    AD=IF(CLOSE>REF(CLOSE,1),CLOSE-TRL,CLOSE-TRH) 
    AD=IF(CLOSE>REF(CLOSE,1),0,CLOSE-REF(CLOSE,1))  # 该指标怀疑有误
    WAD=CUMSUM(AD)
    N=20
    WADMA=MA(WAD,N)
    我们用 WAD 上穿/下穿其均线来产生买入/卖出信号。
    """
    # print(df)

    df['ref_close'] = df['close'].shift(1)
    df['TRH'] = df[['high', 'ref_close']].max(axis=1)
    df['TRL'] = df[['low', 'ref_close']].min(axis=1)
    df['AD'] = np.where(df['close'] > df['close'].shift(1), df['close'] - df['TRL'], df['close'] - df['TRH'])
    df['AD'] = np.where(df['close'] > df['close'].shift(1), 0, df['close'] - df['close'].shift(1))
    # df['WAD'] = df['AD'].cumsum()
    df['WAD'] = df['AD'].rolling(window=9, min_periods=1, closed='right').sum()
    df['WADMA'] = df['WAD'].rolling(n, min_periods=1).mean()

    # print(df.head(100))
    # exit()

    # 去量纲
    df[factor_name] = df['WAD'] / df['WADMA']
    
    del df['ref_close']
    del df['TRH'],df['TRL']
    del df['AD']
    del df['WAD']
    del df['WADMA'] 

    return df

def Dbcd_v2_signal(*args):
    # Dbcd_v2 指标
    """

    """
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    close_s = df['close']
    ma = close_s.rolling(n, min_periods=1).mean()
    bias = 100 * (close_s - ma) / ma
    bias_dif = bias - bias.shift(int(3 * n + 1))
    _dbcd = bias_dif.ewm(alpha=1 / (3 * n + 2), adjust=False).mean()
    df[factor_name] = pd.Series(_dbcd)

    return df

def MtmMean_signal(*args):
    # MtmMean 指标
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df[factor_name] = (df['close'] / df['close'].shift(n) - 1).rolling(
        window=n, min_periods=1).mean()

    return df

def MadisPlaced_signal(*args):
    #该指标使用时注意n不能大于过滤K线数量的一半（不是获取K线数据的一半）
    """
    N=20
    M=10
    MA_CLOSE=MA(CLOSE,N)
    MADisplaced=REF(MA_CLOSE,M)
    MADisplaced 指标把简单移动平均线向前移动了 M 个交易日，用法
    与一般的移动平均线一样。如果收盘价上穿/下穿 MADisplaced 则产
    生买入/卖出信号。
    有点变种bias
    """
    df = args[0]
    n  = args[1]
    diff_num = args[2]
    factor_name = args[3]

    ma = df['close'].rolling(
        2 * n, min_periods=1).mean()  # MA(CLOSE,N) 固定俩个参数之间的关系  减少参数
    ref = ma.shift(n)  # MADisplaced=REF(MA_CLOSE,M)

    df[factor_name] = df['close'] / ref - 1  # 去量纲

    return df

def Trrq_signal(*args):
    # Trrq 指标
    
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['tp'] = (df['high'] + df['low'] + df['close']) / 3
    df['归一成交额'] = df['quote_volume'] / \
        df['quote_volume'].rolling(n, min_periods=1).mean()
    reg_price = ta.LINEARREG(df['tp'], timeperiod=n)
    df['tp_reg涨跌幅'] = reg_price.pct_change(n)
    df['tp_reg涨跌幅除以归一成交额'] = df['tp_reg涨跌幅'] / (eps + df['归一成交额'])
    df[factor_name] = df['tp_reg涨跌幅除以归一成交额'].rolling(n).sum()

    del df['tp'], df['归一成交额'], df['tp_reg涨跌幅'], df['tp_reg涨跌幅除以归一成交额']

    return df

def ZhangDieFuAllHour_signal(*args):
    # ZhangDieFuAllHour
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]
    
    zhangdiefu_hour_list = [2, 3, 5]
    #  --- 涨跌幅_all_hour ---
    for m in zhangdiefu_hour_list:
        df[f'涨跌幅_bh_{m}'] = df['close'].pct_change(m)
        if m == zhangdiefu_hour_list[0]:
            df[f'涨跌幅_all_hour'] = df[f'涨跌幅_bh_{m}']
        else:
            df[f'涨跌幅_all_hour'] = df[f'涨跌幅_all_hour'] + df[f'涨跌幅_bh_{m}']
        del df[f'涨跌幅_bh_{m}']

    df[factor_name] = df[f'涨跌幅_all_hour'] / len(zhangdiefu_hour_list)

    del df[f'涨跌幅_all_hour']
    
    return df

def Copp_signal(*args):

    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['RC'] = 100 * ((df['close'] - df['close'].shift(n)) / df['close'].shift(n) + (df['close'] - df['close'].shift(2 * n)) / df['close'].shift(2 * n))
    df[factor_name] = df['RC'].rolling(n, min_periods=1).mean()

    del df['RC']

    return df

def Dpo_signal(*args):
    # Dpo
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['median'] = df['close'].rolling(
        window=n, min_periods=1).mean()  # 计算中轨
    df[factor_name] = (df['close'] - df['median'].shift(int(n / 2) + 1)) / (df['median'] + eps)

    del df['median']

    return df

def ZhenFuBull_signal(*args):
    # ZhenFuBull
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    high = df[['close', 'open']].max(axis=1)
    low = df[['close', 'open']].min(axis=1)
    high = high.rolling(n, min_periods=1).max()
    high = high.shift(1)
    low = low.rolling(n, min_periods=1).min()
    low = low.shift(1)
    df[factor_name] = (df['close'] - high) / (df['close'] + eps)

    return df

# offset 0
def Apz_signal(*args):

    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['hl'] = df['high'] - df['low']
    df['ema_hl'] = df['hl'].ewm(n, adjust=False).mean()
    df['voll'] = df['ema_hl'].ewm(n, adjust=False).mean()

    # 计算通道 可以作为CTA策略 作为因子的时候进行改造
    df['ema_close'] = df['close'].ewm(2 * n, adjust=False).mean()
    df['ema_ema_close'] = df['ema_close'].ewm(2 * n, adjust=False).mean()
    # EMA去量纲
    df[factor_name] = df['voll'] / df['ema_ema_close']

    del df['hl']
    del df['ema_hl']
    del df['voll']
    del df['ema_close']
    del df['ema_ema_close']

    return df

# offset 7
def Rccd_signal(*args):
    # RCCD 指标
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['RC'] = df['close'] / df['close'].shift(2 * n)
    df['ARC1'] = df['RC'].rolling(2 * n, min_periods=1).mean()
    df['MA1'] = df['ARC1'].shift(1).rolling(n, min_periods=1).mean()
    df['MA2'] = df['ARC1'].shift(1).rolling(2 * n, min_periods=1).mean()
    df['DIF'] = df['MA1'] - df['MA2']
    df[factor_name] = df['DIF'].rolling(2 * n, min_periods=1).mean()

    del df['RC']
    del df['ARC1']
    del df['MA1']
    del df['MA2']
    del df['DIF']

    return df

def Dc_signal(*args):

    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    # DC 指标
    """
    N=20
    UPPER=MAX(HIGH,N)
    LOWER=MIN(LOW,N)
    MIDDLE=(UPPER+LOWER)/2
    DC 指标用 N 天最高价和 N 天最低价来构造价格变化的上轨和下轨，
    再取其均值作为中轨。当收盘价上穿/下穿中轨时产生买入/卖出信号。
    """
    upper = df['high'].rolling(n, min_periods=1).max()
    lower = df['low'].rolling(n, min_periods=1).min()
    middle = (upper + lower) / 2
    width = upper - lower
    # 进行无量纲处理
    df[factor_name] = width / middle

    return df

def MarketPl_signal(*args):
    # MarketPl指标
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    quote_volume_ema = df['quote_volume'].ewm(span=n, adjust=False).mean()
    volume_ema = df['volCcy'].ewm(span=n, adjust=False).mean()
    df['平均持仓成本'] = quote_volume_ema / volume_ema
    df[factor_name] = df['close'] / (df['平均持仓成本'] + eps) - 1

    del df['平均持仓成本']

    return df

def Atr_signal(*args):

    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    """
    N=20
    TR=MAX(HIGH-LOW,ABS(HIGH-REF(CLOSE,1)),ABS(LOW-REF(CLOSE,1)))
    ATR=MA(TR,N)
    MIDDLE=MA(CLOSE,N)
    """
    df['c1'] = df['high'] - df['low']  # HIGH-LOW
    df['c2'] = abs(df['high'] - df['close'].shift(1))  # ABS(HIGH-REF(CLOSE,1)
    df['c3'] = abs(df['low'] - df['close'].shift(1))  # ABS(LOW-REF(CLOSE,1))
    df['TR'] = df[['c1', 'c2', 'c3']].max(
        axis=1)  # TR=MAX(HIGH-LOW,ABS(HIGH-REF(CLOSE,1)),ABS(LOW-REF(CLOSE,1)))
    df['_ATR'] = df['TR'].rolling(n, min_periods=1).mean()  # ATR=MA(TR,N)
    df['middle'] = df['close'].rolling(n, min_periods=1).mean()  # MIDDLE=MA(CLOSE,N)
    # ATR指标去量纲
    df[factor_name] = df['_ATR'] / df['middle']

    del df['c1']
    del df['c2']
    del df['c3']
    del df['TR']
    del df['_ATR']
    del df['middle']

    return df

def Sgcz_signal(*args):
    # 收高差值 指标
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    high = df['high'].rolling(n, min_periods=1).mean()
    close = df['close']
    df[factor_name] = (close - high) / (high + eps)

    return df


def Reg_v2_signal(*args):
    # Reg_v2
    df = args[0]
    n = args[1]
    diff_num = args[2]
    factor_name = args[3]

    df['LINEARREG'] = ta.LINEARREG(df['close'], timeperiod=2 * n)
    df[factor_name] = 100 * (df['close'] - df['LINEARREG']) / (df['LINEARREG'] + eps)

    # 删除多余列
    del df['LINEARREG']

    return df

def cal_factor(df):
    """
    计算单个币种的因子值
    :param df:
    :return:
    """
    # 每日涨跌幅
    df['涨跌幅'] = df['close'].pct_change()
    df['涨跌幅'].fillna(value=df['close'] / df['open'] - 1, inplace=True)

    # 判断币种是上涨还是下跌
    df[['上涨', '下跌']] = 0
    df.loc[df['涨跌幅'] > 0, '上涨'] = 1
    df.loc[df['涨跌幅'] <= 0, '下跌'] = 1

    # 选币因子 - 短周期(N=2)：捕捉近期状态
    Reg_v2_signal(df, 2, 0, 'Reg_v2_2')
    Sgcz_signal(df, 2, 0, 'Sgcz_2')

    # 选币因子 - 长周期(N=5)：确认中期趋势（先下跌）
    Reg_v2_signal(df, 5, 0, 'Reg_v2_5')
    Sgcz_signal(df, 5, 0, 'Sgcz_5')

    # 震荡度因子（|BullPower+BearPower|，越小越震荡）
    # 短周期(N=2)：捕捉"刚刚进入震荡"的择时信号
    Er_signal(df, 2, 0, 'Er_2')

    # 自研因子
    db_volume_v1_signal(df, 2, 0, 'db_volume_v1_2')

    Atr_signal(df, 5, 0, 'Atr_5')
    df['middle_5'] = df['close'].rolling(5, min_periods=1).mean()

    # # 根据指定的参数计算一些技术指标
    for n in [2, 5, 13]:
        df['ma_%s' % n] = df['close'].rolling(n).mean()

    return df


def cal_cross_factor(all_coin_data):
    """
    计算截面的因子数据
    :param all_coin_data:
    :return:
    """
    all_coin_data['上涨数量'] = all_coin_data.groupby('time')['上涨'].transform('sum')
    all_coin_data['下跌数量'] = all_coin_data.groupby('time')['下跌'].transform('sum')
    all_coin_data['上涨比例'] = all_coin_data['上涨数量'] / (all_coin_data['上涨数量'] + all_coin_data['下跌数量'])
    all_coin_data['交易额分位占比'] = all_coin_data.groupby('time')['quote_volume'].rank(method='first', ascending=False, pct=True)
    return all_coin_data
