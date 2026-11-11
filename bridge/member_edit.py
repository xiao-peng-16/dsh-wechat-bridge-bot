# -*- coding: utf-8 -*-
"""查 / 改「群友信息表.xlsx」。

宿主用 stdin 传 JSON；所有定位均按表头映射，不依赖列顺序。旧表头“昵称”只用于兼容读取。
"""

import json
import os
import sys

允许字段 = [
    "微信昵称", "年龄_出生年份", "性别", "星座", "MBTI", "地区", "身高",
    "学历", "职业", "个人信息", "理想型", "大肥鱼对用户印象", "补充", "是否有对象", "群昵称",
]
默认返回列 = ["微信昵称", "年龄_出生年份", "性别", "地区", "理想型", "是否有对象"]


def 读表头(ws):
    return {str(c.value or "").strip(): i for i, c in enumerate(ws[1]) if str(c.value or "").strip()}


def 微信昵称列(hdr):
    return hdr.get("微信昵称", hdr.get("昵称"))


def 取列值(cells, hdr, 列):
    i = hdr.get(列)
    if i is None:
        return ""
    if i >= len(cells):
        return ""
    return str(cells[i] or "").strip()


def 找行(ws, 名):
    try:
        from .member_identity import find_member_row
    except ImportError:
        from member_identity import find_member_row
    hdr = 读表头(ws)
    # The tool accepts one selector; check it against all three fields in order.
    for kwargs in ({'wxid': 名}, {'wechat_nickname': 名}, {'group_nickname': 名}):
        row_number = find_member_row(ws, **kwargs)
        if row_number is not None:
            return ws[row_number], hdr
    return None, hdr


def 年份数(s):
    s = str(s or "").strip()
    if not s.isdigit():
        return None
    n = int(s)
    return 1900 + n if n < 100 and n >= 70 else 2000 + n if n < 100 else n


def 查(ws, 筛选, 要的列, 上限):
    hdr = 读表头(ws)
    返回列 = [x for x in 要的列 if x in hdr] or [x for x in 默认返回列 if x in hdr]
    性别 = 筛选.get("性别") or ""
    地区 = 筛选.get("地区") or ""
    有对象 = 筛选.get("有对象") or ""
    年份从 = str(筛选.get("年份从") or "").strip()
    年份到 = str(筛选.get("年份到") or "").strip()
    关键词 = 筛选.get("关键词") or ""
    结果, 扫描 = [], 0
    for 行号, row in enumerate(ws.iter_rows(min_row=2), start=2):
        cells = [c.value for c in row]
        昵称 = 取列值(cells, hdr, "微信昵称") or 取列值(cells, hdr, "昵称")
        if not 昵称:
            continue
        扫描 += 1
        性别值 = 取列值(cells, hdr, "性别")
        地区值 = 取列值(cells, hdr, "地区")
        对象值 = 取列值(cells, hdr, "是否有对象")
        年份 = 取列值(cells, hdr, "年龄_出生年份")
        if 性别 and 性别值 != 性别:
            continue
        if 地区 and 地区 not in 地区值:
            continue
        if 有对象 == "有" and 对象值 != "有":
            continue
        if 有对象 == "无" and 对象值 == "有":
            continue
        if 年份从 and not (年份数(年份) and 年份数(年份) >= 年份数(年份从)):
            continue
        if 年份到 and not (年份数(年份) and 年份数(年份) <= 年份数(年份到)):
            continue
        if 关键词:
            values = [昵称] + [取列值(cells, hdr, k) for k in ("个人信息", "理想型", "补充", "职业", "群昵称")]
            if 关键词 not in " ".join(values):
                continue
        item = {k: 取列值(cells, hdr, k) for k in 返回列}
        item["行"] = 行号
        结果.append(item)
    超了 = len(结果) > 上限
    return (结果[:上限] if 超了 else 结果), 扫描, 超了


def 摘要(结果, 列):
    行们 = []
    for i, 项 in enumerate(结果, 1):
        parts = []
        for k in 列:
            v = 项.get(k) or ""
            if k == "理想型":
                v = v[:24]
            if v:
                parts.append("%s:%s" % (k, v))
        行们.append("%d. %s" % (i, "｜".join(parts)))
    return "\n".join(行们)


def main():
    try:
        d = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"ok": False, "说": "参数不是 JSON：%r" % e}, ensure_ascii=False))
        return 1
    表 = str(d.get("表") or "").strip()
    动作 = str(d.get("动作") or "").strip()
    if not 表 or not os.path.isfile(表):
        print(json.dumps({"ok": False, "说": "表格不存在：%s" % 表}, ensure_ascii=False))
        return 1
    try:
        import openpyxl
    except Exception as e:
        print(json.dumps({"ok": False, "说": "这台机器没装 openpyxl：%r" % e}, ensure_ascii=False))
        return 1

    if 动作 in ("查", "query", "list", "search"):
        wb = openpyxl.load_workbook(表, read_only=True)
        ws = wb.active
        筛选 = d.get("筛选") or {}
        if not isinstance(筛选, dict):
            筛选 = {}
        要的列 = d.get("字段") or []
        if isinstance(要的列, str):
            要的列 = [x.strip() for x in 要的列.replace("，", ",").split(",") if x.strip()]
        try:
            上限 = max(1, min(int(d.get("上限") or 30), 200))
        except Exception:
            上限 = 30
        结果, 扫描, 超了 = 查(ws, 筛选, 要的列, 上限)
        wb.close()
        条件文 = "、".join("%s=%s" % (k, v) for k, v in 筛选.items() if v) or "全部"
        头 = "共扫 %d 人，命中 %d 人（%s）" % (扫描, len(结果), 条件文)
        if 超了:
            头 += "，只列前 %d 个" % 上限
        输出列 = [x for x in (要的列 or 默认返回列) if x]
        正文 = 摘要(结果, 输出列) if 结果 else "（一个都没命中）"
        print(json.dumps({"ok": True, "说": 头 + "\n" + 正文, "人数": len(结果), "行": 结果}, ensure_ascii=False))
        return 0

    名 = str(d.get("名") or "").strip()
    字段 = str(d.get("字段") or "").strip()
    值 = str(d.get("值") or "").strip()
    模式 = str(d.get("模式") or "update").strip().lower()
    if not 名:
        print(json.dumps({"ok": False, "说": "缺「名」（微信昵称或 wxid）"}, ensure_ascii=False))
        return 1
    if 字段 not in 允许字段:
        print(json.dumps({"ok": False, "说": "这一列不能改：%s（能改：%s）" % (字段, "、".join(允许字段))}, ensure_ascii=False))
        return 1
    if 字段 == "微信昵称" and 模式 != "upsert":
        print(json.dumps({"ok": False, "说": "微信昵称是匹配字段，不直接覆盖；要加叫法用「 / 」拼在值里、或先 upsert"}, ensure_ascii=False))
        return 1
    if not 值:
        print(json.dumps({"ok": False, "说": "值不能是空"}, ensure_ascii=False))
        return 1

    wb = openpyxl.load_workbook(表)
    ws = wb.active
    row, hdr = 找行(ws, 名)
    nick_col = 微信昵称列(hdr)
    wxid_col = hdr.get("wxid")
    if row is None:
        if 模式 != "upsert":
            print(json.dumps({"ok": False, "说": "表里没有「%s」这一行；要新增就带 upsert" % 名}, ensure_ascii=False))
            return 1
        row = ws[ws.max_row + 1]
        if nick_col is not None:
            row[nick_col].value = 名
    if 字段 not in hdr:
        print(json.dumps({"ok": False, "说": "表里没有「%s」这一列" % 字段}, ensure_ascii=False))
        return 1
    row[hdr[字段]].value = 值
    wb.save(表)
    print(json.dumps({"ok": True, "说": "%s 的「%s」改好了" % (名, 字段), "行": row[0].row}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
