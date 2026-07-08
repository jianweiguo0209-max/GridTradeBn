# tests/execution/test_funding_split.py
"""funding 签名权重分摊(spec 2026-07-08-position-ledger 冲突③):
同币双格曾各按 symbol+cursor 摄入同一批支付、各记 100% → 双计;
改 w_g=claim_g/Σclaims 后双格合计 == 交易所实收,单格行为不变。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, n_grids=2):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gids = [gx.open('fake', BTC, dict(GP), tag='t%d' % i) for i in range(n_grids)]
    return ex, gx, gids


def test_two_grids_funding_sums_to_payment(store):
    ex, gx, (ga, gb) = _setup(store)
    ex.set_price(BTC, 98.5)                    # 双格买线成交 → 同号净多
    gx.sync(ga, BTC)                           # 建仓轮:双方账本先暖(线上稳态时序;
    gx.sync(gb, BTC)                           # 冷启动首轮权重偏差是 spec 已知豁免)
    # 支付 ts 必须 ≥ 两格游标(=各自 created_at):CI 慢机上 gb 建格可晚 >10ms,
    # 用 ga.created_at+10 会被 gb 的游标过滤 → gb 零摄入(CI 实证 flake)
    opened = max(gx.grids.get(ga).created_at, gx.grids.get(gb).created_at)
    ex.seed_funding_payments(BTC, [(opened + 10, -1.0)])   # 账户实收一笔 -1.0
    gx.sync(ga, BTC)
    gx.sync(gb, BTC)
    total = gx.live[ga].funding_paid + gx.live[gb].funding_paid
    assert abs(total - (-1.0)) < 1e-9          # 现状 bug 下这里是 -2.0


def test_single_grid_funding_unchanged(store):
    ex, gx, (ga,) = _setup(store, n_grids=1)
    ex.set_price(BTC, 98.5)
    opened = gx.grids.get(ga).created_at
    ex.seed_funding_payments(BTC, [(opened + 10, 0.7)])
    gx.sync(ga, BTC)
    assert abs(gx.live[ga].funding_paid - 0.7) < 1e-12
