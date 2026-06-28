import pytest

from gridtrade.execution.gates import (GridProposal, GateResult, AdmissionGate,
                                       GateChain)


def _proposal(symbol='BTC/USDT:USDT', **kw):
    base = dict(exchange='okx', symbol=symbol,
                grid_params={'low_price': 90.0, 'high_price': 110.0,
                             'grid_count': 10, 'stop_low_price': 85.0,
                             'stop_high_price': 115.0})
    base.update(kw)
    return GridProposal(**base)


class _AlwaysPass(AdmissionGate):
    def check(self, proposal):
        return GateResult(True, 'AlwaysPass')


class _AlwaysFail(AdmissionGate):
    def __init__(self, reason='nope'):
        self.reason = reason
    def check(self, proposal):
        return GateResult(False, 'AlwaysFail', self.reason)


def test_chain_all_pass_returns_passed():
    chain = GateChain([_AlwaysPass(), _AlwaysPass()])
    r = chain.evaluate(_proposal())
    assert r.passed is True


def test_chain_short_circuits_on_first_failure():
    # 第二门失败；第三门即便会失败也不应被求值（用 raise 哨兵证明短路）
    class _Boom(AdmissionGate):
        def check(self, proposal):
            raise AssertionError('should not be evaluated after a failure')
    chain = GateChain([_AlwaysPass(), _AlwaysFail('blocked'), _Boom()])
    r = chain.evaluate(_proposal())
    assert r.passed is False
    assert r.gate == 'AlwaysFail' and r.reason == 'blocked'


def test_chain_filter_keeps_only_passing_proposals():
    # 偶数索引放行、奇数拦截，验证 filter 批量语义
    class _BySymbol(AdmissionGate):
        def check(self, proposal):
            ok = proposal.symbol != 'ETH/USDT:USDT'
            return GateResult(ok, 'BySymbol', '' if ok else 'eth blocked')
    chain = GateChain([_BySymbol()])
    props = [_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT'),
             _proposal('SOL/USDT:USDT')]
    kept = chain.filter(props)
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT', 'SOL/USDT:USDT']


def test_empty_chain_passes():
    assert GateChain([]).evaluate(_proposal()).passed is True
