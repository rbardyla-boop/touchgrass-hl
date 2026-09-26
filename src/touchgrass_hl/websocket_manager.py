"""One public mainnet WebSocket. Reconnect, jittered backoff, resubscribe."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Callable

from touchgrass_hl.logging_setup import log
from touchgrass_hl.util import backoff_delay

logger = logging.getLogger("touchgrass.ws")

Subscription = dict[str, Any]


class SubscriptionManager:
    """Desired-vs-active subscription set. Trade subs are capped by volume rank."""

    def __init__(self, max_trade_subs: int) -> None:
        self.max_trade_subs = max_trade_subs
        self.desired: dict[str, Subscription] = {}
        self.active: dict[str, Subscription] = {}

    def set_universe(self, coins_by_volume: list[str]) -> list[str]:
        """Replace trade subscriptions. Always keep allDexsAssetCtxs.

        Returns the coins actually subscribed (after the cap).
        """
        kept_books = {k: v for k, v in self.desired.items() if k.startswith("l2:") or k.startswith("bbo:")}
        self.desired = {"allDexsAssetCtxs": {"type": "allDexsAssetCtxs"}}
        chosen = []
        for coin in coins_by_volume:
            if len(chosen) >= self.max_trade_subs:
                break
            if not coin:
                continue
            key = f"trades:{coin}"
            self.desired[key] = {"type": "trades", "coin": coin}
            chosen.append(coin)
        self.desired.update(kept_books)
        return chosen

    def watch_book(self, coin: str) -> None:
        self.desired[f"l2:{coin}"] = {"type": "l2Book", "coin": coin}
        self.desired[f"bbo:{coin}"] = {"type": "bbo", "coin": coin}

    def unwatch_book(self, coin: str) -> None:
        self.desired.pop(f"l2:{coin}", None)
        self.desired.pop(f"bbo:{coin}", None)

    def on_disconnect(self) -> None:
        self.active.clear()

    def diff(self) -> tuple[list[tuple[str, Subscription]], list[tuple[str, Subscription]]]:
        to_sub = [(k, v) for k, v in self.desired.items() if k not in self.active]
        to_unsub = [(k, v) for k, v in self.active.items() if k not in self.desired]
        return to_sub, to_unsub

    def mark_active(self, key: str) -> None:
        if key in self.desired:
            self.active[key] = self.desired[key]

    def mark_inactive(self, key: str) -> None:
        self.active.pop(key, None)


class WebsocketManager:
    def __init__(
        self,
        url: str,
        subs: SubscriptionManager,
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        self.url = url
        self.subs = subs
        self.on_message = on_message
        self.synced = False
        self.connects = 0
        self.messages = 0
        self.last_message_ms = 0
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    async def run(self, stop: asyncio.Event) -> None:
        import websockets

        attempt = 0
        while not stop.is_set() and not self._stop:
            try:
                log(logger, logging.INFO, "ws_connecting", url=self.url, attempt=attempt)
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    open_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                ) as ws:
                    self.connects += 1
                    self.subs.on_disconnect()
                    self.synced = False
                    await self._sync(ws)
                    self.synced = True
                    attempt = 0
                    log(
                        logger,
                        logging.INFO,
                        "ws_connected",
                        subscriptions=len(self.subs.active),
                        connects=self.connects,
                    )
                    ping_task = asyncio.create_task(self._ping(ws, stop))
                    try:
                        while not stop.is_set() and not self._stop:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                await self._sync(ws)
                                continue
                            self._handle_raw(raw)
                            await self._sync(ws)
                    finally:
                        ping_task.cancel()
                        self.synced = False
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            self.synced = False
            except asyncio.CancelledError:
                self.synced = False
                raise
            except Exception as exc:
                self.synced = False
                self.subs.on_disconnect()
                log(logger, logging.WARNING, "ws_disconnected", error=type(exc).__name__, detail=str(exc)[:300])
            if stop.is_set() or self._stop:
                break
            delay = backoff_delay(attempt, cap_s=60.0, jitter_unit=random.random())
            attempt += 1
            log(logger, logging.INFO, "ws_backoff", seconds=round(delay, 3), attempt=attempt)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue
        self.synced = False
        log(logger, logging.INFO, "ws_stopped", messages=self.messages)

    async def _sync(self, ws) -> None:
        to_sub, to_unsub = self.subs.diff()
        for key, sub in to_unsub:
            await ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))
            self.subs.mark_inactive(key)
        for key, sub in to_sub:
            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
            self.subs.mark_active(key)

    async def _ping(self, ws, stop: asyncio.Event) -> None:
        while not stop.is_set() and not self._stop:
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
                return
            except asyncio.TimeoutError:
                continue

    def _handle_raw(self, raw: str | bytes) -> None:
        self.messages += 1
        from touchgrass_hl.util import utc_now_ms

        self.last_message_ms = utc_now_ms()
        try:
            if isinstance(raw, bytes):
                raw = raw.decode()
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        channel = message.get("channel")
        if channel in {"pong", "subscriptionResponse"}:
            return
        try:
            self.on_message(message)
        except Exception:
            logger.exception("ws_callback_failed")
