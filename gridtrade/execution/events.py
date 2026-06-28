"""领域事件 + 事件总线（Observer）—— 把 GridOpened/GridClosed 与通知/指标解耦。

handler 是 callable(event)；handler 自行按事件类型（isinstance）过滤关心的事件。
publish 接受任意事件 dataclass，便于未来扩展（如 OrderFilled）。
"""
from dataclasses import dataclass
from typing import Callable, List


@dataclass
class GridOpened:
    grid_id: str
    exchange: str
    symbol: str
    tag: str


@dataclass
class GridClosed:
    grid_id: str
    exchange: str
    symbol: str
    reason: str
    pnl_ratio: float


class EventBus:
    def __init__(self):
        self._handlers: List[Callable] = []

    def subscribe(self, handler: Callable) -> None:
        self._handlers.append(handler)

    def publish(self, event) -> None:
        for handler in self._handlers:
            handler(event)
