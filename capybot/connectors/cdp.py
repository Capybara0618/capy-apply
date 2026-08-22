"""Minimal asynchronous Chrome DevTools Protocol transport."""

from __future__ import annotations

import asyncio
import json
from typing import Any


class CDPError(RuntimeError):
    pass


_NO_ARG = object()


class RawCDPPage:
    """Read a user-controlled Chrome tab without taking browser ownership."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws = None
        self._next_id = 0
        self._closed = False

    async def connect(self) -> "RawCDPPage":
        import websockets

        self._ws = await websockets.connect(
            self.ws_url,
            proxy=None,
            open_timeout=10,
            max_size=None,
        )
        return self

    def is_closed(self) -> bool:
        return self._closed or self._ws is None

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._closed = True
        self._ws = None

    async def close_target(self) -> None:
        """Close the temporary browser tab and then release the CDP socket."""
        if self._ws is not None:
            try:
                await self._send("Page.close")
            except CDPError:
                pass
        await self.close()

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        if wait_until not in {None, "domcontentloaded"}:
            raise ValueError(f"Raw CDP navigation does not support wait_until={wait_until!r}")
        try:
            current = await self.current_url()
            if current == url or current.startswith(url):
                return
        except CDPError:
            pass
        try:
            await self._send("Page.navigate", {"url": url})
        except CDPError:
            await self.evaluate(
                "(target) => { location.href = target; return location.href; }",
                url,
            )
        await asyncio.sleep(2.5)

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)

    async def current_url(self) -> str:
        value = await self.evaluate("location.href")
        return str(value or "")

    async def evaluate(self, source: str, arg: Any = _NO_ARG) -> Any:
        expression = self._expression_for(source, arg)
        data = await self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if data.get("exceptionDetails"):
            detail = data["exceptionDetails"]
            text = detail.get("text") or "CDP Runtime.evaluate failed"
            raise CDPError(text)
        result = data.get("result") or {}
        if "value" in result:
            return result["value"]
        if result.get("type") == "undefined":
            return None
        return result.get("description")

    async def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("Chrome CDP page is not connected")
        self._next_id += 1
        message_id = self._next_id
        try:
            await self._ws.send(
                json.dumps({"id": message_id, "method": method, "params": params or {}})
            )
        except Exception as exc:
            self._closed = True
            raise CDPError(f"Chrome CDP connection closed during {method}: {exc}") from exc
        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=20)
            except asyncio.TimeoutError as exc:
                raise CDPError(f"Chrome CDP command timed out: {method}") from exc
            except Exception as exc:
                self._closed = True
                raise CDPError(f"Chrome CDP connection closed during {method}: {exc}") from exc
            payload = json.loads(raw)
            if payload.get("id") != message_id:
                continue
            if "error" in payload:
                raise CDPError(payload["error"].get("message") or f"CDP {method} failed")
            return payload.get("result") or {}

    @staticmethod
    def _expression_for(source: str, arg: Any) -> str:
        stripped = source.strip()
        callable_source = (
            stripped.startswith("() =>")
            or stripped.startswith("async ")
            or stripped.startswith("(")
            or stripped.startswith("function")
        )
        if callable_source:
            if arg is _NO_ARG:
                return f"({stripped})()"
            return f"({stripped})({json.dumps(arg, ensure_ascii=False)})"
        if arg is not _NO_ARG:
            return f"(({json.dumps(arg, ensure_ascii=False)}) => {stripped})()"
        return stripped
