"""选币结果磁盘缓存：key = 选币参数 + 每币缓存天范围数据指纹；命中即秒回，跳过整段选币计算。
仅回测离线工具用。version+params 双校验防 sha256 碰撞；BT_SELECT_CACHE=off 旁路。
数据指纹用 ParquetCache.list_days（廉价 listdir），故重新预热改变缓存天时会自动换 key、不返回过期结果；
「就地改写某天旧文件内容」这类指纹盖不住的极少数情况靠 CACHE_VERSION bump / BT_SELECT_CACHE=off 兜底。"""
import hashlib
import json
import os
import pickle
import tempfile

CACHE_VERSION = 1
_NAMESPACE = '_select_cache'


def enabled():
    return os.environ.get('BT_SELECT_CACHE', 'on').lower() != 'off'


def _fingerprint(cache, universe, timeframe):
    """每个 symbol 的缓存天范围 [最早日, 最晚日, 天数]；无缓存→None。"""
    fp = {}
    for s in sorted(universe):
        days = cache.list_days(timeframe, s)
        fp[s] = [days[0], days[-1], len(days)] if days else None
    return fp


def compute_key(cache, universe, window_start, window_end, timeframe,
                min_quote_volume, blacklist, strategy_config, factors):
    """返回 (key_hex16, params_dict)。params 含数据指纹，重新预热改变缓存天时自动换 key。"""
    params = {
        'version': CACHE_VERSION,
        'window_start': str(window_start),
        'window_end': str(window_end),
        'timeframe': timeframe,
        'universe': sorted(universe),
        'blacklist': sorted(blacklist),
        'min_quote_volume': float(min_quote_volume),
        'period': strategy_config['period'],
        'weight_list': list(strategy_config['weight_list']),
        'choose_symbols': strategy_config['choose_symbols'],
        'max_candle_num': strategy_config['max_candle_num'],
        'factors': {k: bool(v) for k, v in factors.items()},
        'fingerprint': _fingerprint(cache, universe, timeframe),
    }
    blob = json.dumps(params, sort_keys=True, default=str)
    key = hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]
    return key, params


def _dir(cache):
    return os.path.join(cache.root, _NAMESPACE)


def _path(cache, key):
    return os.path.join(_dir(cache), '%s.pkl' % key)


def load(cache, key, params):
    """命中且 version+params 完全一致 → 返回 grids；否则 None。"""
    p = _path(cache, key)
    if not (os.path.exists(p) and os.path.getsize(p) > 0):
        return None
    try:
        with open(p, 'rb') as f:
            obj = pickle.load(f)
    except BaseException:
        return None
    if obj.get('version') != CACHE_VERSION or obj.get('params') != params:
        return None                       # 防 sha256 碰撞 / 版本漂移
    return obj.get('grids')


def save(cache, key, params, grids):
    """原子写 pkl（临时文件 + os.replace）。"""
    d = _dir(cache)
    os.makedirs(d, exist_ok=True)
    p = _path(cache, key)
    fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            pickle.dump({'version': CACHE_VERSION, 'params': params, 'grids': grids}, f)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
