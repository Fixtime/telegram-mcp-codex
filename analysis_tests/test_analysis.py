import json
import base64
import os
import sys
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telethon import types, utils
from telethon.errors import FloodWaitError

from telegram_analysis.config import ConfigError, Settings, private_read, private_write
from telegram_analysis.server import TOOLS, create_server
from telegram_analysis.service import AnalysisService


def settings(tmp_path, scope="all", **kwargs):
    return Settings(1, "a" * 32, 777, scope, tmp_path, **kwargs)


def user(uid):
    return types.User(id=uid, first_name=f"User {uid}", access_hash=1)


def channel(cid):
    return types.Channel(
        id=cid,
        title=f"Channel {cid}",
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(timezone.utc),
        access_hash=1,
        broadcast=True,
    )


def message(mid, hours=1, text="Hello", date=None):
    return SimpleNamespace(
        id=mid,
        message=text,
        date=date or datetime.now(timezone.utc) - timedelta(hours=hours),
        sender_id=42,
        sender=user(42),
        media=None,
        reply_to_msg_id=None,
    )


class FakeClient:
    def __init__(self, entities=None, messages=None, account=777, failure=None):
        self.entities = entities or [user(42), channel(42)]
        self.messages = messages or [message(i) for i in range(5, 0, -1)]
        self.account = account
        self.failure = failure
        self.requests = []
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return user(self.account)

    async def get_entity(self, peer):
        self.requests.append(("entity", utils.get_peer_id(peer)))
        if self.failure:
            raise self.failure
        return next(e for e in self.entities if utils.get_peer_id(e) == utils.get_peer_id(peer))

    async def iter_dialogs(self, limit):
        self.requests.append(("dialogs", limit))
        for e in self.entities[:limit]:
            yield SimpleNamespace(entity=e)

    async def get_messages(self, entity, ids):
        return next((m for m in self.messages if m.id == ids), None)

    async def iter_messages(
        self,
        entity,
        limit,
        offset_id=0,
        offset_date=None,
        search=None,
        max_id=0,
        min_id=0,
        reverse=False,
    ):
        self.requests.append(("messages", limit))
        rows = [
            m
            for m in self.messages
            if (not offset_id or m.id < offset_id)
            and (not max_id or m.id < max_id)
            and (not min_id or m.id > min_id)
            and (offset_date is None or m.date < offset_date)
            and (search is None or search.lower() in m.message.lower())
        ]
        for m in sorted(rows, key=lambda m: m.id, reverse=not reverse)[:limit]:
            yield m


async def service(tmp_path, scope="all", **kwargs):
    cl = kwargs.pop("client", FakeClient())
    svc = AnalysisService(settings(tmp_path, scope, **kwargs), cl)
    await svc.verify()
    return svc


def write_config(tmp_path, **overrides):
    os.chmod(tmp_path, 0o700)
    data = dict(version=1, api_id=1, api_hash="a" * 32, account_id=777, chat_scope="all")
    data.update(overrides)
    file = tmp_path / "config.json"
    private_write(file, json.dumps(data))
    return file


@pytest.mark.parametrize("scope", [None, "", [], "ALL", [True], ["42"], [0]])
def test_configuration_fails_closed(tmp_path, scope):
    with pytest.raises(ConfigError):
        Settings.load(write_config(tmp_path, chat_scope=scope))


def test_missing_scope_fails_closed(tmp_path):
    path = write_config(tmp_path)
    data = json.loads(path.read_text())
    del data["chat_scope"]
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError):
        Settings.load(path)


def test_explicit_all_and_typed_ids(tmp_path):
    assert Settings.load(write_config(tmp_path)).permits(42)
    s = settings(tmp_path, frozenset({-1000000000042}))
    assert s.permits(-1000000000042)
    assert not s.permits(42)
    assert not s.permits(-42)


@pytest.mark.parametrize("target", ["file", "directory"])
def test_permission_check(tmp_path, target):
    path = write_config(tmp_path)
    os.chmod(path if target == "file" else tmp_path, 0o644 if target == "file" else 0o755)
    with pytest.raises(ConfigError):
        Settings.load(path)


def test_symlink_and_overwrite_rejected(tmp_path):
    path = write_config(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        private_read(link)
    with pytest.raises(FileExistsError):
        private_write(path, "overwrite")


@pytest.mark.asyncio
async def test_account_mismatch(tmp_path):
    svc = AnalysisService(settings(tmp_path), FakeClient(account=778))
    with pytest.raises(ToolError, match="ACCOUNT_MISMATCH"):
        await svc.verify()
    with pytest.raises(ToolError, match="NOT_READY"):
        await svc.list_chats()


@pytest.mark.asyncio
async def test_deny_before_any_network_and_no_id_aliasing(tmp_path):
    svc = await service(tmp_path, frozenset({-1000000000042}))
    for cid in [42, -42, 99]:
        with pytest.raises(ToolError, match="CHAT_DENIED"):
            await svc.list_messages(cid)
    assert svc.client.requests == []
    assert (await svc.get_chat(-1000000000042))["type"] == "channel"
    assert [x["chat_id"] for x in (await svc.list_chats())["results"]] == [-1000000000042]


@pytest.mark.asyncio
async def test_post_resolution_identity_check(tmp_path):
    svc = await service(tmp_path)

    async def wrong(peer):
        return channel(42)

    svc.client.get_entity = wrong
    with pytest.raises(ToolError, match="PEER_MISMATCH"):
        await svc.get_chat(42)


@pytest.mark.asyncio
async def test_catalog_pagination_and_scan_limit_are_explicit(tmp_path):
    svc = await service(tmp_path, client=FakeClient(entities=[user(41), user(42), channel(42)]))
    first = await svc.list_chats(limit=2)
    second = await svc.list_chats(limit=2, cursor=first["next_cursor"])
    assert [r["chat_id"] for page in (first, second) for r in page["results"]] == [
        41,
        42,
        -1000000000042,
    ]
    assert second["next_cursor"] is None
    bounded = await service(tmp_path, max_dialogs=1)
    result = await bounded.list_chats()
    assert result["scan_limit_reached"]
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_search_results_and_context_respect_message_limit(tmp_path):
    svc = await service(
        tmp_path,
        client=FakeClient(messages=[message(5, text="match"), message(4, text="other")]),
        max_messages=1,
    )
    result = await svc.list_messages(42, limit=1, query="match")
    assert [r["message_id"] for r in result["results"]] == [5]
    context = await svc.get_message_context(42, 5, context_size=3)
    assert context["returned_count"] == 1


@pytest.mark.asyncio
async def test_pagination_preserves_default_window_and_no_gaps(tmp_path):
    svc = await service(tmp_path)
    first = await svc.list_messages(42, limit=2)
    second = await svc.list_messages(42, limit=2, cursor=first["next_cursor"])
    third = await svc.list_messages(42, limit=2, cursor=second["next_cursor"])
    assert [m["message_id"] for page in (first, second, third) for m in page["results"]] == [
        5,
        4,
        3,
        2,
        1,
    ]
    assert first["to_exclusive"] == second["to_exclusive"] == third["to_exclusive"]
    assert third["next_cursor"] is None


@pytest.mark.asyncio
async def test_cursor_tampering_and_cross_chat_query(tmp_path):
    svc = await service(tmp_path)
    token = (await svc.list_messages(42, limit=1))["next_cursor"]
    modified = bytearray(base64.urlsafe_b64decode(token))
    modified[0] ^= 1
    tampered = base64.urlsafe_b64encode(modified).decode()
    for cid, q, cursor in [
        (42, None, tampered),
        (-1000000000042, None, token),
        (42, "Hello", token),
    ]:
        with pytest.raises(ToolError, match="INVALID_CURSOR"):
            await svc.list_messages(cid, cursor=cursor, query=q)


@pytest.mark.asyncio
async def test_dates_are_server_limits_and_moscow_days(tmp_path):
    svc = await service(tmp_path)
    lo, hi = svc._window("2026-10-01", "2026-10-01")
    assert lo.isoformat() == "2026-09-30T21:00:00+00:00"
    assert hi.isoformat() == "2026-10-01T21:00:00+00:00"
    for start, end in [
        ("2026-01-01", "2026-10-01"),
        ("bad", "2026-10-01"),
        ("2026-10-02", "2026-10-01"),
    ]:
        with pytest.raises(ToolError, match="INVALID_DATES"):
            await svc.list_messages(42, from_date=start, to_date=end)
    assert not svc.client.requests


@pytest.mark.asyncio
async def test_byte_truncation_paginates_without_skipping_unicode(tmp_path):
    cl = FakeClient(messages=[message(i, text="Ж" * 2000) for i in range(5, 0, -1)])
    svc = await service(tmp_path, client=cl, max_output_bytes=10000)
    pages = []
    page = await svc.list_messages(42)
    for _ in range(5):
        pages.append(page)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 10000
        if not page["next_cursor"]:
            break
        page = await svc.list_messages(42, cursor=page["next_cursor"])
    assert pages[0]["output_truncated"]
    assert [m["message_id"] for p in pages for m in p["results"]] == [5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_context_cannot_escape_window_and_keeps_center(tmp_path):
    cl = FakeClient(messages=[message(5), message(4), message(3), message(2, hours=300)])
    svc = await service(tmp_path, client=cl)
    result = await svc.get_message_context(42, 4)
    assert [m["message_id"] for m in result["results"]] == [3, 4, 5]
    with pytest.raises(ToolError, match="OUTSIDE_DATE_RANGE"):
        await svc.get_message_context(42, 2)


@pytest.mark.asyncio
async def test_rate_and_flood_limits(tmp_path):
    svc = await service(tmp_path, calls_per_minute=1)
    await svc.get_chat(42)
    with pytest.raises(ToolError, match="RATE_LIMIT"):
        await svc.get_chat(42)
    other = await service(
        tmp_path, client=FakeClient(failure=FloodWaitError(request=None, capture=120))
    )
    with pytest.raises(ToolError, match="retry_after_seconds=120"):
        await other.get_chat(42)
    count = len(other.client.requests)
    with pytest.raises(ToolError, match="FLOOD_WAIT"):
        await other.get_chat(42)
    assert len(other.client.requests) == count


@pytest.mark.asyncio
async def test_errors_do_not_leak_provider_payloads(tmp_path):
    svc = await service(
        tmp_path, client=FakeClient(failure=RuntimeError("SECRET_SESSION /private/path"))
    )
    with pytest.raises(ToolError) as exc:
        await svc.get_chat(42)
    assert str(exc.value) == "TELEGRAM_READ_FAILED: check service authorization and connectivity."


@pytest.mark.asyncio
async def test_actual_mcp_protocol_registry_reads_and_denies_writes(tmp_path):
    svc = await service(tmp_path)
    server = create_server(svc)
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == set(TOOLS)
        assert all(t.annotations.readOnlyHint for t in listed.tools)
        for forbidden in [
            "send_message",
            "delete_message",
            "export_contacts",
            "transcribe_voice",
            "download_media",
        ]:
            result = await session.call_tool(forbidden, {})
            assert result.isError
        read = await session.call_tool("list_messages", {"chat_id": 42, "limit": 2})
        assert not read.isError
        assert read.structuredContent["returned_count"] == 2
        assert "telegram_mcp.runtime" not in sys.modules
        assert "telegram_mcp.tools" not in sys.modules
    assert not svc.client.connected


@pytest.mark.asyncio
async def test_actual_stdio_transport_from_another_directory(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    fixture = (
        "from pathlib import Path; "
        "from analysis_tests.test_analysis import FakeClient, settings; "
        "from telegram_analysis.service import AnalysisService; "
        "from telegram_analysis.server import create_server; "
        "create_server(AnalysisService(settings(Path('/tmp')), FakeClient())).run(transport='stdio')"
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-c", fixture], cwd=str(tmp_path), env={"PYTHONPATH": root}
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert "untrusted" in init.instructions
            assert {tool.name for tool in (await session.list_tools()).tools} == set(TOOLS)
            result = await session.call_tool("get_chat", {"chat_id": 42})
            assert result.structuredContent["chat_id"] == 42
            assert (
                await session.call_tool("send_message", {"chat_id": 42, "message": "never"})
            ).isError


@pytest.mark.parametrize("entry", ["telegram_analysis_mcp.py", "main.py"])
def test_default_entrypoint_fails_closed_without_secrets(tmp_path, entry):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / entry), "--config", str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "private configuration/session" in result.stderr
    assert "TELEGRAM_API_ID" not in result.stderr
