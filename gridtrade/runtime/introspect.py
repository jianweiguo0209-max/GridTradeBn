"""运行时自省：从 adapter（可能被 ResilientAdapter 包裹）取实际交易所 API endpoint。

守护进程启动时打印它，使「连的是 testnet 还是 mainnet」在 fly logs 里铁证可见。
"""


def adapter_endpoint(adapter) -> str:
    inner = getattr(adapter, '_inner', adapter)   # 穿透 ResilientAdapter
    client = getattr(inner, 'client', None)        # ccxt 客户端
    if client is None:
        return 'n/a'
    try:
        api = client.urls.get('api')
    except Exception:
        return 'n/a'
    if isinstance(api, dict):
        return api.get('public') or api.get('private') or str(api)
    return str(api) if api else 'n/a'
