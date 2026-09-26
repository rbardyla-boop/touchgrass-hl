"""Hyperliquid TESTNET execution only.

There is no mainnet order path. The URL is checked before the SDK is asked to
sign anything. Withdrawals and transfers are not implemented.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from touchgrass_hl.config import TESTNET_API_URL
from touchgrass_hl.logging_setup import log

logger = logging.getLogger("touchgrass.testnet")


class RefusingMainnetError(RuntimeError):
    """Raised when an order URL is not the official Hyperliquid testnet."""


class TestnetNotConfigured(RuntimeError):
    """Raised when testnet credentials were not provided."""


def assert_testnet_url(base_url: str) -> str:
    normalized = (base_url or "").rstrip("/")
    if normalized != TESTNET_API_URL:
        raise RefusingMainnetError(
            "refusing to sign an order: execution URL is not the official Hyperliquid testnet"
        )
    if normalized == "https://api.hyperliquid.xyz":
        raise RefusingMainnetError("mainnet execution is disabled in v0.1")
    return normalized


class TestnetExecutor:
    def __init__(
        self,
        *,
        private_key: str,
        account_address: str,
        base_url: str,
        kill_switch: bool,
    ) -> None:
        self.base_url = assert_testnet_url(base_url)
        if not private_key:
            raise TestnetNotConfigured("TESTNET_AGENT_PRIVATE_KEY is unset")
        self.account_address = (account_address or "").strip()
        self.kill_switch = kill_switch
        self._private_key = private_key
        self._exchange = None

    def _exchange_client(self):
        if self._exchange is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(self._private_key)
            address = self.account_address or wallet.address
            self._exchange = Exchange(wallet, base_url=self.base_url, account_address=address)
            self.account_address = address
        return self._exchange

    def _refuse_if_killed(self) -> None:
        if self.kill_switch:
            raise RuntimeError("kill switch is active; testnet executor refusing action")

    def place_limit(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        reduce_only: bool = False,
        tif: str = "Gtc",
    ) -> dict[str, Any]:
        self._refuse_if_killed()
        assert_testnet_url(self.base_url)
        exchange = self._exchange_client()
        order_type = {"limit": {"tif": tif}}
        response = exchange.order(coin, is_buy, size, limit_px, order_type, reduce_only=reduce_only)
        log(logger, logging.INFO, "testnet_order", coin=coin, is_buy=is_buy, reduce_only=reduce_only)
        return response if isinstance(response, dict) else {"raw": str(response)}

    def cancel(self, coin: str, oid: int) -> dict[str, Any]:
        self._refuse_if_killed()
        assert_testnet_url(self.base_url)
        response = self._exchange_client().cancel(coin, oid)
        log(logger, logging.INFO, "testnet_cancel", coin=coin, oid=oid)
        return response if isinstance(response, dict) else {"raw": str(response)}

    def market_close(self, coin: str, size: float | None = None) -> dict[str, Any]:
        self._refuse_if_killed()
        assert_testnet_url(self.base_url)
        response = self._exchange_client().market_close(coin, sz=size)
        log(logger, logging.INFO, "testnet_market_close", coin=coin)
        return response if isinstance(response, dict) else {"raw": str(response)}

    def query_order(self, oid: int) -> dict[str, Any]:
        assert_testnet_url(self.base_url)
        info = self._exchange_client().info
        response = info.query_order_by_oid(self.account_address, oid)
        return response if isinstance(response, dict) else {"raw": str(response)}

    def user_state(self) -> dict[str, Any]:
        assert_testnet_url(self.base_url)
        response = self._exchange_client().info.user_state(self.account_address)
        return response if isinstance(response, dict) else {"raw": str(response)}

    def all_mids(self) -> dict[str, Any]:
        assert_testnet_url(self.base_url)
        response = self._exchange_client().info.all_mids()
        return response if isinstance(response, dict) else {"raw": str(response)}


def extract_oid(response: dict[str, Any]) -> int | None:
    statuses = (
        response.get("response", {})
        .get("data", {})
        .get("statuses", [])
        if isinstance(response.get("response"), dict)
        else []
    )
    if not statuses or not isinstance(statuses, list):
        return None
    status = statuses[0]
    if not isinstance(status, dict):
        return None
    if "resting" in status and isinstance(status["resting"], dict):
        oid = status["resting"].get("oid")
        return int(oid) if oid is not None else None
    if "filled" in status and isinstance(status["filled"], dict):
        oid = status["filled"].get("oid")
        return int(oid) if oid is not None else None
    return None


def smoke_order(
    executor: TestnetExecutor,
    *,
    coin: str | None = None,
) -> dict[str, Any]:
    """Place a tiny far-from-market GTC order, query it, cancel it.

    If the order somehow fills, close that coin reduce-only via market_close.
    """
    mids = executor.all_mids()
    chosen = coin
    if not chosen:
        for candidate in ("BTC", "ETH", "SOL"):
            if candidate in mids:
                chosen = candidate
                break
        if not chosen and mids:
            chosen = sorted(mids)[0]
    if not chosen or chosen not in mids:
        raise RuntimeError("testnet smoke could not find a mid price")
    mid = Decimal(str(mids[chosen]))
    if mid <= 0:
        raise RuntimeError("testnet mid is not positive")
    info = executor._exchange_client().info
    asset = info.name_to_asset(chosen)
    sz_decimals = int(info.asset_to_sz_decimals[asset])
    quantum = Decimal(10) ** -sz_decimals
    # Stay above the usual $10 minimum without being large.
    target = Decimal("12")
    size = (target / (mid * Decimal("0.5"))).quantize(quantum)
    if size <= 0:
        size = quantum
    limit_px = (mid * Decimal("0.5")).quantize(Decimal("0.1") if mid >= 10 else Decimal("0.0001"))
    if limit_px <= 0:
        limit_px = mid / Decimal(2)
    response = executor.place_limit(
        coin=chosen,
        is_buy=True,
        size=float(size),
        limit_px=float(limit_px),
        reduce_only=False,
        tif="Gtc",
    )
    oid = extract_oid(response)
    query = executor.query_order(oid) if oid is not None else None
    cancel = None
    close = None
    if oid is not None:
        filled = isinstance(response.get("response", {}).get("data", {}).get("statuses", [{}])[0], dict) and (
            "filled" in response["response"]["data"]["statuses"][0]
        )
        if filled:
            close = executor.market_close(chosen, float(size))
        else:
            cancel = executor.cancel(chosen, oid)
            query_after = executor.query_order(oid)
        if not filled:
            query = {"before_cancel": query, "after_cancel": query_after}
    return {
        "coin": chosen,
        "size": format(size, "f"),
        "limit_px": format(limit_px, "f"),
        "mid": format(mid, "f"),
        "oid": oid,
        "place": response,
        "query": query,
        "cancel": cancel,
        "close": close,
        "base_url": executor.base_url,
    }
