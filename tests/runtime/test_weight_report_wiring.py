"""权重遥测接线：_fetch_pass 逐币驱动 report_weight + 无此方法的适配器 getattr 兜底。"""
import pandas as pd

from gridtrade.runtime.scheduler import _fetch_pass


def _one_bar(*_args):
    return pd.DataFrame({'candle_begin_time': [pd.Timestamp('2026-06-01')],
                         'open': [1.0], 'high': [1.0], 'low': [1.0],
                         'close': [1.0], 'volume': [1.0]})


class _PacedAdapter:
    """有 report_weight 的假适配器：验证取数循环逐币驱动上报（分钟节流在 report 内部）。"""
    def __init__(self):
        self.report_calls = 0

    def report_weight(self, log=None):
        self.report_calls += 1

    def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
        return _one_bar()


class _PlainAdapter:
    """无 report_weight 的假适配器：接线必须 getattr 兜底不炸。"""
    def fetch_ohlcv(self, sym, timeframe, start_ms, end_ms):
        return _one_bar()


def test_fetch_pass_drives_report_weight_per_symbol():
    adp = _PacedAdapter()
    out, skipped, first_err = _fetch_pass(adp, ['A', 'B', 'C'], '1h', 0, 3_600_000,
                                          0, lambda s: None)
    assert adp.report_calls == 3                     # 每币驱动一次
    assert set(out) == {'A', 'B', 'C'} and skipped == []


def test_fetch_pass_tolerates_adapter_without_report_weight():
    out, skipped, first_err = _fetch_pass(_PlainAdapter(), ['A'], '1h', 0, 3_600_000,
                                          0, lambda s: None)
    assert 'A' in out and skipped == []              # 不炸、取数正常
