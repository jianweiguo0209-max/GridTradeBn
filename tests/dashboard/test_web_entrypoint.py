from gridtrade.config import load_deploy_config
from gridtrade.runtime.web import build_web_app
from gridtrade.dashboard.auth import hash_password


def test_build_web_app_offline():
    env = {
        'EXCHANGE': 'fake', 'DATABASE_URL': '',
        'DASHBOARD_USER': 'admin',
        'DASHBOARD_PASSWORD_HASH': hash_password('pw', iterations=1000),
        'DASHBOARD_SESSION_SECRET': 'sekret',
    }
    cfg = load_deploy_config(env)
    app = build_web_app(cfg)
    # FastAPI 应用，挂了我们的路由
    paths = {r.path for r in app.routes}
    assert '/login' in paths and '/' in paths and '/history' in paths
