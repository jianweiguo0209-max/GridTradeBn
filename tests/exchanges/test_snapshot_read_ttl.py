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


# ---- algo 簿 TTL 缓存（spec 2026-07-23）----


class _BookClient(FakeBinanceClient):
    """两簿分开计数：常规簿必须每调真取，algo 簿按 TTL 复用。"""
    def __init__(self):
        super().__init__()
        self.regular_calls = 0
        self.trigger_calls = 0

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        if params and params.get('trigger'):
            self.trigger_calls += 1
            return [{'id': '9', 'clientOrderId': '', 'symbol': 'BTC/USDT:USDT',
                     'side': 'sell', 'price': 40000.0, 'amount': 1.0,
                     'status': 'open', 'filled': 0.0}]
        self.regular_calls += 1
        return [{'id': '7', 'clientOrderId': '', 'symbol': 'BTC/USDT:USDT',
                 'side': 'buy', 'price': 49000.0, 'amount': 1.0,
                 'status': 'open', 'filled': 0.0}]

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        return {'id': '11', 'clientOrderId': '', 'symbol': 'BTC/USDT:USDT',
                'side': side, 'price': 0.0, 'amount': amount,
                'status': 'open', 'filled': 0.0}


def test_algo_book_cached_regular_always_fresh():
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    clk.t += 10.0
    out = a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.regular_calls == 2                  # 常规簿每调真取（判成交核心）
    assert c.trigger_calls == 1                  # algo 簿 TTL 内复用
    assert sorted(o.id for o in out) == ['7', '9']   # merge 结果不变


def test_algo_ttl_expiry_refetches():
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    clk.t += 61.0
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2


def test_algo_cache_invalidated_by_create_stop_order():
    # 挂新丝 → 缓存失效 → 下一轮 algo 簿真取（新丝立即可见，省 order_status 兜底链）
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    a.create_stop_order('BTC/USDT:USDT', 'sell', 1.0, 40000.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2


def test_algo_disabled_when_ttl_nonpositive():
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=0.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2                  # 关闭=每次真取（旧行为）


def test_algo_ghost_row_persists_within_ttl_then_clears():
    # 撤丝后的幽灵行：TTL 窗内仍可见（三态判只看存在性,不会因幽灵行动作）,到期后消失。
    # 这是 spec 预注册的可接受行为——为它立契约,防未来有人"顺手"给撤单也加失效钩子
    # 时误以为现状是 bug。
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])           # 缓存含丝 '9'
    c.fetch_open_orders = (lambda symbol=None, since=None, limit=None, params=None:
                           [] if (params and params.get('trigger'))
                           else [{'id': '7', 'clientOrderId': '',
                                  'symbol': 'BTC/USDT:USDT', 'side': 'buy',
                                  'price': 49000.0, 'amount': 1.0,
                                  'status': 'open', 'filled': 0.0}])  # 交易所侧丝已撤
    clk.t += 10.0
    out = a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert '9' in {o.id for o in out}                    # TTL 窗内幽灵行仍在
    clk.t += 61.0
    out = a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert '9' not in {o.id for o in out}                # 到期真取后消失


def test_cancel_order_trigger_success_invalidates_algo_cache():
    # 撤丝(trigger 回退成功)必须失效 algo 簿缓存：同币并发(cap2) A 格关格撤丝、B 格仍活，
    # 若不失效，TTL 窗内(至多 60s≈2-4 monitor 轮)缓存仍含 A 的幽灵丝行，落在 B 受保护集
    # 合外——孤儿清扫(reconciler.py:81-84)对已不存在的单再 cancel_order 一次 → OrderNotFound
    # 上抛、B 所在 unit 本轮 reconcile 中断,degraded 计数被同币关格污染。
    import ccxt
    c, clk = _BookClient(), _Clock()
    def cancel(order_id, symbol=None, params=None):
        if not (params or {}).get('trigger'):
            raise ccxt.OrderNotFound('binanceusdm {"code":-2011,"msg":"Unknown order sent."}')
    c.cancel_order = cancel
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])           # 填充缓存(trigger_calls==1)
    a.cancel_order('BTC/USDT:USDT', '9')                 # 常规 -2011 → trigger 回退成功
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2                          # 缓存被失效，重新真取


def test_cancel_order_regular_success_does_not_invalidate_algo_cache():
    # 逆向护栏：常规簿撤单直接成功(非 algo 单)不该白白击穿缓存——保留 TTL 缓存收益。
    c, clk = _BookClient(), _Clock()
    c.cancel_order = lambda order_id, symbol=None, params=None: None
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])           # 填充缓存(trigger_calls==1)
    a.cancel_order('BTC/USDT:USDT', '7')                 # 常规撤单直接成功
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 1                          # 缓存保留，未击穿


def test_from_credentials_passes_ttls():
    from gridtrade.exchanges.binance import BinanceAdapter
    a = BinanceAdapter.from_credentials('k', 's', income_ttl_sec=5.0,
                                        algo_book_ttl_sec=7.0)
    assert a.income_ttl_sec == 5.0 and a.algo_book_ttl_sec == 7.0
