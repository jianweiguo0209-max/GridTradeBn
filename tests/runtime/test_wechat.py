from datetime import datetime, timezone

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Balance
from gridtrade.execution.events import GridClosed, GridOpened
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.runtime.wechat import WeChatNotifier
from gridtrade.state.models import ACTIVE, Grid, Record
from gridtrade.state.records import RecordRepository
from gridtrade.state.store import StateStore


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {'errcode': 0, 'errmsg': 'ok'}


def _setup(post=None):
    store = StateStore.in_memory()
    store.create_all()
    adapter = FakeExchange(price=100.0)
    adapter.fetch_balance = lambda: Balance(equity=1000.0, cash=1000.0)
    executor = GridExecutor(adapter, store, cap=100.0, gearing=3.4,
                            stop_orders_enabled=False)
    adapter.seed_leverage_tiers('TIA/USDT:USDT', [
        {'maxLeverage': 20, 'maxNotional': 1_000_000}])
    sent = []

    def _post(url, data, timeout):
        sent.append((url, data, timeout))
        return _Response()

    notifier = WeChatNotifier(
        'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test',
        store, executor, adapter, strategy_name='gjw账户0',
        post=post or _post,
        now_fn=lambda: datetime(2026, 7, 19, 8, 0, 5, tzinfo=timezone.utc))
    grid = executor.grids.create(grid=Grid(
        id='g1', exchange='fake', symbol='TIA/USDT:USDT', status=ACTIVE,
        offset=7, tag='gt07', entry_price=0.35, low_price=0.31,
        high_price=0.39, stop_low_price=0.30, stop_high_price=0.40,
        grid_count=10, order_num=644, leverage=3.4, cap=250.0))
    executor._geom[grid.id] = {'price_array': [0.31, 0.35, 0.39]}
    return store, adapter, executor, notifier, grid, sent


def _content(sent):
    import json
    return json.loads(sent[-1][1])['text']['content']


def test_open_notification_matches_ok_style_and_appends_beijing_time():
    _, _, _, notifier, grid, sent = _setup()
    notifier(GridOpened(grid.id, grid.exchange, grid.symbol, grid.tag))
    msg = _content(sent)
    assert '当前下单信息' in msg and '策略名称： gjw账户0' in msg
    assert '币种：TIAUSDT' in msg and 'offset：7' in msg
    assert '网格上限：0.39' in msg and '下单金额：250.0' in msg
    assert '杠杆：20' in msg
    assert msg.endswith('2026-07-19 16:00:05')


def test_close_notification_uses_final_record_and_reason():
    store, _, _, notifier, grid, sent = _setup()
    RecordRepository(store).add(Record(
        id='', grid_id=grid.id, exchange='fake', symbol=grid.symbol,
        tag=grid.tag, offset=grid.offset, opened_at=1, closed_at=2,
        sz=250.0, total_pnl=-1.34558321, pnl_ratio=-1.34558321 / 250,
        exit_reason='pv主动关网'))
    notifier(GridClosed(grid.id, grid.exchange, grid.symbol,
                        'pv主动关网', -1.34558321 / 250))
    msg = _content(sent)
    assert '[pv主动关网] 网格关闭' in msg
    assert '网格持仓: TIAUSDT' in msg
    assert '网格盈亏金额: -1.35' in msg
    assert '退出原因: pv主动关网' in msg


def test_hourly_notification_sends_once_per_beijing_hour():
    _, _, executor, notifier, grid, sent = _setup()
    executor.live[grid.id] = type('Live', (), {
        'snapshot': lambda self, price: {'pnl_ratio': 0.0123}
    })()
    now = datetime(2026, 7, 19, 8, 0, 5, tzinfo=timezone.utc)
    assert notifier.maybe_send_hourly(now) is True
    assert notifier.maybe_send_hourly(now) is False
    msg = _content(sent)
    assert '当前账户净值: 1000.00' in msg
    assert '当前网格持仓: TIAUSDT' in msg
    assert '当前网格盈亏%: 1.23%' in msg
    assert '当前网格策略标识: gt07' in msg


def test_send_failure_is_fail_soft():
    def _boom(*args, **kwargs):
        raise RuntimeError('network down')
    _, _, _, notifier, _, _ = _setup(post=_boom)
    assert notifier.send('hello') is False


def test_empty_webhook_is_disabled_without_http_call():
    _, _, _, notifier, _, _ = _setup()
    notifier.webhook_url = ''
    assert notifier.enabled is False
    assert notifier.send('hello') is False
