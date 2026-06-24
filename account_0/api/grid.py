import time
import traceback
from utils.notification import send_msg_q_wechat
import ccxt
import json


# 创建网格
def create_grid(exchange, params):
    sign = exchange.sign('tradingBot/grid/order-algo', 'private', 'POST', params)
    return fetch(exchange, sign, act_name='创建网格')


# 停止网格
def stop_grid(exchange, params):
    sign = exchange.sign('tradingBot/grid/stop-order-algo', 'private', 'POST', params)
    return fetch(exchange, sign, act_name='停止网格')


# 查询运行中的网格
def query_running_grid(exchange, params):
    sign = exchange.sign('tradingBot/grid/orders-algo-pending', 'private', 'GET', params)
    return fetch(exchange, sign, act_name='查询运行中的网格')


# 查询历史网格
def query_grid_history(exchange, params):
    sign = exchange.sign('tradingBot/grid/orders-algo-history', 'private', 'GET', params)
    return fetch(exchange, sign, act_name='查询网格信息')


# 网格接口统一请求处理
def fetch(exchange, sign, act_name='', sleep_seconds=3, retry_times=5):
    for _ in range(retry_times):
        try:
            return exchange.fetch(sign['url'], sign['method'],
                                  dict({'Content-Type': 'application/json'}, **sign['headers']),
                                  sign['body'])
        except BaseException as e:
            print(act_name, '报错，报错内容：', str(e), '程序暂停(秒)：', sleep_seconds)
            # 不要将headers打印出来，里面有apikey配置信息
            print('请求参数:', sign['url'], sign['body'])
            print(traceback.format_exc())
            time.sleep(sleep_seconds)

            if isinstance(e, ccxt.ExchangeError):
                error = str(e).replace('okex5', '').strip()
                error_code = json.loads(error)['code']
                error_msg = json.loads(error)['msg']
                # {"code":"51290","data":[],"msg":"The strategy engine is being upgraded, please try again later "}
                # 51290  网格系统在升级
                # 50001  服务无法响应
                # 这里再判断一次，是因为OK有时候status接口返回系统已经更新完成了，但是实际使用的时候，还是没有更新完成
                if error_code in ['51290', '50001']:  # 出现上述错误，直接休息 1 分钟，让程序退出
                    send_msg_q_wechat(f'OK网格接口请求出错, 错误内容:{error_msg}, 准备休息 1 分钟后，程序退出 exit')
                    time.sleep(60)
                    exit()
                elif _ == retry_times - 1:  # 出现其他错误，直接将错误信息推送给机器人
                    send_msg_q_wechat(f'OK网格接口请求出错, 错误内容:{error}')
    else:
        msg = f'{act_name}  报错重试次数超过上限'
        print(msg)
        raise Exception(msg)
