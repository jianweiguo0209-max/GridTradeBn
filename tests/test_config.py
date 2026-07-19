from gridtrade.config import (load_deploy_config, DeployConfig, compute_cap,
                              DEFAULT_STRATEGY_CONFIG, DEFAULT_STOP_CFG)


def test_cap_equity_frac_defaults_and_parsing():
    cfg = load_deploy_config(env={})
    assert abs(cfg.cap_equity_frac - 0.24510) < 1e-4   # 推导值:AL5.0/(12×1.7)——默认与部署值一致(用户定 2026-07-09)
    assert cfg.cap_min == 20.0 and cfg.cap_max == 100000.0
    cfg2 = load_deploy_config(env={'CAP_MIN': '30', 'CAP_MAX': '500'})
    assert cfg2.cap_min == 30.0 and cfg2.cap_max == 500.0


def test_compute_cap_clamps():
    assert compute_cap(1000.0, 0.10, 20.0, 100000.0) == 100.0
    assert compute_cap(150.0, 0.10, 20.0, 100000.0) == 20.0        # clamp 下限
    assert compute_cap(1e9, 0.10, 20.0, 500.0) == 500.0            # clamp 上限
    assert compute_cap(1000.0, 0.0, 20.0, 100000.0) is None        # frac<=0 → 停用(返回 None)


def test_defaults_when_env_empty():
    cfg = load_deploy_config(env={})
    assert isinstance(cfg, DeployConfig)
    assert cfg.exchange == 'binance'
    assert cfg.testnet is False
    assert cfg.cap == 100.0
    assert cfg.grid_gearing == 3.4 and cfg.account_leverage == 5.0
    assert cfg.monitor_interval_sec == 5.0
    assert cfg.scheduler_period == '12H'
    assert cfg.max_concurrent == 12
    assert cfg.api_key == '' and cfg.api_secret == ''


def test_parses_env_with_type_coercion():
    env = {
        'EXCHANGE': 'fake',
        'BINANCE_API_KEY': '0xabc',
        'BINANCE_API_SECRET': 'deadbeef',
        'BINANCE_TESTNET': 'true',
        'DATABASE_URL': 'postgresql+psycopg2://u:p@h/db',
        'CAP': '250.5',
        'GRID_GEARING': '2.0',
        'MONITOR_INTERVAL_SEC': '3.5',
        'SCHEDULER_PERIOD': '6H',
        'MAX_CONCURRENT': '10',
        'TOTAL_BUDGET': '5000',
        'DEFAULT_CAP': '200',
    }
    cfg = load_deploy_config(env=env)
    assert cfg.exchange == 'fake'
    assert cfg.api_key == '0xabc' and cfg.api_secret == 'deadbeef'
    assert cfg.testnet is True
    assert cfg.database_url == 'postgresql+psycopg2://u:p@h/db'
    assert cfg.cap == 250.5 and cfg.grid_gearing == 2.0
    assert cfg.monitor_interval_sec == 3.5 and cfg.scheduler_period == '6H'
    assert cfg.max_concurrent == 10 and cfg.total_budget == 5000.0
    assert cfg.default_cap == 200.0


def test_bool_parsing_variants():
    assert load_deploy_config(env={'BINANCE_TESTNET': 'YES'}).testnet is True
    assert load_deploy_config(env={'BINANCE_TESTNET': '1'}).testnet is True
    assert load_deploy_config(env={'BINANCE_TESTNET': 'off'}).testnet is False
    assert load_deploy_config(env={'BINANCE_TESTNET': 'false'}).testnet is False


def test_default_cap_falls_back_to_cap_when_unset():
    cfg = load_deploy_config(env={'CAP': '300'})
    assert cfg.cap == 300.0 and cfg.default_cap == 300.0   # default_cap 未设 -> 用 cap


def test_blacklist_defaults_to_tier0_env_overrides():
    # 名单单源（spec 2026-07-06-tiered-* 同源性①）：env 未设/空串 → DEFAULT_TIER_POLICY.tier0；
    # 非空 → 覆盖（运维紧急面）。
    from gridtrade.config import DEFAULT_TIER_POLICY
    assert load_deploy_config(env={}).blacklist == DEFAULT_TIER_POLICY.tier0
    assert load_deploy_config(env={'BLACKLIST_SYMBOLS': ''}).blacklist == DEFAULT_TIER_POLICY.tier0
    cfg = load_deploy_config(env={'BLACKLIST_SYMBOLS': 'BTC, ETH ,SOL'})
    assert cfg.blacklist == ('BTC', 'ETH', 'SOL')      # 去空白


def test_default_tier_policy_content():
    from gridtrade.config import DEFAULT_TIER_POLICY
    assert 'FARTCOIN/USDT:USDT' in DEFAULT_TIER_POLICY.tier0     # 币安迁移 USDT 后缀
    assert 'NEIRO/USDT:USDT' in DEFAULT_TIER_POLICY.tier0        # KNEIRO→NEIRO(币安 TRADING)
    assert len(DEFAULT_TIER_POLICY.tier0) == 9
    assert DEFAULT_TIER_POLICY.tier1 == () and DEFAULT_TIER_POLICY.tier2_cap == 2  # 同币开仓上限(2026-07-12 用户定)


def test_tier0_binance_usdt_symbols():
    from gridtrade.config import DEFAULT_TIER_POLICY
    t0 = DEFAULT_TIER_POLICY.tier0
    assert 'BTC/USDT:USDT' in t0 and 'NEIRO/USDT:USDT' in t0
    assert all(s.endswith('/USDT:USDT') for s in t0)     # 无 USDC 残留
    assert len(t0) == 9


def test_whitelist_parsing():
    assert load_deploy_config(env={}).whitelist == ()
    cfg = load_deploy_config(env={'UNIVERSE_WHITELIST': 'BTC/USDT:USDT, ETH/USDT:USDT'})
    assert cfg.whitelist == ('BTC/USDT:USDT', 'ETH/USDT:USDT')


def test_min_quote_volume_24h_default_and_parse():
    assert load_deploy_config(env={}).min_quote_volume_24h == 0.0
    assert load_deploy_config(env={'MIN_QUOTE_VOLUME_24H': '1000000'}).min_quote_volume_24h == 1_000_000.0


def test_display_tz_defaults_and_parsing():
    assert load_deploy_config(env={}).display_tz == 'UTC'
    assert load_deploy_config(env={'DISPLAY_TZ': 'Asia/Shanghai'}).display_tz == 'Asia/Shanghai'


def test_quote_currency_optional_defaults_empty():
    # 未设 -> 空串（用适配器类默认 HL=USDC / OKX=USDT）
    assert load_deploy_config(env={}).quote_currency == ''
    assert load_deploy_config(env={'QUOTE_CURRENCY': 'USDC'}).quote_currency == 'USDC'


def test_scheduler_run_on_start_flag():
    assert load_deploy_config(env={}).scheduler_run_on_start is False
    assert load_deploy_config(
        env={'SCHEDULER_RUN_ON_START': 'true'}).scheduler_run_on_start is True


def test_strategy_defaults_mirror_legacy():
    assert DEFAULT_STRATEGY_CONFIG['period'] == '12H'
    assert DEFAULT_STRATEGY_CONFIG['grid_version'] == 2
    assert DEFAULT_STRATEGY_CONFIG['price_limit'] == [0.25, 0.25]
    assert DEFAULT_STRATEGY_CONFIG['stop_limit'] == 0.01
    assert DEFAULT_STRATEGY_CONFIG['grid_v2_config']['grid_count_max'] == 149
    # 2026-07-19 重扫v2 回滚候选A 三值(诚实引擎终审:旧现值唯一 4/6 窗正+零破网;候选A 系
    # 引擎保真度 bug——首触丢弃/pv前视等——的幻觉产物;详 config 注释与 memory binance-param-resweep)
    assert DEFAULT_STRATEGY_CONFIG['grid_v2_config']['grid_count_min'] == 10
    assert DEFAULT_STOP_CFG['stop_loss'] == 0.045    # HL 时代原值;0.035 留出窗现形(HOLD-A 垫底)
    assert DEFAULT_STOP_CFG['trailing_k'] == 0.3     # 连续回撤止盈恢复——诚实引擎下是真保护(关掉 OOS 多亏 3.5pp)
    assert DEFAULT_STOP_CFG['trailing_floor'] == 0.00618
    assert DEFAULT_STOP_CFG['fundingRate_stop_loss'] == 0.0015
    assert DEFAULT_STOP_CFG['pv_pnl_thr'] == 0.005    # 尖峰时浮盈<+0.5%即撤(2026-07-07 PV研究)
    assert DEFAULT_STOP_CFG['pv_mult'] == 3
    assert DEFAULT_STOP_CFG['pv_n'] == 100            # 量能基线 25h 真滚动窗(n 扫描甜点档)
    assert DEFAULT_STOP_CFG['pv_period'] == '15min'   # 非 '15m'（pandas 会当成月）


def test_scheduler_fetch_pace_ms_default_and_override():
    from gridtrade.config import load_deploy_config
    assert load_deploy_config(env={'EXCHANGE': 'fake'}).scheduler_fetch_pace_ms == 250.0
    assert load_deploy_config(env={'EXCHANGE': 'fake',
                                   'SCHEDULER_FETCH_PACE_MS': '0'}).scheduler_fetch_pace_ms == 0.0


def test_monitor_parallel_config():
    cfg = load_deploy_config(env={})
    assert cfg.monitor_parallel == 4 and cfg.monitor_unit_warn_sec == 30.0
    cfg = load_deploy_config(env={'MONITOR_PARALLEL': '1',
                                  'MONITOR_UNIT_WARN_SEC': '10'})
    assert cfg.monitor_parallel == 1 and cfg.monitor_unit_warn_sec == 10.0


def test_derive_frac_and_new_keys():
    from gridtrade.config import derive_frac, load_deploy_config
    assert abs(derive_frac(3.5, 12, 3.4) - 0.17157) < 1e-4     # 部署值(spec 2026-07-07)
    assert abs(derive_frac(2.0, 12, 3.4) - 0.09804) < 1e-4     # 纯函数参考点(≈旧0.10行为)
    env = {'ACCOUNT_LEVERAGE': '3.5', 'MAX_CONCURRENT': '12', 'GRID_GEARING': '3.4'}
    cfg = load_deploy_config(env)
    assert abs(cfg.cap_equity_frac - 0.17157) < 1e-4            # frac 是推导值,不再来自 env
    assert cfg.grid_gearing == 3.4 and cfg.account_leverage == 3.5


def test_legacy_keys_raise_loudly():
    import pytest
    from gridtrade.config import load_deploy_config
    with pytest.raises(RuntimeError, match='GRID_GEARING'):
        load_deploy_config({'LEVERAGE': '5'})
    with pytest.raises(RuntimeError, match='ACCOUNT_LEVERAGE'):
        load_deploy_config({'CAP_EQUITY_FRAC': '0.10'})


def test_shock_brake_config():
    cfg = load_deploy_config(env={})
    assert cfg.shock_thr == 0.025 and cfg.shock_k_hours == 4 and cfg.shock_pause_hours == 2
    cfg2 = load_deploy_config(env={'SHOCK_THR': '0', 'SHOCK_K_HOURS': '2', 'SHOCK_PAUSE_HOURS': '6'})
    assert cfg2.shock_thr == 0.0 and cfg2.shock_k_hours == 2 and cfg2.shock_pause_hours == 6


def test_binance_credentials_and_defaults():
    from gridtrade.config import load_deploy_config
    cfg = load_deploy_config({'BINANCE_API_KEY': 'k', 'BINANCE_API_SECRET': 's',
                              'BINANCE_TESTNET': 'true'})
    assert cfg.exchange == 'binance'          # 默认交易所=binance
    assert cfg.api_key == 'k' and cfg.api_secret == 's'
    assert cfg.testnet is True


def test_hl_legacy_keys_rejected():
    import pytest
    from gridtrade.config import load_deploy_config
    for key in ('HL_WALLET_ADDRESS', 'HL_PRIVATE_KEY', 'HL_TESTNET'):
        with pytest.raises(RuntimeError):
            load_deploy_config({key: 'x'})


def test_universe_top_volume_pct_parsed():
    # 票池相对口径 env（spec 2026-07-14-universe-top-volume-pct）；默认 0=停用
    from gridtrade.config import load_deploy_config
    assert load_deploy_config({'UNIVERSE_TOP_VOLUME_PCT': '0.55'}).universe_top_volume_pct == 0.55
    assert load_deploy_config({}).universe_top_volume_pct == 0.0


def test_fuse_min_coverage_parsed():
    # 保险丝覆盖率门槛（spec 2026-07-15）；默认 1.0=必须足额，0=仅审计不干预
    from gridtrade.config import load_deploy_config
    assert load_deploy_config({}).fuse_min_coverage == 1.0
    assert load_deploy_config({'FUSE_MIN_COVERAGE': '0'}).fuse_min_coverage == 0.0


def test_fuse_min_coverage_above_one_rejected():
    # >1 无意义且是语义陷阱（"留 20% 余量"的自然误读会把已足额币白缩仓）→ boot 直接报错
    import pytest
    from gridtrade.config import load_deploy_config
    with pytest.raises(RuntimeError):
        load_deploy_config({'FUSE_MIN_COVERAGE': '1.2'})


def test_live_open_offsets_parsing():
    # 实盘 offset 启用数组（int CSV）：空=停用；去空白+去重+排序（spec 2026-07-17）
    from gridtrade.config import load_deploy_config
    assert load_deploy_config(env={}).live_open_offsets == ()
    assert load_deploy_config(env={'LIVE_OPEN_OFFSETS': '0,6'}).live_open_offsets == (0, 6)
    assert load_deploy_config(env={'LIVE_OPEN_OFFSETS': ' 6 , 0 , 6 '}).live_open_offsets == (0, 6)


def test_live_open_offsets_out_of_range_or_nonint_rejected():
    # 越界/非 int → 响亮报错（禁静默丢弃，沿退役键惯例）。period=12H → 合法 0..11
    import pytest
    from gridtrade.config import load_deploy_config
    for bad in ('12', '15', '-1', 'a'):
        with pytest.raises(RuntimeError):
            load_deploy_config(env={'LIVE_OPEN_OFFSETS': bad})


def test_live_open_offsets_validated_against_scheduler_period():
    # 校验区间由 SCHEDULER_PERIOD 定：24H → 合法 0..23，15 放行
    from gridtrade.config import load_deploy_config
    cfg = load_deploy_config(env={'SCHEDULER_PERIOD': '24H', 'LIVE_OPEN_OFFSETS': '15'})
    assert cfg.live_open_offsets == (15,)


def test_live_open_offsets_rescales_cap_frac_by_enabled_count():
    # 方案 B：frac 分母 = 启用 offset 数 N（去重后），空集/全开都回落到 max_concurrent=12
    from gridtrade.config import load_deploy_config, derive_frac
    assert abs(load_deploy_config(env={}).cap_equity_frac - 0.24510) < 1e-4      # 空=停用 → N=12
    cfg2 = load_deploy_config(env={'LIVE_OPEN_OFFSETS': '0,6'})                   # N=2
    assert abs(cfg2.cap_equity_frac - derive_frac(5.0, 2, 3.4)) < 1e-4
    assert abs(cfg2.cap_equity_frac - 1.47059) < 1e-4
    alloff = ','.join(str(i) for i in range(12))                                 # 显式全开 → N=12
    assert abs(load_deploy_config(env={'LIVE_OPEN_OFFSETS': alloff}).cap_equity_frac - 0.24510) < 1e-4
    dup = load_deploy_config(env={'LIVE_OPEN_OFFSETS': '0,0,6'})                  # 去重后 N=2（非 3）
    assert abs(dup.cap_equity_frac - derive_frac(5.0, 2, 3.4)) < 1e-4


def test_eff_concurrency_field_follows_enabled_offsets():
    # spec 2026-07-18-margin-gate-exchange-im：eff_concurrency 持久化到 config，
    # 供 MaxConcurrentGate 用（frac 按 N 放大 cap 后，并发上限必须同步收紧到 N）
    from gridtrade.config import load_deploy_config
    assert load_deploy_config(env={}).eff_concurrency == 12                       # 空=全开 → max_concurrent
    assert load_deploy_config(env={'LIVE_OPEN_OFFSETS': '2,4'}).eff_concurrency == 2
    alloff = ','.join(str(i) for i in range(12))
    assert load_deploy_config(env={'LIVE_OPEN_OFFSETS': alloff}).eff_concurrency == 12


def test_universe_min_leverage_default_off_env_on():
    # 票池杠杆预过滤阈值(2026-07-18):默认 0=停用(零行为变更);prod toml 设 10
    from gridtrade.config import load_deploy_config
    assert load_deploy_config(env={}).universe_min_leverage == 0.0
    assert load_deploy_config(env={'UNIVERSE_MIN_LEVERAGE': '10'}).universe_min_leverage == 10.0


def test_margin_gate_k_default_env_and_fail_fast():
    import pytest
    from gridtrade.config import load_deploy_config
    assert load_deploy_config(env={}).margin_gate_k == 1.25
    assert load_deploy_config(env={'MARGIN_GATE_K': '2'}).margin_gate_k == 2.0
    with pytest.raises(RuntimeError):        # k<1 = 余量为负,配置错了要响亮
        load_deploy_config(env={'MARGIN_GATE_K': '0.8'})


def test_wechat_config_defaults_and_env_override():
    cfg = load_deploy_config(env={})
    assert cfg.wechat_webhook_url == ''
    assert cfg.wechat_timezone == 'Asia/Shanghai'
    assert cfg.strategy_name == 'gridtrade'
    cfg = load_deploy_config(env={
        'WECHAT_WEBHOOK_URL': 'https://example.invalid/hook',
        'WECHAT_TIMEZONE': 'UTC', 'STRATEGY_NAME': 'prod'})
    assert cfg.wechat_webhook_url.endswith('/hook')
    assert cfg.wechat_timezone == 'UTC' and cfg.strategy_name == 'prod'
