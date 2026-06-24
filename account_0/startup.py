import ccxt
from config import OK_CONFIG, strategy_config, rebalance, njob
from utils.functions import *
from api.kline import ccxt_fetch_ok_exchangeinfo
from api.status import check_ok_service_status
from utils.tools import sleep_until_run_time
from utils.notification import send_msg_q_wechat
import warnings
warnings.filterwarnings('ignore')

# ===创建ok交易所
exchange = ccxt.okex5(OK_CONFIG)
# 模拟盘配置（需要配置模拟盘的apikey）
# exchange.set_sandbox_mode(True)


def run():
    while True:
        # =====sleep到下次运行时间
        run_time = sleep_until_run_time('1h', if_sleep=True, cheat_seconds=0)
        # 测试时使用的代码
        # run_time = datetime.strptime('2022-11-19 16:00:00', "%Y-%m-%d %H:%M:%S")

        # 判断一下，当前策略配置的offset是否需要下单
        offset_order_tag, offset = get_order_offset_tag(strategy_config, run_time)
        if offset_order_tag is None:
            print('当前配置的策略没有offset需要下单，等待下一次')
            continue
        print(f'当前对offset{offset}进行下单')

        # =====检查OK服务状态是否可用
        check_ok_service_status(exchange)

        # =====查询当前开仓的网格信息，关键看tag信息
        grid_df = query_grid(exchange)
        print(grid_df)

        # =====停止网格
        grid_ids = close_grid(exchange, grid_df, tag=offset_order_tag)

        # ======获取市场交易对数据
        symbol_dict = ccxt_fetch_ok_exchangeinfo(exchange)

        # =====获取所有k线数据
        symbol_candle_data = fetch_all_binance_swap_candle_data(exchange, symbol_dict.keys(), run_time, njob, strategy_config['max_candle_num'])

        # =====发送刚刚平仓的网格的盈亏情况
        send_last_grid_info(exchange, grid_ids)

        strategy_config['factors'] = {"Reg_v2_2": True, "Sgcz_2": True}

        # =====数据整理 & 选币 & 生成下单信息
        all_order_df = proceed_order_for_strategy_config(symbol_dict, symbol_candle_data, run_time, strategy_config, offset)
        if all_order_df.empty:
            save_order_info(all_order_df, offset_order_tag)
            print('当前小时，没有下单数据')
            send_msg_q_wechat(f'当前小时，offset{offset}没有下单数据')
            continue
        print('本次下单信息:\n', all_order_df)

        # =====计算下单金额,每个offset都是均仓下单
        order_price = calc_order_price(exchange, strategy_config, rebalance)
        all_order_df['下单金额'] = order_price
        send_order_info(all_order_df, strategy_config)  # 企业微信发送开仓信息

        # =====开启网格
        open_grid(exchange, all_order_df)

        # =====保存下单信息
        save_order_info(all_order_df, offset_order_tag)

        # 本次循环结束
        print('-' * 20, '本次循环结束', '-' * 20)


if __name__ == '__main__':
    while True:
        try:
            run()
        except KeyboardInterrupt:
            print('手动停止')
        except BaseException as err:
            msg = 'startup 系统出错，10s之后重新运行，出错原因: ' + str(err)
            print(msg)
            send_msg_q_wechat(msg)
            print(traceback.format_exc())
            time.sleep(10)
