"""monitor_grid：单网格监控步（sync → 评估退出 → 触发则平仓）。循环/调度归 P4 运行时。"""
from gridtrade.core.stop_rules import evaluate_exit


def monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05, skip_replenish=False,
                 pv_spike=0, pv_dir=0, funding_rate=0.0, snapshot=None, defer_close=False):
    """defer_close(spec 2026-07-11-symbol-desk 组件二):True=只读决策,触发时返回
    close_intent 不执行——monitor 轮阶段 B 按币合并成 close_set(同币市价单永不并发);
    默认 False 即时关格,既有直调路径零改动。
    pv_dir/net_position(spec 2026-07-19-pv-directional):方向门控输入——净仓取 sync 快照
    (live 账本,与 pnl 同源),flag 关时 evaluate_exit 忽略之(零行为变更)。"""
    res = executor.sync(grid_id, symbol, skip_replenish=skip_replenish, snapshot=snapshot)
    snap = res['snapshot']
    acc = executor.accounting.get(grid_id)
    pnl_ratio_max = acc.pnl_ratio_max if acc is not None else snap['pnl_ratio']
    reason = evaluate_exit(snap['pnl_ratio'], pnl_ratio_max, net_value=snap['net_value'],
                           stop_cfg=stop_cfg, margin_rate=margin_rate,
                           funding_rate=funding_rate, pv_spike=pv_spike, pv_dir=pv_dir,
                           net_position=snap.get('net_position'))
    if reason:
        if defer_close:
            return {'closed': False, 'reason': None, 'close_intent': reason,
                    'pnl_ratio': snap['pnl_ratio'], 'fills': res.get('fills', [])}
        executor.close(grid_id, symbol, reason)
        return {'closed': True, 'reason': reason, 'pnl_ratio': snap['pnl_ratio'],
                'fills': res.get('fills', [])}
    return {'closed': False, 'reason': None, 'pnl_ratio': snap['pnl_ratio'],
            'fills': res.get('fills', [])}
