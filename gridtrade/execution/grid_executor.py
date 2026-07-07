"""GridExecutor：自管理挂单网格生命周期（开网/同步补单/平网）。
驱动 ExchangeAdapter + 状态层仓储 + LiveEquity。交易所为订单/持仓真相源；
client_oid='{grid_id}:{line}:{seq}' 确定性映射网格线，供对账。
"""
import itertools

from gridtrade.config import compute_cap
from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
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

        grid = self.grids.create(Grid(
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
        # 资金费游标从开仓时刻起算（而非 0），否则会把开仓前的历史 funding 计入本网格。
        self._funding_cursor[gid] = grid.created_at

        self.grids.transition_status(gid, OPENING, expected_version=grid.version)

        # 真中性：开网不建底仓，净仓从 0 开始（价涨→挂单成交转净空，价跌→转净多）。

        # 逐线挂限价单
        for i, p in enumerate(price_array):
            if p > entry:
                side = 'sell'
            elif p < entry:
                side = 'buy'
            else:
                continue
            oid = self._next_oid(gid, i)
            order = self.adapter.create_limit_order(symbol, side, p, order_num,
                                                    post_only=False, client_oid=oid)
            self.orders.upsert(GridOrder(client_oid=oid, grid_id=gid, line_index=i,
                                         side=side, price=p, size=order_num, status='open',
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
            # 标记成交订单 closed
            self.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                         line_index=line_index, side=t.side, price=t.price,
                                         size=t.size, status='closed'))
            open_lines.discard((line_index, t.side))   # 成交单离场，其 (line,side) 腾空
            # 补对侧单（halt 时跳过：fills/记账/止损仍正常，但不挂新单）
            if not skip_replenish:
                opp_line = line_index - 1 if t.side == 'sell' else line_index + 1
                if 0 <= opp_line < len(price_array):
                    opp_side = 'buy' if t.side == 'sell' else 'sell'
                    # opp_line 已有同向 resting 单则不重复挂（防双倍建仓）
                    if (opp_line, opp_side) not in open_lines:
                        p = price_array[opp_line]
                        oid = self._next_oid(grid_id, opp_line)
                        try:
                            order = self.adapter.create_limit_order(symbol, opp_side, p, order_num,
                                                                    post_only=False, client_oid=oid)
                        except Exception as exc:
                            # 线上只有异常字符串可见：交易所拒补单（如 HL min $10）时必须带上
                            # 实际参数，否则"合法名义额为何被拒"不可查（2026-07-05 VVV 之谜）。
                            # 保留原异常类型（重试/熔断分类、monitor 降级路径不变）。
                            raise type(exc)(
                                'replenish %s %s line=%d px=%.8g sz=%.8g notional=%.2f: %s'
                                % (symbol, opp_side, opp_line, p, order_num,
                                   p * order_num, exc)) from exc
                        self.orders.upsert(GridOrder(client_oid=oid, grid_id=grid_id, line_index=opp_line,
                                                     side=opp_side, price=p, size=order_num, status='open',
                                                     exchange_order_id=getattr(order, 'id', None)))
                        open_lines.add((opp_line, opp_side))

        # 资金费流水
        fcur = self._funding_cursor.get(grid_id, 0)
        pays = (snapshot.funding_for(symbol, since_ms=fcur) if snapshot is not None
                else self.adapter.fetch_funding_payments(symbol, since_ms=fcur))
        for p in pays:
            self.live[grid_id].add_funding(p.amount)
        if pays:
            self._funding_cursor[grid_id] = pays[-1].ts + 1

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
        grid = self.grids.get(grid_id)
        self.grids.set_close_reason(grid_id, reason)   # 真因先落库：中途失败续平不丢
        if grid.status != CLOSING:
            self.grids.transition_status(grid_id, CLOSING, expected_version=grid.version)
        return self.finalize_close(grid_id, symbol, reason)

    def finalize_close(self, grid_id, symbol, reason):
        # 幂等续平（grid 须已 CLOSING）：撤单 + 有界 reduce 残仓 + 落库(只一次) + 转 CLOSED。
        # close() 中途失败留下的 CLOSING 网格由 monitor 循环调本方法续平自愈——
        # 否则残仓无人认领、状态机卡死（瞬时网络/交易所抖动即触发，mainnet 上很危险）。
        grid = self.grids.get(grid_id)
        # 续平路径还原关格真因（'周期再平衡(续平)'）：'平仓恢复' 只是恢复动作，不是
        # 触发原因，裸写会盖掉真因（mainnet 实证 SPX 轮换关格被记成平仓恢复）。
        if reason == '平仓恢复' and grid.close_reason:
            reason = '%s(续平)' % grid.close_reason
        # 同币兄弟格感知（cap=2 双格）：symbol 级撤单/全净仓 reduce 会误伤幸存格——
        # mainnet 2026-07-07 NBIS 实证：关 gt06 撤光并平掉 gt08 的 0.44 → 幸存格
        # 永久幻影账簿（平仓成交挂发起格 oid，兄弟 sync 永不摄入）。有兄弟 → 格级
        # 收敛（只撤自己的单、只平自己的模型份额）；无兄弟 → 保留 symbol 级扫除
        # （孤儿单/孤儿仓卫生，旧行为不变）。
        siblings = [g for g in self.grids.list_active()
                    if g.symbol == symbol and g.id != grid_id]
        if siblings:
            for o in self.orders.list_open_by_grid(grid_id):
                if o.status == 'open' and o.exchange_order_id:
                    try:
                        self.adapter.cancel_order(symbol, o.exchange_order_id)
                    except Exception:
                        pass        # 已成交/已撤 → 目标态已达
        else:
            self.adapter.cancel_all(symbol)
        # 撤掉未触发的另一张保险丝（cancel_all 在多数所已覆盖触发单，这里再 best-effort 补刀，跨所稳妥）。
        for oid in (grid.fuse_low_oid, grid.fuse_high_oid):
            if oid:
                try:
                    self.adapter.cancel_order(symbol, oid)
                except Exception:
                    pass
        for o in self.orders.list_open_by_grid(grid_id):
            self.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=grid_id,
                                         line_index=o.line_index, side=o.side, price=o.price,
                                         size=o.size, status='canceled'))
        # reduce 市价单可能部分成交（HL 滑点/薄盘）；重拉持仓、补 reduce 直至 <= min_amount。
        pos = self.adapter.fetch_positions(symbol)
        attempt = 0
        if siblings:
            # 只平自己的模型份额（accounting=最后一次 sync 的净仓，与 live 快照同源）；
            # 份额未知宁可不平——残仓并入兄弟世界由漂移告警接手，绝不误杀兄弟仓位。
            # 交易所净仓与自身份额符号相反=兄弟已对冲掉我们的份额 → 无可平。
            acc0 = self.accounting.get(grid_id)
            remaining = float(acc0.net_position or 0.0) if acc0 is not None else 0.0
            while (abs(remaining) > self.min_amount and pos.net_size * remaining > 0
                   and attempt < 3):
                qty = min(abs(remaining), abs(pos.net_size))
                side = 'sell' if remaining > 0 else 'buy'
                self.adapter.create_market_order(symbol, side, qty, reduce_only=True,
                                                 client_oid='%s:close:%d' % (grid_id, attempt))
                remaining -= qty if remaining > 0 else -qty
                attempt += 1
                pos = self.adapter.fetch_positions(symbol)
        else:
            while abs(pos.net_size) > self.min_amount and attempt < 3:
                side = 'sell' if pos.net_size > 0 else 'buy'
                self.adapter.create_market_order(symbol, side, abs(pos.net_size),
                                                 reduce_only=True,
                                                 client_oid='%s:close:%d' % (grid_id, attempt))
                attempt += 1
                pos = self.adapter.fetch_positions(symbol)
        snap = self.live[grid_id].snapshot(float(self.adapter.fetch_price(symbol)))
        # 记录按该格真实资金计钱：pnl_ratio 分母是 LiveEquity.cap==grid.cap（动态 cap），
        # 乘 executor 静态默认 cap 会整体错标（restore-cap 同族，mainnet 2026-07-06 实证低报 3x）。
        grid_cap = grid.cap if grid.cap else self.cap
        if not self.records.list_by_grid(grid_id):   # 幂等：续平不重复落库
            self.records.add(Record(id='', grid_id=grid_id, exchange=grid.exchange, symbol=symbol,
                                    tag=grid.tag, offset=grid.offset, opened_at=grid.created_at,
                                    closed_at=now_ms(), sz=grid_cap, total_pnl=snap['pnl_ratio'] * grid_cap,
                                    pnl_ratio=snap['pnl_ratio'], exit_reason=reason))
        g2 = self.grids.get(grid_id)
        self.grids.transition_status(grid_id, CLOSED, expected_version=g2.version)
        # 平仓可观测性：止损/PV/轮换关格此前零日志（mainnet 5+ 例靠 DB 反推），
        # 一行结构化日志供事件监控实时捕获（reason 含 止损/止盈/再平衡 关键词）。
        print('[close] grid %s %s tag=%s reason=%s pnl_ratio=%+.6f'
              % (grid_id, symbol, grid.tag, reason, snap['pnl_ratio']), flush=True)
        return {'reason': reason, 'pnl_ratio': snap['pnl_ratio']}
