#!/usr/bin/env python

import asyncio
import aiohttp
import logging
import os
import time
from typing import (
    AsyncIterable,
    Dict,
    Optional
)
import ujson
import websockets
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.logger import HummingbotLogger

PEATIO_API_ENDPOINT = "https://opendax.tokamaktech.net/api/v2/"
PEATIO_SIGNIN = "barong/identity/sessions"
PEATIO_USER_STREAM_ENDPOINT = "peatio/account/balances"


class PeatioAPIUserStreamDataSource(UserStreamTrackerDataSource):

    MESSAGE_TIMEOUT = 30.0
    PING_TIMEOUT = 10.0

    _bausds_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._bausds_logger is None:
            cls._bausds_logger = logging.getLogger(__name__)
        return cls._bausds_logger

    def __init__(self):
        self._peatio_client = PeatioClient()
        self._current_listen_key = None
        self._listen_for_user_stream_task = None
        self._last_recv_time: float = 0
        super().__init__()

    @property
    def last_recv_time(self) -> float:
        return self._last_recv_time

    async def get_listen_key(self):
        async with aiohttp.ClientSession() as client:
            async with client.post(f"{PEATIO_API_ENDPOINT}{PEATIO_SIGNIN}",
                                   params={"email": self._peatio_client.email,
                                           "password": self._peatio_client.password}) as response:
                response: aiohttp.ClientResponse = response
                if response.status != 200:
                    raise IOError(f"Error fetching Peatio user stream listen key. HTTP status is {response.status}.")
                data = client.cookie_jar
                return data

    async def ping_listen_key(self, listen_key: str) -> bool:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{PEATIO_API_ENDPOINT}{PEATIO_USER_STREAM_ENDPOINT}",
                                  cookies = self._current_listen_key) as response:
                data: [str, any] = await response.json()
                if "code" in data:
                    self.logger().warning(f"Failed to refresh the listen key {listen_key}: {data}")
                    return False
                return True

    async def _inner_messages(self, ws: websockets.WebSocketClientProtocol) -> AsyncIterable[str]:
        # Terminate the recv() loop as soon as the next message timed out, so the outer loop can reconnect.
        try:
            while True:
                try:
                    msg: str = await asyncio.wait_for(ws.recv(), timeout=self.MESSAGE_TIMEOUT)
                    self._last_recv_time = time.time()
                    yield msg
                except asyncio.TimeoutError:
                    try:
                        pong_waiter = await ws.ping()
                        await asyncio.wait_for(pong_waiter, timeout=self.PING_TIMEOUT)
                        self._last_recv_time = time.time()
                    except asyncio.TimeoutError:
                        raise
        except asyncio.TimeoutError:
            self.logger().warning("WebSocket ping timed out. Going to reconnect...")
            return
        except websockets.exceptions.ConnectionClosed:
            return
        finally:
            await ws.close()

    async def messages(self) -> AsyncIterable[str]:
        try:
            async with (await self.get_ws_connection()) as ws:
                async for msg in self._inner_messages(ws):
                    yield msg
        except asyncio.CancelledError:
            return

    async def get_ws_connection(self) -> websockets.WebSocketClientProtocol:
        stream_url: str = "wss://opendax.tokamaktech.net/api/v2/ranger/private/?stream=order&stream=trade"
        self.logger().info(f"Reconnecting to {stream_url}.")

        # Create the WS connection.
        return websockets.connect(stream_url, extra_headers=websockets.http.Headers({"cookie": self._current_listen_key}))

    async def listen_for_user_stream(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                if self._current_listen_key is None:
                    self._current_listen_key = await self.get_listen_key()
                    self.logger().debug(f"Obtained listen key {self._current_listen_key}.")
                    if self._listen_for_user_stream_task is not None:
                        self._listen_for_user_stream_task.cancel()
                    self._listen_for_user_stream_task = safe_ensure_future(self.log_user_stream(output))
                    await self.wait_til_next_tick(seconds=60.0)

                success: bool = await self.ping_listen_key(self._current_listen_key)
                if not success:
                    self._current_listen_key = None
                    if self._listen_for_user_stream_task is not None:
                        self._listen_for_user_stream_task.cancel()
                        self._listen_for_user_stream_task = None
                    continue
                self.logger().debug(f"Refreshed listen key {self._current_listen_key}.")

                await self.wait_til_next_tick(seconds=60.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error while maintaining the user event listen key. Retrying after "
                                    "5 seconds...", exc_info=True)
                await asyncio.sleep(5)

    async def log_user_stream(self, output: asyncio.Queue):
        while True:
            try:
                async for message in self.messages():
                    decoded: Dict[str, any] = ujson.loads(message)
                    output.put_nowait(decoded)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error. Retrying after 5 seconds...", exc_info=True)
                await asyncio.sleep(5.0)


class PeatioClient:
    def __init__(self):
        self.email = os.getenv("PEATIO_EXCHANGE_API_KEY")
        self.password = os.getenv("PEATIO_PW")
