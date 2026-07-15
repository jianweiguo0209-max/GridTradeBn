"""币安 USDT-M 永续适配器：API key 凭证/资金费 8h/结算币过滤/真实 quote_volume。
spec: docs/superpowers/specs/2026-07-14-binance-migration-design.md §3.1
"""
import re

import ccxt
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
        s = str(client_oid)
        # gid 段压缩（testnet 实证 2026-07-14）：grid_id 实为 32-hex uuid（spec §5.1 原按
        # 6 位整数建模长度是错的）——'{gid}:0:0' 恰 36 字符压线、'{gid}:fuse:low' 41 字符
        # 必越界 → 保险丝下单失败、格卡 OPENING。取 gid 前 12 hex（16^12≈2.8e14，同账户
        # 并发格碰撞可忽略），确定性单向映射；成交/对账走 exchange order id，无回读依赖。
        parts = s.split(':', 1)
        if len(parts) == 2 and len(parts[0]) > 12:
            s = parts[0][:12] + ':' + parts[1]
        s = _CLOID_BAD.sub('-', s)
        # 越界断言（spec §5.1）：压缩后内部格式 ≤22 字符远低于 36 上限；仍超限=上游 ID
        # 生成异常，静默截断可能产生跨单碰撞（假去重），宁可 fail-loud 拒单。
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
        # ccxt 对无 symbol 的 fetchOpenOrders 默认抛错(权重防呆护栏)；账户级快照本就要
        # 全账户一次(权重40,spec §3.1)——显式关闭护栏(终审实证:不关则 monitor 快照上线即死)。
        client.options['warnOnFetchOpenOrdersWithoutSymbol'] = False
        if testnet:
            # 币安期货 testnet 已弃用：ccxt 4.5.61 对 futures 的 set_sandbox_mode 直接抛
            # NotSupported(冒烟实测 2026-07-14)。官方替代=Demo Trading——enable_demo_trading
            # 把 API 指向 demo-fapi.binance.com；demo API key 在 https://demo.binance.com
            # 的 API Management 生成。BINANCE_TESTNET=true 的语义即 Demo Trading。
            client.enable_demo_trading(True)
        return cls(client)

    def create_stop_order(self, symbol, side, size, trigger_price, *,
                          reduce_only=True, slippage=0.15, client_oid=None):
        """STOP_MARKET 触发市价单（灾难保险丝）。币安无滑点底线参数——slippage
        接受但忽略（语义差已文档化，spec §5.2；软止损仍是主刹车）。
        数量按 MARKET_LOT_SIZE.maxQty 封顶（testnet PORTAL 实证 2026-07-14：低价币
        worst=order_num×grid_count 超市价单上限 11.8 倍 → -4005 拒单、开格卡死 OPENING；
        限价单上限 LOT_SIZE 远大于市价上限故网格挂单不受影响）。reduce-only 触发时交易所
        按实际持仓执行，封顶后丝仍护到 maxQty，超出部分由软止损(5s 轮)+爆仓线兜底。"""
        mx = self._market_max_qty(symbol)
        if mx is not None and size > mx:
            print('[binance] %s 保险丝数量 %.8g > MARKET_LOT_SIZE.maxQty %.8g -> 封顶'
                  '(丝保护不足额，超出部分依赖软止损)' % (symbol, size, mx), flush=True)
            size = mx
        trigger_price = self.quantize_price(symbol, trigger_price)   # 触发价按 tickSize 量化(防 -1111)
        p = self._params(reduce_only, client_oid)
        p['stopLossPrice'] = trigger_price
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     None, p)
        return self._to_order(r)

    def _market_max_qty(self, symbol):
        """MARKET_LOT_SIZE.maxQty（市价单单笔上限，ccxt limits.market.max 标准映射）；
        缺失/异常 → None（fail-open 不封顶，交易所自会校验）。"""
        try:
            if not getattr(self.client, 'markets', None):
                self.client.load_markets()
            m = (self.client.markets or {}).get(symbol) or {}
            mx = ((m.get('limits') or {}).get('market') or {}).get('max')
            return float(mx) if mx else None
        except Exception:
            return None

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
        # 防跨计价污染:回退拼接用符号自身 quote(旧 HL USDC 符号绝不可映射到 USDT 行情——
        # 不在市则交易所报错优雅降级)。
        return symbol.split('/')[0] + (symbol.split('/')[1].split(':')[0]
                                       if '/' in symbol else self.quote_currency)

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

    # 币安 userTrades:startTime 过旧时 ccxt 自动加 endTime=startTime+7d 封顶——since=0 会查
    # 1970 年第一周返回空(终审实证)。网格生命周期 ≤12h,过旧 since 收敛到近 6.5 天。
    # fetch_my_trades_all 继承基类逐 symbol 循环(调用 self.fetch_my_trades)——下面的收敛
    # 对全账户批量读自动生效,无需覆写 fetch_my_trades_all。
    _TRADES_LOOKBACK_MS = int(6.5 * 24 * 3600 * 1000)

    def fetch_my_trades(self, symbol, since_ms=None):
        if since_ms is not None:
            floor_ms = self._now_ms() - self._TRADES_LOOKBACK_MS
            if since_ms < floor_ms:
                since_ms = floor_ms
        return super().fetch_my_trades(symbol, since_ms=since_ms)

    # ---- 账户级批量读（monitor 5s 快照权重预算核心，spec §3.1）----
    # 权重预算（终审修正）：12 格满仓每周期 40(openOrders)+5(positionRisk)+2(tickerPrice)+
    # 30(income)+12×5(userTrades)+5(balance)≈142，5s 轮→每分钟 ~1700，低于 fapi IP 上限
    # 2400/min（原估算"~60+12×5≈1400"漏计 fetch_balance 且批量调用总量被低估）。
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

    # ---- 触发单(algo/conditional)独立订单簿适配（demo 冒烟实测 2026-07-14 发现）----
    # 币安 USDT-M 已把 STOP_MARKET 等触发单放入独立 algo 订单簿（ccxt 4.5.61：
    # stopLossPrice → fapiPrivatePostAlgoOrder，返回 algoId 号段与常规 orderId 不同）。
    # 三个后果必须适配，否则重演 HL 时代孤儿触发单事故：
    # ①常规撤单端点对 algoId 报 -2011 → cancel_order 先常规后 trigger 回退；
    # ②常规 openOrders 看不见触发单 → 不并读 algo 簿，对账器会误判保险丝丢失反复重挂；
    # ③常规 cancelAll 杀不掉触发单 → 关格必须两簿齐清，防残留丝在关格后触发。

    def cancel_order(self, symbol, order_id) -> None:
        native = self.to_native(symbol)
        try:
            self.client.cancel_order(order_id, native)
        except ccxt.OrderNotFound:
            # 可能是 algo 簿的触发单（保险丝）：走 trigger 路径重试；
            # 仍不存在则由 algo 路径原样抛 OrderNotFound（语义=确实已不在）。
            self.client.cancel_order(order_id, native, {'trigger': True})

    def fetch_open_orders(self, symbol):
        # 两簿并读（常规限价 + algo 触发单）——镜像 fake/HL 的"挂单含触发单"既有语义，
        # 保险丝对账依赖看得见触发单。
        native = self.to_native(symbol)
        rows = list(self.client.fetch_open_orders(native))
        # ccxt 签名 (symbol, since, limit, params)：params 必须关键字传——位置传会落到
        # since 上，algo 簿静默查不到（demo 实测 2026-07-14 TypeError 抓获）
        rows += list(self.client.fetch_open_orders(native, params={'trigger': True}))
        return [self._to_order(r) for r in rows]

    def cancel_all(self, symbol) -> None:
        # 两簿齐清：常规 allOpenOrders + algo algoOpenOrders
        native = self.to_native(symbol)
        self.client.cancel_all_orders(native)
        self.client.cancel_all_orders(native, {'trigger': True})

    def fetch_open_orders_all(self, symbols):
        # 账户级两簿并读（常规 40 + algo 40 权重；5s 轮预算升至 ~2180/min，仍低于
        # 2400——若见 429 优先调大 MONITOR_INTERVAL_SEC）
        want = set(symbols)
        rows = list(self.client.fetch_open_orders(None))
        rows += list(self.client.fetch_open_orders(None, params={'trigger': True}))
        return [o for o in (self._to_order(r) for r in rows) if o.symbol in want]

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
