"""GateChain.on_reject 钩子：拒绝动作结构化外送(落库审计),fail-soft 不阻断开仓。"""
from gridtrade.execution.gates import (AdmissionGate, GateChain, GateResult,
                                       GridProposal)


def _p(symbol='BTC/USDT:USDT'):
    return GridProposal(exchange='binance', symbol=symbol, tag='gt7',
                        grid_params={'low_price': 90.0, 'high_price': 110.0,
                                     'grid_count': 10, 'stop_low_price': 85.0,
                                     'stop_high_price': 115.0})


class _RejectEth(AdmissionGate):
    def check(self, proposal):
        ok = proposal.symbol != 'ETH/USDT:USDT'
        return GateResult(ok, 'RejectEth', '' if ok else 'eth blocked')


def test_on_reject_called_with_proposal_and_result_only_on_rejection():
    seen = []
    chain = GateChain([_RejectEth()], on_reject=lambda p, r: seen.append((p, r)))
    kept = chain.filter([_p('BTC/USDT:USDT'), _p('ETH/USDT:USDT')])
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT']
    assert len(seen) == 1
    p, r = seen[0]
    assert p.symbol == 'ETH/USDT:USDT' and p.tag == 'gt7'
    assert r.gate == 'RejectEth' and r.reason == 'eth blocked'


def test_on_reject_raising_never_blocks_batch():
    # 审计失败绝不阻断开仓：钩子抛错 → 其余提议照常过滤,并留痕日志
    logs = []

    def _boom(p, r):
        raise RuntimeError('db down')

    chain = GateChain([_RejectEth()], log=logs.append, on_reject=_boom)
    kept = chain.filter([_p('ETH/USDT:USDT'), _p('SOL/USDT:USDT')])
    assert [p.symbol for p in kept] == ['SOL/USDT:USDT']
    assert any('audit' in m for m in logs), logs


def test_on_reject_default_none_backwards_compatible():
    chain = GateChain([_RejectEth()])
    kept = chain.filter([_p('ETH/USDT:USDT'), _p('SOL/USDT:USDT')])
    assert [p.symbol for p in kept] == ['SOL/USDT:USDT']
