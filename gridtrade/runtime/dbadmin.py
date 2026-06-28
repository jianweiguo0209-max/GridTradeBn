"""DB 管理一次性入口：create / reset。在 fly 上用 `fly machine run <image> \
python -m gridtrade.runtime.dbadmin reset` 跑一次。

- create：仅 create_all（幂等，安全）。
- reset：drop_all + create_all（**销毁所有表数据**，仅 testnet/无价值数据时用）。
"""
import sys

from gridtrade.config import load_deploy_config
from gridtrade.state.store import StateStore


def _store():
    cfg = load_deploy_config()
    return (StateStore.from_url(cfg.database_url) if cfg.database_url
            else StateStore.in_memory())


def run(action, *, store_factory=None):
    store = store_factory() if store_factory else _store()
    if action == 'reset':
        store.drop_all()
        store.create_all()
        return 'reset'
    if action == 'create':
        store.create_all()
        return 'create'
    raise SystemExit('usage: python -m gridtrade.runtime.dbadmin [create|reset]')


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'create'
    result = run(action)
    print('[dbadmin] %s done' % result, flush=True)


if __name__ == '__main__':
    main()
