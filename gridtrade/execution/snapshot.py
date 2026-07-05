"""AccountSnapshot：monitor 轮首账户级批量读（设计：docs/superpowers/specs/2026-07-06-account-snapshot-batch-reads-design.md）。

每轮 5 次账户级调用替代逐格逐调（HL 全部为账户级端点，权重与格数解耦）。
不可变只读对象，并行单元共享零竞态；构建失败异常上抛，由 cycle 整轮跳过。
"""
from dataclasses import dataclass

from gridtrade.state.models import now_ms


@dataclass(frozen=True)
class AccountSnapshot:
    ts_ms: int
    trades: tuple              # Trade 升序
    orders_by_symbol: dict     # {symbol: tuple(Order)}
    positions: dict            # {symbol: net_size 带符号}
    prices: dict               # {symbol: mid}
    funding_by_symbol: dict    # {symbol: tuple(FundingPayment)} 升序

    def trades_for(self, symbol, since_ms=0):
        return [t for t in self.trades if t.symbol == symbol and t.ts >= since_ms]

    def orders_for(self, symbol):
        return list(self.orders_by_symbol.get(symbol, ()))

    def position(self, symbol):
        return self.positions.get(symbol)      # None=快照无此仓位行（调用方视为 0）

    def price(self, symbol):
        return self.prices.get(symbol)         # None=缺币价（调用方降级报错）

    def funding_for(self, symbol, since_ms=0):
        return [p for p in self.funding_by_symbol.get(symbol, ()) if p.ts >= since_ms]


def build_account_snapshot(adapter, symbols, *, trade_since_ms=0,
                           funding_since_ms=0) -> AccountSnapshot:
    """5 次账户级调用（经 ResilientAdapter 电路）。任一失败 → 异常上抛。"""
    symbols = sorted(set(symbols))
    trades = sorted(adapter.fetch_my_trades_all(symbols, since_ms=trade_since_ms),
                    key=lambda t: t.ts)
    by_sym = {}
    for o in adapter.fetch_open_orders_all(symbols):
        by_sym.setdefault(o.symbol, []).append(o)
    positions = dict(adapter.fetch_positions_all(symbols))
    prices = dict(adapter.fetch_prices_all(symbols))
    funding = {s: tuple(v) for s, v in
               adapter.fetch_funding_payments_all(symbols, since_ms=funding_since_ms).items()}
    return AccountSnapshot(ts_ms=now_ms(), trades=tuple(trades),
                           orders_by_symbol={s: tuple(v) for s, v in by_sym.items()},
                           positions=positions, prices=prices,
                           funding_by_symbol=funding)
