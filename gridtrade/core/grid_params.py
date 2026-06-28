import math

import numpy as np


def calc_grid_params_v1(row, price_limit, stop_limit, **kwargs):
    """
    V1 原始布网逻辑（保持原有行为不变）
    - 网格区间: min(3 * ATR_5, price_limit)
    - 终止价:   基于固定 price_limit + stop_limit（不跟随动态区间）
    - 格间距:   固定 1.4%
    - 网格数:   区间宽度 / 格间距，上限 149，无下限保护
    """
    atr_5 = row['Atr_5']
    close = row['close']
    middle_5 = row['middle_5']

    # 网格区间: 基于ATR动态调整，上限为price_limit
    range_pct_up = min(3 * atr_5, price_limit[1])
    range_pct_down = min(3 * atr_5, price_limit[0])

    high_price = close * (1 + range_pct_up)
    low_price = close * (1 - range_pct_down)

    # 终止价: 基于固定price_limit（不跟随动态区间）
    stop_high_price = close * (1 + price_limit[1]) * (1 + stop_limit)
    stop_low_price = close * (1 - price_limit[0]) * (1 - stop_limit)

    # 网格数: 固定1.4%格间距
    grid_spacing = middle_5 * 0.014
    flex_grid_count = round((high_price - low_price) / grid_spacing) if grid_spacing > 0 else 25
    grid_count = min(flex_grid_count, 149)

    return {
        'high_price': high_price,
        'low_price': low_price,
        'stop_high_price': stop_high_price,
        'stop_low_price': stop_low_price,
        'grid_count': grid_count,
    }


# ========== V2 优化布网逻辑 ==========
def calc_grid_params_v2(row, price_limit, stop_limit, v2_config, **kwargs):
    """
    V2 优化布网逻辑
    相比V1的改进:
    1. 网格区间增加下限保护(range_pct_min)，防止极低波动时区间过窄
    2. 终止价跟随实际动态区间 + 缓冲，而非固定price_limit
    3. 格间距基于ATR动态计算，而非固定1.4%
    4. 网格数有上下限保护(grid_count_min ~ grid_count_max)
    """
    atr_5 = row['Atr_5']
    close = row['close']
    middle_5 = row['middle_5']

    atr_range_mult = v2_config['atr_range_multiplier']
    range_pct_min = v2_config['range_pct_min']
    range_pct_max = v2_config['range_pct_max']
    spacing_atr_ratio = v2_config['grid_spacing_atr_ratio']
    spacing_min = v2_config['grid_spacing_min']
    spacing_max = v2_config['grid_spacing_max']
    count_min = v2_config['grid_count_min']
    count_max = v2_config['grid_count_max']
    stop_buffer = v2_config['stop_buffer_ratio']

    # ---- 1. 网格区间（带下限保护）----
    range_pct = min(max(atr_5 * atr_range_mult, range_pct_min), range_pct_max)

    high_price = close * (1 + range_pct)
    low_price = close * (1 - range_pct)

    # ---- 2. 终止价跟随动态区间 + 缓冲 ----
    stop_high_price = close * (1 + range_pct) * (1 + stop_buffer)
    stop_low_price = close * (1 - range_pct) * (1 - stop_buffer)

    # ---- 3. 格间距基于ATR动态计算 ----
    # 高波动时格间距变大（避免噪音频繁触发），低波动时格间距变小（捕捉小震荡）
    grid_spacing_ratio = min(max(atr_5 * spacing_atr_ratio, spacing_min), spacing_max)
    grid_spacing = middle_5 * grid_spacing_ratio

    # ---- 4. 网格数（带上下限保护）----
    price_range = high_price - low_price
    flex_grid_count = round(price_range / grid_spacing) if grid_spacing > 0 else count_min
    grid_count = max(count_min, min(flex_grid_count, count_max))

    return {
        'high_price': high_price,
        'low_price': low_price,
        'stop_high_price': stop_high_price,
        'stop_low_price': stop_low_price,
        'grid_count': grid_count,
    }


# ========== 格式化辅助函数 ==========
def _format_price(price, accuracy):
    """根据精度格式化价格，解决科学计数法问题"""
    return np.format_float_positional(
        round(price, accuracy), precision=accuracy, unique=False
    )
