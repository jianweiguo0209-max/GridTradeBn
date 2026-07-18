"""门拒绝审计仓储(2026-07-18,spec margin-gate-exchange-im 追加)。

「该开未开必须留痕」升级为持久化可查:stdout 拒因随 fly logs 分钟级滚掉(mainnet 02:00
MET 被 MarginGate 拒,排查靠容器内重演)。GateChain.on_reject → add() 逐条 append;
审计写入失败由链侧兜住(fail-soft),绝不阻断开仓。查询:
  psql> SELECT * FROM gate_rejections ORDER BY id DESC LIMIT 20;
"""
from sqlalchemy import insert, select

from gridtrade.state.models import gate_rejections, now_ms


class GateRejectionRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add(self, *, exchange: str, symbol: str, tag: str, gate: str,
            reason: str) -> None:
        ts = now_ms()
        with self.engine.begin() as c:
            c.execute(insert(gate_rejections).values(
                ts=ts, exchange=exchange, symbol=symbol, tag=tag or '',
                gate=gate, reason=reason, created_at=ts))

    def list_recent(self, limit: int = 50):
        """最新在前:[{'ts','exchange','symbol','tag','gate','reason','created_at'}]。"""
        with self.engine.connect() as c:
            rows = c.execute(
                select(gate_rejections)
                .order_by(gate_rejections.c.id.desc())
                .limit(int(limit))).fetchall()
        return [{k: r._mapping[k] for k in
                 ('ts', 'exchange', 'symbol', 'tag', 'gate', 'reason', 'created_at')}
                for r in rows]
