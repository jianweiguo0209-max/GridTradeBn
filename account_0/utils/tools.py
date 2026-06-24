import os
import time
import traceback
import pandas as pd
from collections import Counter
from datetime import datetime, timedelta
from config import black_dict, order_path
from utils.notification import send_msg_q_wechat


def retry_wrapper(func, params=dict(), act_name='', sleep_seconds=3, retry_times=5, is_exit=True):
    """
    需要在出错时不断重试的函数，例如和交易所交互，可以使用本函数调用。
    :param func: 需要重试的函数名
    :param params: func的参数
    :param act_name: 本次动作的名称
    :param sleep_seconds: 报错后的sleep时间
    :param retry_times: 为最大的出错重试次数
    :param is_exit: 是否需要退出
    :return:
    """

    for _ in range(retry_times):
        try:
            # 这里有个小细节：如果程序请求出现问题，并且包含当前时间戳，重试的时候必须将时间戳重置，不然就会出现本地时间与服务器时间差距较大，请求失败
            if isinstance(params, dict) and 'timestamp' in list(params.keys()):
                params['timestamp'] = int(time.time()) * 1000
            result = func(params=params)
            return result
        except Exception as e:
            print(act_name, '报错，报错内容：', str(e), '程序暂停(秒)：', sleep_seconds)
            print('请求参数:', params)
            print(traceback.format_exc())
            time.sleep(sleep_seconds)
    else:
        print(act_name, '报错重试次数超过上限，程序退出。')
        send_msg_q_wechat(act_name + '报错重试次数超过上限，程序退出。')
        if is_exit:
            exit()


# =====辅助功能函数
# ===下次运行时间，和课程里面讲的函数是一样的
def next_run_time(time_interval, ahead_seconds=5, cheat_seconds=100):
    """
    根据time_interval，计算下次运行的时间，下一个整点时刻。
    目前只支持分钟和小时。
    :param time_interval: 运行的周期，15m，1h
    :param ahead_seconds: 预留的目标时间和当前时间的间隙
    :return: 下次运行的时间
    案例：
    15m  当前时间为：12:50:51  返回时间为：13:00:00
    15m  当前时间为：12:39:51  返回时间为：12:45:00
    10m  当前时间为：12:38:51  返回时间为：12:40:00
    5m  当前时间为：12:33:51  返回时间为：12:35:00

    5m  当前时间为：12:34:51  返回时间为：12:40:00

    30m  当前时间为：21日的23:33:51  返回时间为：22日的00:00:00

    30m  当前时间为：14:37:51  返回时间为：14:56:00

    1h  当前时间为：14:37:51  返回时间为：15:00:00

    """
    if time_interval.endswith('m') or time_interval.endswith('h') or time_interval.endswith('s'):
        pass
    elif time_interval.endswith('T'):
        time_interval = time_interval.replace('T', 'm')
    elif time_interval.endswith('H'):
        time_interval = time_interval.replace('H', 'h')
    elif time_interval.endswith('S'):
        time_interval = time_interval.replace('S', 's')
    else:
        print('time_interval格式不符合规范。程序exit')
        exit()

    ti = pd.to_timedelta(time_interval)
    now_time = datetime.now()
    this_midnight = now_time.replace(hour=0, minute=0, second=0, microsecond=0)

    if ti.seconds < 60:
        min_step = timedelta(seconds=1)
        target_time = now_time.replace(microsecond=0)
    else:
        min_step = timedelta(minutes=1)
        target_time = now_time.replace(second=0, microsecond=0)

    if ti >= timedelta(days=1):
        temp_s = 60 * 60 * 24 * int(ti / timedelta(days=1)) + ti.seconds
    else:
        temp_s = ti.seconds
    while True:
        target_time = target_time + min_step
        delta = target_time - this_midnight
        if delta >= timedelta(days=1):
            d_seconds = 60 * 60 * 24 * int(delta / timedelta(days=1)) + delta.seconds
        else:
            d_seconds = delta.seconds
        if d_seconds % temp_s == 0 and (target_time - now_time).seconds >= ahead_seconds:
            # 当符合运行周期，并且目标时间有足够大的余地，默认为60s
            break
    if cheat_seconds != 0:
        target_time = target_time - timedelta(seconds=cheat_seconds)

    print('程序下次运行的时间：', target_time, '\n')

    return target_time


# ===依据时间间隔, 自动计算并休眠到指定时间
def sleep_until_run_time(time_interval, ahead_time=1, if_sleep=True, cheat_seconds=120):
    """
    根据next_run_time()函数计算出下次程序运行的时候，然后sleep至该时间
    :param if_sleep:
    :param time_interval:
    :param ahead_time:
    :return:
    """
    # 计算下次运行时间
    run_time = next_run_time(time_interval, ahead_time, cheat_seconds)
    # sleep
    if if_sleep:
        # 如果监控时间很短，计算获得的 run_time 小于 now, sleep就会睡 一天
        _now = datetime.now()
        if run_time > _now:
            time.sleep(max(0, (run_time - _now).seconds))
        while True:  # 在靠近目标时间时
            if datetime.now() > run_time:
                break
    return run_time


def get_temp_black_list():
    file_path = os.path.join(order_path, 'orderInfo.pkl')
    order_df = pd.read_pickle(file_path) if os.path.exists(file_path) else pd.DataFrame()
    # 拿到当前下单的所有币种数据
    symbol_list = order_df['symbol'].to_list() if not order_df.empty else []

    temp_black_list = []
    counter = Counter(symbol_list)
    print('所有币种信息: ', counter)
    print('黑名单配置信息: ', black_dict)
    for k in sorted(black_dict.keys()):
        # 没有配置直接跳过
        if not len(black_dict[k]):
            continue
        # 0 全拉黑
        if int(k) == 0:
            temp_black_list += black_dict[k]
            continue
        # 根据 key 的数字，来判断是否需要拉黑币种
        for c in counter:
            if counter[c] >= int(k):
                if 'OTHERS' in black_dict[k] or c in black_dict[k]:
                    temp_black_list.append(c)

    print('生成临时黑名数据:', temp_black_list)
    return temp_black_list


# 获取距离当前时间最近的上一个周期时间
def get_before_period_time(period):
    # 获取下个周期时间
    target_time = next_run_time(period, ahead_seconds=0, cheat_seconds=0)
    # 最小周期时间
    ti = pd.to_timedelta(period)
    # 获得减去2个周期(一个下个周期，一个当前周期)，获得上个周期时间
    target_time = target_time - 2 * ti
    print('当前配置: ', period, '  距离当前时间最近的上一个周期时间是:', target_time)
    return target_time
