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
                self.ex._book_ids.setdefault(grid_id, set()).add(fill.trade_id)

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

    # ── 关格净额化 ──

    def close_share(self, grid_id, symbol):
        """关格净额化(finalize_close 兄弟分支收编):
        ① clamp reduce 自己份额(v23 语义:只平交易所净仓同号部分,≤3 次);每次 reduce
           写 ledger:reduce 合成行入本格账本——账本始终反映"还剩多少没平",崩溃续平时
           restore 重放即恢复,不会二次转仓(claim 真相源是 live 账本,非 accounting 快照);
        ② 残余(被兄弟对冲的部分)按 mark 价转给反号 claim 幸存格 → 双方模型与交易所对齐
           (v23 残留根治:此前正确不动手但幸存格永久带差);
        ③ 无幸存格接收(同轮全关竞态)→ 留差给漂移告警(概率极低,不越权动仓)。"""
        ex = self.ex
        remaining = self.claim(grid_id)
        if abs(remaining) <= ex.min_amount:
            return
        pos = ex.adapter.fetch_positions(symbol)
        attempt = 0
        while (abs(remaining) > ex.min_amount and pos.net_size * remaining > 0
               and attempt < 3):
            qty = min(abs(remaining), abs(pos.net_size))
            side = 'sell' if remaining > 0 else 'buy'
            ex.adapter.create_market_order(symbol, side, qty, reduce_only=True,
                                           client_oid='%s:close:%d' % (grid_id, attempt))
            self._record_synthetic(grid_id, side, qty,
                                   float(ex.adapter.fetch_price(symbol)), 'reduce')
            remaining -= qty if remaining > 0 else -qty
            attempt += 1
            pos = ex.adapter.fetch_positions(symbol)
        if abs(remaining) <= ex.min_amount:
            return
        g = ex.grids.get(grid_id)
        sibs = [s for s in ex.grids.list_active()
                if s.symbol == symbol and s.exchange == g.exchange and s.id != grid_id]
        if not sibs:
            return
        # 优先反号 claim 幸存格(正是对冲掉我们份额的一方);cap=2 至多一个兄弟
        target = next((s for s in sibs if self.claim(s.id) * remaining < 0), sibs[0])
        if len(sibs) > 1:
            print('[ledger] WARN close_share %s: %d survivors (cap=2 设计外) target=%s'
                  % (grid_id, len(sibs), target.id), flush=True)
        self.settle_transfer(grid_id, target.id, symbol, remaining,
                             float(ex.adapter.fetch_price(symbol)), 'closeshare')

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
                    ex._book_ids.setdefault(grid_id, set()).add(fill.trade_id)
                n += 1
        if n:
            print('[ledger] fuse fills ingested grid=%s %s oid=%s n=%d'
                  % (grid_id, symbol, fuse_oid, n), flush=True)
        return n
