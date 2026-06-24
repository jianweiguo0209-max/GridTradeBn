import time
from datetime import datetime
import pandas as pd
from utils.tools import retry_wrapper


def check_ok_service_status(exchange):
    """
    检测OK系统的服务状态是否可用
    :param exchange: 交易所对象
    """
    while True:
        # 获取OK系统的服务状态
        res = retry_wrapper(func=exchange.public_get_system_status, act_name='获取当前OK系统状态', sleep_seconds=6)
        state_df = pd.DataFrame(res['data'])

        # ===系统正常
        if state_df.empty:
            print('OK系统正常')
            break

        # ===处理数据
        utc_offset = int(time.localtime().tm_gmtoff / 60 / 60)
        state_df['begin'] = pd.to_datetime(state_df['begin'], unit='ms') + pd.Timedelta(hours=utc_offset)
        state_df['end'] = pd.to_datetime(state_df['end'], unit='ms') + pd.Timedelta(hours=utc_offset)
        print('当前OK系统状态：', state_df)

        # ===系统维护中
        # 筛选出服务更新的类型。OK文档：https://www.okx.com/docs-v5/zh/#rest-api-status
        state_df = state_df[state_df['serviceType'].isin(['5', '6', '7'])]  # 0：WebSocket;5：交易服务；6：大宗交易；7：策略交易；99：其他
        state_df = state_df[state_df['begin'] <= datetime.now()]  # 系统更新已经开始
        # 若state_df为空，表示当前时间在开始时间之前，更新还未开始
        if state_df.empty:
            print('服务更新暂未开始')
            break
        state_df = state_df[state_df['end'] >= datetime.now()]  # 系统更新已经结束
        # 若state_df为空，表示上述服务都已经更新完毕，可以进行后续操作
        if state_df.empty:
            print('服务更新已完成')
            break

        print('服务更新中······休息60s后，再次检测服务状态')
        # 休息60s，等待下一次OK服务状态检测
        time.sleep(60)
