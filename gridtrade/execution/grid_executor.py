"""GridExecutor：自管理挂单网格生命周期（开网/同步补单/平网）。
驱动 ExchangeAdapter + 状态层仓储 + LiveEquity。交易所为订单/持仓真相源；
client_oid='{grid_id}:{line}:{seq}' 确定性映射网格线，供对账。
"""
import itertools

from gridtrade.config import compute_cap
from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
from gridtrade.execution.position_ledger import PositionLedger
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import (ACTIVE, CLOSED, CLOSING, Fill, Grid, GridOrder, OPENING, Record, now_ms)
from gridtrade.state.orders import OrderRepository
from gridtrade.state.records import RecordRepository

# E4：成交游标留重叠——从 max_ts 往回 5min 再拉，靠 fills.add_if_new(trade_id) 去重。
# 防「晚可见、ts 低于被别的成交推高的 max_ts」的成交被游标跳过永久漏（HL fill 可见性延迟）。
_TRADE_REFETCH_OVERLAP_MS = 5 * 60 * 1000


class GridExecutor:
    def __init__(self, adapter, store, *, cap, gearing=None, leverage=None, fee=0.0002,
                 c_rate_taker=0.0005, max_rate=None, min_amount=0.0,
                 stop_orders_enabled=False, stop_slippage=0.15,
                 cap_equity_frac=0.0, cap_min=0.0, cap_max=float('inf')):
        self.adapter = adapter
        self.grids = GridRepository(store)
        self.orders = OrderRepository(store)
        self.accounting = AccountingRepository(store)
        self.records = RecordRepository(store)
        self.fills = FillRepository(store)
        self.cap = float(cap)
        # gearing(单格名义部署倍数,挂单总名义额=gearing×cap)= 旧 leverage×max_rate;
        # 旧参数保留向后兼容(spec 2026-07-07-account-leverage-gearing),折算后行为逐位不变。
        if gearing is None:
            gearing = float(leverage if leverage is not None else 5.0) \
                      * float(max_rate if max_rate is not None else 0.68)
        self.gearing = float(gearing)
        self.fee = float(fee)
        self.c_rate_taker = float(c_rate_taker)
        self.min_amount = float(min_amount)
        self.stop_orders_enabled = bool(stop_orders_enabled)
        self.stop_slippage = float(stop_slippage)
        self.cap_equity_frac = float(cap_equity_frac)
        self.cap_min = float(cap_min)
        self.cap_max = float(cap_max)
        self._fuses = {}      # grid_id -> {'low': exchange_oid, 'high': exchange_oid}
        self.live = {}        # grid_id -> LiveEquity
        self._geom = {}       # grid_id -> dict(price_array, order_num)
        self._seq = {}        # grid_id -> itertools.count
        self._trade_cursor = {}
        self._funding_cursor = {}
        self._book_ids = {}   # grid_id -> 已入内存账本的 trade_id 集合(账本↔DB 对齐)
        # 同币多格内部净额化(spec 2026-07-08-position-ledger):按仓位操作经账本净差额
        self.ledger = PositionLedger(self)

    def _next_oid(self, grid_id, line_index):
        return '%s:%d:%d' % (grid_id, line_index, next(self._seq[grid_id]))

    def is_loaded(self, grid_id) -> bool:
        """内存态是否已就绪（同进程 open 或 reconciler.restore 重建后）。"""
        return grid_id in self._geom

    def _resolve_cap(self):
        """cap_equity_frac>0 时按当前权益动态定 cap = clamp(equity×frac, min, max)；
        未启用或余额读取失败 → 回退固定 self.cap（不阻塞开网）。"""
        if not self.cap_equity_frac or self.cap_equity_frac <= 0:
            return self.cap
        try:
            equity = float(self.adapter.fetch_balance().equity)
        except Exception:
            return self.cap
        dyn = compute_cap(equity, self.cap_equity_frac, self.cap_min, self.cap_max)
        return dyn if dyn is not None else self.cap

    def open(self, exchange, symbol, grid_params, *, offset=0, tag='', cap=None):
        if cap is None:
            cap = self._resolve_cap()
        gi = grid_order_info(cap, self.gearing, grid_params['low_price'],
                             grid_params['high_price'], int(grid_params['grid_count']),
                             grid_params['stop_low_price'], grid_params['stop_high_price'],
                             min_amount=self.min_amount, max_rate=1.0)
        if gi is None:
            raise RuntimeError('建网失败：保证金不足')
        price_array = [float(p) for p in gi['价格序列']]
        order_num = float(gi['每笔数量'])
        entry = float(self.adapter.fetch_price(symbol))

        # 杠杆感知槽位上限(spec 2026-07-11-symbol-desk 组件四):maxlev 未知 → None=原行为
        from gridtrade.config import DEFAULT_TIER_POLICY
        from gridtrade.core.tier_policy import cap_for
        try:
            _ml = self.adapter.max_leverage(symbol)
        except Exception:
            _ml = None                       # fail-open:取数失败退化为无分级
        _slots = cap_for(symbol, DEFAULT_TIER_POLICY, maxlev=_ml)
        grid = self.grids.create(max_slots=_slots, grid=Grid(
            id='', exchange=exchange, symbol=symbol, status='PENDING', offset=offset, tag=tag,
            entry_price=entry, low_price=grid_params['low_price'], high_price=grid_params['high_price'],
            stop_low_price=grid_params['stop_low_price'], stop_high_price=grid_params['stop_high_price'],
            grid_count=int(grid_params['grid_count']), order_num=order_num,
            leverage=self.gearing, cap=cap))   # 列名沿用,语义=gearing(审计;行为惰性,见 DB 影响矩阵)
        gid = grid.id
        self.accounting.init(gid)
        self._geom[gid] = {'price_array': price_array, 'order_num': order_num}
        self._seq[gid] = itertools.count()
        self.live[gid] = LiveEquity(cap, self.fee, self.c_rate_taker, entry_price=entry)
        self._trade_cursor[gid] = 0
        self._book_ids[gid] = set()
        # 资金费游标从开仓时刻起算（而非 0），否则会把开仓前的历史 funding 计入本网格。
        self._funding_cursor[gid] = grid.created_at

        self.grids.transition_status(gid, OPENING, expected_version=grid.version)

        # 真中性：开网不建底仓，净仓从 0 开始（价涨→挂单成交转净空，价跌→转净多）。

        # 逐线挂限价单
        # 下单量先自量化（memory quantized-size-fallback-bug：HL create 响应不带数量，
        # "存回传 amount"退化为存原始值 → AVAX 等量化缩量币吃满永假、线卡死不补单）
        wire_qty = self.adapter.quantize_amount(symbol, order_num)
        for i, p in enumerate(price_array):
            if p > entry:
                side = 'sell'
            elif p < entry:
                side = 'buy'
            else:
                continue
            oid = self._next_oid(gid, i)
            order = self.adapter.create_limit_order(symbol, side, p, wire_qty,
                                                    post_only=False, client_oid=oid)
            # 行 size 存真实可成交量：优先交易所回传，缺失时回退到自量化值（而非原始值）
            placed = float(getattr(order, 'size', 0.0) or 0.0) or wire_qty
            self.orders.upsert(GridOrder(client_oid=oid, grid_id=gid, line_index=i,
                                         side=side, price=p, size=placed, status='open',
                                         exchange_order_id=getattr(order, 'id', None)))

        # 灾难保险丝：两张 reduce-only 触发市价单，破网价触发（reduce_only 封顶到真实仓）。
        # exchange order id 持久化到 grids 行，供跨重启对账判定已触发。
        if self.stop_orders_enabled:
            worst = order_num * int(grid_params['grid_count'])
            low = self.adapter.create_stop_order(
                symbol, 'sell', worst, grid_params['stop_low_price'],
                reduce_only=True, slippage=self.stop_slippage,
                client_oid='%s:fuse:low' % gid)
            high = self.adapter.create_stop_order(
                symbol, 'buy', worst, grid_params['stop_high_price'],
                reduce_only=True, slippage=self.stop_slippage,
                client_oid='%s:fuse:high' % gid)
            self.grids.set_fuse_oids(gid, low_oid=getattr(low, 'id', None),
                                     high_oid=getattr(high, 'id', None))
            self._fuses[gid] = {'low': getattr(low, 'id', None),
                                'high': getattr(high, 'id', None)}

        g2 = self.grids.get(gid)
        self.grids.transition_status(gid, ACTIVE, expected_version=g2.version)
        return gid

    def _replenish_opposite(self, grid_id, symbol, line_index, side):
        """按 sync 同款守卫补对侧单（E2 兜底路径复用；guard=对侧 (line,side) 已有 open 行则跳过）。
        与 sync 内联块语义一致（memory quantized-size-fallback-bug 修复配套）。"""
        geom = self._geom.get(grid_id)
        if not geom:
            return False
        price_array = geom['price_array']
        opp_line = line_index - 1 if side == 'sell' else line_index + 1
        if not (0 <= opp_line < len(price_array)):
            return False
        opp_side = 'buy' if side == 'sell' else 'sell'
        open_lines = {(o.line_index, o.side)
                      for o in self.orders.list_by_grid(grid_id) if o.status == 'open'}
        if (opp_line, opp_side) in open_lines:
            return False
        p = price_array[opp_line]
        rq = self.adapter.quantize_amount(symbol, geom['order_num'])
        oid = self._next_oid(grid_id, opp_line)
        order = self.adapter.create_limit_order(symbol, opp_side, p, rq,
                                                post_only=False, client_oid=oid)
        placed = float(getattr(order, 'size', 0.0) or 0.0) or rq
        self.orders.upsert(GridOrder(client_oid=oid, grid_id=grid_id, line_index=opp_line,
                                     side=opp_side, price=p, size=placed, status='open',
                                     exchange_order_id=getattr(order, 'id', None)))
        return True

    def finalize_filled_order(self, grid_id, symbol, go):
        """E2 兜底（memory quantized-size-fallback-bug）：交易所权威 status='filled' 但行仍
        open（历史行存了未量化 size、吃满判定永假）→ 闭合行 + 腾线 + 补对侧。成交本体
        已由 sync 按真实 fills 全量摄入（记账/账本无缺口），此处只修行状态与呼吸。"""
        self.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                     line_index=go.line_index, side=go.side, price=go.price,
                                     size=go.size, status='closed',
                                     exchange_order_id=go.exchange_order_id,
                                     filled=float(go.filled or 0.0)))
        self._replenish_opposite(grid_id, symbol, go.line_index, go.side)

    def sync(self, grid_id, symbol, *, skip_replenish=False, snapshot=None):
        geom = self._geom[grid_id]
        price_array = geom['price_array']
        order_num = geom['order_num']
        cursor = max(0, self.fills.max_ts(grid_id) - _TRADE_REFETCH_OVERLAP_MS)
        # 快照=轮首账户级批量读（权重与格数解耦）；None=逐格取数（测试基线/回退面）
        trades = (snapshot.trades_for(symbol, since_ms=cursor) if snapshot is not None
                  else self.adapter.fetch_my_trades(symbol, since_ms=cursor))
        # 按 exchange order id 把成交映射回网格线（跨所通用；HL fill 只带 oid，不带 cloid）。
        # 中性底仓/平仓的市价单不在 grid_orders → 其成交 order_id 不在 by_oid，自动排除。
        _all = self.orders.list_by_grid(grid_id)
        by_oid = {o.exchange_order_id: o for o in _all if o.exchange_order_id}
        # 已 resting 的 (line,side) 集合：补对侧单前查重，防同 line 同向重复挂单
        # → 双倍建仓（testnet OP/gt00 实证：中性网格价格震荡下重复单持久叠加）。
        open_lines = {(o.line_index, o.side) for o in _all if o.status == 'open'}
        candidates = [t for t in trades if t.order_id in by_oid]
        candidates.sort(key=lambda t: t.ts)

        new_count = 0
        new_fills_payload = []
        for t in candidates:
            go = by_oid[t.order_id]
            line_index = go.line_index
            fill = Fill(trade_id=str(t.id), grid_id=grid_id, line_index=line_index,
                        side=t.side, price=float(t.price), size=float(t.size),
                        fee=float(t.fee), ts=int(t.ts))
            if not self.fills.add_if_new(fill):
                continue   # 已摄入：去重，跳过（不重复记账/补单）
            new_count += 1
            new_fills_payload.append({'line_index': line_index, 'side': t.side,
                                      'price': float(t.price), 'size': float(t.size),
                                      'fee': float(t.fee), 'ts': int(t.ts)})
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts, float(t.fee))
            self._book_ids.setdefault(grid_id, set()).add(fill.trade_id)
            # 部分成交生命周期(spec 2026-07-09,mainnet GRAM 实证):累计 filled、吃满才
            # closed;行字段保真——旧代码首笔部分成交即 closed 且抹掉 exchange_order_id、
            # size 被 t.size 覆写 → 跨轮后续部分成交无从匹配被静默丢(幻影仓)。
            new_filled = float(go.filled or 0.0) + float(t.size)
            fully = new_filled >= float(go.size) - max(1e-9, float(go.size) * 1e-6)
            go = GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                           line_index=line_index, side=go.side, price=go.price,
                           size=go.size, status='closed' if fully else 'open',
                           exchange_order_id=go.exchange_order_id, filled=new_filled)
            self.orders.upsert(go)
            by_oid[t.order_id] = go        # 同轮多笔部分成交累计正确
            if not fully:
                continue                   # 未吃满:线仍占用、不补单,等后续部分成交
            open_lines.discard((line_index, t.side))   # 吃满离场，其 (line,side) 腾空
            # 补对侧单（halt 时跳过：fills/记账/止损仍正常，但不挂新单）
            if not skip_replenish:
                opp_line = line_index - 1 if t.side == 'sell' else line_index + 1
                if 0 <= opp_line < len(price_array):
                    opp_side = 'buy' if t.side == 'sell' else 'sell'
                    # opp_line 已有同向 resting 单则不重复挂（防双倍建仓）
                    if (opp_line, opp_side) not in open_lines:
                        p = price_array[opp_line]
                        oid = self._next_oid(grid_id, opp_line)
                        rq = self.adapter.quantize_amount(symbol, order_num)
                        try:
                            order = self.adapter.create_limit_order(symbol, opp_side, p, rq,
                                                                    post_only=False, client_oid=oid)
                        except Exception as exc:
                            # 线上只有异常字符串可见：交易所拒补单（如 HL min $10）时必须带上
                            # 实际参数，否则"合法名义额为何被拒"不可查（2026-07-05 VVV 之谜）。
                            # 保留原异常类型（重试/熔断分类、monitor 降级路径不变）。
                            raise type(exc)(
                                'replenish %s %s line=%d px=%.8g sz=%.8g notional=%.2f: %s'
                                % (symbol, opp_side, opp_line, p, order_num,
                                   p * order_num, exc)) from exc
                        placed = float(getattr(order, 'size', 0.0) or 0.0) or rq
                        self.orders.upsert(GridOrder(client_oid=oid, grid_id=grid_id, line_index=opp_line,
                                                     side=opp_side, price=p, size=placed, status='open',
                                                     exchange_order_id=getattr(order, 'id', None)))
                        open_lines.add((opp_line, opp_side))

        # 资金费流水:按签名权重分摊(同币双格曾各记 100% → 双计;交易所只按净仓收一次)。
        # 各格仍用自己的游标摄入同批行、各乘权重;单格 w=1 与旧行为逐位一致。
        fcur = self._funding_cursor.get(grid_id, 0)
        pays = (snapshot.funding_for(symbol, since_ms=fcur) if snapshot is not None
                else self.adapter.fetch_funding_payments(symbol, since_ms=fcur))
        if pays:
            w = self.ledger.funding_weight(grid_id, symbol)
            for p in pays:
                self.live[grid_id].add_funding(p.amount * w)
        if pays:
            self._funding_cursor[grid_id] = pays[-1].ts + 1

        # 账本↔DB 对齐(spec 2026-07-09-book-db-alignment):grid_fills 是第三方可写的
        # 真相源(scheduler 转仓合成行/手工修复补摄入),跨进程写入不经交易所、上面的摄入
        # 循环拉不到(mainnet GRAM 转仓首样本实证:幸存格 acc 停旧值直到重启)→ 每轮把
        # 内存账本收敛到 DB 集合。顺序新行追加;乱序(补历史成交)整本重建——LiveEquity
        # 平均成本路径依赖,乱序追加必错。单进程常规流:自己写的行都在集合里,此步空转。
        known = self._book_ids.setdefault(grid_id, set())
        db_fills = self.fills.list_by_grid(grid_id)          # 已按 ts 升序
        missing = [f for f in db_fills if f.trade_id not in known]
        if missing:
            live = self.live[grid_id]
            last_ts = live.last_fill_ts
            if last_ts is None or all(f.ts >= last_ts for f in missing):
                for f in missing:
                    live.record_fill(f.price, f.side, f.size, f.ts, f.fee)
                    known.add(f.trade_id)
                rebuilt = False
            else:
                fresh = LiveEquity(live.cap, self.fee, self.c_rate_taker,
                                   entry_price=live.entry_price)
                for f in db_fills:
                    fresh.record_fill(f.price, f.side, f.size, f.ts, f.fee)
                fresh.funding_paid = live.funding_paid       # funding 与 fills 分账
                self.live[grid_id] = fresh
                self._book_ids[grid_id] = {f.trade_id for f in db_fills}
                rebuilt = True
            print('[ledger] book catch-up grid=%s rows=%d rebuild=%s'
                  % (grid_id, len(missing), rebuilt), flush=True)

        if snapshot is not None:
            px = snapshot.price(symbol)
            if px is None:   # 快照缺币价（allMids 罕见缺行）→ 本格降级，勿用 0 价算净值
                raise RuntimeError('snapshot missing price for %s' % symbol)
        else:
            px = self.adapter.fetch_price(symbol)
        snap = self.live[grid_id].snapshot(float(px))
        acc = self.accounting.get(grid_id)
        if acc is not None:
            acc.realized_pnl = snap['realized_pnl']
            acc.fee_paid = snap['fee_paid']
            acc.funding_paid = snap['funding_paid']
            acc.net_position = snap['net_position']
            acc.avg_price = snap['avg_price']
            acc.funding_cursor = self._funding_cursor.get(grid_id, 0)
            self.accounting.save(acc)
            self.accounting.bump_peak(grid_id, snap['pnl_ratio'])
        return {'new_fills': new_count, 'fills': new_fills_payload, 'snapshot': snap}

    def close(self, grid_id, symbol, reason):
        # 关格唯一入口(spec 2026-07-11-symbol-desk):单格集合退化,行为 ≡ 旧路径。
        return self.ledger.close_set([grid_id], symbol, reason)[0]

    def _cancel_orders_for(self, grid_id, symbol, cancel_lines):
        """撤单段(finalize_close 拆分,close_set 复用):cancel_lines=True 逐张撤本格
        线单(有其他同币活跃格,不可 cancel_all);恒撤本格保险丝+标记行 canceled(保留
        oid/filled:撤单窗口内在途部分成交仍可按 oid 匹配摄入)。"""
        grid = self.grids.get(grid_id)
        if cancel_lines:
            for o in self.orders.list_open_by_grid(grid_id):
                if o.status == 'open' and o.exchange_order_id:
                    try:
                        self.adapter.cancel_order(symbol, o.exchange_order_id)
                    except Exception:
                        pass        # 已成交/已撤 → 目标态已达
        for oid in (grid.fuse_low_oid, grid.fuse_high_oid):
            if oid:
                try:
                    self.adapter.cancel_order(symbol, oid)
                except Exception:
                    pass
        for o in self.orders.list_open_by_grid(grid_id):
            self.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=grid_id,
                                         line_index=o.line_index, side=o.side, price=o.price,
                                         size=o.size, status='canceled',
                                         exchange_order_id=o.exchange_order_id,
                                         filled=o.filled))

    def _flatten_symbol(self, grid_id, symbol):
        """无兄弟收尾段:symbol 级扫平(孤儿仓卫生,旧行为);reduce 市价单可能部分成交,
        重拉持仓补 reduce 直至 <= min_amount。每步落 ledger:reduce 合成行(spec
        2026-07-12 补):此前扫平退出不入 grid_fills → 账本重放 net≠0,record 只能靠
        snapshot 的 mark 兜、verify-ledger --records 离线不可重验;补行后关格流水自洽,
        与 close_share reduce 同规范(mark 价合成,真实滑点差留给交易所快照)。"""
        pos = self.adapter.fetch_positions(symbol)
        attempt = 0
        while abs(pos.net_size) > self.min_amount and attempt < 3:
            side = 'sell' if pos.net_size > 0 else 'buy'
            qty = abs(pos.net_size)
            self.adapter.create_market_order(symbol, side, qty, reduce_only=True,
                                             client_oid='%s:close:%d' % (grid_id, attempt))
            self.ledger._record_synthetic(grid_id, side, qty,
                                          float(self.adapter.fetch_price(symbol)),
                                          'reduce')
            attempt += 1
            pos = self.adapter.fetch_positions(symbol)

    def _finalize_record(self, grid_id, symbol, reason):
        """落库段(拆分复用):snapshot → record(幂等) → CLOSED → 结构化日志。"""
        grid = self.grids.get(grid_id)
        snap = self.live[grid_id].snapshot(float(self.adapter.fetch_price(symbol)))
        # 记录按该格真实资金计钱：pnl_ratio 分母是 LiveEquity.cap==grid.cap（动态 cap），
        # 乘 executor 静态默认 cap 会整体错标（restore-cap 同族，mainnet 实证低报 3x）。
        grid_cap = grid.cap if grid.cap else self.cap
        if not self.records.list_by_grid(grid_id):   # 幂等：续平不重复落库
            self.records.add(Record(id='', grid_id=grid_id, exchange=grid.exchange, symbol=symbol,
                                    tag=grid.tag, offset=grid.offset, opened_at=grid.created_at,
                                    closed_at=now_ms(), sz=grid_cap, total_pnl=snap['pnl_ratio'] * grid_cap,
                                    pnl_ratio=snap['pnl_ratio'], exit_reason=reason))
        g2 = self.grids.get(grid_id)
        self.grids.transition_status(grid_id, CLOSED, expected_version=g2.version)
        print('[close] grid %s %s tag=%s reason=%s pnl_ratio=%+.6f'
              % (grid_id, symbol, grid.tag, reason, snap['pnl_ratio']), flush=True)
        return {'grid_id': grid_id, 'reason': reason, 'pnl_ratio': snap['pnl_ratio']}

    def finalize_close(self, grid_id, symbol, reason):
        # 幂等续平（grid 须已 CLOSING）：撤单 + 有界 reduce 残仓 + 落库(只一次) + 转 CLOSED。
        # close() 中途失败留下的 CLOSING 网格由 monitor 循环调本方法续平自愈——
        # 否则残仓无人认领、状态机卡死。三段拆分(spec 2026-07-11)后行为逐位不变。
        grid = self.grids.get(grid_id)
        # 续平路径还原关格真因（'周期再平衡(续平)'）：'平仓恢复' 只是恢复动作,裸写会盖真因。
        if reason == '平仓恢复' and grid.close_reason:
            reason = '%s(续平)' % grid.close_reason
        siblings = [g for g in self.grids.list_active()
                    if g.symbol == symbol and g.id != grid_id]
        if not siblings:
            self.adapter.cancel_all(symbol)
        self._cancel_orders_for(grid_id, symbol, cancel_lines=bool(siblings))
        if siblings:
            self.ledger.close_share(grid_id, symbol)
        else:
            self._flatten_symbol(grid_id, symbol)
        return self._finalize_record(grid_id, symbol, reason)
