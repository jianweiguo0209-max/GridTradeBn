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
    # legacy 止盈止损 + 已接线的资金费/pv 主动止损参数（pv 由 2026-03~06 回测扫描调优）
    assert DEFAULT_STOP_CFG['stop_loss'] == 0.045   # 固定止损放宽(2026-07-10 stop 扫描保守候选,详 config 注释)
    assert DEFAULT_STOP_CFG['trailing_k'] == 0.15    # 回撤止盈换挡(2026-07-10 trail 扫描,k 不敏感取最优)
    assert DEFAULT_STOP_CFG['trailing_floor'] == 0.015  # floor 单调敏感,0.015 饱和点;触发占比 6-12%→~1%
    assert DEFAULT_STOP_CFG['fundingRate_stop_loss'] == 0.0015
    assert DEFAULT_STOP_CFG['pv_pnl_thr'] == 0.005    # 尖峰时浮盈<+0.5%即撤(2026-07-07 PV研究)
    assert DEFAULT_STOP_CFG['pv_mult'] == 3
    assert DEFAULT_STOP_CFG['pv_n'] == 100            # 量能基线 25h 真滚动窗(n 扫描甜点档)
    assert DEFAULT_STOP_CFG['pv_period'] == '15min'   # 非 '15m'（pandas 会当成月）


def test_scheduler_fetch_pace_ms_default_and_override():
    from gridtrade.config import load_deploy_config
    assert load_deploy_config(env={'EXCHANGE': 'fake'}).scheduler_fetch_pace_ms == 2000.0
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
