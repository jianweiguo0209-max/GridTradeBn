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

LADDER = 1.01
MIN_WINDOW_BARS = 600


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


def p12_eff(cross1, mae):
    return cross1 / (1.0 + 100.0 * mae)
