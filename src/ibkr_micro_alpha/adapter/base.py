from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from ..types import BrokerSnapshot, EngineEvent, TradeSide


EventPublisher = Callable[[EngineEvent], Awaitable[None]]


class AbstractBrokerAdapter(ABC):
    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def subscribe_market_data(self, symbols: list[str], depth_levels: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def place_limit_order(
        self,
        local_order_id: str,
        symbol: str,
        side: TradeSide,
        quantity: int,
        limit_price: float | None,
        purpose: str,
        parent_local_order_id: str | None = None,
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, broker_order_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def request_reconcile(self) -> BrokerSnapshot:
        raise NotImplementedError

    @abstractmethod
    def set_publisher(self, publisher: EventPublisher) -> None:
        raise NotImplementedError
