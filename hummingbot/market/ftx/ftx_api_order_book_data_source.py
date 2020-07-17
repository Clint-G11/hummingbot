#!/usr/bin/env python
import asyncio
import logging
import time
from base64 import b64decode
from typing import Optional, List, Dict, AsyncIterable, Any
from zlib import decompress, MAX_WBITS

import aiohttp
import pandas as pd
import signalr_aio
import ujson
from signalr_aio import Connection
from signalr_aio.hubs import Hub
from async_timeout import timeout

from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.order_book_tracker_entry import OrderBookTrackerEntry
from hummingbot.core.utils import async_ttl_cache
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.logger import HummingbotLogger
from hummingbot.market.ftx.ftx_active_order_tracker import FtxActiveOrderTracker
from hummingbot.market.ftx.ftx_order_book import FtxOrderBook
from hummingbot.market.ftx.ftx_order_book_tracker_entry import FtxOrderBookTrackerEntry


EXCHANGE_NAME = "ftx"

FTX_REST_URL = "https://ftx.com/api"
FTX_EXCHANGE_INFO_PATH = "/markets"
FTX_WS_FEED = "wss://ftx.com/ws/"

MAX_RETRIES = 20
MESSAGE_TIMEOUT = 30.0
SNAPSHOT_TIMEOUT = 10.0
NaN = float("nan")


class FtxAPIOrderBookDataSource(OrderBookTrackerDataSource):
    PING_TIMEOUT = 10.0

    _ftxaobds_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._ftxaobds_logger is None:
            cls._ftxaobds_logger = logging.getLogger(__name__)
        return cls._ftxaobds_logger

    def __init__(self, trading_pairs: Optional[List[str]] = None):
        super().__init__()
        self._trading_pairs: Optional[List[str]] = trading_pairs
        self._websocket_connection: Optional[Connection] = None
        self._websocket_hub: Optional[Hub] = None
        self._snapshot_msg: Dict[str, any] = {}

    @classmethod
    @async_ttl_cache(ttl=60 * 30, maxsize=1)
    async def get_active_exchange_markets(cls) -> pd.DataFrame:
        """
        Returned data frame should have trading pair as index and include USDVolume, baseAsset and quoteAsset
        """
        market_path_url = f"{FTX_REST_URL}{FTX_EXCHANGE_INFO_PATH}"

        async with aiohttp.ClientSession() as client:

            market_response = await client.get(market_path_url)

            market_response: aiohttp.ClientResponse = market_response

            if market_response.status != 200:
                raise IOError(
                    f"Error fetching active ftx markets information. " f"HTTP status is {market_response.status}."
                )

            raw_market_data = market_response.json()

            market_data = [data for data in raw_market_data if raw_market_data["type"] == "spot"]

            all_markets: pd.DataFrame = pd.DataFrame.from_records(data=market_data, index="name")
            all_markets.rename(
                {"baseCurrency": "baseAsset", "quoteCurrency": "quoteAsset", "volumeUsd24h": "USDVolume"}, axis="columns", inplace=True
            )

            btc_usd_price: float = float(all_markets.loc["BTC-USD"].lastTradeRate)
            eth_usd_price: float = float(all_markets.loc["ETH-USD"].lastTradeRate)

            
            await client.close()
            return all_markets.sort_values("USDVolume", ascending=False)

    async def get_trading_pairs(self) -> List[str]:
        if not self._trading_pairs:
            try:
                active_markets: pd.DataFrame = await self.get_active_exchange_markets()
                self._trading_pairs = active_markets.index.tolist()
            except Exception:
                self._trading_pairs = []
                self.logger().network(
                    f"Error getting active exchange information.",
                    exc_info=True,
                    app_warning_msg=f"Error getting active exchange information. Check network connection.",
                )
        return self._trading_pairs

    async def websocket_connection(self) -> (signalr_aio.Connection, signalr_aio.hubs.Hub):
        if self._websocket_connection and self._websocket_hub:
            return self._websocket_connection, self._websocket_hub

        self._websocket_connection = signalr_aio.Connection(FTX_WS_FEED, session=None)

        trading_pairs = await self.get_trading_pairs()
        for trading_pair in trading_pairs:
            trading_pair = f"{trading_pair.split('/')[1]}-{trading_pair.split('/')[0]}"
            self.logger().info(f"Subscribed to {trading_pair} deltas")
            self._websocket_hub.server.invoke("SubscribeToExchangeDeltas", trading_pair)

            self.logger().info(f"Query {trading_pair} snapshot.")
            self._websocket_hub.server.invoke("queryExchangeState", trading_pair)

        self._websocket_connection.start()

        return self._websocket_connection, self._websocket_hub

    async def wait_for_snapshot(self, trading_pair: str, invoke_timestamp: int) -> Optional[OrderBookMessage]:
        try:
            async with timeout(SNAPSHOT_TIMEOUT):
                while True:
                    msg: Dict[str, any] = self._snapshot_msg.pop(trading_pair, None)
                    if msg and msg["timestamp"] >= invoke_timestamp:
                        return msg["content"]
                    await asyncio.sleep(1)
        except asyncio.TimeoutError:
            raise

    async def get_snapshot(client: aiohttp.ClientSession, trading_pair: str, limit: int = 1000) -> Dict[str, Any]:
        params: Dict = {"limit": str(limit), "symbol": trading_pair} if limit != 0 else {"symbol": trading_pair}
        async with client.get(SNAPSHOT_REST_URL, params=params) as response:
            response: aiohttp.ClientResponse = response
            if response.status != 200:
                raise IOError(f"Error fetching Binance market snapshot for {trading_pair}. "
                              f"HTTP status is {response.status}.")
            data: Dict[str, Any] = await response.json()

            # Need to add the symbol into the snapshot message for the Kafka message queue.
            # Because otherwise, there'd be no way for the receiver to know which market the
            # snapshot belongs to.

            return data

   async def get_tracking_pairs(self) -> Dict[str, OrderBookTrackerEntry]:
        # Get the currently active markets
        async with aiohttp.ClientSession() as client:
            trading_pairs: List[str] = await self.get_trading_pairs()
            retval: Dict[str, OrderBookTrackerEntry] = {}

            number_of_pairs: int = len(trading_pairs)
            for index, trading_pair in enumerate(trading_pairs):
                try:
                    snapshot: Dict[str, Any] = await self.get_snapshot(client, trading_pair, 1000)
                    snapshot_timestamp: float = time.time()
                    snapshot_msg: OrderBookMessage = BinanceOrderBook.snapshot_message_from_exchange(
                        snapshot,
                        snapshot_timestamp,
                        metadata={"trading_pair": trading_pair}
                    )
                    order_book: OrderBook = self.order_book_create_function()
                    order_book.apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
                    retval[trading_pair] = OrderBookTrackerEntry(trading_pair, snapshot_timestamp, order_book)
                    self.logger().info(f"Initialized order book for {trading_pair}. "
                                       f"{index+1}/{number_of_pairs} completed.")
                    # Each 1000 limit snapshot costs 10 requests and Binance rate limit is 20 requests per second.
                    await asyncio.sleep(1.0)
                except Exception:
                    self.logger().error(f"Error getting snapshot for {trading_pair}. ", exc_info=True)
                    await asyncio.sleep(5)
            return retval

    async def listen_for_trades(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        # Trade messages are received as Orderbook Deltas and handled by listen_for_order_book_stream()
        pass

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        # Orderbooks Deltas and Snapshots are handled by listen_for_order_book_stream()
        pass

    async def _socket_stream(self) -> AsyncIterable[str]:
        try:
            while True:
                async with timeout(MESSAGE_TIMEOUT):  # Timeouts if not receiving any messages for 10 seconds(ping)
                    conn: signalr_aio.Connection = (await self.websocket_connection())[0]
                    yield await conn.msg_queue.get()
        except asyncio.TimeoutError:
            self.logger().warning("Message recv() timed out. Going to reconnect...")
            return

    def _transform_raw_message(self, msg) -> Dict[str, Any]:
        def _decode_message(raw_message: bytes) -> Dict[str, Any]:
            try:
                decoded_msg: bytes = decompress(b64decode(raw_message, validate=True), -MAX_WBITS)
            except SyntaxError:
                decoded_msg: bytes = decompress(b64decode(raw_message, validate=True))
            except Exception:
                return {}

            return ujson.loads(decoded_msg.decode())

        def _is_snapshot(msg) -> bool:
            return type(msg.get("R", False)) is not bool

        def _is_market_delta(msg) -> bool:
            return len(msg.get("M", [])) > 0 and type(msg["M"][0]) == dict and msg["M"][0].get("M", None) == "uE"

        output: Dict[str, Any] = {"nonce": None, "type": None, "results": {}}
        msg: Dict[str, Any] = ujson.loads(msg)

        if _is_snapshot(msg):
            output["results"] = _decode_message(msg["R"])

            # TODO: Refactor accordingly when V3 WebSocket API is released
            # WebSocket API returns market trading pairs in 'Quote-Base' format
            # Code below converts 'Quote-Base' -> 'Base-Quote'
            output["results"].update({
                "M": f"{output['results']['M'].split('-')[1]}-{output['results']['M'].split('-')[0]}"
            })

            output["type"] = "snapshot"
            output["nonce"] = output["results"]["N"]

        elif _is_market_delta(msg):
            output["results"] = _decode_message(msg["M"][0]["A"][0])

            # TODO: Refactor accordingly when V3 WebSocket API is released
            # WebSocket API returns market trading pairs in 'Quote-Base' format
            # Code below converts 'Quote-Base' -> 'Base-Quote'
            output["results"].update({
                "M": f"{output['results']['M'].split('-')[1]}-{output['results']['M'].split('-')[0]}"
            })

            output["type"] = "update"
            output["nonce"] = output["results"]["N"]

        return output

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        # Technically this does not listen for snapshot, Instead it periodically queries for snapshots.
        while True:
            try:
                connection, hub = await self.websocket_connection()
                trading_pairs = await self.get_trading_pairs()  # Symbols of trading pair in V3 format i.e. 'Base-Quote'
                for trading_pair in trading_pairs:
                    # TODO: Refactor accordingly when V3 WebSocket API is released
                    # WebSocket API requires trading_pair to be in 'Quote-Base' format
                    trading_pair = f"{trading_pair.split('-')[1]}-{trading_pair.split('-')[0]}"
                    hub.server.invoke("queryExchangeState", trading_pair)
                    self.logger().info(f"Query {trading_pair} snapshots.[Scheduled]")

                # Waits for delta amount of time before getting new snapshots
                this_hour: pd.Timestamp = pd.Timestamp.utcnow().replace(minute=0, second=0, microsecond=0)
                next_hour: pd.Timestamp = this_hour + pd.Timedelta(hours=1)
                delta: float = next_hour.timestamp() - time.time()
                await asyncio.sleep(delta)
            except Exception:
                self.logger().error("Unexpected error occurred invoking queryExchangeState", exc_info=True)

    async def listen_for_order_book_stream(self,
                                           ev_loop: asyncio.BaseEventLoop,
                                           snapshot_queue: asyncio.Queue,
                                           diff_queue: asyncio.Queue):
        while True:
            connection, hub = await self.websocket_connection()
            try:
                async for raw_message in self._socket_stream():
                    decoded: Dict[str, Any] = self._transform_raw_message(raw_message)
                    trading_pair: str = decoded["results"].get("M")

                    if not trading_pair:  # Ignores any other websocket response messages
                        continue

                    # Processes snapshot messages
                    if decoded["type"] == "snapshot":
                        snapshot: Dict[str, any] = decoded
                        snapshot_timestamp = snapshot["nonce"]
                        snapshot_msg: OrderBookMessage = FtxOrderBook.snapshot_message_from_exchange(
                            snapshot["results"], snapshot_timestamp
                        )
                        snapshot_queue.put_nowait(snapshot_msg)
                        self._snapshot_msg[trading_pair] = {
                            "timestamp": int(time.time()),
                            "content": snapshot_msg
                        }

                    # Processes diff messages
                    if decoded["type"] == "update":
                        diff: Dict[str, any] = decoded
                        diff_timestamp = diff["nonce"]
                        diff_msg: OrderBookMessage = FtxOrderBook.diff_message_from_exchange(
                            diff["results"], diff_timestamp
                        )
                        diff_queue.put_nowait(diff_msg)

            except Exception:
                self.logger().error("Unexpected error when listening on socket stream.", exc_info=True)
            finally:
                connection.close()
                self._websocket_connection = self._websocket_hub = None
                self.logger().info("Reinitializing websocket connection...")
