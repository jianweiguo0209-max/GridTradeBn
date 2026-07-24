"""容器内 dump selection_snapshots → JSON(供 scripts/cf_patrol.py 本地反事实巡检)。
用法: flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_selection_snapshots.py > snaps.json
env SNAP_DAYS 回看天数(默认 2)。⚠ranked 实为选中币(triggers.py 写 select_grid_coin
输出,非全池)——票池由 cf_patrol 按选币同规则 PIT 重建(spec §4)。
"""
import json
import os
import time

from sqlalchemy import create_engine, text

url = os.environ['DATABASE_URL']
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql://', 1)
days = float(os.environ.get('SNAP_DAYS', '2'))
lo = int((time.time() - days * 86400) * 1000)
out = []
with create_engine(url).connect() as c:
    q = text('SELECT exchange, run_time, "offset", ranked, picks '
             'FROM selection_snapshots WHERE run_time >= :lo ORDER BY run_time')
    for r in c.execute(q, {'lo': lo}).mappings():
        out.append({'exchange': r['exchange'], 'run_time': int(r['run_time']),
                    'offset': int(r['offset']), 'ranked': json.loads(r['ranked']),
                    'picks': json.loads(r['picks'])})
print(json.dumps(out))
