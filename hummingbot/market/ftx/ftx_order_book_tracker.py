#!/usr/bin/env python
import asyncio
import bisect
import logging
import time
from collections import (
    defaultdict,
    deque,
)
from typing import (
    Optional,
    Dict,
    List,
    Set,
    Deque,
)

from hummingbot.core.data_type.order_book_message import (
    OrderBookMessageType,
    OrderBookMessage,
)
from hummingbot.core.event.events import TradeType
from hummingbot.logger import HummingbotLogger
from hummingbot.core.data_type.order_book_tracker import OrderBookTracker, OrderBookTrackerDataSourceType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.market.ftx.ftx_active_order_tracker import FtxActiveOrderTracker
from hummingbot.market.ftx.ftx_api_order_book_data_source import FtxAPIOrderBookDataSource
from hummingbot.market.ftx.ftx_order_book import FtxOrderBook
from hummingbot.market.ftx.ftx_order_book_message import FtxOrderBookMessage
from hummingbot.market.ftx.ftx_order_book_tracker_entry import FtxOrderBookTrackerEntry
from hummingbot.core.utils.async_utils import safe_ensure_future


class FtxOrderBookTracker(OrderBookTracker):
    _btobt_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._btobt_logger is None:
            cls._btobt_logger = logging.getLogger(__name__)
        return cls._btobt_logger

    def __init__(
        self,
        data_source_type: OrderBookTrackerDataSourceType = OrderBookTrackerDataSourceType.EXCHANGE_API,
        trading_pairs: Optional[List[str]] = None,
    ):
        super().__init__(data_source_type=data_source_type)

        self._ev_loop: asyncio.BaseEventLoop = asyncio.get_event_loop()
        self._data_source: Optional[OrderBookTrackerDataSource] = None
        self._order_book_snapshot_stream: asyncio.Queue = asyncio.Queue()
        self._order_book_diff_stream: asyncio.Queue = asyncio.Queue()
        self._process_msg_deque_task: Optional[asyncio.Task] = None
        self._past_diffs_windows: Dict[str, Deque] = {}
        self._order_books: Dict[str, FtxOrderBook] = {}
        self._saved_message_queues: Dict[str, Deque[FtxOrderBookMessage]] = defaultdict(lambda: deque(maxlen=1000))
        self._active_order_trackers: Dict[str, FtxActiveOrderTracker] = defaultdict(FtxActiveOrderTracker)
        self._trading_pairs: Optional[List[str]] = trading_pairs
        self._order_book_stream_listener_task: Optional[asyncio.Task] = None

    @property
    def data_source(self) -> OrderBookTrackerDataSource:
        if not self._data_source:
            if self._data_source_type is OrderBookTrackerDataSourceType.EXCHANGE_API:
                self._data_source = FtxAPIOrderBookDataSource(trading_pairs=self._trading_pairs)
            else:
                raise ValueError(f"data_source_type {self._data_source_type} is not supported.")
        return self._data_source

    @property
    def exchange_name(self) -> str:
        return "ftx"

    async def _refresh_tracking_tasks(self):
        """
        Starts tracking for any new trading pairs, and stop tracking for any inactive trading pairs.
        """
        tracking_trading_pair: Set[str] = set(
            [key for key in self._tracking_tasks.keys() if not self._tracking_tasks[key].done()]
        )
        available_pairs: Dict[str, FtxOrderBookTrackerEntry] = await self.data_source.get_tracking_pairs()
        available_trading_pair: Set[str] = set(available_pairs.keys())
        new_trading_pair: Set[str] = available_trading_pair - tracking_trading_pair
        deleted_trading_pair: Set[str] = tracking_trading_pair - available_trading_pair

        for trading_pair in new_trading_pair:
            order_book_tracker_entry: FtxOrderBookTrackerEntry = available_pairs[trading_pair]
            self._active_order_trackers[trading_pair] = order_book_tracker_entry.active_order_tracker
            self._order_books[trading_pair] = order_book_tracker_entry.order_book
            self._tracking_message_queues[trading_pair] = asyncio.Queue()
            self._tracking_tasks[trading_pair] = asyncio.ensure_future(self._track_single_book(trading_pair))
            self.logger().info(f"Started order book tracking for {trading_pair}.")

        for trading_pair in deleted_trading_pair:
            self._tracking_tasks[trading_pair].cancel()
            del self._tracking_tasks[trading_pair]
            del self._order_books[trading_pair]
            del self._active_order_trackers[trading_pair]
            del self._tracking_message_queues[trading_pair]
            self.logger().info(f"Stopped order book tracking for {trading_pair}.")

    async def _track_single_book(self, trading_pair: str):
        past_diffs_window: Deque[OrderBookMessage] = deque()
        self._past_diffs_windows[trading_pair] = past_diffs_window

        message_queue: asyncio.Queue = self._tracking_message_queues[trading_pair]
        order_book: OrderBook = self._order_books[trading_pair]
        last_message_timestamp: float = time.time()
        diff_messages_accepted: int = 0

        while True:
            try:
                message: OrderBookMessage = None
                saved_messages: Deque[OrderBookMessage] = self._saved_message_queues[trading_pair]
                active_order_tracker = self._active_order_trackers[trading_pair]
                # Process saved messages first if there are any
                if len(saved_messages) > 0:
                    message = saved_messages.popleft()
                else:
                    message = await message_queue.get()
                if message.type is OrderBookMessageType.DIFF:
                    bids, asks = active_order_tracker.convert_diff_message_to_order_book_row(message)
                    order_book.apply_diffs(bids, asks, message.timestamp)
                    past_diffs_window.append(message)
                    while len(past_diffs_window) > self.PAST_DIFF_WINDOW_SIZE:
                        past_diffs_window.popleft()
                    diff_messages_accepted += 1

                    # Output some statistics periodically.
                    now: float = time.time()
                    if int(now / 60.0) > int(last_message_timestamp / 60.0):
                        self.logger().debug("Processed %d order book diffs for %s.",
                                            diff_messages_accepted, trading_pair)
                        diff_messages_accepted = 0
                    last_message_timestamp = now
                elif message.type is OrderBookMessageType.SNAPSHOT:
                    bids, asks = active_order_tracker.convert_snapshot_message_to_order_book_row(message)
                    order_book.apply_snapshot(bids, asks, message.timestamp)
                    self.logger().debug("Processed order book snapshot for %s.", trading_pair)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    f"Unexpected error tracking order book for {trading_pair}.",
                    exc_info=True,
                    app_warning_msg=f"Unexpected error tracking order book. Retrying after 5 seconds."
                )
                await asyncio.sleep(5.0)