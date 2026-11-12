# -*- coding: utf-8 -*-
"""Query and update structured member details in the bridge memory SQLite database."""

import json
import os
import sqlite3
import sys
import time

FIELDS = [
    "微信昵称", "年龄_出生年份", "性别", "星座", "MBTI", "地区", "身高",
    "学历", "职业", "个人信息", "理想型", "大肥鱼对用户印象", "补充", "是否有对象", "群昵称",
]
DEFAULT_FIELDS = ["微信昵称", "年龄_出生年份", "性别", "地区", "理想型", "是否有对象", "群昵称"]


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS member_details("
        "account TEXT, member_id TEXT, name TEXT DEFAULT '', details TEXT DEFAULT '{}',"
        "updated_at REAL DEFAULT 0, PRIMARY KEY(account, member_id))"
    )
    return con


def year_number(value):
    value = str(value or "").strip()
    if not value.isdigit():
        return None
    number = int(value)
    return 1900 + number if 70 <= number < 100 else 2000 + number if number < 100 else number


def query(con, account, filters, fields, limit):
    rows = con.execute(
        "SELECT member_id,name,details FROM member_details WHERE account=? ORDER BY name,member_id",
        (account,),
    ).fetchall()
    output_fields = [field for field in fields if field in FIELDS] or DEFAULT_FIELDS
    results = []
    scanned = 0
    for row in rows:
        try:
            details = json.loads(row["details"] or "{}")
        except (TypeError, ValueError):
            details = {}
        item = {"wxid": row["member_id"], "微信昵称": row["name"] or details.get("微信昵称", "")}
        item.update(details)
        if not item.get("微信昵称"):
            continue
        scanned += 1
        if filters.get("性别") and item.get("性别") != filters["性别"]:
            continue
        if filters.get("地区") and filters["地区"] not in str(item.get("地区", "")):
            continue
        if filters.get("有对象") == "有" and item.get("是否有对象") != "有":
            continue
        if filters.get("有对象") == "无" and item.get("是否有对象") == "有":
            continue
        low = year_number(filters.get("年份从"))
        high = year_number(filters.get("年份到"))
        year = year_number(item.get("年龄_出生年份"))
        if low is not None and (year is None or year < low):
            continue
        if high is not None and (year is None or year > high):
            continue
        keyword = str(filters.get("关键词") or "")
        if keyword and keyword not in " ".join(str(item.get(key, "")) for key in
                                                ("微信昵称", "群昵称", "个人信息", "理想型", "补充", "职业")):
            continue
        results.append({key: item.get(key, "") for key in output_fields if key == "wxid" or key in FIELDS})
    return results[:limit], scanned, len(results), len(results) > limit


def main():
    con = None
    try:
        request = json.load(sys.stdin)
        database = str(request.get("数据库") or "").strip()
        account = str(request.get("账号") or "").strip()
        action = str(request.get("动作") or "").strip()
        if not database or not account:
            raise ValueError("缺少记忆数据库或微信账号")
        con = connect(database)
        if action in ("查", "query", "list", "search"):
            filters = request.get("筛选") or {}
            if not isinstance(filters, dict):
                filters = {}
            fields = request.get("字段") or []
            if isinstance(fields, str):
                fields = [part.strip() for part in fields.replace("，", ",").split(",") if part.strip()]
            limit = max(1, min(int(request.get("上限") or 30), 200))
            results, scanned, matched, truncated = query(con, account, filters, fields, limit)
            con.close()
            con = None
            conditions = "、".join("%s=%s" % pair for pair in filters.items() if pair[1]) or "全部"
            header = "共查 %d 人，命中 %d 人（%s）" % (scanned, matched, conditions)
            if truncated:
                header += "，只列前 %d 个" % limit
            lines = []
            for index, item in enumerate(results, 1):
                values = ["%s:%s" % (key, str(item.get(key) or "")[:24] if key == "理想型" else item.get(key))
                          for key in (fields or DEFAULT_FIELDS) if item.get(key)]
                lines.append("%d. %s" % (index, "｜".join(values)))
            print(json.dumps({"ok": True, "说": header + "\n" + ("\n".join(lines) or "（一个都没命中）"),
                              "人数": matched, "行": results}, ensure_ascii=False))
            return 0

        member_id = str(request.get("名") or "").strip()
        if not request.get("主人"):
            raise ValueError("成员资料修改仅允许宿主授权")
        field = str(request.get("字段") or "").strip()
        value = str(request.get("值") or "").strip()
        mode = str(request.get("模式") or "update").strip().lower()
        if not member_id:
            raise ValueError("缺「名」（微信昵称或 wxid）")
        if field not in FIELDS or field == "微信昵称":
            raise ValueError("这一列不能改：%s（可改：%s）" % (field, "、".join(x for x in FIELDS if x != "微信昵称")))
        if not value:
            raise ValueError("值不能是空")
        row = con.execute(
            "SELECT member_id,name,details FROM member_details WHERE account=? AND member_id=?",
            (account, member_id),
        ).fetchone()
        if row is None:
            matches = con.execute(
                "SELECT member_id,name,details FROM member_details WHERE account=? AND name=? LIMIT 2",
                (account, member_id),
            ).fetchall()
            if len(matches) > 1:
                raise ValueError("微信昵称「%s」有重名，请使用 wxid" % member_id)
            row = matches[0] if matches else None
        if row is None and mode != "upsert":
            raise ValueError("记忆库里没有「%s」；要新增请带 upsert，且必须提供 wxid" % member_id)
        if row is None:
            if not member_id.startswith("wxid_"):
                raise ValueError("新增资料必须用微信 wxid 作为昵称参数")
            name = member_id
            details = {}
            actual_id = member_id
        else:
            name = row["name"] or member_id
            actual_id = row["member_id"]
            try:
                details = json.loads(row["details"] or "{}")
            except (TypeError, ValueError):
                details = {}
        details[field] = value
        con.execute(
            "INSERT INTO member_details(account,member_id,name,details,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(account,member_id) DO UPDATE SET name=excluded.name,details=excluded.details,updated_at=excluded.updated_at",
            (account, actual_id, name, json.dumps(details, ensure_ascii=False), time.time()),
        )
        con.commit()
        con.close()
        con = None
        print(json.dumps({"ok": True, "说": "%s 的「%s」改好了" % (name, field), "wxid": actual_id}, ensure_ascii=False))
        return 0
    except Exception as error:
        if con is not None:
            con.close()
        print(json.dumps({"ok": False, "说": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
