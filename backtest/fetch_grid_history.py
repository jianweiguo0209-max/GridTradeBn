"""
拉取账户真实「合约网格平仓历史」作为仿真器校准真值（只读，用 .env 的 API key）。

OKX tradingBot/grid/orders-algo-history（复用 account_0/api/grid.py 的签名封装）返回每个已平网格的
开仓参数(maxPx/minPx/gridNum/lever/...) + 实际平仓 totalPnl/pnlRatio + 开/平时间。
保存到 data/order/gridResult_okx.csv，供 calibrate_grid_sim.py 对比。
"""
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ACC = os.path.join(os.path.dirname(_HERE), 'account_0')
for _p in (_ACC, os.path.join(_ACC, 'utils'), os.path.join(_ACC, 'api')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ccxt  # noqa: E402
import requests  # noqa: E402
from config import OK_CONFIG, OK_SIMULATED  # noqa: E402

_PATH = 'tradingBot/grid/orders-algo-history'


def _signed_get(exchange, params, simulated=False):
    """用 ccxt 的 sign 生成鉴权头，直接 requests GET（绕开 account_0.api.grid.fetch 包装器）。
    simulated=True 时加 x-simulated-trading:1（模拟盘密钥必需；ccxt 2.0.58 的 set_sandbox_mode 不加此头）。"""
    sign = exchange.sign(_PATH, 'private', 'GET', params)
    headers = dict(sign['headers'])
    if simulated:
        headers['x-simulated-trading'] = '1'
    r = requests.get(sign['url'], headers=headers, timeout=15)
    return r.json()


def fetch_all_history(exchange, simulated=False):
    """分页拉全部 contract_grid 历史（orders-algo-history 每页 100，用 after=algoId 翻页）。"""
    rows = []
    after = None
    for _ in range(50):  # 最多 5000 条兜底
        params = {'algoOrdType': 'contract_grid', 'instType': 'SWAP', 'limit': '100'}
        if after:
            params['after'] = after
        resp = _signed_get(exchange, params, simulated=simulated)
        if resp.get('code') != '0':
            print('OKX 返回非0:', resp.get('code'), resp.get('msg')); break
        data = resp.get('data', [])
        if not data:
            break
        rows.extend(data)
        after = data[-1].get('algoId')
        if len(data) < 100:
            break
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser(description='拉账户合约网格平仓历史（校准真值）')
    ap.add_argument('--simulated', dest='simulated', action='store_true', default=None,
                    help='强制模拟盘头；默认读 .env 的 OK_SIMULATED')
    ap.add_argument('--live', dest='simulated', action='store_false', help='强制实盘')
    args = ap.parse_args()

    simulated = OK_SIMULATED if args.simulated is None else args.simulated
    exchange = ccxt.okex5(OK_CONFIG)
    print('拉取账户 contract_grid 历史 (simulated=%s, 来源=%s)...'
          % (simulated, '.env OK_SIMULATED' if args.simulated is None else 'CLI'))
    rows = fetch_all_history(exchange, simulated=simulated)
    print('拿到 %d 条历史网格' % len(rows))
    if not rows:
        print('账户无历史网格记录'); return
    df = pd.DataFrame(rows)
    print('字段:', list(df.columns))
    out = os.path.join(os.path.dirname(_HERE), 'data', 'order', 'gridResult_okx.csv')
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print('已保存 -> %s' % out)
    # 校准关心的字段抽样
    cols = [c for c in ['algoId', 'instId', 'runType', 'maxPx', 'minPx', 'gridNum', 'lever',
                        'sz', 'totalPnl', 'pnlRatio', 'tpTriggerPx', 'slTriggerPx',
                        'cTime', 'uTime', 'triggerTime', 'state', 'tag'] if c in df.columns]
    print('\n校准相关字段样本:')
    print(df[cols].head(8).to_string(index=False))


if __name__ == '__main__':
    main()
