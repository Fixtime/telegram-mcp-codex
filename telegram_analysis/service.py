"""Bounded Telegram reads. No send, mutation, file, callback or transcription APIs."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import secrets
import time
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mcp.server.fastmcp.exceptions import ToolError
from telethon import types, utils
from telethon.errors import FloodWaitError

from .config import ConfigError, Settings, peer_id


def clean(text, length: int) -> tuple[str, bool]:
    value = "".join(
        ch for ch in (text or "") if unicodedata.category(ch) not in {"Cc", "Cf"} or ch in "\n\t"
    )
    return value[:length], len(value) > length


def display(entity) -> str:
    text = getattr(entity, "title", None) or " ".join(
        value
        for value in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
        if value
    )
    return clean(text, 256)[0].replace("\n", " ").replace("\r", " ")


def kind(entity) -> str:
    if isinstance(entity, types.User):
        return "chat"
    if isinstance(entity, types.Channel):
        return "group" if entity.megagroup else "channel"
    return "group"


class AnalysisService:
    def __init__(self, settings: Settings, client):
        self.settings = settings
        self.client = client
        self._cursor_key = secrets.token_bytes(32)
        self._calls = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self._verified = False

    async def verify(self):
        if not await self.client.is_user_authorized():
            raise ToolError("NOT_AUTHORIZED: run local setup; never paste credentials into Codex.")
        me = await self.client.get_me()
        if me is None or me.id != self.settings.account_id or getattr(me, "bot", False):
            raise ToolError("ACCOUNT_MISMATCH: service refused the Telegram account.")
        self._verified = True

    @asynccontextmanager
    async def operation(self):
        now = time.monotonic()
        if self._blocked_until > now:
            raise ToolError(f"FLOOD_WAIT: retry_after_seconds={int(self._blocked_until-now)+1}.")
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        if len(self._calls) >= self.settings.calls_per_minute:
            raise ToolError("RATE_LIMIT: retry after one minute; do not loop.")
        self._calls.append(now)
        try:
            # Timeout includes queueing, so callers cannot accumulate unbounded waiters.
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._lock:
                    if self._blocked_until > time.monotonic():
                        raise ToolError("FLOOD_WAIT: Telegram cooldown is active; do not retry.")
                    if not self._verified:
                        raise ToolError("NOT_READY: account has not been verified.")
                    yield
        except FloodWaitError as exc:
            seconds = max(1, exc.seconds)
            self._blocked_until = time.monotonic() + seconds
            raise ToolError(
                f"FLOOD_WAIT: retry_after_seconds={seconds}; do not retry early."
            ) from None
        except TimeoutError:
            raise ToolError("TIMEOUT: read did not finish; narrow the request.") from None
        except ToolError:
            raise
        except (ValueError, ConfigError):
            raise ToolError(
                "INVALID_REQUEST: check canonical chat ID, dates and pagination."
            ) from None
        except Exception:
            # Never return provider exceptions, secrets, paths or request dumps.
            raise ToolError(
                "TELEGRAM_READ_FAILED: check service authorization and connectivity."
            ) from None

    def _limit(self, value: int, high: int) -> int:
        if type(value) is not int or not 1 <= value <= high:
            raise ToolError(f"INVALID_LIMIT: allowed range is 1..{high}.")
        return value

    def _access(self, cid: int):
        try:
            allowed = self.settings.permits(cid)
        except ConfigError:
            raise ToolError(
                "INVALID_CHAT_ID: use the canonical integer ID from list_chats."
            ) from None
        if not allowed:
            raise ToolError("CHAT_DENIED: chat is outside configured access policy.")

    async def _entity(self, cid: int):
        self._access(cid)  # Before remote lookup, including cache warming.
        bare, cls = utils.resolve_id(cid)
        peer = cls(bare)
        try:
            entity = await self.client.get_entity(peer)
        except ValueError:
            entity = None
            async for dialog in self.client.iter_dialogs(limit=self.settings.max_dialogs):
                if utils.get_peer_id(dialog.entity) == cid:
                    entity = dialog.entity
                    break
            if entity is None:
                raise ToolError("CHAT_NOT_FOUND: chat was not found within the dialog scan limit.")
        if utils.get_peer_id(entity) != cid:
            raise ToolError("PEER_MISMATCH: resolved entity differs from requested canonical ID.")
        self._access(utils.get_peer_id(entity))
        return entity

    def _window(self, start: str | None, end: str | None):
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(timezone.utc)

        def parse(value, upper=False):
            if not isinstance(value, str) or len(value) > 40:
                raise ValueError
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if len(value) == 10 and upper:
                dt += timedelta(days=1)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(timezone.utc)

        try:
            hi = parse(end, True) if end is not None else now
            lo = (
                parse(start)
                if start is not None
                else hi - timedelta(days=min(7, self.settings.max_days))
            )
            if not lo < hi or hi - lo > timedelta(days=self.settings.max_days):
                raise ValueError
            if hi > now + timedelta(days=1):
                raise ValueError
        except (ValueError, TypeError):
            raise ToolError(
                f"INVALID_DATES: ordered range of at most {self.settings.max_days} days required."
            ) from None
        return lo, hi

    def _token(self, data) -> str:
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.digest(self._cursor_key, payload, "sha256")
        return base64.urlsafe_b64encode(signature + payload).decode()

    def _decode(self, token, expected):
        if not isinstance(token, str) or len(token) > 4096:
            raise ToolError("INVALID_CURSOR: use next_cursor from this running service.")
        try:
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            sig, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(sig, hmac.digest(self._cursor_key, payload, "sha256")):
                raise ValueError
            data = json.loads(payload)
            if any(data.get(key) != value for key, value in expected.items()):
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError):
            raise ToolError(
                "INVALID_CURSOR: cursor belongs to another request or service instance."
            ) from None

    def _record(self, msg, cid: int):
        text, truncated = clean(getattr(msg, "message", None), self.settings.max_text_chars)
        _, peer_type = utils.resolve_id(cid)
        link = None
        if peer_type == types.PeerChannel:
            link = f"https://t.me/c/{-cid-1000000000000}/{msg.id}"
        return {
            "chat_id": cid,
            "message_id": msg.id,
            "date": msg.date.isoformat(),
            "sender_id": getattr(msg, "sender_id", None),
            "sender": display(msg.sender) if getattr(msg, "sender", None) else None,
            "text": text,
            "text_truncated": truncated,
            "has_media": bool(getattr(msg, "media", None)),
            "reply_to_message_id": getattr(msg, "reply_to_msg_id", None),
            "link": link,
        }

    def _fit(self, response):
        """Bound the actual JSON bytes, including metadata and signed cursor."""

        def size():
            return len(json.dumps(response, ensure_ascii=False).encode("utf-8"))

        while size() > self.settings.max_output_bytes and response["results"]:
            response["results"].pop()
            response["output_truncated"] = True
        if response["output_truncated"] and not response["results"]:
            raise ToolError("OUTPUT_LIMIT: one record cannot fit the configured output limit.")
        response["returned_count"] = len(response["results"])
        if size() > self.settings.max_output_bytes:
            raise ToolError("OUTPUT_LIMIT: narrow request.")
        return response

    async def list_chats(self, limit=50, cursor=None):
        async with self.operation():
            limit = self._limit(limit, 200)
            offset = self._decode(cursor, {"tool": "chats"})["offset"] if cursor else 0
            if type(offset) is not int or not 0 <= offset < self.settings.max_dialogs:
                raise ToolError("INVALID_CURSOR: dialog scan limit reached.")
            records, scanned, exhausted, has_more = [], 0, True, False
            async for dialog in self.client.iter_dialogs(limit=self.settings.max_dialogs + 1):
                if scanned >= self.settings.max_dialogs:
                    exhausted = False
                    break
                scanned += 1
                if scanned <= offset:
                    continue
                cid = utils.get_peer_id(dialog.entity)
                if not self.settings.permits(cid):
                    continue
                if len(records) == limit:
                    has_more = True
                    exhausted = False
                    break
                records.append(
                    {
                        "chat_id": cid,
                        "name": display(dialog.entity),
                        "type": kind(dialog.entity),
                        "_position": scanned,
                    }
                )
            response = self._fit(
                {
                    "results": records,
                    "output_truncated": False,
                    "returned_count": len(records),
                    "content_is_untrusted": True,
                    "next_cursor": self._token(
                        {"tool": "chats", "offset": self.settings.max_dialogs}
                    ),
                    "scan_limit_reached": not exhausted and not has_more,
                    "ordering_note": "Telegram dialog order may change between pages. Deduplicate by chat_id.",
                }
            )
            last = records[-1]["_position"] if records else offset
            response["next_cursor"] = (
                self._token({"tool": "chats", "offset": last})
                if (
                    records
                    and (has_more or response["output_truncated"])
                    and last < self.settings.max_dialogs
                )
                else None
            )
            response["scan_limit_reached"] = (
                not exhausted and not has_more and not response["output_truncated"]
            )
            for record in records:
                record.pop("_position")
            return response

    async def get_chat(self, chat_id: int):
        async with self.operation():
            entity = await self._entity(chat_id)
            return {
                "chat_id": chat_id,
                "name": display(entity),
                "type": kind(entity),
                "content_is_untrusted": True,
            }

    async def list_messages(
        self, chat_id, limit=100, from_date=None, to_date=None, cursor=None, query=None
    ):
        async with self.operation():
            limit = self._limit(limit, self.settings.max_messages)
            if query is not None and (not isinstance(query, str) or not 1 <= len(query) <= 256):
                raise ToolError("INVALID_QUERY: query must contain 1..256 characters.")
            # Cursor carries original dates so defaults do not shift on the next page.
            if cursor:
                data = self._decode(cursor, {"tool": "messages", "chat": chat_id, "query": query})
                lo, hi = self._window(data["start"], data["end"])
                if from_date is not None or to_date is not None:
                    supplied_lo, supplied_hi = self._window(
                        from_date or data["start"], to_date or data["end"]
                    )
                    if (lo, hi) != (supplied_lo, supplied_hi):
                        raise ToolError("INVALID_CURSOR: date range changed.")
                before = data["before"]
            else:
                lo, hi = self._window(from_date, to_date)
                before = 0
            entity = await self._entity(chat_id)
            records, has_more, scan_limited = [], False, False
            # Bound API work even if search yields many out-of-window records.
            scan = self.settings.max_messages * 3 + 1
            scanned, last_scanned = 0, before
            async for msg in self.client.iter_messages(
                entity, limit=scan, offset_id=before, offset_date=hi, search=query
            ):
                scanned += 1
                last_scanned = msg.id
                if msg.date < lo:
                    break
                if msg.date >= hi:
                    continue
                if len(records) == limit:
                    has_more = True
                    break
                records.append(self._record(msg, chat_id))
            else:
                scan_limited = scanned == scan
            base = {
                "tool": "messages",
                "chat": chat_id,
                "query": query,
                "start": lo.isoformat(),
                "end": hi.isoformat(),
            }
            response = {
                "results": records,
                "chat_id": chat_id,
                "from_inclusive": lo.isoformat(),
                "to_exclusive": hi.isoformat(),
                "timezone": self.settings.timezone,
                "content_is_untrusted": True,
                "output_truncated": False,
                "scan_limit_reached": scan_limited,
                "returned_count": len(records),
            }
            # Reserve worst-case cursor/metadata space before deciding pagination.
            response["next_cursor"] = self._token(dict(base, before=2**31 - 1))
            response = self._fit(response)
            next_id = records[-1]["message_id"] if records else last_scanned
            response["next_cursor"] = (
                self._token(dict(base, before=next_id))
                if (next_id and (has_more or scan_limited or response["output_truncated"]))
                else None
            )
            return response

    async def get_message_context(
        self, chat_id, message_id, context_size=3, from_date=None, to_date=None
    ):
        async with self.operation():
            if type(context_size) is not int or not 0 <= context_size <= 20:
                raise ToolError("INVALID_LIMIT: context_size must be 0..20.")
            context_size = min(context_size, (self.settings.max_messages - 1) // 2)
            if type(message_id) is not int or not 1 <= message_id < 2**31:
                raise ToolError("INVALID_MESSAGE_ID.")
            lo, hi = self._window(from_date, to_date)
            entity = await self._entity(chat_id)
            msg = await self.client.get_messages(entity, ids=message_id)
            if msg is None:
                raise ToolError("MESSAGE_NOT_FOUND.")
            if not lo <= msg.date < hi:
                raise ToolError("OUTSIDE_DATE_RANGE: choose a period containing the message.")
            records = [self._record(msg, chat_id)]
            async for item in self.client.iter_messages(
                entity, limit=context_size, max_id=message_id
            ):
                if lo <= item.date < hi:
                    records.append(self._record(item, chat_id))
            async for item in self.client.iter_messages(
                entity, limit=context_size, min_id=message_id, reverse=True
            ):
                if lo <= item.date < hi:
                    records.append(self._record(item, chat_id))
            response = self._fit(
                {
                    "results": records,
                    "returned_count": len(records),
                    "central_message_id": message_id,
                    "from_inclusive": lo.isoformat(),
                    "to_exclusive": hi.isoformat(),
                    "content_is_untrusted": True,
                    "output_truncated": False,
                }
            )
            response["results"].sort(key=lambda item: item["message_id"])
            return response
