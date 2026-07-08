"""Reconciler：重启对账自愈。restore 重建执行器内存态；reconcile_open_orders 按 exchange order id 对账。"""
import itertools

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
from gridtrade.state.models import GridOrder


class Reconciler:
    def __init__(self, executor, replace_grace=2):
        self.ex = executor
        # E2：重挂宽限——一张 open 单连续 replace_grace 轮从挂单簿消失才重挂。延迟窗口内
        # 「成交但成交尚不可见」也表现为消失；立即重挂会覆盖成交 oid 致漏摄入。给 sync 时间先摄入。
        self._replace_grace = replace_grace
        self._missing = {}   # grid_id -> {client_oid: 连续 missing 轮数}

    def restore(self, grid_id):
        ex = self.ex
        g = ex.grids.get(grid_id)
        if g is None:
            raise ValueError('grid %s not found' % grid_id)
        # cap 必须用网格行持久化的开仓真值（动态 cap 下 ex.cap 是 config 默认，会错）：
        # mainnet 2026-07-06 实证——用 ex.cap($100) 重建 cap=$302 的网格 → order_num 缩 1/3
        # （补单 $8.63<$10 被拒/静默 1/3 量成交）+ LiveEquity 分母错 → 止损止盈 3 倍提前。
        grid_cap = g.cap if g.cap else ex.cap
        gi = grid_order_info(grid_cap, ex.gearing, g.low_price, g.high_price,
                             int(g.grid_count), g.stop_low_price, g.stop_high_price,
                             min_amount=ex.min_amount, max_rate=1.0)
        price_array = [float(p) for p in gi['价格序列']]
        # order_num 优先取开仓持久化真值（与在场挂单/补单口径逐位一致）；老行缺失才回退重算
        order_num = float(g.order_num) if g.order_num else float(gi['每笔数量'])
        ex._geom[grid_id] = {'price_array': price_array, 'order_num': order_num}
        ex._seq[grid_id] = itertools.count(10_000_000)  # 高位起，避免与历史 seq 相撞

        live = LiveEquity(grid_cap, ex.fee, ex.c_rate_taker, entry_price=g.entry_price)
        # 真中性：无 init 底仓（与 open 对称）；仅从持久化成交重建，否则重启后模型多出幻影多头。
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
            live.record_fill(f.price, f.side, f.size, f.ts, f.fee)
        acc = ex.accounting.get(grid_id)
        if acc is not None:
            live.funding_paid = acc.funding_paid      # recover cumulative funding (durable)
        ex.live[grid_id] = live
        ex._trade_cursor[grid_id] = ex.fills.max_ts(grid_id)
        # 无已推进的游标（acc 缺失或 0=开仓后尚未 sync）时回退到开仓时刻，而非 0，
        # 否则首次 sync 会把开仓前的历史 funding 计入本网格。
        ex._funding_cursor[grid_id] = (acc.funding_cursor if acc is not None and acc.funding_cursor
                                       else g.created_at)
        ex._fuses[grid_id] = {'low': g.fuse_low_oid, 'high': g.fuse_high_oid}

    def reconcile_open_orders(self, grid_id, symbol, snapshot=None):
        ex = self.ex
        # 按 exchange order id 对账（跨所通用；HL open order 只带 oid、不带我方 cloid）。
        expected = {o.exchange_order_id: o for o in ex.orders.list_open_by_grid(grid_id)
                    if o.exchange_order_id}
        src = (snapshot.orders_for(symbol) if snapshot is not None
               else ex.adapter.fetch_open_orders(symbol))
        on_exchange = {o.id: o for o in src}

        # 「unexpected 撤单」的受保护集合 = 本币**全部活跃格**的挂单 ∪ 保险丝——
        # cap=2 后同币可有多格，只按本格 expected 判孤儿会互撤同门全部挂单
        # （mainnet KIOXIA 2026-07-06 实证：双格互杀、挂单存活仅 33s、两格经济死亡）。
        # 保险丝必须在内：HL fetch_open_orders 含 trigger 单而 fuse 不在 grid_orders。
        g = ex.grids.get(grid_id)
        protected = set(expected)
        for sib in ex.grids.list_active():
            if sib.symbol != symbol or sib.exchange != g.exchange:
                continue
            for oid in (sib.fuse_low_oid, sib.fuse_high_oid):
                if oid is not None:
                    protected.add(oid)
            if sib.id == grid_id:
                continue
            protected.update(o.exchange_order_id
                             for o in ex.orders.list_open_by_grid(sib.id)
                             if o.exchange_order_id)

        canceled = 0
        for oid, o in on_exchange.items():
            if oid not in protected:
                ex.adapter.cancel_order(symbol, o.id)
                canceled += 1

        missing = self._missing.setdefault(grid_id, {})
        seen_missing = set()
        replaced = 0
        for oid, go in expected.items():
            if oid not in on_exchange:
                # E2 宽限：连续 missing 达到 grace 才处置——延迟窗口内可能是「成交但成交尚不可见」，
                # 立即重挂会覆盖成交 oid → 漏摄入。给 sync 时间先摄入（它会把该单标 closed、移出 expected）。
                cnt = missing.get(go.client_oid, 0) + 1
                missing[go.client_oid] = cnt
                seen_missing.add(go.client_oid)
                if cnt < self._replace_grace:
                    continue
                # 三态主判(spec 2026-07-09,复用 reconcile_fuses 模式)：重挂前问权威状态——
                # 'filled'=已吃满、成交由 sync 摄入(oid 已保真可匹配),重挂即重复建仓,禁止;
                # 'open'=信息面盲区仍在挂,不动;其余(canceled/unknown)才撤旧重挂。
                status = ex.adapter.order_status(symbol, oid)
                if status in ('open', 'filled'):
                    missing.pop(go.client_oid, None)
                    continue
                try:
                    ex.adapter.cancel_order(symbol, oid)
                except Exception:
                    pass
                # 部分成交后只重挂残量(size−filled)；残量为尘 → 线视为完成,闭合行不重挂。
                done = float(go.filled or 0.0)
                remnant = float(go.size) - done
                if remnant <= max(ex.min_amount, float(go.size) * 1e-6):
                    ex.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                               line_index=go.line_index, side=go.side,
                                               price=go.price, size=go.size, status='closed',
                                               exchange_order_id=go.exchange_order_id,
                                               filled=done))
                    missing.pop(go.client_oid, None)
                    continue
                order = ex.adapter.create_limit_order(symbol, go.side, go.price, remnant,
                                                      post_only=False, client_oid=go.client_oid)
                placed = float(getattr(order, 'size', 0.0) or 0.0) or remnant
                # size 校正为 filled+交易所量化后的残量：吃满判定基准与真实可成交量一致
                ex.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                           line_index=go.line_index, side=go.side, price=go.price,
                                           size=done + placed, status='open',
                                           exchange_order_id=getattr(order, 'id', None),
                                           filled=done))
                missing.pop(go.client_oid, None)   # 重挂后清零
                replaced += 1
        for coid in list(missing):                 # 不再 missing（重回 book / 已 closed 移出 expected）→ 清零
            if coid not in seen_missing:
                missing.pop(coid)
        return {'canceled': canceled, 'replaced': replaced}

    def check_position_drift(self, grid_id, symbol, *, tol_lots=1.5, snapshot=None):
        """净仓对账（防御纵深）：比较模型净仓（grid_accounting.net_position）与交易所真实持仓。

        **只读告警**，不自动改仓（自动纠仓风险高，留人工/后续处置）。容差 = tol_lots × 每格量
        （正常 sync 时序内的瞬时差应 < 1 格；持续超过 ~1.5 格即真实背离，如漏摄入成交）。
        无每格量（未 restore）时容差 0。返回 None 表示无法判定（无记账行）。
        """
        ex = self.ex
        acc = ex.accounting.get(grid_id)
        if acc is None:
            return None
        geom = ex._geom.get(grid_id)
        order_num = float(geom['order_num']) if geom else 0.0
        # cap=2 后同币可有多格，而交易所仓位是账户级按币聚合——model 必须取本币
        # **全部活跃格**的净仓之和，否则双格下必然假背离。容差同样按格数放大。
        g = ex.grids.get(grid_id)
        model = 0.0
        n_sib = 0
        for sib in ex.grids.list_active():
            if sib.symbol != g.symbol or sib.exchange != g.exchange:
                continue
            sib_acc = acc if sib.id == grid_id else ex.accounting.get(sib.id)
            if sib_acc is not None:
                model += float(sib_acc.net_position)
                n_sib += 1
        order_num *= max(1, n_sib)
        if snapshot is not None:
            pos = snapshot.position(symbol)
            real = float(pos) if pos is not None else 0.0   # 快照无仓位行 = 交易所 flat
        else:
            real = float(ex.adapter.fetch_positions(symbol).net_size)
        drift = model - real
        tol = tol_lots * order_num
        return {'grid_id': grid_id, 'model': model, 'exchange': real,
                'drift': drift, 'tol': tol, 'ok': abs(drift) <= tol}

    def _fuse_filled(self, symbol, oid, since_ms=None, snapshot=None):
        """保险丝是否已成交。按 exchange order id 匹配（唯一）；since_ms 作限时优化，
        传 None 则全量扫（FakeExchange 用逻辑计数器 ts，与 epoch ms 不可比较时退化全量）。

        快照路径盲区（XYZ-MSTR 2026-07-05 实证）：快照 trades 窗口起点=全格最小游标，
        活跃格会把窗口推过安静格的保险丝成交时刻 → 漏判"已触发"→ 误重挂覆写 oid →
        成交证据永久丢失、模型永久背离。故快照查不到时**重挂前逐格全量直查**兜底——
        本方法只在 fuse 从 book 消失的罕见分支被调用，多一次调用换确定性。
        """
        if oid is None:
            return False
        if snapshot is not None:
            if any(t.order_id == oid for t in snapshot.trades_for(symbol)):
                return True
            return any(t.order_id == oid
                       for t in self.ex.adapter.fetch_my_trades(symbol, since_ms=None))
        return any(t.order_id == oid
                   for t in self.ex.adapter.fetch_my_trades(symbol, since_ms=since_ms))

    def reconcile_fuses(self, grid_id, symbol, snapshot=None):
        """灾难保险丝三态对账：在挂→无动作；已触发→撑网全拆；被丢→撤旧+重挂。

        三态主判升级（KIOXIA/XYZ-MSTR 事故根治 2026-07-06）：fuse 不在（我们可见的）
        挂单簿时先问 orderStatus 权威状态——'open'=信息面盲区（如 builder-dex），不动；
        'filled'=已触发；其余才重挂且**先撤旧 oid**（防不可见在挂单堆积成孤儿，
        166 张实证）。orderStatus 不可用(unknown)时退回 fills 直查后备。"""
        ex = self.ex
        if not ex.stop_orders_enabled:
            return {'replaced': 0, 'fired': False}
        g = ex.grids.get(grid_id)
        src = (snapshot.orders_for(symbol) if snapshot is not None
               else ex.adapter.fetch_open_orders(symbol))
        on_exchange = {o.id for o in src}
        specs = [('low', 'sell', g.stop_low_price, g.fuse_low_oid),
                 ('high', 'buy', g.stop_high_price, g.fuse_high_oid)]
        replaced = 0
        for key, side, trigger, oid in specs:
            if oid is not None and oid in on_exchange:
                continue                                   # 在挂
            status = (ex.adapter.order_status(symbol, oid)
                      if oid is not None else 'canceled')
            if status == 'open':
                continue                                   # 权威在挂（信息面盲区）→ 不动
            if status == 'filled' or (status == 'unknown'
                                      and self._fuse_filled(symbol, oid,
                                                            snapshot=snapshot)):
                # 丝成交先入触发格账本(真实 fee → 计入 record pnl;账本含 reduce 后
                # close_share 残余计算自洽,兄弟份额经标准转仓结算,不再互噬)。
                ex.ledger.ingest_fuse_fills(grid_id, symbol, oid)
                ex.close(grid_id, symbol, '保险丝触发')   # 已触发 -> 撑网全拆
                return {'replaced': replaced, 'fired': True}
            # 被丢/已撤（或迁移空 oid）-> 先撤旧（容错：已没则 no-op）再重挂，回写新 oid
            if oid is not None:
                try:
                    ex.adapter.cancel_order(symbol, oid)
                except Exception:
                    pass
            worst = float(g.grid_count) * float(g.order_num)
            order = ex.adapter.create_stop_order(
                symbol, side, worst, trigger, reduce_only=True,
                slippage=ex.stop_slippage, client_oid='%s:fuse:%s' % (grid_id, key))
            new_oid = getattr(order, 'id', None)
            ex.grids.set_fuse_oids(grid_id, **{'%s_oid' % key: new_oid})
            ex._fuses.setdefault(grid_id, {})[key] = new_oid
            replaced += 1
        return {'replaced': replaced, 'fired': False}
