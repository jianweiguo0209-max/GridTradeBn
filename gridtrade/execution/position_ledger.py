"""PositionLedger:同币多格内部净额化(spec 2026-07-08-position-ledger)。

核心不变量:Σ claims(本币全部活跃格) = 交易所净仓。HL 每币单一净仓、系统每格一本账,
所有"按仓位操作"(关格 reduce/保险丝/funding/对账)必须经账本按净差额进行,否则互相踩踏
(v23 关格相残、丝互噬、funding 双计)。破坏不变量的操作以**合成成交**结算:
trade_id 前缀 'ledger:'、line_index=-1、零费,写 grid_fills;LiveEquity 数学天然消化,
restore 重放自动恢复——claims 持久化零新表。合成行不推进 max_ts 游标(fills.py)。

无状态:全部从 stores/live 派生;claim 真相源 = live 账本(accounting 是上次 sync 快照,
关格续平场景会过期,用它会导致崩溃恢复后二次转仓)。
"""
import itertools

from gridtrade.state.models import Fill, now_ms

LEDGER_PREFIX = 'ledger:'


class PositionLedger:
    def __init__(self, executor):
        self.ex = executor
        self._seq = itertools.count()   # 合成 trade_id 去撞(同 ms 多笔)

    # ── claims ──

    def claim(self, grid_id):
        """该格份额 = live 账本净仓;未加载(如兄弟尚未 restore)回退 accounting 快照。"""
        live = self.ex.live.get(grid_id)
        if live is not None:
            return float(live.net_position)
        acc = self.ex.accounting.get(grid_id)
        return float(acc.net_position or 0.0) if acc is not None else 0.0

    def claims(self, symbol, exchange):
        """{grid_id: claim} 本币全部活跃格(与 finalize_close 兄弟判定同口径)。"""
        return {g.id: self.claim(g.id) for g in self.ex.grids.list_active()
                if g.symbol == symbol and g.exchange == exchange}

    # ── funding 签名权重 ──

    def funding_weight(self, grid_id, symbol):
        """w_g = claim_g / Σclaims。HL 按净仓收费、per-unit 费率均匀 → 签名分摊经济上精确
        (对冲侧负权重=赚对侧 funding);双格权重和=1 ⇒ 总额=账户实收。单格恒 1(现行为)。
        Σ≈0 → 支付本身≈0,均分兜底。"""
        g = self.ex.grids.get(grid_id)
        cl = self.claims(symbol, g.exchange)
        if grid_id not in cl or len(cl) == 1:
            return 1.0
        total = sum(cl.values())
        if abs(total) < max(self.ex.min_amount, 1e-12):
            return 1.0 / len(cl)
        return cl[grid_id] / total

    # ── 合成成交 ──

    def _record_synthetic(self, grid_id, side, qty, px, event):
        """单边合成行:落 grid_fills(去重)+ 已加载的 live 账本同步 record_fill。"""
        ts = now_ms()
        fill = Fill(trade_id='%s%s:%s:%d:%d' % (LEDGER_PREFIX, event, grid_id, ts,
                                                next(self._seq)),
                    grid_id=grid_id, line_index=-1, side=side,
                    price=float(px), size=abs(float(qty)), fee=0.0, ts=ts)
        if self.ex.fills.add_if_new(fill):
            live = self.ex.live.get(grid_id)
            if live is not None:
                live.record_fill(fill.price, fill.side, fill.size, fill.ts, 0.0)

    def settle_transfer(self, from_gid, to_gid, symbol, qty, mark_px, event):
        """内部转仓:from 格转出带符号份额 qty(>0=多头)给 to 格,按 mark 价、零费。
        纯账本操作(交易所净仓不变、不变量保持);双方各realize/建仓于市价,经济上公平。"""
        if abs(qty) <= 0:
            return
        out_side = 'sell' if qty > 0 else 'buy'
        in_side = 'buy' if qty > 0 else 'sell'
        self._record_synthetic(from_gid, out_side, qty, mark_px, event)
        self._record_synthetic(to_gid, in_side, qty, mark_px, event)
        print('[ledger] transfer %s: %s -> %s qty=%+.8g @ %.8g (event=%s)'
              % (symbol, from_gid, to_gid, qty, float(mark_px), event), flush=True)

    # ── 丝成交摄入 ──

    def ingest_fuse_fills(self, grid_id, symbol, fuse_oid):
        """丝成交按 fuse oid 从 trades 摄入触发格账本:真实 trade_id/价格/fee,
        line_index=-1(不属于任何网格线)。真实 trade_id 正常参与 max_ts 游标(它是
        真实交易所成交)。摄入后账本含丝的 reduce → 后续 close_share 残余计算自洽,
        丝成交计入 record pnl(根治 snapshot-fuse-blind-window 余项)。幂等(add_if_new)。"""
        if fuse_oid is None:
            return 0
        ex = self.ex
        n = 0
        for t in ex.adapter.fetch_my_trades(symbol, since_ms=None):
            if t.order_id != fuse_oid:
                continue
            fill = Fill(trade_id=str(t.id), grid_id=grid_id, line_index=-1,
                        side=t.side, price=float(t.price), size=float(t.size),
                        fee=float(t.fee), ts=int(t.ts))
            if ex.fills.add_if_new(fill):
                live = ex.live.get(grid_id)
                if live is not None:
                    live.record_fill(t.price, t.side, t.size, t.ts, float(t.fee))
                n += 1
        if n:
            print('[ledger] fuse fills ingested grid=%s %s oid=%s n=%d'
                  % (grid_id, symbol, fuse_oid, n), flush=True)
        return n
