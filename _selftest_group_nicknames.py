import sqlite3
import tempfile
import unittest
from pathlib import Path

from bridge.group_nicknames import get_group_nickname_status, load_group_nickname_map, resolve_mention_name


def _varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number, payload):
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


class GroupNicknameTests(unittest.TestCase):
    def _db(self, payload):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "contact.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT, owner TEXT, ext_buffer BLOB)")
            conn.execute("INSERT INTO chat_room VALUES (1, ?, '', ?)", ("room@chatroom", payload))
        return temp, path

    def test_current_layout_maps_username_to_card(self):
        username = "wxid_member123"
        record = _field(1, username.encode()) + _field(2, "群名片甲".encode())
        temp, path = self._db(_field(1, record))
        try:
            self.assertEqual(load_group_nickname_map(str(path), "room@chatroom", [username]), {username: "群名片甲"})
        finally:
            import gc
            gc.collect()
            temp.cleanup()

    def test_legacy_layout_maps_only_unambiguous_card(self):
        username = "wxid_member456"
        record = _field(4, username.encode()) + _field(1, "旧群名片".encode())
        temp, path = self._db(_field(1, record))
        try:
            self.assertEqual(load_group_nickname_map(str(path), "room@chatroom", [username]), {username: "旧群名片"})
        finally:
            import gc
            gc.collect()
            temp.cleanup()

    def test_status_only_counts_missing_group_card_not_contact_nickname(self):
        username = "wxid_member789"
        record = _field(1, username.encode()) + _field(2, "群名片丙".encode())
        temp, path = self._db(_field(1, record))

        class FakeDB:
            workdir = ""
            _db_files = []

            def get_group_members(self, chatroom):
                return [
                    {"username": username, "nick_name": "微信昵称"},
                    {"username": "wxid_member000", "nick_name": "另一个联系人名"},
                ]

        try:
            status = get_group_nickname_status(FakeDB(), "room@chatroom", str(path))
            self.assertEqual(status["nickname_map"], {username: "群名片丙"})
            self.assertEqual([m["username"] for m in status["unset"]], ["wxid_member000"])
        finally:
            import gc
            gc.collect()
            temp.cleanup()

    def test_mention_reads_latest_card_then_raw_nickname(self):
        username = 'wxid_member123'
        payload = _field(1, _field(1, username.encode()) + _field(2, '群名片'.encode()))
        temp, path = self._db(payload)
        class FakeDB:
            workdir = str(path.parent)
            _db_files = [('contact.db', str(path), 0)]
            def _contact_conn(self):
                return sqlite3.connect(path)
        try:
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)')
                conn.execute('INSERT INTO contact VALUES (?, ?, ?)', (username, '微信昵称', '不应使用的备注'))
            db = FakeDB()
            self.assertEqual(resolve_mention_name(db, 'room@chatroom', username), '群名片')
            with sqlite3.connect(path) as conn:
                conn.execute('UPDATE chat_room SET ext_buffer=?', (b'',))
                conn.execute('UPDATE contact SET nick_name=?', ('实时新昵称',))
            self.assertEqual(resolve_mention_name(db, 'room@chatroom', username), '实时新昵称')
            self.assertEqual(resolve_mention_name(db, 'room@chatroom', 'unknown'), '')
            conn.close()
        finally:
            import gc
            gc.collect()
            temp.cleanup()

    def test_non_group_or_missing_database_returns_empty(self):
        self.assertEqual(load_group_nickname_map("missing.db", "wxid_contact", ["wxid_member"]), {})


if __name__ == "__main__":
    unittest.main()
