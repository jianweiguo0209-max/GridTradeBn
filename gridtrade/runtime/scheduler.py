"""scheduler 机入口（常驻 process group）：每整点关旧 tag → 选币 → 准入 → 开新 → 心跳。

默认仅整点跑（避免部署 mid-hour 重处理当前 offset 而 churn）；SCHEDULER_RUN_ON_START
=true 时启动立即跑一次（testnet 调试）。单轮异常降级续跑不退出，SIGTERM 优雅停。
"""
import signal
import time
from collections import Counter

import pandas as pd

from gridtrade.config import (DEFAULT_STRATEGY_CONFIG, DEFAULT_TIER_POLICY,
                              load_deploy_config)
from gridtrade.core.selection import compute_offset
from gridtrade.core.tier_policy import capped_symbols
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.shock import cross_median_k
from gridtrade.runtime.cycles import braked_symbols, run_scheduler_cycle
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.runtime.universe import resolve_live_universe


# 逐币取数间隔（ms）。币安 USDM 权重制（2026-07-16 testnet 实测重校，替代原 HL candleSnapshot 推导）：
# scheduler 有独立出口 IP（实测 .109，与 monitor .111 分离）→ 独享 2400 权重/分；klines 取 160 根
# limit≤500 实测权重 2/币（配合 fetch_ohlcv 的 limit 贴合修复；原恒 limit=1500=权重 10）。COIN-only 票池
# 实测 ~275 币。250ms → 每币 ~0.31s(+~0.06s 拉取延迟) → 一轮 ~1.4 分钟，峰值 ~390 权重/分（~16% 预算，
# 余量给重试突发；原 2000ms≈9.5 分钟/275 币，严重超 HH:00–12 窗）。串行往返地板 ~17s（275 次×~60ms），
# 再快需并发（过度工程）。env SCHEDULER_FETCH_PACE_MS 可调、0=关。ccxt enableRateLimit 的 50ms 名义间隔对
# 权重制无效（2026-07-05 mainnet 429 风暴实证，故须显式节流：未节流时 50ms=1200 请求/分 × 权重 ≫ 2400，
# 会打爆 scheduler 自身那个 IP）。
FETCH_PACE_MS_DEFAULT = 250.0

# 熔断感知补捞(2026-07-19 12:00 UTC 66/284 币票池塌陷根因,memory binance-migration):
# 瞬时故障连败 ≥5 → 行情 CircuitBreaker open(cooldown 30s)→ 取数循环里剩余币即时失败、
# 逐币 try/except 静默跳过 → 残池选币。坏币基线 0-4 个/轮,跳过量级达两位数=级联而非坏币。
SALVAGE_MIN_SKIPPED = 10       # 跳过 ≥ 此数 → 判定级联,触发补捞
SALVAGE_COOLDOWN_S = 60.0      # 补捞前冷却:≥ breaker cooldown(30s),给瞬时故障退场时间
POOL_GUARD_FRAC = 0.8          # 方案A 池尺寸守卫:取数幸存 < 此比例×应取 → 残池,本轮只关不开。
                               # 补捞治瞬时级联;此守卫兜持续型故障——残池冠军选错代价上不封顶,
                               # 空转一轮机会成本仅 ~1 格(2026-07-19 66/284 事故:残池 FIL 顶替 UAI)。


def _fetch_pass(adapter, symbols, timeframe, start_ms, end_ms, pace_ms, sleep):
    """单轮逐币拉取:返回 (成功dict, 跳过名单, 首个错误样本)。空 df 不算跳过(合法无数据)。"""
    out, skipped, first_err = {}, [], None
    for i, sym in enumerate(symbols):
        if i and pace_ms > 0:
            sleep(pace_ms / 1000.0)   # 币间节流（默认开；env SCHEDULER_FETCH_PACE_MS 可调，0=关）
        try:
            df = adapter.fetch_ohlcv(sym, timeframe, start_ms, end_ms)
        except Exception as exc:
            skipped.append(sym)     # 坏币（BadSymbol/无数据/拉取失败）跳过，不阻塞整池
            if first_err is None:
                first_err = '%s -> %r' % (sym, exc)
            continue
        if df is not None and not df.empty:
            out[sym] = df
    return out, skipped, first_err


def fetch_universe_candles(adapter, symbols, run_time, *, timeframe='1h',
                           max_candle_num=160, pace_ms=None, sleep=time.sleep) -> dict:
    end_ms = int(pd.Timestamp(run_time).timestamp() * 1000)
    start_ms = end_ms - max_candle_num * 3600 * 1000   # 1h 根
    if pace_ms is None:
        pace_ms = FETCH_PACE_MS_DEFAULT
    out, skipped, first_err = _fetch_pass(adapter, symbols, timeframe,
                                          start_ms, end_ms, pace_ms, sleep)
    if len(skipped) >= SALVAGE_MIN_SKIPPED:
        # 仅一轮补捞:故障若仍持续,诚实保留残池(不无限重试;12H 周期晚 ~2 分钟无影响)。
        print('[scheduler] 取数跳过 %d/%d 币,疑似熔断级联 → 冷却 %.0fs 后补捞'
              % (len(skipped), len(symbols), SALVAGE_COOLDOWN_S), flush=True)
        sleep(SALVAGE_COOLDOWN_S)
        out2, skipped2, err2 = _fetch_pass(adapter, skipped, timeframe,
                                           start_ms, end_ms, pace_ms, sleep)
        out.update(out2)
        print('[scheduler] 补捞成功 %d/%d,仍失败 %d'
              % (len(out2), len(skipped), len(skipped2)), flush=True)
        skipped, first_err = skipped2, (err2 or first_err)
    if skipped:
        print('[scheduler] skipped %d symbols (e.g. %s)' % (len(skipped), first_err),
              flush=True)
    return out


def run_scheduler_once(runtime, *, now_fn=time.time,
                       fetch_candles=fetch_universe_candles) -> dict:
    rt = runtime
    flags = getattr(rt, 'flags', None)
    if flags is not None:
        if flags.get('trading_halted'):
            rt.heartbeats.beat('scheduler')
            return {'skipped': 'halted'}
        if flags.get('scheduler_paused'):
            rt.heartbeats.beat('scheduler')
            return {'skipped': 'paused'}
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    period = rt.config.scheduler_period
    offset = compute_offset(run_time, period)
    tag = '%s%d' % (DEFAULT_STRATEGY_CONFIG['strategy_tag'], offset)
    # 实盘 offset 启用门(spec 2026-07-17)：当前 offset ∉ 启用集 → 只关不开(灰度上量/减仓)。
    # 前移到取数之前——被拦的小时跳过 universe 解析/保险丝审计/~275 币 K 线取数/选币,只做换仓关格
    # +心跳(关格不需 K 线;不开仓则审计/shock/快照都无意义)。旧 cohort 在各自换仓轮被自然排空。
    _oe = getattr(rt.config, 'live_open_offsets', ())
    if _oe and offset not in _oe:
        print('[offset-gate] offset=%d 不在实盘启用集 %s → 本轮只关不开(跳过取数/选币)'
              % (offset, list(_oe)), flush=True)
        braked = braked_symbols(flags)        # 仍排除干预熔断币,不轮换关其格
        result = run_scheduler_cycle(rt.manager, rt.trigger_engine, rt.reconciler,
                                     ctx=None, close_tag=tag, open_enabled=False,
                                     braked_symbols=frozenset(braked))
        result.pop('shock_braked', None)      # 非 shock:去误导键,只留 offset_gated
        result['offset_gated'] = True
        rt.heartbeats.beat('scheduler')
        return result
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist,
                                     rt.config.whitelist, rt.config.min_quote_volume_24h,
                                     top_volume_pct=rt.config.universe_top_volume_pct)
    # 保险丝覆盖审计（spec 2026-07-15 §六）。真实成本（诚实披露，勿写"零额外 API"）：
    #   ① limits 复用 ccxt 缓存 markets（零权重）；
    #   ② fetch_prices_all（全市场 ticker/price，权重 2）；
    #   ③ _resolve_cap() 在 cap_equity_frac>0（生产默认）时会触发一次 fetch_balance（权重 5）
    #      ——本轮不开仓时这是审计独有的净增成本。
    # 合计 ≈7 权重/选币轮（每小时一次，相对 fapi 2400/min 预算可忽略，但非零）。
    # 报出不足额币 = 权益已跨临界（≈$36.7k）→ 门链开始降 cap，且实盘几何开始偏离回测（§七）。
    try:
        from gridtrade.execution.fuse_policy import audit_fuse_coverage
        _mq = {i.symbol: float(getattr(i, 'market_max_qty', 0.0) or 0.0)
               for i in rt.adapter.list_instruments()}
        _au = audit_fuse_coverage(universe, rt.adapter.fetch_prices_all(universe), _mq,
                                  rt.executor._resolve_cap(), rt.executor.gearing)
        if _au['short']:
            print('[audit] 保险丝不足额 %d/%d 币（最差 %s %.0f%%）：门链将降 cap 护全额；'
                  '实盘几何已偏离回测（spec 2026-07-15 §七）'
                  % (len(_au['short']), _au['total'], _au['short'][0][0],
                     100.0 * _au['short'][0][1]), flush=True)
        else:
            print('[audit] 保险丝覆盖 OK：票池 %d 币全足额（满仓名义 $%.0f）'
                  % (_au['total'], _au['need']), flush=True)
    except Exception as exc:      # 审计失败绝不阻断选币轮
        print('[audit] 保险丝覆盖审计跳过: %r' % (exc,), flush=True)
    # 方案A（legacy 半拉黑档2 执行位对齐）经共享 tier_policy 表达（spec 同源性②，
    # 与回测 allocate_with_tiers 同一判定源）：触顶币在选币入口剔出票池——连 K 线都
    # 不拉，因子排名自动落次优币；否则榜一触顶会在开仓时被 DB 槽位拒、当轮空转
    # （testnet SOL×2/HYPE 实证）。本轮换仓 tag 自己持有的币即将被 close_tag 释放
    # → 不计 held（允许连任，状态供给侧口径）。竞态守卫=GridRepository 槽位 UNIQUE
    # （SlotExhausted 由 open_proposals 逐提议捕获，SymbolLockGate 已删）。
    braked = braked_symbols(flags)            # 外部干预熔断币(spec 2026-07-12 组件三)
    if braked:
        universe = [s for s in universe if s not in braked]
        print('[intervention] scheduler pre-filter: -%d braked %s'
              % (len(braked), sorted(braked)), flush=True)
    held = Counter(g.symbol for g in rt.manager.executor.grids.list_active()
                   if g.tag != tag)
    try:
        _mlmap = rt.adapter.fetch_max_leverages()   # 杠杆感知上限(组件四);失败 fail-open
    except Exception:
        _mlmap = {}
    banned = capped_symbols(universe, held, DEFAULT_TIER_POLICY, maxlev_map=_mlmap)
    if banned:
        universe = [s for s in universe if s not in banned]
        print('[scheduler] symbol-lock pre-filter: -%d held %s'
              % (len(banned), sorted(banned)), flush=True)
    # 票池杠杆预过滤(2026-07-18,UNIVERSE_MIN_LEVERAGE)：pick_L<阈值 的币连 K 线都不拉——
    # 低杠杆档币 IM(整梯名义/L)吃满余额、必被 MarginGate 拒,top-1 选中它=整轮空转
    # (04:00 MYX 实证 L=5→IM $511=全余额)。与开仓/MarginGate 同源 pick_leverage 预演;
    # 判据=第一档最大杠杆<min_lev 剔(2026-07-19 修正,见 eligible_min_leverage);notional 保留
    # 传参兼容但过滤不再用。fail-open:档位/余额取不到跳过。
    _minlev = float(getattr(rt.config, 'universe_min_leverage', 0.0) or 0.0)
    if _minlev > 0:
        try:
            from gridtrade.execution.leverage_policy import eligible_min_leverage
            _tmap = rt.adapter.fetch_leverage_tiers_map()
            _notional = float(rt.executor._resolve_cap()) * float(rt.executor.gearing)
            universe, _lowlev = eligible_min_leverage(universe, _tmap, _notional,
                                                      rt.executor.gearing, _minlev)
            if _lowlev:
                print('[scheduler] min-leverage pre-filter: -%d 币 pick_L<%g '
                      '(notional≈$%.0f, e.g. %s)'
                      % (len(_lowlev), _minlev, _notional, sorted(_lowlev)[:5]),
                      flush=True)
        except Exception as exc:      # fail-open:预过滤失败绝不禁池(MarginGate 仍兜底)
            print('[scheduler] min-leverage pre-filter skipped: %r' % (exc,), flush=True)
    candles = fetch_candles(rt.adapter, universe, run_time,
                            max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'],
                            pace_ms=getattr(rt.config, 'scheduler_fetch_pace_ms', None))
    # 池尺寸守卫(方案A,2026-07-19 66/284 塌陷事故):幸存/应取 低于 POOL_GUARD_FRAC →
    # 本轮只关不开(复用 open_enabled 通道,与 shock/offset-gate 同语义)。
    pool_ok = len(candles) >= POOL_GUARD_FRAC * len(universe)
    if not pool_ok:
        print('[pool-guard] 取数幸存 %d/%d < %.0f%% → 残池,本轮只关不开'
              % (len(candles), len(universe), POOL_GUARD_FRAC * 100), flush=True)
    # 票池快照(2026-07-12,选币可复现性):落"实际进入排名的集合"(post 地板/黑名单/
    # held 预过滤/braked/取数跳过)——因子名次是组内相对名次,没有它历史选币不可精确
    # 复现(实证:TRUMP 在 168 币集合无影、57 币线上集合进 #4)。fail-soft:快照失败
    # 绝不阻塞选币开格。
    try:
        from gridtrade.state.universe_snapshots import UniverseSnapshotRepository
        # 方案B(每轮取数成功率落库):expected=应取(post 预过滤票池)、ok=幸存(含空df剔除)。
        # 常态成功率史从此可查(66/284 事故前只有易逝的容器日志);守卫面包屑仅触发轮才有。
        _exc = {'held_banned': sorted(banned), 'braked': sorted(braked),
                'fetch': {'expected': len(universe), 'ok': len(candles)}}
        if not pool_ok:
            _exc['pool_guard'] = {'survivors': len(candles), 'universe': len(universe)}
        UniverseSnapshotRepository(rt.store).add(
            rt.config.exchange, int(run_time.value // 1_000_000),
            list(candles.keys()), excluded=_exc)
    except Exception as exc:
        print('[scheduler] universe snapshot skipped: %r' % exc, flush=True)
    # MarketShockBrake(spec 2026-07-08):|票池中位数 k 小时收益|≥thr → 本轮只关不开,
    # 并暂停 pause 小时;状态进程内(信号自持 ~k 小时,重启自愈,约束 pause<=k)。
    open_enabled = pool_ok
    thr = float(getattr(rt.config, 'shock_thr', 0.0) or 0.0)
    if thr > 0:
        k = int(getattr(rt.config, 'shock_k_hours', 4))
        pause = int(getattr(rt.config, 'shock_pause_hours', 2))
        if pause > k:
            print('[shock] WARN pause(%dh)>k(%dh):重启窗口可能漏暂停' % (pause, k), flush=True)
        med = cross_median_k(candles, run_time, k)
        until = getattr(rt, '_shock_until', None)
        if med is not None and abs(med) >= thr:
            new_until = run_time + pd.Timedelta(hours=pause)
            if until is None or new_until > until:
                until = new_until
                setattr(rt, '_shock_until', until)
            print('[shock] med_%dh=%+.1f%% |>=%.1f%%| -> 暂停开格至 %s'
                  % (k, 100 * med, 100 * thr, until), flush=True)
        if until is not None and run_time < until:
            open_enabled = False
            if med is None or abs(med) < thr:
                print('[shock] braked until %s(信号已回落,窗口内继续暂停)' % until, flush=True)
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    result = run_scheduler_cycle(rt.manager, rt.trigger_engine, rt.reconciler,
                                 ctx, close_tag=tag, open_enabled=open_enabled,
                                 braked_symbols=frozenset(braked))
    if not pool_ok:
        result['pool_guarded'] = True
    # 选币名次快照(record-and-replay,2026-07-17 实盘对账):记本 tick 排名 picks+因子值,
    # 供离线复放精确对齐名次(universe 记了票池集合、此表补因子/名次)。fail-soft:绝不阻塞。
    try:
        if ctx.selection_ranked:
            from gridtrade.state.reconciliation_snapshots import SelectionSnapshotRepository
            SelectionSnapshotRepository(rt.store).add(
                rt.config.exchange, int(run_time.value // 1_000_000),
                ctx.selection_offset or 0, ctx.selection_ranked,
                [r['symbol'] for r in ctx.selection_ranked])
    except Exception as exc:
        print('[scheduler] selection snapshot skipped: %r' % exc, flush=True)
    rt.heartbeats.beat('scheduler')
    return result


def _seconds_to_next_hour(now_epoch) -> int:
    return 3600 - (int(now_epoch) % 3600)


def _safe_run(runtime, run_once_fn, now_fn, log):
    try:
        run_once_fn(runtime, now_fn=now_fn)
    except Exception as exc:          # 降级：记录 + 续跑，绝不退出
        log('[scheduler] degraded: %r' % exc)


def run_scheduler(runtime, *, once=False, sleep=time.sleep, now_fn=time.time,
                  log=print, run_once_fn=run_scheduler_once,
                  should_stop=None, run_on_start=False):
    if run_on_start:
        _safe_run(runtime, run_once_fn, now_fn, log)
        if once:
            return
    while True:
        sleep(_seconds_to_next_hour(now_fn()))
        _safe_run(runtime, run_once_fn, now_fn, log)
        if once:
            return
        if should_stop is not None and should_stop():
            return


def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    rt.adapter.assert_account_mode()   # 账户模式不符→boot 失败（fail-fast，勿带病起跑）
    print('[scheduler] exchange=%s testnet=%s endpoint=%s run_on_start=%s period=%s open_offsets=%s'
          % (rt.config.exchange, rt.config.testnet, adapter_endpoint(rt.adapter),
             rt.config.scheduler_run_on_start, rt.config.scheduler_period,
             list(rt.config.live_open_offsets) or '全开'),
          flush=True)
    stop = {'flag': False}

    def _graceful(signum, frame):
        stop['flag'] = True

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    run_scheduler(rt, should_stop=lambda: stop['flag'],
                  run_on_start=rt.config.scheduler_run_on_start)


if __name__ == '__main__':
    main()
