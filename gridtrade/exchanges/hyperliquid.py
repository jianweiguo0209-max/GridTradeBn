"""Hyperliquid 适配器：钱包凭证/资金费 1h/USDC 计价符号映射。"""
from gridtrade.exchanges.base import FundingPayment
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class HyperliquidAdapter(CcxtAdapter):
    name = 'hyperliquid'
    quote_currency = 'USDC'   # HL 以 USDC 计价/保证金
    FUNDING_INTERVAL_HOURS = 1

    def __init__(self, client):
        super().__init__(client, name='hyperliquid')

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
        return [o for o in (self._to_order(r) for r in self.client.fetch_open_orders(None))
                if o.symbol in want]

    def fetch_positions_all(self, symbols):
        want = set(symbols)
        out = {}
        for p in self.client.fetch_positions():
            sym = self.to_canonical(p['symbol'])
            if sym not in want:
                continue
            contracts = float(p.get('contracts') or 0.0)
            out[sym] = contracts if p.get('side') == 'long' else -contracts
        return out

    def fetch_prices_all(self, symbols):
        # allMids 权重 2（fetchTickers 走高权重端点，不用）
        mids = self.client.publicPostInfo({'type': 'allMids'}) or {}
        cmap = self._coin_map()
        want = set(symbols)
        return {cmap[c]: float(px) for c, px in mids.items()
                if cmap.get(c) in want}

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
