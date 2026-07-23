"""snapshot 重读降频：income TTL + algo 簿 TTL（spec 2026-07-23-snapshot-heavy-reads-ttl）。"""
from tests.exchanges.test_binance_adapter import FakeBinanceClient


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _adapter(client, clock, income_ttl=300.0, algo_ttl=60.0):
    from gridtrade.exchanges.binance import BinanceAdapter
    return BinanceAdapter(client, income_ttl_sec=income_ttl,
                          algo_book_ttl_sec=algo_ttl, now_fn=clock)


SYM2 = ['BTC/USDT:USDT', 'ETH/USDT:USDT']


def _flat(out):
    return {s: [(p.ts, p.amount) for p in v] for s, v in out.items()}


def test_income_ttl_hit_single_fetch_same_result():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    first = a.fetch_funding_payments_all(SYM2, since_ms=500)
    clk.t += 10.0
    second = a.fetch_funding_payments_all(SYM2, since_ms=500)
    assert len(c.income_calls) == 1              # TTL 内第二调命中缓存
    assert _flat(first) == _flat(second)


def test_income_hit_filters_by_later_since():
    # cursor 前进：命中时按请求 since 本地切片（ETH ts=1000 应被 since=1500 滤掉）
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    a.fetch_funding_payments_all(SYM2, since_ms=500)
    out = a.fetch_funding_payments_all(SYM2, since_ms=1500)
    assert len(c.income_calls) == 1
    assert [p.ts for p in out['BTC/USDT:USDT']] == [2000]
    assert out['ETH/USDT:USDT'] == []            # 键仍在（契约：请求 symbols 全集）


def test_income_since_regression_busts_cache():
    # 新开格 cursor=0 把 since 拉回 → 必须击穿缓存真取（漏记防线）
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    a.fetch_funding_payments_all(SYM2, since_ms=1500)
    out = a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert len(c.income_calls) == 2
    assert [p.ts for p in out['ETH/USDT:USDT']] == [1000]   # 拉回后旧行可见


def test_income_symbols_superset_busts_cache():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    a.fetch_funding_payments_all(['BTC/USDT:USDT'], since_ms=0)
    out = a.fetch_funding_payments_all(SYM2, since_ms=0)     # 新币入快照 → miss
    assert len(c.income_calls) == 2
    assert 'ETH/USDT:USDT' in out


def test_income_ttl_expiry_refetches():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk, income_ttl=300.0)
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    clk.t += 301.0
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert len(c.income_calls) == 2


def test_income_disabled_when_ttl_nonpositive():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk, income_ttl=0.0)
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert len(c.income_calls) == 2              # 关闭=每次真取（旧行为）


def test_income_fetch_error_propagates_and_not_cached():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)

    def boom(params=None):
        raise RuntimeError('income down')
    c.fapiPrivateGetIncome = boom
    import pytest
    with pytest.raises(RuntimeError):
        a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert a._income_cache is None               # 失败不污染缓存
