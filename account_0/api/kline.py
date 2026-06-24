import time
import traceback
from datetime import datetime

import pandas as pd

from utils.tools import retry_wrapper, get_temp_black_list


def ccxt_fetch_ok_exchangeinfo(exchange):
    """
    获取市场数据
    :param exchange: 交易所对象
    :return: 一个字典
             symbol --- 价格精度
    """
    start_time = datetime.now()
    # 获取OK市场所有币种数据
    symbol_list = retry_wrapper(func=exchange.public_get_public_instruments, params={'instType': 'SWAP'}, act_name='ok获取交易对数据')

    # 获取U本位合约，过滤掉退市的币种，过滤掉上线不足3天的币种
    now_ms = datetime.now().timestamp() * 1000
    symbol_list = list(filter(lambda s: s['state'] == 'live' and
                                        ((now_ms - int(s['listTime'])) / 1000 / 86400) >= 15 and
                                        s['instId'].endswith('-USDT-SWAP'), symbol_list['data']))
    # 保存币种信息和币种价格精度
    symbol_dict = {}
    temp_black_list = get_temp_black_list()
    for symbol in symbol_list:
        # 处在黑名单的币种不参与交易
        if symbol['instId'] in temp_black_list:
            continue
        symbol_dict[symbol['instId']] = symbol['tickSz']

    print('获取OK市场所有币种数据完成: ', datetime.now() - start_time)
    return symbol_dict


def fetch_ok_swap_candle_data(exchange, symbol, run_time, limit=100, interval='1H'):
    try:
        start_time_dt = run_time - pd.to_timedelta(interval) * limit
        params = {
            'instId': symbol,
            'bar': interval,
            'before': int(time.mktime(start_time_dt.timetuple())) * 1000,
            'limit': limit if limit < 300 else 300,
        }
        df_list = []
        data_len = 0
        # 兼容时区
        utc_offset = int(time.localtime().tm_gmtoff / 60 / 60)
        while True:
            kline = retry_wrapper(exchange.public_get_market_candles, params=params, act_name='获取K线数据', is_exit=False)
            # 将数据转换为DataFrame
            df = pd.DataFrame(kline['data'], dtype='float')

            # 整理数据
            columns = {0: 'candle_begin_time', 1: 'open', 2: 'high', 3: 'low', 4: 'close', 5: 'vol', 6: 'volCcy', 7: 'volCcyQuote'}
            df.rename(columns=columns, inplace=True)
            df['candle_begin_time'] = pd.to_datetime(df['candle_begin_time'], unit='ms')
            # df['quote_volume'] = abs(df['open'] + df['close']) / 2.0 * df['volCcy']  # 添加成交额
            df['quote_volume'] = df['volCcyQuote']  # 添加成交额
            df['symbol'] = symbol
            # 保留指定字段
            columns = ['symbol', 'candle_begin_time', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'quote_volume']
            df = df[columns]
            df.sort_values('candle_begin_time', inplace=True)

            # 添加到list中
            df_list.append(df)
            data_len += df.shape[0]

            # 判断获取k线长度是否足够
            if data_len >= limit:
                break

            # 重新设置一下请求参数
            start_time_dt = df.iloc[0]['candle_begin_time'] + pd.Timedelta(hours=utc_offset) - pd.to_timedelta(interval) * limit
            params['before'] = int(time.mktime(start_time_dt.timetuple())) * 1000
            params['after'] = int(time.mktime((df.iloc[0]['candle_begin_time'] + pd.Timedelta(hours=utc_offset)).timetuple())) * 1000

        # 数据合并
        all_df = pd.concat(df_list, ignore_index=True)
        all_df.sort_values(by=['candle_begin_time'], inplace=True)
        all_df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)

        # 删除runtime那行的数据，如果有的话
        all_df = all_df[(all_df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)) < run_time]
        all_df.reset_index(drop=True, inplace=True)

        return symbol, all_df
    except Exception as e:
        print(traceback.format_exc())
        return symbol, None