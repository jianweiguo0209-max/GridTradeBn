from gridtrade.config import (load_deploy_config, DeployConfig,
                              DEFAULT_STRATEGY_CONFIG, DEFAULT_STOP_CFG)


def test_defaults_when_env_empty():
    cfg = load_deploy_config(env={})
    assert isinstance(cfg, DeployConfig)
    assert cfg.exchange == 'hyperliquid'
    assert cfg.testnet is False
    assert cfg.cap == 100.0
    assert cfg.leverage == 5.0
    assert cfg.monitor_interval_sec == 5.0
    assert cfg.scheduler_period == '12H'
    assert cfg.max_concurrent == 20
    assert cfg.utc_offset == 8
    assert cfg.wallet_address == '' and cfg.private_key == ''


def test_parses_env_with_type_coercion():
    env = {
        'EXCHANGE': 'okx',
        'HL_WALLET_ADDRESS': '0xabc',
        'HL_PRIVATE_KEY': 'deadbeef',
        'HL_TESTNET': 'true',
        'DATABASE_URL': 'postgresql+psycopg2://u:p@h/db',
        'CAP': '250.5',
        'LEVERAGE': '3',
        'MONITOR_INTERVAL_SEC': '3.5',
        'SCHEDULER_PERIOD': '6H',
        'MAX_CONCURRENT': '10',
        'TOTAL_BUDGET': '5000',
        'DEFAULT_CAP': '200',
        'UTC_OFFSET': '0',
    }
    cfg = load_deploy_config(env=env)
    assert cfg.exchange == 'okx'
    assert cfg.wallet_address == '0xabc' and cfg.private_key == 'deadbeef'
    assert cfg.testnet is True
    assert cfg.database_url == 'postgresql+psycopg2://u:p@h/db'
    assert cfg.cap == 250.5 and cfg.leverage == 3.0
    assert cfg.monitor_interval_sec == 3.5 and cfg.scheduler_period == '6H'
    assert cfg.max_concurrent == 10 and cfg.total_budget == 5000.0
    assert cfg.default_cap == 200.0 and cfg.utc_offset == 0


def test_bool_parsing_variants():
    assert load_deploy_config(env={'HL_TESTNET': 'YES'}).testnet is True
    assert load_deploy_config(env={'HL_TESTNET': '1'}).testnet is True
    assert load_deploy_config(env={'HL_TESTNET': 'off'}).testnet is False
    assert load_deploy_config(env={'HL_TESTNET': 'false'}).testnet is False


def test_default_cap_falls_back_to_cap_when_unset():
    cfg = load_deploy_config(env={'CAP': '300'})
    assert cfg.cap == 300.0 and cfg.default_cap == 300.0   # default_cap 未设 -> 用 cap


def test_blacklist_parsing():
    assert load_deploy_config(env={}).blacklist == ()
    cfg = load_deploy_config(env={'BLACKLIST_SYMBOLS': 'BTC, ETH ,SOL'})
    assert cfg.blacklist == ('BTC', 'ETH', 'SOL')      # 去空白
    assert load_deploy_config(env={'BLACKLIST_SYMBOLS': ''}).blacklist == ()


def test_whitelist_parsing():
    assert load_deploy_config(env={}).whitelist == ()
    cfg = load_deploy_config(env={'UNIVERSE_WHITELIST': 'BTC/USDT:USDT, ETH/USDT:USDT'})
    assert cfg.whitelist == ('BTC/USDT:USDT', 'ETH/USDT:USDT')


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
    assert DEFAULT_STOP_CFG['stop_loss'] == 0.034
    assert DEFAULT_STOP_CFG['trailing_k'] == 0.3
    assert DEFAULT_STOP_CFG['trailing_floor'] == 0.00618
    assert DEFAULT_STOP_CFG['fundingRate_stop_loss'] == 0.0015
    assert DEFAULT_STOP_CFG['pv_pnl_thr'] == -0.02
    assert DEFAULT_STOP_CFG['pv_period'] == '15min'   # 非 '15m'（pandas 会当成月）
