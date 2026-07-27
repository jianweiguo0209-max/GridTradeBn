"""p12(过去12h)标签:cross1/mae/eff —— 与回测 stage_L(holdout_gate._label_one)逐位同源。

⚠ 同源性红线(动任何一行先对 golden 测试):
  - cross1 = floor(ln(close)/ln(1.01)) 阶梯的收盘价穿越计数;dstep 在**整段传入序列**上
    diff(prepend=首值)后再按时间切窗——窗首根带着窗前最后一根的过渡,调用方须多给窗前 bar
    (LabelFeed 缓冲 13h、窗 12h,留 1h 余量即为此)。
  - o = 窗内第一根 1m 的 **close**(不是 open);mae = max(|maxH/o−1|, |minL/o−1|)。
  - 窗内 bar < 600(应 720)⇒ 返 None ⇒ 该币该轮缺标签不参选(回测 inner join 同款)。
"""
import numpy as np
import pandas as pd

# ⭐ eff1 因子的**唯一定义处**。实盘(runtime/label_feed)、回测(backtest/p12_replay)、
#    config 的 DEFAULT_EFF1_CFG 全部引用这四个常量,不得各写各的
#    (2026-07-27 之前 12h 窗被独立写了三遍 ⇒ 改一处漏两处就实盘/回测静默分叉;
#     由 tests/core/test_eff1_single_source.py 钉死)。
LADDER = 1.01            # 对数阶梯步长:cross1 数的是穿过 1% 阶梯的次数
MIN_WINDOW_BARS = 600    # 窗内 1m 根数下限(满窗 720);不足 ⇒ 该(轮,币)无标签、不参选
LABEL_HOURS = 12         # 标签窗 = [rt−LABEL_HOURS, rt)
MAE_COEF = 100.0         # p12_eff = cross1 / (1 + MAE_COEF × mae)


def ladder_dstep(close):
    step = np.floor(np.log(np.clip(np.asarray(close, dtype=float), 1e-18, None))
                    / np.log(LADDER))
    return np.abs(np.diff(step, prepend=step[0]))


def window_label(bars, w0, w1):
    t = bars['candle_begin_time'].values
    m = (t >= np.datetime64(pd.Timestamp(w0))) & (t < np.datetime64(pd.Timestamp(w1)))
    if int(m.sum()) < MIN_WINDOW_BARS:
        return None
    c = bars['close'].to_numpy(dtype=float)
    sd = ladder_dstep(c)
    cw = c[m]
    o = cw[0]
    hi = bars['high'].to_numpy(dtype=float)[m].max()
    lo = bars['low'].to_numpy(dtype=float)[m].min()
    return float(sd[m].sum()), max(abs(float(hi / o - 1.0)), abs(float(lo / o - 1.0)))


def window_labels_batch(bars, starts, hours=LABEL_HOURS):
    """同一 symbol 的一串窗口起点批量算标签 —— **与逐窗 window_label 逐位一致**。

    只为回测吞吐存在(选币回放 ~1400 轮 × ~280 币,逐窗调会每次 O(n) 重算掩码)。
    等价性靠三件事:①dstep 仍在**整段序列**上算一次(与逐窗版看同一前驱,断档处也是);
    ②cross1 用前缀和取窗内区间和(浮点上与逐窗 sum 同为顺序累加);③mae 用同一 o/hi/lo 定义。
    parity 由 tests/core/test_p12_labels_batch.py 逐位钉死,动这里必先过它。

    返回与 starts 等长的 list,元素为 (cross1, mae) 或 None(窗内 bar < MIN_WINDOW_BARS)。
    """
    out = []
    n = len(bars)
    if n == 0:
        return [None] * len(starts)
    t = bars['candle_begin_time'].to_numpy(dtype='datetime64[ns]')
    c = bars['close'].to_numpy(dtype=float)
    h = bars['high'].to_numpy(dtype=float)
    lo_arr = bars['low'].to_numpy(dtype=float)
    csum = np.concatenate([[0.0], np.cumsum(ladder_dstep(c))])
    span = np.timedelta64(int(hours * 3600 * 1e9), 'ns')
    for w0 in starts:
        a = np.datetime64(pd.Timestamp(w0))
        i0 = int(np.searchsorted(t, a, side='left'))
        i1 = int(np.searchsorted(t, a + span, side='left'))
        if i1 - i0 < MIN_WINDOW_BARS:
            out.append(None)
            continue
        o = c[i0]
        out.append((float(csum[i1] - csum[i0]),
                    max(abs(float(h[i0:i1].max() / o - 1.0)),
                        abs(float(lo_arr[i0:i1].min() / o - 1.0)))))
    return out


def p12_eff(cross1, mae):
    return cross1 / (1.0 + MAE_COEF * mae)
