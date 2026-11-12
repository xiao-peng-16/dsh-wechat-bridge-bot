"""Regression tests for SQLite-backed member details (no workbook access)."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from bridge import member_edit


def run():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        con = member_edit.connect(str(db))
        con.execute(
            "INSERT INTO member_details(account,member_id,name,details) VALUES(?,?,?,?)",
            ("wxid_owner", "wxid_alice", "Alice", json.dumps({
                "微信昵称": "Alice", "群昵称": "群内名", "性别": "女", "地区": "福建厦门",
                "年龄_出生年份": "94", "理想型": "靠谱的人", "是否有对象": "无",
            }, ensure_ascii=False)),
        )
        con.commit()
        results, scanned, matched, truncated = member_edit.query(
            con, "wxid_owner", {"地区": "厦门", "年份从": "90", "年份到": "99"}, [], 30,
        )
        assert scanned == 1 and matched == 1 and not truncated
        assert results[0]["微信昵称"] == "Alice" and results[0]["群昵称"] == "群内名"
        assert member_edit.query(con, "wxid_someone_else", {}, [], 30)[0] == []
        con.execute(
            "INSERT INTO member_details(account,member_id,name,details) VALUES(?,?,?,?)",
            ("wxid_other", "wxid_bob", "Bob", json.dumps({"微信昵称": "Bob"})),
        )
        con.commit()
        assert member_edit.query(con, "wxid_owner", {}, [], 30)[0][0]["微信昵称"] == "Alice"
        assert member_edit.query(con, "wxid_other", {}, [], 30)[0][0]["微信昵称"] == "Bob"
        con.execute(
            "INSERT INTO member_details(account,member_id,name,details) VALUES(?,?,?,?)",
            ("wxid_owner", "wxid_alice_two", "Alice", json.dumps({"微信昵称": "Alice"})),
        )
        con.commit()
        limited, scanned, matched, truncated = member_edit.query(con, "wxid_owner", {}, [], 1)
        assert scanned == 2 and matched == 2 and truncated and len(limited) == 1
        con.close()
        con = None

        def call(payload):
            previous = sys.stdin
            output = io.StringIO()
            try:
                sys.stdin = io.StringIO(json.dumps({"数据库": str(db), "账号": "wxid_owner", **payload}))
                with contextlib.redirect_stdout(output):
                    code = member_edit.main()
            finally:
                sys.stdin = previous
            return code, json.loads(output.getvalue())

        code, response = call({"动作": "改", "主人": True, "名": "Alice", "字段": "星座", "值": "天秤座"})
        assert code != 0 and "重名" in response["说"]
        code, response = call({"动作": "改", "主人": True, "名": "wxid_alice", "字段": "星座", "值": "天秤座"})
        assert code == 0 and response["ok"]
        con = sqlite3.connect(db)
        saved = json.loads(con.execute("SELECT details FROM member_details WHERE member_id='wxid_alice'").fetchone()[0])
        assert saved["星座"] == "天秤座" and saved["地区"] == "福建厦门"
        con.close()

        code, response = call({"动作": "改", "名": "wxid_alice", "字段": "MBTI", "值": "INTJ"})
        assert code != 0 and not response["ok"]
        code, response = call({"动作": "改", "主人": True, "名": "wxid_new", "字段": "MBTI", "值": "INTJ", "模式": "upsert"})
        assert code == 0 and response["ok"]
        code, response = call({"动作": "改", "主人": True, "名": "New Name", "字段": "MBTI", "值": "INTJ", "模式": "upsert"})
        assert code != 0 and not response["ok"]
        code, response = call({"动作": "改", "名": "wxid_alice", "字段": "微信昵称", "值": "Other"})
        assert code != 0 and not response["ok"]
    print("SQLite member-details query/update/upsert/authorization regression passed")


if __name__ == "__main__":
    run()
