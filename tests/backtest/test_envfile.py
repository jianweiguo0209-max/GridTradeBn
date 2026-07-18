"""回测入口 .env 便利加载(2026-07-18)：BT 票池杠杆过滤需私有档位端点凭证,本地跑回测
不该要求手动 export。边界:仅回测 CLI 入口调用;override=False → 显式 shell env 恒优先;
runtime(fly)不受影响(走 secrets/[env],.dockerignore 已排除 .env)。"""
from gridtrade.backtest.envfile import load_env_file


def test_loads_keys_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv('GT_TEST_ENVFILE_KEY', raising=False)
    f = tmp_path / '.env'
    f.write_text('GT_TEST_ENVFILE_KEY=hello\n# 注释行\n\n')
    assert load_env_file(f) is True
    import os
    assert os.environ.get('GT_TEST_ENVFILE_KEY') == 'hello'
    monkeypatch.delenv('GT_TEST_ENVFILE_KEY', raising=False)


def test_never_overrides_exported_shell_env(tmp_path, monkeypatch):
    # 显式 export 的值恒优先——.env 只兜底,不覆盖(最小惊讶原则)
    monkeypatch.setenv('GT_TEST_ENVFILE_KEY', 'from-shell')
    f = tmp_path / '.env'
    f.write_text('GT_TEST_ENVFILE_KEY=from-file\n')
    load_env_file(f)
    import os
    assert os.environ['GT_TEST_ENVFILE_KEY'] == 'from-shell'


def test_missing_file_noop(tmp_path):
    assert load_env_file(tmp_path / 'no-such.env') is False
