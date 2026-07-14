"""币安期货 Demo Trading 端到端冒烟（联网、非 pytest；spec 2026-07-14 §八/§5.1 实测）。
前置：export BINANCE_API_KEY/BINANCE_API_SECRET——Demo Trading key，在
https://demo.binance.com 的 API Management 生成（旧 futures testnet 已被币安弃用）。
跑：.venv/bin/python scripts/binance_testnet_smoke.py
验证：账户模式断言 / cloid 直传合法性(冒号) / 限价挂撤 / STOP_MARKET 挂撤 /
批量读五方法 / 精度量化。全程远离盘口价，不产生成交。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gridtrade.exchanges.binance import BinanceAdapter

SYM = 'BTC/USDT:USDT'


def main():
    a = BinanceAdapter.from_credentials(os.environ['BINANCE_API_KEY'],
                                        os.environ['BINANCE_API_SECRET'],
                                        testnet=True)
    print('== assert_account_mode ==')
    a.assert_account_mode()
    print('   OK（单向持仓/单币保证金）')

    print('== 清理上次残留（幂等；崩溃遗留孤儿单会以 -4067 挡 marginType）==')
    a.cancel_all(SYM)
    print('   cleaned')

    print('== 行情/精度 ==')
    px = a.fetch_price(SYM)
    qty = a.quantize_amount(SYM, 0.002)
    print('   price=%s quantized(0.002)=%s' % (px, qty))
    insts = {i.symbol: i for i in a.list_instruments()}
    print('   instruments=%d BTC.min_cost=%s' % (len(insts), insts[SYM].min_cost))
    assert insts[SYM].min_cost > 0, 'BTC min_cost 未从 exchangeInfo 加载——按币下限门将退化为全局下限'

    print('== cloid 直传实测（含冒号，spec §5.1）==')
    a.set_leverage(SYM, 2)
    o = a.create_limit_order(SYM, 'buy', round(px * 0.5, 1), qty,
                             client_oid='999999:1:1')
    print('   placed id=%s cloid=%s' % (o.id, o.client_oid))
    try:
        assert o.client_oid == '999999:1:1', \
            'cloid 被改写——需启用替换编码并更新 spec §5.1'
        opens = a.fetch_open_orders(SYM)
        print('   open_orders=%d' % len(opens))
    finally:
        a.cancel_order(SYM, o.id)          # 断言失败也要撤单（勿留孤儿挂单）
        print('   canceled')

    print('== STOP_MARKET 保险丝挂撤 ==')
    s = a.create_stop_order(SYM, 'sell', qty, round(px * 0.5, 1),
                            client_oid='999999:fuse:low')
    print('   stop id=%s' % s.id)
    a.cancel_order(SYM, s.id)
    print('   canceled')

    print('== 批量读快照 ==')
    print('   prices_all:', a.fetch_prices_all([SYM]))
    print('   positions_all:', a.fetch_positions_all([SYM]))
    print('   open_orders_all:', len(a.fetch_open_orders_all([SYM])))
    print('   trades_all:', len(a.fetch_my_trades_all([SYM])))
    print('   funding_all:', {k: len(v) for k, v in
                              a.fetch_funding_payments_all([SYM]).items()})
    print('SMOKE PASS')


if __name__ == '__main__':
    main()
