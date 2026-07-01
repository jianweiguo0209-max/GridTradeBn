"""回归锁：真中性网格记账在净多/净空/穿零下均等于模型无关的现金流盯市真值。
真值 = Σ(卖入现金 − 买出现金) + 期末净仓×mark − 真实费。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
      'stop_low_price': 80.0, 'stop_high_price': 120.0}
CAP, LEV = 1000.0, 5.0


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.001, 1e-6, 1e-6, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    return ex, GridExecutor(ex, store, cap=CAP, leverage=LEV)


def _drive(gx, ex, gid, path):
    """细步长逐线成交（贴近真实连续行情，避免批量成交破坏 last_touch 链），每步后 sync。"""
    cur = ex.fetch_price(SYM)
    for target in path:
        step = 0.1 if target >= cur else -0.1
        p = cur
        while (step > 0 and p < target) or (step < 0 and p > target):
            p = round(p + step, 4)
            if (step > 0 and p > target) or (step < 0 and p < target):
                p = target
            ex.set_price(SYM, p)
            gx.sync(gid, SYM)
        cur = target


def _oracle_pnl(ex, mark):
    cash = fees = 0.0
    for t in ex.fetch_my_trades(SYM):
        cash += t.price * t.size if t.side == 'sell' else -t.price * t.size
        fees += t.fee
    return cash + ex.fetch_positions(SYM).net_size * mark - fees


def test_neutral_accounting_exact_ending_long(store):
    ex, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    _drive(gx, ex, gid, [90, 110, 95, 105, 97])
    mark = 103.3
    eng = (gx.live[gid].snapshot(mark)['net_value'] - 1.0) * CAP
    assert abs(eng - _oracle_pnl(ex, mark)) < 1e-6


def test_neutral_accounting_exact_sustained_short(store):
    ex, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    _drive(gx, ex, gid, [110])          # 涨到顶 → 持续净空
    mark = 112.0
    snap = gx.live[gid].snapshot(mark)
    assert snap['net_position'] < 0
    eng = (snap['net_value'] - 1.0) * CAP
    assert abs(eng - _oracle_pnl(ex, mark)) < 1e-6


def test_neutral_accounting_exact_zero_crossing_to_short(store):
    ex, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    _drive(gx, ex, gid, [90, 110, 92, 108, 100, 111])   # 多次穿零收净空
    mark = 108.0
    snap = gx.live[gid].snapshot(mark)
    assert snap['net_position'] < 0
    eng = (snap['net_value'] - 1.0) * CAP
    assert abs(eng - _oracle_pnl(ex, mark)) < 1e-6
    assert abs(snap['net_position'] - ex.fetch_positions(SYM).net_size) < 1e-9
