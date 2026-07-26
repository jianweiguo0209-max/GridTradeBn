"""成交密度 → 引擎修正系数(aggTrades 真实逐笔实测标定,2026-07-26)。

**性质**:这不是"过滤",是**修正**。tick 是物理约束(那个价位在交易所不存在,单挂不出去)⇒ 该过滤;
成交密度是**认知边界**(网格物理上能那样成交,只是引擎的 4-tick 分钟内路径近似没被验证过)
⇒ 不该丢样本,该把不确定性变成系数。

**标定方法**(aggtrades_path/aggtrades_density,已过仪器校验):
  X_engine  1m→4tick 近似穿越数(回测口径)
  X_true    aggTrades 真实逐笔路径的穿越数
  X_real    真实路径 + 模拟 executor(逐线静置挂单、成交后对侧补单、补单延迟
            = prod MONITOR_INTERVAL_SEC=5s、方向匹配才成交)⇒ 实盘真能吃到几笔
  净修正 = X_real / X_engine
仪器校验:crossings() 跑 4-tick 路径 vs get_trade_info **132/132 逐位相等**;
已知点校准:实盘 121 格 X_real/实盘真值 = **1.0058**,逐格 97.5% 精确。

**为什么修正这么小**:两个误差方向相反、几乎对消——
4-tick 近似在高密度**少数**(真实/近似 0.997→1.55),而 5s 补单延迟让实盘**吃不到**
(可实现/真实 1.000→0.49)。净效应在实盘 max 的 7.6 倍处仍只差 24%。

⚠ **用法边界**:
  · 这是**臂级近似**——按该臂的平均 fills 折算密度取系数。逐格密度有分布,精确做法是
    逐格查表,但扫描不落逐格明细,故取均值近似。
  · 收益折算用 `(1+ret)^c - 1`:每格 pnl 约正比于成交数 ⇒ lane 连乘 ≈ 幂次。
    一阶近似,未计费用的非线性。
  · 密度 > 0.5 穿越/bar 超出标定范围(最高箱中位 0.497),外推需谨慎。
"""
import bisect

# (穿越/bar 中位, 净修正) —— 来自 aggtrades_density.parquet 分箱聚合
CURVE = [(0.0028, 0.9967), (0.0069, 0.9851), (0.0139, 0.9798), (0.0289, 0.9730),
         (0.0557, 0.9594), (0.1108, 0.9343), (0.2065, 0.8738), (0.4972, 0.7612)]
BARS_12H = 720.0
CALIB_MAX = CURVE[-1][0]


def correction(fills, bars=BARS_12H):
    """臂级平均 fills → 净修正系数(线性插值;两端夹到端点值)。"""
    d = float(fills) / bars
    xs = [x for x, _ in CURVE]
    ys = [y for _, y in CURVE]
    if d <= xs[0]:
        return ys[0]
    if d >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_left(xs, d)
    x0, y0, x1, y1 = xs[i - 1], ys[i - 1], xs[i], ys[i]
    return y0 + (y1 - y0) * (d - x0) / (x1 - x0)


def corrected_ret(ret_pct, fills, bars=BARS_12H):
    """(1+ret)^c - 1,ret_pct 与返回值均为百分数。"""
    c = correction(fills, bars)
    r = float(ret_pct) / 100.0
    if r <= -1:
        return ret_pct
    return ((1.0 + r) ** c - 1.0) * 100.0


def out_of_calib(fills, bars=BARS_12H):
    return float(fills) / bars > CALIB_MAX


if __name__ == '__main__':
    print('密度 → 净修正(标定曲线)')
    for x, y in CURVE:
        print('  %.4f 穿越/bar  →  %.4f   (12h格 ≈ %5.1f fills)' % (x, y, x * BARS_12H))
    print('\n关键刻度(12h 格):')
    for f, lab in ((5.3, '实盘现值 b3_c16'), (9.2, '清洗后 b2_c16'),
                   (11.1, '旧闸门 OK 线'), (27.5, '清洗后 b2_c26'),
                   (47.2, '旧闸门 越界线'), (76.3, '原扫描 b2_c26'),
                   (151.3, '原扫描 b2_c26 × OOS')):
        c = correction(f)
        print('  %-24s fills=%6.1f → %.4f/bar → c=%.3f  %s'
              % (lab, f, f / BARS_12H, c, '⚠超标定范围' if out_of_calib(f) else ''))
    print('\n自检:')
    assert correction(0) == CURVE[0][1] and correction(1e9) == CURVE[-1][1], '端点夹持失效'
    assert all(correction(a) >= correction(b) for a, b in zip(range(1, 60), range(2, 61))), \
        '修正系数应随 fills 单调不增'
    assert abs(corrected_ret(0.0, 5.3)) < 1e-9, 'ret=0 修正后仍应为 0'
    print('  ✓ 端点夹持 / 单调性 / 零点不变 全部通过')
