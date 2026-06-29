# tests/dashboard/test_formatting.py
from gridtrade.dashboard.formatting import (ms_to_human, age_human, fmt_num,
                                            fmt_pct, fmt_size, pnl_class)


def test_fmt_size():
    assert fmt_size(None) == '-'
    assert fmt_size(0.001) == '0.001'          # 小数量不再被 2 位截成 0.00
    assert fmt_size(0.00012345) == '0.00012345'
    assert fmt_size(26.0) == '26'              # 整数量去掉尾部 0 与小数点
    assert fmt_size(1.5) == '1.5'
    assert fmt_size(0.0) == '0'


def test_ms_to_human():
    assert ms_to_human(None) == '-'
    assert ms_to_human(0) == '1970-01-01 00:00:00'


def test_age_human():
    assert age_human(None) == '-'
    assert age_human(5) == '5s'
    assert age_human(90) == '1m'
    assert age_human(7200) == '2h'
    # 边界：<60 / <3600 严格小于，故 60→分、3600→时
    assert age_human(59) == '59s'
    assert age_human(60) == '1m'
    assert age_human(3599) == '59m'
    assert age_human(3600) == '1h'
    # 负龄（时钟漂移）当作不可用
    assert age_human(-5) == '-'


def test_fmt_num_and_pct():
    assert fmt_num(None) == '-'
    assert fmt_num(1.2345, 2) == '1.23'
    assert fmt_pct(None) == '-'
    assert fmt_pct(0.1234, 1) == '12.3%'


def test_pnl_class():
    assert pnl_class(3.0) == 'pos'
    assert pnl_class(-3.0) == 'neg'
    assert pnl_class(0.0) == 'zero'
    assert pnl_class(None) == 'zero'
