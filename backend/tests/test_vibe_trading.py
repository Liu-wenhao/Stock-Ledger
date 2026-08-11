import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.api import vibe
from app.core.config import Settings
from app.schemas.vibe import VibeAnalyzeRequest
from app.services.vibe_trading import VibeTradingClient, VibeTradingError


def _settings(**overrides):
    values = {
        "database_url": "sqlite://",
        "vibe_trading_url": "http://vibe.test",
        "vibe_trading_timeout_seconds": 30,
    }
    values.update(overrides)
    return Settings(**values)


class VibeTradingClientTests(IsolatedAsyncioTestCase):
    async def test_bearer_header_and_create_session_payload(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"session_id": "session-1", "title": "Test", "status": "idle"},
            )

        client = VibeTradingClient(
            _settings(vibe_trading_api_key="secret"),
            transport=httpx.MockTransport(handler),
        )
        await client.create_session("Test")

        self.assertEqual(requests[0].headers["Authorization"], "Bearer secret")
        self.assertEqual(json.loads(requests[0].content), {"title": "Test", "config": None})

    async def test_upload_and_message_requests(self) -> None:
        paths = []

        async def handler(request: httpx.Request) -> httpx.Response:
            paths.append((request.method, request.url.path))
            if request.url.path == "/upload":
                self.assertIn(b"ledger.csv", request.content)
                return httpx.Response(200, json={"status": "ok", "file_path": "uploads/1.csv"})
            if request.url.path.endswith("/goal"):
                payload = json.loads(request.content)
                self.assertEqual(payload["objective"], "Analyze")
                self.assertEqual(payload["risk_tier"], "research_general")
                return httpx.Response(201, json={"goal": {"goal_id": "goal-1"}})
            return httpx.Response(200, json={"status": "queued"})

        client = VibeTradingClient(_settings(), transport=httpx.MockTransport(handler))
        await client.upload_csv("ledger.csv", b"Symbol,Side\nAAPL,Buy\n")
        await client.create_goal("session-1", "Analyze", "Risk review")
        await client.send_message("session-1", "Analyze")

        self.assertEqual(
            paths,
            [
                ("POST", "/upload"),
                ("POST", "/sessions/session-1/goal"),
                ("POST", "/sessions/session-1/messages"),
            ],
        )

    async def test_upstream_error_is_wrapped(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "Invalid API key"})

        client = VibeTradingClient(_settings(), transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(VibeTradingError, "Invalid API key"):
            await client.list_sessions()


class VibeApiTests(IsolatedAsyncioTestCase):
    async def test_status_reports_unavailable_service(self) -> None:
        fake_client = SimpleNamespace(
            base_url="http://vibe.test",
            list_sessions=AsyncMock(side_effect=VibeTradingError("无法连接")),
        )
        with patch.object(vibe, "_client", return_value=fake_client):
            result = await vibe.vibe_status()

        self.assertFalse(result.available)
        self.assertEqual(result.detail, "无法连接")

    async def test_analyze_uploads_creates_session_and_dispatches(self) -> None:
        fake_client = SimpleNamespace(
            upload_csv=AsyncMock(return_value={"file_path": "uploads/1.csv"}),
            create_session=AsyncMock(
                return_value={"session_id": "session-1", "title": "Research", "status": "idle"}
            ),
            create_goal=AsyncMock(return_value={"goal": {"goal_id": "goal-1"}}),
            send_message=AsyncMock(return_value={"status": "queued"}),
        )
        query = SimpleNamespace(
            filter=lambda *_: query,
            count=lambda: 1,
        )
        fake_db = SimpleNamespace(query=lambda *_: query)

        with (
            patch.object(vibe, "_client", return_value=fake_client),
            patch.object(vibe, "export_csv", return_value="Symbol,Side\nAAPL,Buy\n"),
        ):
            result = await vibe.analyze_trades(
                VibeAnalyzeRequest(prompt="检查风险", include_trades=True),
                db=fake_db,
            )

        self.assertEqual(result.session_id, "session-1")
        fake_client.upload_csv.assert_awaited_once()
        fake_client.create_goal.assert_awaited_once()
        goal_prompt = fake_client.create_goal.await_args.kwargs["objective"]
        self.assertIn("uploads/1.csv", goal_prompt)
        self.assertIn("不执行任何交易", goal_prompt)
        dispatched_prompt = fake_client.send_message.await_args.args[1]
        self.assertIn("uploads/1.csv", dispatched_prompt)
        self.assertIn("不执行任何交易", dispatched_prompt)


class VibeSessionValidationTests(TestCase):
    def test_rejects_invalid_session_id(self) -> None:
        with self.assertRaises(HTTPException) as context:
            vibe._valid_session_id("../../sessions")
        self.assertEqual(context.exception.status_code, 400)
