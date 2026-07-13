"""币安 USDT-M 永续适配器：API key 凭证/资金费 8h/结算币过滤/真实 quote_volume。
spec: docs/superpowers/specs/2026-07-14-binance-migration-design.md §3.1
"""
import re

import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS
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
