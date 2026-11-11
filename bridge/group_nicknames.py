# -*- coding: utf-8 -*-
"""Read WeChat 4.x per-chatroom member display names from contact.db."""

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional


def load_group_nickname_map(contact_db_path: str, chatroom_id: str, sender_usernames: list[str]) -> dict[str, str]:
    """Best-effort mapping username -> group card, returning {} for unknown layouts."""
    chatroom = str(chatroom_id or "").strip()
    if not chatroom.endswith("@chatroom"):
        return {}
    targets = list(dict.fromkeys(str(value or "").strip() for value in sender_usernames if str(value or "").strip()))
    if not targets:
        return {}
    target_set = set(targets)

    def decode_varint(raw: bytes, offset: int) -> tuple[Optional[int], int]:
        value = shift = 0
        pos = int(offset)
        while pos < len(raw):
            byte = raw[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, pos
            shift += 7
            if shift > 63:
                return None, len(raw)
        return None, len(raw)

    def iter_fields(raw: bytes):
        idx = 0
        while idx < len(raw):
            tag, next_idx = decode_varint(raw, idx)
            if tag is None or next_idx <= idx:
                break
            idx = next_idx
            field_no, wire_type = tag >> 3, tag & 7
            if wire_type == 0:
                _, next_idx = decode_varint(raw, idx)
                if next_idx <= idx:
                    break
                idx = next_idx
            elif wire_type == 2:
                size, next_idx = decode_varint(raw, idx)
                if size is None or next_idx <= idx:
                    break
                idx = next_idx
                end = idx + size
                if end > len(raw):
                    break
                yield field_no, wire_type, raw[idx:end]
                idx = end
            elif wire_type == 1:
                idx += 8
            elif wire_type == 5:
                idx += 4
            else:
                break

    def strong_id(value: str) -> bool:
        return value.startswith(("wxid_", "gh_")) or value.endswith("@chatroom") or "@" in value

    def looks_like_username(value: str) -> bool:
        if strong_id(value):
            return True
        return (6 <= len(value) <= 32 and not re.search(r"\s", value)
                and re.match(r"^[A-Za-z][A-Za-z0-9_-]+$", value) is not None and not value.isdigit())

    def pick_display(strings: list[tuple[int, str]], target: str, allowed: set[int]) -> str:
        candidates = []
        for index, (field_no, value) in enumerate(strings):
            if field_no not in allowed or not value or value == target or strong_id(value):
                continue
            if "\n" in value or "\r" in value or len(value) > 64:
                continue
            score = (100 if field_no == 2 else 0) + (20 if not looks_like_username(value) else 0) + max(0, 32 - len(value))
            candidates.append((score * 1000 - index, value))
        return max(candidates)[1] if candidates else ""

    conn = None
    try:
        path = Path(contact_db_path).resolve()
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        row = conn.execute("SELECT ext_buffer FROM chat_room WHERE username = ? LIMIT 1", (chatroom,)).fetchone()
        if not row or not isinstance(row[0], (bytes, bytearray, memoryview)):
            return {}
        raw = bytes(row[0])
        if not raw:
            return {}
        records = []
        for _, wire, chunk in iter_fields(raw):
            if wire != 2 or not chunk:
                continue
            strings = []
            for field_no, inner_wire, value in iter_fields(chunk):
                if inner_wire != 2 or not value or len(value) > 256:
                    continue
                try:
                    text = value.decode("utf-8", "strict").strip()
                except UnicodeDecodeError:
                    continue
                if text:
                    strings.append((field_no, text))
            if strings:
                records.append(strings)

        current = any(field == 2 or (field == 1 and value in target_set)
                      for record in records for field, value in record)
        output: dict[str, str] = {}
        if current:
            for record in records:
                for target in (value for field, value in record if field == 1 and value in target_set and value not in output):
                    display = pick_display(record, target, {2})
                    if display:
                        output[target] = display
            return output

        legacy = [record for record in records if any(field == 4 and value in target_set for field, value in record)]
        if not legacy or any(not (values := [v for f, v in record if f == 1]) or any(looks_like_username(v) for v in values) for record in legacy):
            return {}
        for record in legacy:
            for target in (value for field, value in record if field == 4 and value in target_set and value not in output):
                display = pick_display(record, target, {1})
                if display:
                    output[target] = display
        return output
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def resolve_mention_name(db, chatroom_id: str, username: str) -> str:
    """Read current contact snapshot at send time; group card first, raw nickname second.

    Never reads Excel, cached display names, or contact remarks.
    """
    if not username:
        return ""
    conn = None
    try:
        conn = db._contact_conn()  # Refresh the decrypted snapshot through wechatauto.
        if conn is None:
            return ""
        row = conn.execute('SELECT nick_name FROM contact WHERE username=? LIMIT 1', (username,)).fetchone()
        nickname = str(row[0] or '').strip() if row else ''
        path = next(os.path.join(db.workdir, rel.replace(os.sep, '__'))
                    for rel, source, _ in db._db_files
                    if os.path.basename(source).lower() == 'contact.db')
        card = load_group_nickname_map(path, chatroom_id, [username]).get(username, '')
        return str(card or nickname).strip()
    except Exception:
        return ""
    finally:
        if conn is not None:
            conn.close()


def get_group_nickname_status(db, chatroom_id: str, contact_db_path: str | None = None) -> dict:
    """Return members, resolved cards, and users with no configured group card."""
    chatroom = str(chatroom_id or "").strip()
    if not chatroom.endswith("@chatroom"):
        return {"group": chatroom, "members": [], "nickname_map": {}, "unset": [], "error": "不是群聊"}
    conn = None
    try:
        members = db.get_group_members(chatroom) or []
        if not contact_db_path:
            contact_db_path = next(
                os.path.join(db.workdir, rel.replace(os.sep, "__"))
                for rel, path, _ in db._db_files
                if os.path.basename(path).lower() == "contact.db"
            )
        usernames = [str(member.get("username") or "").strip() for member in members]
        nickname_map = load_group_nickname_map(contact_db_path, chatroom, usernames)
        enriched = []
        unset = []
        for member in members:
            username = str(member.get("username") or "").strip()
            card = str(nickname_map.get(username) or "").strip()
            item = dict(member)
            item["group_nickname"] = card
            enriched.append(item)
            if not card:
                unset.append(item)
        return {"group": chatroom, "members": enriched, "nickname_map": nickname_map, "unset": unset, "error": ""}
    except Exception as exc:
        return {"group": chatroom, "members": [], "nickname_map": {}, "unset": [], "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()
