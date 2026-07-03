# tests/dashboard/test_formatting.py
from gridtrade.dashboard.formatting import (ms_to_human, age_human, fmt_num,
                                            fmt_pct, fmt_size, fmt_fee, fmt_price,
                                            pnl_class)


def test_fmt_price():
    assert fmt_price(None) == '-'
    assert fmt_price(0.0) == '0'
    # 低价币：fmt_num(2 位) 会塌成 0.08/0.37，须保留有效数字
    assert fmt_price(0.07768) == '0.07768'
    assert fmt_price(0.06251234) == '0.0625123'      # 6 位有效数字
    assert fmt_price(0.36948) == '0.36948'
    assert fmt_price(1.78601414) == '1.78601'
    assert fmt_price(0.00123456) == '0.00123456'     # sub-cent 不再塌成 0.00
    # 高价币：够即可、去尾零
    assert fmt_price(561.84) == '561.84'
    assert fmt_price(60949.0) == '60949'


def test_fmt_fee():
    assert fmt_fee(None) == '-'
    # maker 手续费 ~0.002：不能被 2 位精度截成 0.00（Recent Fills fee 列显示 bug）
    assert fmt_fee(0.001955) == '0.001955'
    assert fmt_fee(0.00204) == '0.00204'
    assert fmt_fee(0.015866) == '0.015866'   # 累计 fee_paid
    assert fmt_fee(0.0) == '0'               # 真 0 仍显示 0（去尾零）
    assert fmt_fee(1.5) == '1.5'


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


def test_to_display_dt_utc_default_and_iana_and_fallback():
    from gridtrade.dashboard.formatting import to_display_dt
    ts = 1704067200000  # 2024-01-01 00:00:00 UTC
    assert to_display_dt(ts).strftime('%Y-%m-%d %H:%M') == '2024-01-01 00:00'
    assert to_display_dt(ts, 'Asia/Shanghai').strftime('%Y-%m-%d %H:%M') == '2024-01-01 08:00'
    # 非法时区回退 UTC、不抛
    assert to_display_dt(ts, 'Nowhere/Nope').strftime('%Y-%m-%d %H:%M') == '2024-01-01 00:00'


def test_ms_to_human_respects_tz():
    from gridtrade.dashboard.formatting import ms_to_human
    ts = 1704067200000
    assert ms_to_human(ts) == '2024-01-01 00:00:00'
    assert ms_to_human(ts, 'Asia/Shanghai') == '2024-01-01 08:00:00'


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
