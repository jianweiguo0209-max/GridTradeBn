# gridtrade/runtime/web.py
"""web 机入口（fly 第三进程）：组装只读 dashboard，起 uvicorn。绝不写库/写交易所。"""
import secrets

from gridtrade.config import load_deploy_config
from gridtrade.dashboard.app import create_app
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.introspect import adapter_endpoint


def build_web_app(config=None):
    config = config or load_deploy_config()
    rt = build_runtime(config)
    secret = config.dashboard_session_secret or secrets.token_hex(32)
    return create_app(rt.store, rt.adapter,
                      username=config.dashboard_user,
                      password_hash=config.dashboard_password_hash,
                      session_secret=secret)


def main() -> None:   # composition root（不单测）
    import uvicorn
    config = load_deploy_config()
    app = build_web_app(config)
    print('[web] exchange=%s testnet=%s endpoint=%s port=%s'
          % (config.exchange, config.testnet,
             adapter_endpoint(build_runtime(config).adapter),
             config.dashboard_port), flush=True)
    uvicorn.run(app, host='0.0.0.0', port=config.dashboard_port)


if __name__ == '__main__':
    main()
