"""open() 挂单前设杠杆（spec 2026-07-15-open-set-leverage §3.3）：减一档 L；fail-open 不阻断。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'KITE/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
KITE = [{'maxLeverage': 5, 'maxNotional': 5000.0}, {'maxLeverage': 4, 'maxNotional': 10000.0},
        {'maxLeverage': 3, 'maxNotional': 30000.0}, {'maxLeverage': 2, 'maxNotional': 80000.0},
        {'maxLeverage': 1, 'maxNotional': 200000.0}]


def _gx(store, tiers=None, cap=1000.0, gearing=3.4):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    if tiers is not None:
        ex.seed_leverage_tiers(SYM, tiers)
    # gearing= 直传(镜像生产 factory:62 config.grid_gearing);非 leverage=(那会 ×0.68 折算)
    return ex, GridExecutor(ex, store, cap=cap, gearing=gearing)


def test_open_sets_leverage_from_tiers(store):
    # 币安原生(spec 2026-07-19):单侧≈$1870×1.2≈$2244 落 KITE 首档($5k)→ 最高档 5x
    # (旧机制双侧+减一档给 4x;全仓 L 不影响强平,取最高档=押金最少)
    ex, gx = _gx(store, tiers=KITE)
    gx.open(ex.name, SYM, GP, tag='t')
    assert ex._leverage_calls == [(SYM, 5)]        # 首档最高杠杆,不再减档


def test_open_no_tiers_skips_set_leverage(store):
    # 未 seed 档位 → fetch 返 [] → pick None → 不设杠杆(退化现状)
    ex, gx = _gx(store, tiers=None)
    gx.open(ex.name, SYM, GP, tag='t')
    assert ex._leverage_calls == []


def test_open_set_leverage_failure_is_failopen(store):
    # set_leverage 抛异常 → open 不中断,挂单/丝照常(fail-open)
    ex, gx = _gx(store, tiers=KITE)
    def boom(symbol, leverage): raise RuntimeError('-4000 set lev failed')
    ex.set_leverage = boom
    gid = gx.open(ex.name, SYM, GP, tag='t')       # 不抛
    from gridtrade.state.grids import GridRepository
    assert GridRepository(store).get(gid).status == 'ACTIVE'
    assert len(ex.fetch_open_orders(SYM)) == 9      # 9 挂单照常


def test_open_infeasible_warns(store, capsys):
    # worst 名义 > 4x 档上限 → WARN(设尽力 L,-2027 由 open_proposals 隔离)
    tiny = [{'maxLeverage': 5, 'maxNotional': 100.0}, {'maxLeverage': 4, 'maxNotional': 200.0},
            {'maxLeverage': 3, 'maxNotional': 500.0}]
    ex, gx = _gx(store, tiers=tiny)                 # worst 名义 ~ gearing×cap ≫ $200
    gx.open(ex.name, SYM, GP, tag='t')
    out = capsys.readouterr().out
    assert '档上限' in out                    # infeasible 分支独有(grid_executor.py:133)
    assert '跳过(fail-open)' not in out        # 确非 fail-open 异常分支(:137)
    assert ex._leverage_calls != []            # set_leverage 确实调了(尽力 L)
