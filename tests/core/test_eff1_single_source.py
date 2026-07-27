"""eff1 因子参数的**单一事实源**钉子:定义只有一份,实盘/回测/config 三处必须指向它。

背景(2026-07-27):这些参数一度散在三个文件里,12h 标签窗被独立写了三遍
(label_feed 内联字面量 / p12_replay 自己的 LABEL_HOURS / p12_labels 默认参数)。
**改一处、漏两处 ⇒ 实盘与回测静默分叉**,和当天 CANDLE_COLS 缺 `ts` 是同一类失败:
不报错、看起来正常。本文件让"分叉"变成红灯。
"""
import inspect

from gridtrade.backtest import p12_replay as PR
from gridtrade.config import DEFAULT_EFF1_CFG
from gridtrade.core import p12_labels as PL
from gridtrade.runtime import label_feed as LF


def test_config_mirrors_core_definition():
    """config 里的 DEFAULT_EFF1_CFG 必须逐值等于 core 的定义(它只是聚合视图,不是第二份定义)。"""
    assert DEFAULT_EFF1_CFG == {
        'ladder': PL.LADDER,
        'mae_coef': PL.MAE_COEF,
        'label_hours': PL.LABEL_HOURS,
        'min_window_bars': PL.MIN_WINDOW_BARS,
    }


def test_live_and_backtest_share_the_same_label_window():
    """实盘 LabelFeed 与回测 p12_replay 必须用**同一个** LABEL_HOURS 对象,不是各写各的 12。"""
    assert LF.LABEL_HOURS is PL.LABEL_HOURS
    assert PR.LABEL_HOURS is PL.LABEL_HOURS


def test_live_buffer_is_derived_from_label_window_not_hardcoded():
    """缓冲必须 = 标签窗 + 前驱余量(派生),否则改窗宽会让缓冲不够、窗首前驱缺席。"""
    assert LF.BUFFER_HOURS == PL.LABEL_HOURS + LF.PREHEAT_HOURS
    assert LF.PREHEAT_HOURS >= 1


def test_no_literal_12_hour_window_left_in_call_sites():
    """源码级守卫:两个消费方都不许再出现 `hours=12` 字面量(退回到重复定义)。"""
    for mod in (LF, PR):
        src = inspect.getsource(mod)
        assert 'hours=12' not in src, '%s 又写死了 12h 窗,应引用 p12_labels.LABEL_HOURS' % mod.__name__


def test_p12_eff_uses_named_coefficient():
    """公式系数走具名常量:改系数=改因子定义,必须在一个地方改。"""
    assert PL.p12_eff(10.0, 0.05) == 10.0 / (1.0 + PL.MAE_COEF * 0.05)
