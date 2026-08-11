from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings


class VibeTradingError(RuntimeError):
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class VibeTradingClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = settings.vibe_trading_url.rstrip("/")
        self.api_key = settings.vibe_trading_api_key
        self.timeout = httpx.Timeout(settings.vibe_trading_timeout_seconds, connect=5.0)
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise VibeTradingError(
                f"无法连接 Vibe-Trading（{self.base_url}），请确认服务已启动"
            ) from exc
        except httpx.TimeoutException as exc:
            raise VibeTradingError("Vibe-Trading 请求超时，请稍后重试", 504) from exc
        except httpx.HTTPError as exc:
            raise VibeTradingError(f"Vibe-Trading 请求失败：{exc}") from exc

        if response.is_error:
            detail: Any = None
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else payload
            except ValueError:
                detail = response.text.strip()
            message = str(detail or f"HTTP {response.status_code}")
            raise VibeTradingError(f"Vibe-Trading：{message}", 502)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise VibeTradingError("Vibe-Trading 返回了无法解析的响应") from exc

    async def list_sessions(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/sessions")
        return payload if isinstance(payload, list) else []

    async def create_session(self, title: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/sessions",
            json={"title": title, "config": None},
        )

    async def upload_csv(self, filename: str, content: bytes) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/upload",
            files={"file": (filename, content, "text/csv")},
        )

    async def create_goal(
        self,
        session_id: str,
        objective: str,
        ui_summary: str = "",
    ) -> dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return await self._request(
            "POST",
            f"/sessions/{safe_id}/goal",
            json={
                "objective": objective,
                "criteria": [],
                "ui_summary": ui_summary[:500],
                "protocol": "thesis_review",
                "risk_tier": "research_general",
            },
        )

    async def send_message(self, session_id: str, content: str) -> dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return await self._request(
            "POST",
            f"/sessions/{safe_id}/messages",
            json={"content": content},
        )

    async def get_session(self, session_id: str) -> dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return await self._request("GET", f"/sessions/{safe_id}")

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        safe_id = quote(session_id, safe="")
        payload = await self._request("GET", f"/sessions/{safe_id}/messages")
        return payload if isinstance(payload, list) else []

    async def cancel(self, session_id: str) -> dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return await self._request("POST", f"/sessions/{safe_id}/cancel")
