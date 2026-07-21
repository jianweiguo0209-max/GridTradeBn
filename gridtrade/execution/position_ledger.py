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
import threading

from gridtrade.state.models import CLOSED, CLOSING, Fill, now_ms

LEDGER_PREFIX = 'ledger:'


def LEDGER_EPS(ex):
    """零判定锚:prod 未配 min_amount(=0)时,防浮点尘埃(多笔成交带符号和 ~1e-16,
    mainnet ETHFI gt08 实证)写 1e-14 级合成转仓行(2026-07-11 巡查已批的一行修)。"""
    return max(ex.min_amount, 1e-9)


def _split_residual(remaining, survivors):
    """残余分摊(spec 2026-07-11-symbol-desk 组件一)。survivors: [(gid, claim)]。
    反号优先按 |claim| 比例(正是对冲掉本格份额的各方,按对冲贡献分);无反号 →
    全体按 |claim| 比例(总账守恒,避免单格背全部);全零 → 均分(与 funding 兜底同构)。
    守恒逐位:最后一名吃余数,Σ份额 == remaining。N=2 单反号幸存格分得 100% ≡ 旧行为。"""
    opp = [(g, c) for g, c in survivors if c * remaining < 0]
    pool = opp or list(survivors)
    w = [abs(c) for _, c in pool]
    tw = sum(w)
    if tw <= 0:
        w = [1.0] * len(pool)
        tw = float(len(pool))
    out, acc = [], 0.0
    for i, (g, _) in enumerate(pool):
        q = remaining - acc if i == len(pool) - 1 else remaining * (w[i] / tw)
        out.append((g, q))
        acc += q
    return out


class PositionLedger:
    def __init__(self, executor):
        self.ex = executor
        self._seq = itertools.count()   # 合成 trade_id 去撞(同 ms 多笔)
        self._sym_locks = {}            # 币级互斥(close_set;进程内)
        self._sym_locks_guard = threading.Lock()

    def _symbol_lock(self, symbol):
        with self._sym_locks_guard:
            lk = self._sym_locks.get(symbol)
            if lk is None:
                lk = self._sym_locks[symbol] = threading.Lock()
        return lk

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
        if abs(total) < LEDGER_EPS(self.ex):
            return 1.0 / len(cl)
        return cl[grid_id] / total

    # ── 合成成交 ──

    def _record_synthetic(self, grid_id, side, qty, px, event, eid=None, fee=0.0):
        """单边合成行:落 grid_fills(去重)+ 已加载的 live 账本同步 record_fill。
        eid(spec 2026-07-11 组件三):转仓对由 settle_transfer 生成一次、两行共享 →
        审计可精确配对;None=自生成(reduce 等单边行)。格式 'ts-seq'。
        fee(2026-07-14 testnet 实证补):reduce 单边行背后是真实市价单,携真实 taker 费
        入账(调用方经 _reduce_fill_px_fee 回捞);转仓双边行(内部净额化,无真实成交)恒 0。"""
        ts = now_ms()
        if eid is None:
            eid = '%d-%d' % (ts, next(self._seq))
        fill = Fill(trade_id='%s%s:%s:%s' % (LEDGER_PREFIX, event, grid_id, eid),
                    grid_id=grid_id, line_index=-1, side=side,
                    price=float(px), size=abs(float(qty)), fee=float(fee), ts=ts)
        if self.ex.fills.add_if_new(fill):
            live = self.ex.live.get(grid_id)
            if live is not None:
                live.record_fill(fill.price, fill.side, fill.size, fill.ts, float(fee))
                self.ex._book_ids.setdefault(grid_id, set()).add(fill.trade_id)

    def settle_transfer(self, from_gid, to_gid, symbol, qty, mark_px, event):
        """内部转仓:from 格转出带符号份额 qty(>0=多头)给 to 格,按 mark 价、零费。
        纯账本操作(交易所净仓不变、不变量保持);双方各realize/建仓于市价,经济上公平。"""
        if abs(qty) <= 0:
            return
        out_side = 'sell' if qty > 0 else 'buy'
        in_side = 'buy' if qty > 0 else 'sell'
        eid = '%d-%d' % (now_ms(), next(self._seq))   # 一对共享(审计配对锚)
        self._record_synthetic(from_gid, out_side, qty, mark_px, event, eid=eid)
        self._record_synthetic(to_gid, in_side, qty, mark_px, event, eid=eid)
        print('[ledger] transfer %s: %s -> %s qty=%+.8g @ %.8g (event=%s)'
              % (symbol, from_gid, to_gid, qty, float(mark_px), event), flush=True)

    # ── 关格净额化 ──

    def close_share(self, grid_id, symbol, exclude=frozenset(), maker_first=False):
        """关格净额化(finalize_close 兄弟分支收编;spec 2026-07-11-symbol-desk 组件一):
        ① clamp reduce 自己份额(v23 语义:只平交易所净仓同号部分,≤3 次);每次 reduce
           写 ledger:reduce 合成行入本格账本——账本始终反映"还剩多少没平",崩溃续平时
           restore 重放即恢复,不会二次转仓(claim 真相源是 live 账本,非 accounting 快照);
        ② 残余(被兄弟对冲的部分)按 mark 价 **比例分摊** 给幸存格(_split_residual:
           反号优先按 |claim| 比例——正是对冲掉本格份额的各方;N=2 单反号幸存格得 100%,
           与旧行为逐位一致);exclude=同批关格集合(close_set 用),不作接收方;
        ③ 无幸存格接收(同轮全关竞态)→ 留差给漂移告警(概率极低,不越权动仓)。"""
        ex = self.ex
        eps = LEDGER_EPS(ex)
        remaining = self.claim(grid_id)
        if abs(remaining) <= eps:
            return
        pos = ex.adapter.fetch_positions(symbol)
        attempt = 0
        while (abs(remaining) > eps and pos.net_size * remaining > 0
               and attempt < 3):
            qty = min(abs(remaining), abs(pos.net_size))
            side = 'sell' if remaining > 0 else 'buy'
            _orders = ex._place_reduce(symbol, side, qty,
                                       '%s:close:%d' % (grid_id, attempt), maker_first)
            r_px, r_fee, r_filled = 0.0, 0.0, 0.0
            for o in _orders:
                _p, _f, _q = ex._reduce_fill_px_fee(symbol, o)   # 真实成交均价+真实费+真实成交量
                if _q > 0:
                    r_px = (r_px * r_filled + _p * _q) / (r_filled + _q)
                    r_fee += _f
                    r_filled += _q
            # 按**实际成交量**记账+扣 remaining(非请求量 qty):reduce-only 市价单可部分成交,
            # 按 qty 记会过度减仓、remaining 提前归 0 → 循环误退出、交易所留孤儿仓 → 账本背离/熔断
            # (GP 系统实战坑;循环已每轮重读净仓,续减剩余量即可,≤3 次内收敛,超则走残余分摊)。
            if r_filled > eps:
                self._record_synthetic(grid_id, side, r_filled, r_px, 'reduce', fee=r_fee)
                remaining -= r_filled if remaining > 0 else -r_filled
            attempt += 1
            pos = ex.adapter.fetch_positions(symbol)
        if abs(remaining) <= eps:
            return
        g = ex.grids.get(grid_id)
        sibs = [s for s in ex.grids.list_active()
                if s.symbol == symbol and s.exchange == g.exchange
                and s.id != grid_id and s.id not in exclude]
        if not sibs:
            return
        survivors = [(s.id, self.claim(s.id)) for s in sibs]
        shares = _split_residual(remaining, survivors)
        px = float(ex.adapter.fetch_price(symbol))
        print('[ledger] closeshare split %s residual=%+.8g -> %s'
              % (grid_id, remaining,
                 {gid: round(q, 8) for gid, q in shares}), flush=True)
        for gid, q in shares:
            self.settle_transfer(grid_id, gid, symbol, q, px, 'closeshare')

    # ── 关格集合净额化(spec 2026-07-11-symbol-desk 组件二) ──

    def close_set(self, grid_ids, symbol, reason):
        """同币关格集合的唯一入口:币级互斥 → 逐格 CLOSING CAS → 撤单撤丝 →
        **内部预净额**(其余关格 claim 全额纯记账转给执行格,对冲部分账内互相抵消,
        永不触碰交易所)→ 执行格收尾(有外部幸存格走 close_share 残余分摊,无则
        symbol 级扫平)→ 逐格落库。单格集合 ≡ 旧 ex.close 逐位;对冲对同关=零交易所单;
        N 格同关最多 1 张净额市价单(滑点归因=执行格承担,用户已决)。
        已 CLOSED 成员跳过(幂等重入,records 回读)。"""
        ex = self.ex
        with self._symbol_lock(symbol):
            out, todo = [], []
            for gid in grid_ids:
                g = ex.grids.get(gid)
                if g.status == CLOSED:
                    recs = ex.records.list_by_grid(gid)
                    out.append({'grid_id': gid,
                                'reason': recs[0].exit_reason if recs else reason,
                                'pnl_ratio': recs[0].pnl_ratio if recs else 0.0})
                else:
                    todo.append(g)
            if not todo:
                return out
            for g in todo:                       # ①真因落库 + CLOSING CAS(旧 close 头部语义)
                ex.grids.set_close_reason(g.id, reason)
                if g.status != CLOSING:
                    ex.grids.transition_status(g.id, CLOSING,
                                               expected_version=g.version)
            ids = [g.id for g in todo]
            idset = set(ids)
            others = [s for s in ex.grids.list_active()
                      if s.symbol == symbol and s.exchange == todo[0].exchange
                      and s.id not in idset]
            if not others:                        # ②撤单:无外部幸存格 → symbol 级扫除
                ex.adapter.cancel_all(symbol)
            for g in todo:
                ex._cancel_orders_for(g.id, symbol, cancel_lines=bool(others))
            exec_gid = ids[0]
            if len(todo) > 1:                     # ③内部预净额:同号最大者为执行格
                net = ex.adapter.fetch_positions(symbol).net_size
                claims = [(g.id, self.claim(g.id)) for g in todo]
                same = [x for x in claims if x[1] * net > 0]
                exec_gid = max(same or claims, key=lambda x: abs(x[1]))[0]
                px = float(ex.adapter.fetch_price(symbol))
                eps = LEDGER_EPS(ex)
                for gid, c in claims:
                    if gid != exec_gid and abs(c) > eps:
                        self.settle_transfer(gid, exec_gid, symbol, c, px, 'closeset')
            # B案:周期再平衡(含续平变体)且旗标开 → maker-first;紧急原因恒市价
            _mk = (getattr(ex, 'maker_close_rebalance', False)
                   and str(reason).startswith('周期再平衡'))
            if others:                            # ④执行格收尾
                self.close_share(exec_gid, symbol, exclude=frozenset(ids), maker_first=_mk)
            else:
                ex._flatten_symbol(exec_gid, symbol, maker_first=_mk)
            for g in todo:                        # ⑤逐格落库(pnl 已按 mark 实现)
                out.append(ex._finalize_record(g.id, symbol, reason))
            return out

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
