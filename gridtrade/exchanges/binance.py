"""币安 USDT-M 永续适配器：API key 凭证/资金费 8h/结算币过滤/真实 quote_volume。
spec: docs/superpowers/specs/2026-07-14-binance-migration-design.md §3.1
"""
import re

import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FundingPayment
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter

# 币安 futures newClientOrderId 官方正则 ^[\.A-Z\:/a-z0-9_-]{1,36}$（含 ':' '.'）（spec §5.1）。
# 内部 '{gid}:{line}:{seq}' 直传合法；非法字符确定性替换 '-'（testnet 实测见冒烟脚本）。
_CLOID_BAD = re.compile(r'[^\.A-Z\:/a-z0-9_-]')


class BinanceAdapter(CcxtAdapter):
    name = 'binance'
    FUNDING_INTERVAL_HOURS = 8   # 信息性：部分币 4h/1h；记账走真实流水不受影响（spec §九）

    def __init__(self, client):
        super().__init__(client, name='binance')

    # fapi 同时挂 USDT-M 与 USDC-M 合约：只收本结算币，防 USDC 合约混入票池（spec §3.1）
    def _include_market(self, m) -> bool:
        return m.get('settle') == self.quote_currency

    def encode_cloid(self, client_oid):
        if client_oid is None:
            return None
        s = _CLOID_BAD.sub('-', str(client_oid))
        # 越界断言（spec §5.1）：内部格式 ~13 字符远低于 36 上限；超限=上游 ID 生成异常，
        # 静默截断可能产生跨单碰撞（假去重），宁可 fail-loud 拒单。
        if len(s) > 36:
            raise ValueError('client_oid 超长(%d>36): %r' % (len(s), client_oid))
        return s or None

    def exchange_status(self) -> str:
        # fapi 无期货维护状态公共端点：ping 判定（权重1；spec §3.1）
        try:
            self.client.fapiPublicGetPing()
            return 'ok'
        except Exception:
            return 'maintenance'

    @classmethod
    def from_credentials(cls, api_key, secret, *, testnet=False, proxies=None,
                         timeout=10000):
        import ccxt
        client = ccxt.binanceusdm({
            'apiKey': api_key, 'secret': secret,
            'timeout': timeout, 'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if testnet:
            client.set_sandbox_mode(True)
        return cls(client)

    def create_stop_order(self, symbol, side, size, trigger_price, *,
                          reduce_only=True, slippage=0.15, client_oid=None):
        """STOP_MARKET 触发市价单（灾难保险丝）。币安无滑点底线参数——slippage
        接受但忽略（语义差已文档化，spec §5.2；软止损仍是主刹车）。"""
        p = self._params(reduce_only, client_oid)
        p['stopLossPrice'] = trigger_price
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     None, p)
        return self._to_order(r)

    def set_leverage(self, symbol, leverage) -> None:
        """先确保 CROSSED 全仓（幂等，吞 -4046 无需更改），再设杠杆（币安要求整数）。
        全仓对齐 账户杠杆/gearing 仓位体系假设（spec §3.1）。"""
        native = self.to_native(symbol)
        try:
            self.client.set_margin_mode('cross', native)
        except Exception as exc:
            msg = str(exc)
            if '-4046' not in msg and 'No need to change' not in msg:
                raise
        self.client.set_leverage(int(leverage), native)

    def assert_account_mode(self) -> None:
        """单向持仓 + 关闭联合保证金（引擎净仓/单币权益假设，spec §3.1）。"""
        dual = self.client.fapiPrivateGetPositionSideDual() or {}
        if str(dual.get('dualSidePosition')).lower() in ('true', '1'):
            raise RuntimeError('币安账户为双向持仓(hedge)模式：执行引擎按净仓语义工作，'
                               '请在合约偏好设置切换为单向持仓后重启')
        multi = self.client.fapiPrivateGetMultiAssetsMargin() or {}
        if str(multi.get('multiAssetsMargin')).lower() in ('true', '1'):
            raise RuntimeError('币安联合保证金(Multi-Assets)开启：权益口径须为单一 %s，'
                               '请关闭后重启' % self.quote_currency)

    def _market_id(self, symbol):
        """canonical → 币安原生 id（'BTC/USDT:USDT'→'BTCUSDT'）。markets 惰性加载；
        查不到（极新上市）按命名规则回退拼接。"""
        if not getattr(self.client, 'markets', None):
            self.client.load_markets()
        m = (self.client.markets or {}).get(symbol)
        if m and m.get('id'):
            return m['id']
        return symbol.split('/')[0] + self.quote_currency

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms) -> pd.DataFrame:
        """原生 klines 端点（分页语义同基类），取**真实 quote_volume**（第8列）——
        选币因子 vwap=quote_volume/volCcy 与回测(Vision 归档)同分布（spec §5.4）。"""
        native_id = self._market_id(symbol)
        tf_ms = int(self.client.parse_timeframe(timeframe) * 1000)
        all_rows = []
        cursor = int(start_ms)
        bound = min(int(end_ms), self._now_ms())   # 不向未来翻页（同基类）
        guard = 0
        while cursor <= bound and guard < 10000:
            guard += 1
            batch = self.client.fapiPublicGetKlines({
                'symbol': native_id, 'interval': timeframe,
                'startTime': int(cursor), 'limit': 1500})
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts < cursor:
                break
            cursor = last_ts + tf_ms
            if last_ts >= end_ms:
                break
        if not all_rows:
            return pd.DataFrame(columns=CANDLE_COLS)
        df = pd.DataFrame(all_rows, columns=[
            'ts', 'open', 'high', 'low', 'close', 'vol', 'close_time',
            'quote_volume', 'count', 'tbv', 'tbqv', 'ignore'])
        df['ts'] = df['ts'].astype('int64')
        df = df.drop_duplicates(subset=['ts'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        for c in ('open', 'high', 'low', 'close', 'vol', 'quote_volume'):
            df[c] = df[c].astype(float)
        df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
        df['symbol'] = symbol
        df['volCcy'] = df['vol']
        return df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)

    # ---- 账户级批量读（monitor 5s 快照权重预算核心，spec §3.1）----
    def _id_map(self):
        """原生 id('BTCUSDT') → canonical。只收本结算币 swap；实例缓存。"""
        if getattr(self, '_id_map_cache', None) is None:
            if not getattr(self.client, 'markets', None):
                self.client.load_markets()
            m2 = {}
            for m in (self.client.markets or {}).values():
                if m.get('swap') is not True or not self._include_market(m):
                    continue
                m2[m['id']] = self.to_canonical(m['symbol'])
            self._id_map_cache = m2
        return self._id_map_cache

    def fetch_open_orders_all(self, symbols):
        # 无 symbol 的 openOrders：全账户一次（权重40），替代逐币 N 次
        want = set(symbols)
        return [o for o in (self._to_order(r)
                            for r in self.client.fetch_open_orders(None))
                if o.symbol in want]

    def fetch_positions_all(self, symbols):
        # positionRisk 全账户（权重5）；无持仓行=缺省（monitor 按 0 处理）
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
        # 全市场 ticker/price（权重2），替代逐币 fetchTicker
        want = set(symbols)
        idmap = self._id_map()
        out = {}
        for r in self.client.fapiPublicGetTickerPrice():
            sym = idmap.get(r.get('symbol'))
            if sym in want:
                out[sym] = float(r['price'])
        for s in want - set(out):          # 罕见后备（新上市 markets 未刷新）
            out[s] = float(self.fetch_price(s))
        return out

    def fetch_funding_payments_all(self, symbols, since_ms=None):
        """income(FUNDING_FEE) 账户级单流（权重30）——币安按 symbol 正确打标，
        分组回各币种。无 since → 币安默认近7天。统一"支付为正"（income 正=收入取负）。
        分页含边界重取+tranId 去重：资金费全仓位同刻并结，+1 前进会丢页界并列行（评审实证）。"""
        idmap = self._id_map()
        out = {s: [] for s in symbols}
        params = {'incomeType': 'FUNDING_FEE', 'limit': 1000}
        if since_ms is not None:
            params['startTime'] = int(since_ms)
        seen = set()
        guard = 0
        while guard < 50:
            guard += 1
            rows = self.client.fapiPrivateGetIncome(dict(params))
            new_any = False
            for r in rows:
                ts = int(r['time'])
                if since_ms is not None and ts < since_ms:
                    continue
                key = (str(r.get('tranId')), r.get('symbol'), ts, str(r.get('income')))
                if key in seen:
                    continue
                seen.add(key)
                new_any = True
                sym = idmap.get(r.get('symbol'))
                if sym in out:
                    out[sym].append(FundingPayment(ts=ts, amount=-float(r['income'])))
            if len(rows) < 1000:
                break
            nxt = int(rows[-1]['time'])
            if not new_any and nxt == params.get('startTime'):
                break        # 整页并列且无新行：防死转（理论病态，防御性护栏）
            params['startTime'] = nxt   # 含边界重取（勿 +1：并列行会被跳过）
        for s in out:
            out[s].sort(key=lambda p: p.ts)
        return out
