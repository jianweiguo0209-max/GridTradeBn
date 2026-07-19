"""企业微信实盘通知。

格式与 ok_grid/account_0 保持一致：开网在整批委托成功后，关网在真实平仓完成并
产生最终净损益后，整点逐活跃 offset 发当前净损益。通知失败永不影响交易主链。
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from gridtrade.execution.events import GridClosed, GridOpened
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.records import RecordRepository


def _display_symbol(symbol):
    """TIA/USDT:USDT -> TIAUSDT，与 Binance 页面标识一致。"""
    try:
        base, rest = str(symbol).split('/', 1)
        quote = rest.split(':', 1)[0]
        return base + quote
    except ValueError:
        return str(symbol)


class WeChatNotifier:
    def __init__(self, webhook_url, store, executor, adapter, *,
                 strategy_name='gridtrade', timezone_name='Asia/Shanghai',
                 timeout=10, post=requests.post, log=print, now_fn=None):
        self.webhook_url = str(webhook_url or '').strip()
        self.store = store
        self.executor = executor
        self.adapter = adapter
        self.strategy_name = strategy_name
        self.tz = ZoneInfo(timezone_name)
        self.timeout = timeout
        self.post = post
        self.log = log
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._last_hour = None

    @property
    def enabled(self):
        return bool(self.webhook_url)

    def send(self, content):
        if not self.enabled:
            return False
        try:
            now = self.now_fn()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            stamp = now.astimezone(self.tz).strftime('%Y-%m-%d %H:%M:%S')
            payload = {'msgtype': 'text', 'text': {'content': str(content) + '\n' + stamp}}
            r = self.post(self.webhook_url, data=json.dumps(payload), timeout=self.timeout)
            if hasattr(r, 'raise_for_status'):
                r.raise_for_status()
            body = r.json() if hasattr(r, 'json') else {}
            if body and int(body.get('errcode', 0)) != 0:
                raise RuntimeError('WeChat errcode=%s errmsg=%s'
                                   % (body.get('errcode'), body.get('errmsg')))
            self.log('[wechat] sent')
            return True
        except Exception as exc:
            self.log('[wechat] send failed: %r' % exc)
            return False

    def __call__(self, event):
        try:
            if isinstance(event, GridOpened):
                self._opened(event.grid_id)
            elif isinstance(event, GridClosed):
                self._closed(event.grid_id, event.reason)
        except Exception as exc:
            self.log('[wechat] format failed: %r' % exc)

    def _opened(self, grid_id):
        g = GridRepository(self.store).get(grid_id)
        if g is None:
            return
        content = '当前下单信息\n\n'
        content += '策略名称： %s\n' % self.strategy_name
        content += '网格上限：%s \n' % g.high_price
        content += '网格下限：%s \n' % g.low_price
        content += '网格终止最高价：%s \n' % g.stop_high_price
        content += '网格终止最低价：%s \n' % g.stop_low_price
        content += '网格数目：%s \n' % g.grid_count
        content += '杠杆：%s \n' % self._native_leverage(g)
        content += '币种：%s \n' % _display_symbol(g.symbol)
        content += 'offset：%s \n' % g.offset
        content += '下单金额：%s \n\n' % g.cap
        content += '-' * 10 + '\n'
        self.send(content)

    def _native_leverage(self, grid):
        """复用开网同源算法显示 Binance 原生杠杆；grids.leverage 历史列存的是
        gearing，不能直接当作交易所杠杆。"""
        try:
            from gridtrade.execution.leverage_policy import (
                BRACKET_HEADROOM, pick_leverage_max, worst_side_notional)
            prices = self.executor._geom[grid.id]['price_array']
            side = worst_side_notional(prices, float(grid.order_num),
                                       float(grid.entry_price))
            tiers = self.adapter.fetch_leverage_tiers(grid.symbol)
            value = pick_leverage_max(side * BRACKET_HEADROOM, tiers)
            return int(value) if value is not None else grid.leverage
        except Exception:
            return grid.leverage

    def _closed(self, grid_id, reason):
        g = GridRepository(self.store).get(grid_id)
        recs = RecordRepository(self.store).list_by_grid(grid_id)
        if g is None or not recs:
            return
        r = recs[-1]
        cap = float(r.sz or g.cap or 0.0)
        pnl = float(r.total_pnl or 0.0)
        ratio = float(r.pnl_ratio or 0.0)
        why = r.exit_reason or reason or '关网'
        content = '[%s] 网格关闭\n\n' % why
        content += '网格持仓: %s\n' % _display_symbol(g.symbol)
        content += '网格策略标识: %s\n' % g.tag
        content += '网格净值: %.2f\n' % (cap + pnl)
        content += '网格盈亏%%: %.2f%%\n' % (ratio * 100)
        content += '网格盈亏金额: %.2f\n' % pnl
        content += '网格初始本金: %.2f\n' % cap
        content += '退出原因: %s\n' % why
        self.send(content)

    def maybe_send_hourly(self, now=None):
        """与 OK monitor 一致：只在整点且存在活跃网格时发一次。"""
        if not self.enabled:
            return False
        now = now or self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(self.tz)
        if local.minute != 0:
            return False
        key = local.strftime('%Y-%m-%dT%H')
        if key == self._last_hour:
            return False
        grids = [g for g in GridRepository(self.store).list_active()
                 if g.status == 'ACTIVE']
        if not grids:
            return False
        balance = self.adapter.fetch_balance()
        content = '当前账户净值: %.2f\n\n' % float(balance.equity)
        accs = AccountingRepository(self.store)
        for g in sorted(grids, key=lambda x: (x.offset, x.symbol)):
            pnl = 0.0
            if self.executor.is_loaded(g.id) and g.id in self.executor.live:
                snap = self.executor.live[g.id].snapshot(float(self.adapter.fetch_price(g.symbol)))
                pnl = float(snap['pnl_ratio']) * float(g.cap or 0.0)
            else:
                acc = accs.get(g.id)
                if acc is not None:
                    px = float(self.adapter.fetch_price(g.symbol))
                    pnl = (float(acc.realized_pnl) +
                           float(acc.net_position) * (px - float(acc.avg_price)) -
                           float(acc.fee_paid) - float(acc.funding_paid))
            cap = float(g.cap or 0.0)
            ratio = pnl / cap if cap else 0.0
            content += '当前网格净值: %.2f\n' % (cap + pnl)
            content += '当前网格持仓: %s\n' % _display_symbol(g.symbol)
            content += '当前网格盈亏%%: %.2f%%\n' % (ratio * 100)
            content += '当前网格盈亏金额: %.2f\n' % pnl
            content += '当前网格策略标识: %s\n' % g.tag
            content += '-' * 10 + '\n\n'
        sent = self.send(content)
        if sent:
            self._last_hour = key
        return sent
