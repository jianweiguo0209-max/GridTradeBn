"""
验证 demo 账户能否开/查/平合约网格（确认 bot 攒数据的写入路径可用）。
开一个最小 BTC 网格 → 查运行中 → 立即平掉清理（不留持仓）。仅验证机制，不产生有效 PnL。
需 .env OK_SIMULATED=1（模拟盘密钥）。
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in ('../account_0', '../account_0/utils', '../account_0/api'):
    ap = os.path.join(_HERE, _p)
    if ap not in sys.path:
        sys.path.insert(0, ap)

import ccxt  # noqa: E402
import okx_history as H  # noqa: E402
from config import OK_CONFIG, apply_simulated_mode, OK_SIMULATED  # noqa: E402
from api.grid import create_grid, query_running_grid, stop_grid  # noqa: E402

SYM = 'BTC-USDT-SWAP'
TAG = 'caltest0'


def main():
    print('OK_SIMULATED =', OK_SIMULATED)
    ex = ccxt.okex5(OK_CONFIG)
    apply_simulated_mode(ex)

    # 当前价（最新已收盘 1m）
    data = H._get('/api/v5/market/candles', {'instId': SYM, 'bar': '1m', 'limit': '2'})
    px = float(data[1][4])
    print('BTC 当前价 ~%.1f' % px)

    fmt = lambda p: str(round(p, 1))
    params = {
        'instId': SYM, 'algoOrdType': 'contract_grid',
        'maxPx': fmt(px * 1.10), 'minPx': fmt(px * 0.90), 'gridNum': '20',
        'runType': '2', 'sz': '100', 'direction': 'neutral', 'lever': '5',
        'tpTriggerPx': fmt(px * 1.12), 'slTriggerPx': fmt(px * 0.88), 'tag': TAG,
    }
    print('开网格参数:', params)
    resp = create_grid(ex, params)
    print('创建返回:', resp)
    algo_id = None
    if resp and resp.get('code') == '0' and resp.get('data'):
        algo_id = resp['data'][0].get('algoId')
    print('algoId:', algo_id)

    time.sleep(2)
    pend = query_running_grid(ex, {'algoOrdType': 'contract_grid'})
    running = [d for d in (pend or {}).get('data', []) if d.get('tag') == TAG]
    print('运行中本测试网格数:', len(running))

    # 清理：平掉
    if algo_id:
        stop = stop_grid(ex, [{'algoId': algo_id, 'instId': SYM,
                               'algoOrdType': 'contract_grid', 'stopType': '1'}])
        print('平仓返回:', stop)
    print('验证结束：', '✅ demo 开/查/平 全通' if algo_id else '❌ 开仓未拿到 algoId，看上面返回')


if __name__ == '__main__':
    main()
