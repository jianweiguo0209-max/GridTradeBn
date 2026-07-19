"""标量退出评估器：把 grid_engine._apply_exit 的逐 bar 优先级判定提成一个标量函数，
供实盘监控按当前 (pnl_ratio, pnl_ratio_max, net_value, funding_rate, pv_spike) 判定止盈止损。
优先级与 _apply_exit 完全同序（由 tests/core/test_stop_rules.py 等价测试锁定）。
"""
from typing import Optional


def evaluate_exit(pnl_ratio: float, pnl_ratio_max: float, *, net_value: float,
                  stop_cfg: Optional[dict] = None, margin_rate: float = 0.05,
                  funding_rate: Optional[float] = None, pv_spike: int = 0,
                  pv_dir: int = 0, net_position: Optional[float] = None) -> Optional[str]:
    """返回退出原因或 None。优先级：固定止损 > 连续回撤止盈 > 资金费率止损 > pv主动止损 > 爆仓。
    stop_cfg=None 时仅查爆仓（与 _apply_exit 一致）。

    方向性门控(spec 2026-07-19-pv-directional,stop_cfg['pv_directional'] 开时):
    pv 按净仓×同窗方向门控(净多只听跌尖峰 pv_dir<0、净空只听涨、零仓不触发);funding 加零仓门控。
    net_position=None = 接线缺失 → fail-open 回旧行为(monitor 必传,接线测试钉住)。"""
    _directional = bool(stop_cfg.get('pv_directional')) if stop_cfg else False
    if stop_cfg is not None:
        if pnl_ratio < -stop_cfg['stop_loss']:
            return '固定止损'
        k = stop_cfg.get('trailing_k')
        floor = stop_cfg.get('trailing_floor')
        if k is not None and floor is not None:
            allowed = max(floor, k * pnl_ratio_max)
            if (pnl_ratio_max - pnl_ratio >= allowed) and (pnl_ratio_max > floor):
                return '连续回撤止盈'
        fr_thr = stop_cfg.get('fundingRate_stop_loss')
        if fr_thr is not None and funding_rate is not None:
            if abs(funding_rate) > fr_thr:
                if not (_directional and net_position is not None and net_position == 0):
                    return '资金费率止损'
        if pv_spike == 1 and pnl_ratio < stop_cfg.get('pv_pnl_thr', -0.015):
            if not _directional or net_position is None:
                return 'pv主动止损'
            if (net_position > 0 and pv_dir < 0) or (net_position < 0 and pv_dir > 0):
                return 'pv主动止损'
    if net_value < margin_rate:
        return '爆仓'
    return None
