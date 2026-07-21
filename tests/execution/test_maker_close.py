"""B案(2026-07-21):周期再平衡平仓 maker-first。
契约:旗标默认关=市价路径逐位不变;开且 reason=周期再平衡 → post-only 限价先行,
超时/拒单撤转市价;紧急原因(固定止损等)恒市价。FakeExchange 限价下单即按现价撮合
(现价即成交=maker 秒成场景);resting 场景以停用 _match 模拟。"""
import random

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def build_stack(store, *, maker=False, schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    faulty = FaultyAdapter(fake, schedule or {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0,
                      maker_close_rebalance=maker)
    return fake, faulty, gx


def _seed_position(fake, gx):
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 98.5)
    gx.sync(gid, SYM)
    assert fake.fetch_positions(SYM).net_size > 0
    return gid


def _limit_reduces(fake):
    """FakeExchange 成交流水里 client_oid 以 'm' 结尾的关格限价单成交。"""
    return [t for t in fake.fetch_my_trades(SYM)
            if any(o.id == t.order_id and (o.client_oid or '').endswith('m')
                   for o in fake._all_orders())] if hasattr(fake, '_all_orders') else None


def test_flag_off_close_uses_market_only(store):
    fake, _, gx = build_stack(store, maker=False)
    gid = _seed_position(fake, gx)
    seen = []
    orig = fake.create_limit_order

    def spy(*a, **k):
        seen.append(k.get('client_oid'))
        return orig(*a, **k)

    fake.create_limit_order = spy
    gx.close(gid, SYM, '周期再平衡')
    closes = [c for c in seen if c and ':close:' in str(c)]
    assert closes == []                                     # 默认关:平仓不碰限价单
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9
    assert gx.grids.get(gid).status == 'CLOSED'


def test_maker_fill_closes_without_market_reduce(store):
    fake, _, gx = build_stack(store, maker=True)
    gid = _seed_position(fake, gx)
    mkt = []
    orig = fake.create_market_order

    def spy(*a, **k):
        mkt.append(k.get('client_oid'))
        return orig(*a, **k)

    fake.create_market_order = spy
    gx.close(gid, SYM, '周期再平衡')
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9    # 平干净
    assert gx.grids.get(gid).status == 'CLOSED'
    closes = [c for c in mkt if c and ':close:' in str(c)]
    assert closes == []                                      # 全程无市价减仓单(maker 秒成)


def test_maker_resting_times_out_and_falls_back_to_market(store):
    fake, _, gx = build_stack(store, maker=True)
    gid = _seed_position(fake, gx)
    fake._match = lambda *a, **k: None                       # 限价永不成交(resting 模拟)
    clock = {'t': 0.0}
    gx._now = lambda: clock['t']

    def fast_sleep(sec):
        clock['t'] += float(sec)

    gx._sleep = fast_sleep
    gx.close(gid, SYM, '周期再平衡')
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9    # 市价回退平干净
    assert gx.grids.get(gid).status == 'CLOSED'
    assert clock['t'] >= gx.MAKER_CLOSE_TIMEOUT_S            # 真等满了超时窗


def test_maker_reject_falls_back_to_market(store):
    fake, _, gx = build_stack(store, maker=True)
    gid = _seed_position(fake, gx)

    def reject(*a, **k):
        raise RuntimeError('GTX would immediately match')

    fake.create_limit_order = reject                        # 交易所拒 post-only(会吃单)
    gx.close(gid, SYM, '周期再平衡')
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9
    assert gx.grids.get(gid).status == 'CLOSED'


def _run_close_timeout(gx, gid):
    """resting + 快进时钟，跑完 maker 超时→市价回退，不真等。"""
    clock = {'t': 0.0}
    gx._now = lambda: clock['t']
    gx._sleep = lambda s: clock.__setitem__('t', clock['t'] + float(s))
    gx.close(gid, SYM, '周期再平衡')


def test_maker_sell_close_places_at_best_ask_not_last(store):
    # 平多头(sell)的 maker post-only 必须挂 best ask(被动侧),不能挂 last——挂 last 会跨点差
    # 被币安 GTX 拒、立即回退市价、maker 拿不到返佣(ARIA/prod 2026-07-20 实证回退 taker)。
    fake, _, gx = build_stack(store, maker=True)   # 默认价 100 开仓
    gid = _seed_position(fake, gx)                 # 价跌到 98.5→net long → 平仓=sell
    fake.set_bid_ask(SYM, 98.4, 98.6)              # last=98.5, bid=98.4, ask=98.6
    placed = []
    orig = fake.create_limit_order
    fake.create_limit_order = lambda symbol, side, price, size, **k: (
        placed.append((side, price, k.get('client_oid'))) or orig(symbol, side, price, size, **k))
    fake._match = lambda *a, **k: None             # resting(不即时成交)
    _run_close_timeout(gx, gid)
    maker = [(s, p) for s, p, c in placed if c and str(c).endswith('m')]
    assert maker, 'maker post-only 限价单未下出'
    assert maker[0][0] == 'sell' and maker[0][1] == 98.6   # 挂 best ask,非 last 98.5


def test_maker_buy_close_places_at_best_bid_not_last(store):
    # 平空头(buy)的 maker post-only 挂 best bid(被动侧)。
    fake, _, gx = build_stack(store, maker=True)     # 默认价 100 开仓
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 101.5); gx.sync(gid, SYM)    # 价涨→sell 线成交→net short → 平仓=buy
    assert fake.fetch_positions(SYM).net_size < 0
    fake.set_bid_ask(SYM, 101.4, 101.6)             # last=101.5, bid=101.4, ask=101.6
    placed = []
    orig = fake.create_limit_order
    fake.create_limit_order = lambda symbol, side, price, size, **k: (
        placed.append((side, price, k.get('client_oid'))) or orig(symbol, side, price, size, **k))
    fake._match = lambda *a, **k: None
    _run_close_timeout(gx, gid)
    maker = [(s, p) for s, p, c in placed if c and str(c).endswith('m')]
    assert maker and maker[0][0] == 'buy' and maker[0][1] == 101.4   # 挂 best bid,非 last 101.5


def test_urgent_reason_never_uses_maker(store):
    fake, _, gx = build_stack(store, maker=True)
    gid = _seed_position(fake, gx)
    seen = []
    orig = fake.create_limit_order

    def spy(*a, **k):
        seen.append(k.get('client_oid'))
        return orig(*a, **k)

    fake.create_limit_order = spy
    gx.close(gid, SYM, '固定止损')
    closes = [c for c in seen if c and ':close:' in str(c)]
    assert closes == []                                      # 紧急链恒市价
    assert gx.grids.get(gid).status == 'CLOSED'
