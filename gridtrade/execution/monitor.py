"""monitor_grid：单网格监控步（sync → 评估退出 → 触发则平仓）。循环/调度归 P4 运行时。"""
from gridtrade.core.stop_rules import evaluate_exit


def monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05, skip_replenish=False):
    res = executor.sync(grid_id, symbol, skip_replenish=skip_replenish)
    snap = res['snapshot']
    acc = executor.accounting.get(grid_id)
    pnl_ratio_max = acc.pnl_ratio_max if acc is not None else snap['pnl_ratio']
    reason = evaluate_exit(snap['pnl_ratio'], pnl_ratio_max, net_value=snap['net_value'],
                           stop_cfg=stop_cfg, margin_rate=margin_rate,
                           funding_rate=0.0, pv_spike=0)
    if reason:
        executor.close(grid_id, symbol, reason)
        return {'closed': True, 'reason': reason, 'pnl_ratio': snap['pnl_ratio'],
                'fills': res.get('fills', [])}
    return {'closed': False, 'reason': None, 'pnl_ratio': snap['pnl_ratio'],
            'fills': res.get('fills', [])}
