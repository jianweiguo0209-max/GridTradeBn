import ccxt
from config import OK_CONFIG, strategy_config, rebalance, njob, apply_simulated_mode
from utils.functions import *
from api.kline import ccxt_fetch_ok_exchangeinfo
from api.status import check_ok_service_status
from utils.tools import sleep_until_run_time
from utils.notification import send_msg_q_wechat
import warnings
warnings.filterwarnings('ignore')

# ===创建ok交易所
exchange = ccxt.okex5(OK_CONFIG)
# 模拟盘配置：由 .env 的 OK_SIMULATED 控制（模拟盘密钥须置 1），实盘时为 no-op
apply_simulated_mode(exchange)


def run():
    # 默认取当前时间的整点
    # run_time = pd.to_datetime(datetime.now().strftime("%Y-%m-%d %H:00:00"))
    # 测试用：指定时间(注意：必须是整点时间！！)
    run_time = datetime.strptime('2025-06-24 14:00:00', "%Y-%m-%d %H:%M:%S")

    # 计算offset 以及 策略标记：acc1at
    period = strategy_config['period']
    utc_offset = int(time.localtime().tm_gmtoff / 60 / 60)  # 兼容时区
    utc_run_time = run_time - pd.Timedelta(hours=utc_offset)
    offset = int(((utc_run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % int(period[:-1]))
    offset_order_tag = f'{strategy_config["strategy_tag"]}{offset}'

    # =====检查OK服务状态是否可用
    check_ok_service_status(exchange)

    # =====查询当前开仓的网格信息，关键看tag信息
    grid_df = query_grid(exchange)
    print(grid_df)

    # =====停止网格
    grid_ids = close_grid(exchange, grid_df, tag=offset_order_tag)

    # ======获取市场交易对数据
    symbol_dict = ccxt_fetch_ok_exchangeinfo(exchange)

    # 测试用：仅获取几个币的K线，加速测试
    # symbol_dict = {'BTC-USDT-SWAP': '0.1', 'ETH-USDT-SWAP': '0.01'}#, 'SOL-USDT-SWAP': '0.01', 'TON-USDT-SWAP': '0.001'}
    # symbol_dict = {'PENGU-USDT-SWAP': '0.000001'}

    # =====获取所有k线数据（utc）
    symbol_candle_data = fetch_all_binance_swap_candle_data(exchange, symbol_dict.keys(), run_time, njob, strategy_config['max_candle_num'])
    # print(symbol_candle_data)

    # =====发送刚刚平仓的网格的盈亏情况
    send_last_grid_info(exchange, grid_ids)

    # GJW对offset做判断，设置不同的因子
    # if offset == 0:
    #     strategy_config['factors'] = {'涨跌幅': True}
    # elif offset == 5:
    #     strategy_config['factors'] = {"Pmo_2": False, "Wad_2": False}
    strategy_config['factors'] = {"Reg_v2_2": True, "Sgcz_2": True}


    # =====周期转换（utc+8） & 因子计算 & 选币 & 生成下单信息
    all_order_df = proceed_order_for_strategy_config(symbol_dict, symbol_candle_data, run_time, strategy_config, offset)
    if all_order_df.empty:
        print(f'当前小时，offset{offset}没有下单数据')
        send_msg_q_wechat(f"因子:{strategy_config['factors']}\n当前小时，offset{offset}没有下单数据")
    print('本次下单信息:\n', all_order_df)

    # =====计算下单金额,每个offset都是均仓下单
    order_price = calc_order_price(exchange, strategy_config, rebalance)
    all_order_df['下单金额'] = order_price
    send_order_info(all_order_df, strategy_config)  # 企业微信发送开仓信息

    # =====开启网格
    open_grid(exchange, all_order_df)

    # =====保存下单信息
    save_order_info(all_order_df, offset_order_tag)

    # 单次下单结束
    print('单次下单结束！')


if __name__ == '__main__':
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
