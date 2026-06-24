import time
import traceback
import ccxt
import pandas as pd
from datetime import datetime
from config import stop_loss_period, OK_CONFIG, strategy_config
from utils.functions import query_grid, close_grid, calc_stop_grid, update_pnlRatio
from utils.notification import send_msg_q_wechat
from utils.tools import retry_wrapper, sleep_until_run_time
from api.status import check_ok_service_status
import warnings
warnings.filterwarnings('ignore')


# ===创建ok交易所
exchange = ccxt.okex5(OK_CONFIG)
# 模拟盘配置（需要配置模拟盘的apikey）
# exchange.set_sandbox_mode(True)


def main():
    # 标识整点是否已经发送信息通知
    is_send = False
    # 新建全局df_max DataFrame，重启程序就会清0
    df_max = pd.DataFrame(columns=['algoId', 'instId', 'tag', 'pnlRatio'])

    while True:
        # =====sleep到下一次运行时间
        sleep_until_run_time(stop_loss_period, if_sleep=True, cheat_seconds=0)

        # =====检查OK服务状态是否可用
        # 降低一下监控的频率
        if datetime.now().minute % 5 == 0 and datetime.now().second == 0:
            check_ok_service_status(exchange)

        # =====查询正在运行中的网格
        grid_df = query_grid(exchange)
        if grid_df.empty:
            continue

        # =====计算止盈止损
        grid_df, df_max = calc_stop_grid(exchange, grid_df, df_max, strategy_config)
        if grid_df.empty:
            continue
            
        print(grid_df[['instId', 'sz', 'totalPnl', 'pnlRatio', 'pnlRatio_max', 'tag', '止盈止损']])

        # =====筛选出需要止盈止损的网格
        stop_grid_df = grid_df[grid_df['止盈止损'].notna()]
        if not stop_grid_df.empty:
            close_grid(exchange, stop_grid_df)  # 停止网格

        # =====整点发送账户持仓情况
        _now = datetime.now()
        if _now.minute > 0:
            is_send = False
        if _now.minute == 0 and not is_send:
            balance = retry_wrapper(exchange.private_get_account_balance, params={'ccy': 'USDT'}, act_name='获取账户资金')
            balance = float(balance['data'][0]['details'][0]['eq'])
            msg = f'当前账户净值: {"%.2f" % balance}\n\n'
            for index, row in grid_df.iterrows():
                msg += f'当前网格净值: {"%.2f" % (float(row["totalPnl"]) + float(row["sz"]))}\n'
                msg += f'当前网格持仓: {row["instId"]}\n'
                msg += f'当前网格盈亏%: {"%.2f%%" % (float(row["pnlRatio"]) * 100)}\n'
                msg += f'当前网格盈亏金额: {"%.2f" % float(row["totalPnl"])}\n'
                msg += f'当前网格策略标识: {row["tag"]}\n'
                msg += '-'*10
                msg += '\n\n'
            send_msg_q_wechat(msg)
            is_send = True


if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print('手动停止')
        except BaseException as err:
            msg = 'monitor 系统出错，10s之后重新运行，出错原因: ' + str(err)
            print(msg)
            send_msg_q_wechat(msg)
            print(traceback.format_exc())
            time.sleep(10)
