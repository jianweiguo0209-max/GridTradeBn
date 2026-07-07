"""Hyperliquid 适配器：钱包凭证/资金费 1h/USDC 计价符号映射。"""
from gridtrade.exchanges.base import FundingPayment
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class HyperliquidAdapter(CcxtAdapter):
    name = 'hyperliquid'
    quote_currency = 'USDC'   # HL 以 USDC 计价/保证金
    FUNDING_INTERVAL_HOURS = 1

    def __init__(self, client):
        super().__init__(client, name='hyperliquid')

    def _include_market(self, m) -> bool:
        """剔除 builder-dex(HIP-3)资产:回测不可复现(Reservoir 归档无 builder 数据、
        assemble 静默丢格)+ 部分 dex 非 USDC 保证金(hyna=USDE)+ allMids/signals 盲窗事故史
        (memory builder-dex-backtest-blindspot)。判据=市场 info.dex 非空(主 dex 为 None)。
        只影响 universe 候选;已持有的 builder 格监控/平仓路径不受影响。"""
        return not (m.get('info') or {}).get('dex')

    # 规范符号如实反映结算币：HL 恒 USDC -> 'BTC/USDC:USDC'（由 self.quote_currency 派生，
    # 单一事实源）。None 原样返回：HL createOrder 响应不带 symbol，ccxt 解析出 None，
    # 勿在其上 .split 崩溃。
    def to_native(self, symbol: str) -> str:
        if not symbol:
            return symbol
        base = symbol.split('/')[0]
        q = self.quote_currency
        return f'{base}/{q}:{q}'

    def to_canonical(self, native: str) -> str:
        if not native:
            return native
        base = native.split('/')[0]
        q = self.quote_currency
        return f'{base}/{q}:{q}'

    def encode_cloid(self, client_oid):
        # HL 的 cloid 须 128-bit hex；我们的 client_oid 是字符串。省略 cloid，
        # 改按 exchange order id 匹配 fill/对账（HL fill/open order 只带 oid）。
        return None

    def cancel_all(self, symbol) -> None:
        # ccxt 的 HL 无 cancelAllOrders；逐个撤当前挂单。
        for o in self.fetch_open_orders(symbol):
            self.cancel_order(symbol, o.id)

    def fetch_funding_payments(self, symbol, since_ms=None):
        # 实测：HL 的 fetch_funding_history 返回【账户级全币种】流水，并把【查询的 symbol】
        # 盖到每行的 symbol 字段（无法据此区分币种）；真实资产在 info.delta.coin。
        # 故按 info.coin 过滤只留本币种，避免把别币种 funding 计入本网格。
        base = symbol.split('/')[0] if symbol else symbol
        rows = self.client.fetch_funding_history(self.to_native(symbol), since=since_ms)
        out = []
        for r in rows:
            ts = int(r['timestamp'])
            if since_ms is not None and ts < since_ms:
                continue
            coin = ((r.get('info') or {}).get('delta') or {}).get('coin')
            if coin != base:
                continue
            # ccxt 约定 amount 负=支付；统一成"支付为正"
            out.append(FundingPayment(ts=ts, amount=-float(r['amount'])))
        out.sort(key=lambda p: p.ts)
        return out

    # ---- builder-dex(HIP-3) 信息面适配（mainnet 2026-07-06 KIOXIA 实证）----
    # HL 的下单/撤单 action 按全局 asset id 跨 dex 通用，但 info 查询
    # （frontendOpenOrders/clearinghouseState）默认只查主 dex——builder 资产必须带
    # dex 参数，否则挂单/持仓不可见（fuse 每轮误判被丢重挂堆积 166 张孤儿触发单、
    # 净仓对账拿假 0）。userFills 例外（账户全局）。主 dex 路径保持逐字节不变。

    def _dex_of(self, symbol):
        """builder dex 名（如 'xyz'）；主 dex 返回 None。无 markets 的测试桩防御性回退主 dex。"""
        if not hasattr(self.client, 'load_markets'):
            return None
        self.client.load_markets()
        m = (getattr(self.client, 'markets', None) or {}).get(self.to_native(symbol)) or {}
        return (m.get('info') or {}).get('dex') or None

    def _dexes_for(self, symbols) -> set:
        return {d for d in (self._dex_of(s) for s in symbols) if d}

    def _raw_open_order_to_order(self, r):
        """frontendOpenOrders 原始行 → Order（HL side: A=ask=卖 / B=bid=买）。"""
        from gridtrade.exchanges.base import Order
        coin = r.get('coin')
        sym = self._coin_map().get(coin, coin)
        oid = str(r.get('oid'))
        return Order(id=oid, client_oid=oid, symbol=sym,
                     side=('sell' if r.get('side') == 'A' else 'buy'),
                     price=float(r.get('limitPx') or 0.0),
                     size=float(r.get('sz') or 0.0), filled=0.0, status='open',
                     reduce_only=bool(r.get('reduceOnly', False)))

    def _dex_open_orders(self, dex):
        rows = self.client.publicPostInfo({'type': 'frontendOpenOrders',
                                           'user': self.client.walletAddress,
                                           'dex': dex}) or []
        return [self._raw_open_order_to_order(r) for r in rows]

    def fetch_open_orders(self, symbol):
        dex = self._dex_of(symbol)
        if not dex:
            return super().fetch_open_orders(symbol)     # 主 dex：ccxt 原路径零变化
        return [o for o in self._dex_open_orders(dex) if o.symbol == symbol]

    def fetch_positions(self, symbol):
        dex = self._dex_of(symbol)
        if not dex:
            return super().fetch_positions(symbol)
        from gridtrade.exchanges.base import Position
        ch = self.client.publicPostInfo({'type': 'clearinghouseState',
                                         'user': self.client.walletAddress,
                                         'dex': dex}) or {}
        cmap = self._coin_map()
        for p in ch.get('assetPositions', []):
            pos = p.get('position') or {}
            if cmap.get(pos.get('coin')) == symbol:
                return Position(symbol, float(pos.get('szi') or 0.0),
                                float(pos.get('entryPx') or 0.0))
        return Position(symbol, 0.0, 0.0)

    def order_status(self, symbol, order_id) -> str:
        """orderStatus 端点（weight 2；仅 fuse 三态判定的罕见分支调用）：
        'open'/'filled'/'canceled'/'unknown'。"""
        try:
            resp = self.client.publicPostInfo({'type': 'orderStatus',
                                               'user': self.client.walletAddress,
                                               'oid': int(order_id)})
        except Exception:
            return 'unknown'
        if not resp or resp.get('status') != 'order':
            return 'unknown'
        st = ((resp.get('order') or {}).get('status') or '').lower()
        if st in ('open', 'resting'):
            return 'open'
        if st == 'filled':
            return 'filled'
        if st in ('canceled', 'cancelled', 'rejected', 'margincanceled',
                  'reduceonlycanceled', 'triggered'):
            # triggered=触发已转市价：对保险丝语义等同"已触发执行"
            return 'filled' if st == 'triggered' else 'canceled'
        return 'unknown'

    # ---- 账户级批量读（HL 原生：fills/orders/positions/funding 端点本就账户级）----
    def _coin_map(self):
        # HL 原生 coin 名（如 'kPEPE'）→ canonical symbol。必须经 ccxt markets 映射，
        # 勿 f-string 拼接（大小写/前缀会错）。实例内缓存（新上币重启进程后可见）。
        if getattr(self, '_coin_map_cache', None) is None:
            self.client.load_markets()
            m2 = {}
            for m in self.client.markets.values():
                if m.get('swap') is not True:
                    continue
                coin = ((m.get('info') or {}).get('name')) or m.get('base')
                m2[coin] = self.to_canonical(m['symbol'])
            self._coin_map_cache = m2
        return self._coin_map_cache

    def fetch_my_trades_all(self, symbols, since_ms=None):
        want = set(symbols)
        out = [self._to_trade(r) for r in self.client.fetch_my_trades(None, since=since_ms)]
        out = [t for t in out if t.symbol in want]
        out.sort(key=lambda t: t.ts)
        return out

    def fetch_open_orders_all(self, symbols):
        want = set(symbols)
        out = [o for o in (self._to_order(r) for r in self.client.fetch_open_orders(None))
               if o.symbol in want]
        for dex in sorted(self._dexes_for(symbols)):    # builder dex 逐个补查（info 默认只查主 dex）
            out.extend(o for o in self._dex_open_orders(dex) if o.symbol in want)
        return out

    def fetch_positions_all(self, symbols):
        want = set(symbols)
        out = {}
        for p in self.client.fetch_positions():
            sym = self.to_canonical(p['symbol'])
            if sym not in want:
                continue
            contracts = float(p.get('contracts') or 0.0)
            out[sym] = contracts if p.get('side') == 'long' else -contracts
        cmap = self._coin_map()
        for dex in sorted(self._dexes_for(symbols)):    # builder 持仓补查（否则拿假 0）
            ch = self.client.publicPostInfo({'type': 'clearinghouseState',
                                             'user': self.client.walletAddress,
                                             'dex': dex}) or {}
            for p in ch.get('assetPositions', []):
                pos = p.get('position') or {}
                sym = cmap.get(pos.get('coin'))
                if sym in want:
                    out[sym] = float(pos.get('szi') or 0.0)
        return out

    def _dex_mids(self, dex):
        """dex 版 allMids（0.1s/权重2）→ {canonical: mid}。"""
        mids = self.client.publicPostInfo({'type': 'allMids', 'dex': dex}) or {}
        cmap = self._coin_map()
        return {cmap[c]: float(px) for c, px in mids.items() if c in cmap}

    def _main_mids(self):
        """主 dex allMids（0.1s/权重2）→ {canonical: mid}。无该端点的测试桩回退空。"""
        if not hasattr(self.client, 'publicPostInfo'):
            return {}
        mids = self.client.publicPostInfo({'type': 'allMids'}) or {}
        cmap = self._coin_map()
        return {cmap[c]: float(px) for c, px in mids.items() if c in cmap}

    def fetch_price(self, symbol) -> float:
        # builder 资产：fetchTicker 实测 ~10s/次（曾把 mainnet 轮长 2.4s 拖到 13.6s），
        # 改走 dex 版 allMids（0.1s）。主 dex 对称同治：ccxt fetchTicker 在多 dex 时代
        # 扫全部 dex meta，实测恒定 ~12s/次（2026-07-08 dashboard 首页 73.6s + 开格/
        # 平仓取价卡顿实证）；allMids 查不到（罕见）才回退 ccxt 原路径。
        dex = self._dex_of(symbol)
        px = (self._dex_mids(dex) if dex else self._main_mids()).get(symbol)
        if px is not None:
            return px
        return super().fetch_price(symbol)

    def fetch_prices_all(self, symbols):
        # allMids 权重 2（fetchTickers 走高权重端点，不用）
        mids = self.client.publicPostInfo({'type': 'allMids'}) or {}
        cmap = self._coin_map()
        want = set(symbols)
        out = {cmap[c]: float(px) for c, px in mids.items()
               if cmap.get(c) in want}
        # HIP-3 builder 资产不在主 allMids → 按 dex 批量 allMids 补齐（每 dex 一次 0.1s；
        # mainnet 2026-07-05 XYZ-MSTR 缺价盲窗 + 2026-07-06 fetchTicker 10s 慢路径双实证）。
        missing = want - set(out)
        if missing:
            for dex in sorted(self._dexes_for(missing)):
                dm = self._dex_mids(dex)
                out.update({s: dm[s] for s in missing if s in dm})
        for s in want - set(out):          # 最终罕见后备（不可映射市场自愈）
            out[s] = float(super().fetch_price(s))
        return out

    def fetch_funding_payments_all(self, symbols, since_ms=None):
        # userFunding 本就账户级且把查询 symbol 盖到每行（见 fetch_funding_payments 注释）；
        # 任取一个 symbol 触发查询，按 info.delta.coin 分组回各币种。
        probe = symbols[0] if symbols else None
        rows = self.client.fetch_funding_history(
            self.to_native(probe) if probe else None, since=since_ms)
        cmap = self._coin_map()
        out = {s: [] for s in symbols}
        for r in rows:
            ts = int(r['timestamp'])
            if since_ms is not None and ts < since_ms:
                continue
            coin = ((r.get('info') or {}).get('delta') or {}).get('coin')
            sym = cmap.get(coin)
            if sym not in out:
                continue
            out[sym].append(FundingPayment(ts=ts, amount=-float(r['amount'])))
        for s in out:
            out[s].sort(key=lambda p: p.ts)
        return out

    def create_market_order(self, symbol, side, size, *,
                            reduce_only=False, client_oid=None):
        # HL 无真正市价单：ccxt 需一个参考价来算滑点上限（默认 5%）。传当前价。
        price = self.fetch_price(symbol)
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     price, self._params(reduce_only, client_oid))
        return self._to_order(r)

    @classmethod
    def from_credentials(cls, wallet_address, private_key, *, proxies=None,
                         testnet=False):
        import ccxt
        client = ccxt.hyperliquid({
            'walletAddress': wallet_address,
            'privateKey': private_key,
            'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if testnet:
            client.set_sandbox_mode(True)
        return cls(client)
