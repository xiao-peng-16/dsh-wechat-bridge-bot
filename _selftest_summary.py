# -*- coding: utf-8 -*-
"""离线演练：从 wechat_bridge.py 里抽出「历史转文字」的真实源码跑一遍。

不连微信库，只验证：同人连发合并、90 秒边界、不同人不并、非文本跳过、
总字上限、行内截断。跑法（在仓库根目录）：
  python _selftest_summary.py
"""
import os
import re
import sys
import textwrap
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge", "wechat_bridge.py")

_MENTION_ID = re.compile(r"wxid_[0-9a-zA-Z_-]+[:：]?")
_AT_TAG = re.compile(r"@[^\s@]{1,24}\s?")


def clean_text(text):
    t = _MENTION_ID.sub("", text or "")
    t = _AT_TAG.sub("", t)
    return " ".join(t.split()).strip()


class 假db:
    wxid = "me"


def 取函数():
    src = open(SRC, encoding="utf-8").read()
    i = src.index("    def 历史转文字(")
    j = src.index("\n    # ---------- 表情包", i)
    body = textwrap.dedent(src[i:j])
    ns = {
        "time": time,
        "clean_text": clean_text,
        "display_name": lambda sid, chatroom="": {"a": "小明", "b": "小红", "c": "小刚"}.get(sid, sid),
        "db": 假db,
    }
    exec(compile(body, "历史转文字", "exec"), ns)
    return ns["历史转文字"]


def 取时间函数():
    """抽「总结时间范围」那一整段真源码（含两个正则、中文数字、时段表）。"""
    src = open(SRC, encoding="utf-8").read()
    i = src.index("    _总结词 = re.compile(")
    j = src.index("\n    def 取库历史(", i)
    body = textwrap.dedent(src[i:j])
    ns = {"time": time, "re": re}
    exec(compile(body, "总结时间范围", "exec"), ns)
    return ns["总结时间范围"]


def 造(rows):
    return [{"type": "文本", "content": t, "create_time": ts, "sender_username": sid} for ts, sid, t in rows]


def main():
    历史转文字 = 取函数()
    失败 = []

    def 检查(名, 条件, 说明=""):
        if 条件:
            print("✓ " + 名)
        else:
            失败.append(名 + ("：" + 说明 if 说明 else ""))
            print("✗ " + 名 + ("：" + 说明 if 说明 else ""))

    # 同一人 30 秒内连发三条 → 一行，用 " / " 连
    rows = 造([
        (1000, "a", "我沉迷盲盒"), (1030, "a", "那段时间"), (1060, "a", "我甚至很少打开微信"),
    ])
    out = 历史转文字(rows)
    检查("同人连发并成一行", len(out) == 1, repr(out))
    检查("合并用的是「 / 」", "我沉迷盲盒 / 那段时间 / 我甚至很少打开微信" in out[0], repr(out))

    # 不同人，即便只隔 1 秒也不并
    rows = 造([(1000, "a", "一"), (1001, "b", "二"), (1002, "a", "三")])
    out = 历史转文字(rows)
    检查("不同人不合并", len(out) == 3, repr(out))

    # 同一个人，隔了 91 秒 → 不并
    rows = 造([(1000, "a", "一"), (1092, "a", "二")])
    out = 历史转文字(rows)
    检查("超过 90 秒不合并", len(out) == 2, repr(out))

    # 91 秒以内（90 整）→ 并
    rows = 造([(1000, "a", "一"), (1090, "a", "二")])
    out = 历史转文字(rows)
    检查("整整 90 秒仍合并", len(out) == 1, repr(out))

    # 非文本、空、<开头的跳过
    rows = [
        {"type": "图片", "content": "x", "create_time": 1000, "sender_username": "a"},
        {"type": "文本", "content": "   ", "create_time": 1001, "sender_username": "a"},
        {"type": "文本", "content": "<img/>", "create_time": 1002, "sender_username": "a"},
        {"type": "文本", "content": "真话", "create_time": 1003, "sender_username": "a"},
    ]
    out = 历史转文字(rows)
    检查("非文本 / 空 / < 开头都跳过", len(out) == 1 and "真话" in out[0], repr(out))

    # 行内截断：单条超长合并后截到 120 字 + …
    rows = 造([(1000, "a", "长" * 200)])
    out = 历史转文字(rows)
    检查("超长行被截断", out[0].endswith("…") and "长" * 120 in out[0], repr(out[:1]))
    检查("截断后不超过 120 字", len(out[0].split("：", 1)[1]) <= 121, repr(out))

    # 总字上限：塞爆之后，后面的行不进、末尾给提示
    rows = 造([(1000 + i, "a" if i % 2 else "b", "字" * 50) for i in range(400)])
    out = 历史转文字(rows, 每行上限=120, 总字上限=800)
    检查("超总字上限时给出提示", any("没放进来" in x for x in out), repr(out[-2:]))
    检查("提示不是凭空多出来的", len([x for x in out if "没放进来" not in x]) > 0)

    # 我 的识别：sender == db.wxid 时显示「我」
    rows = 造([(1000, "me", "我自己"), (1060, "a", "别人")])
    out = 历史转文字(rows)
    检查("自己显示成「我」", out[0].endswith("我：我自己"), repr(out))
    检查("别人显示昵称", out[1].endswith("小明：别人"), repr(out))

    # 时间戳为 0 时不炸
    rows = 造([(0, "a", "没时间")])
    out = 历史转文字(rows)
    检查("时间戳为 0 不报错", len(out) == 1, repr(out))

    # —— 时间窗解析 ——
    总结时间范围 = 取时间函数()
    now = time.time()

    def 时长(起, 终):
        return 终 - 起

    f, 终, 标签 = 总结时间范围("总结一下今天下午3点到5点")
    检查("下午 3 点到 5 点 = 两小时", abs(时长(f, 终) - 7200) < 1, 标签)
    检查("标签带小时", "15:00-17:00" in 标签, 标签)

    f, 终, 标签 = 总结时间范围("总结昨晚8点到11点")
    检查("昨晚 8 点到 11 点 = 三小时", abs(时长(f, 终) - 10800) < 1, 标签)
    检查("昨晚也算昨天", 标签.startswith("昨天"), 标签)

    f, 终, 标签 = 总结时间范围("总结今天早上9点半到12点")
    检查("9 点半到 12 点 = 2.5 小时", abs(时长(f, 终) - 9000) < 1, 标签)
    检查("半点也认", "9:30-12:00" in 标签, 标签)

    f, 终, 标签 = 总结时间范围("总结今天下午三点到五点")
    检查("中文数字下午三点到五点", abs(时长(f, 终) - 7200) < 1, 标签)

    f, 终, 标签 = 总结时间范围("总结最近两小时")
    检查("最近两小时 ≈ 2 小时", abs(时长(f, 终) - 7200) < 2, 标签)
    检查("最近标签按小时写", 标签 == "最近2小时", 标签)

    f, 终, 标签 = 总结时间范围("总结最近30分钟")
    检查("最近 30 分钟", abs(时长(f, 终) - 1800) < 2, 标签)

    f, 终, 标签 = 总结时间范围("总结今天9:30到11:00")
    检查("HH:MM 区间 = 1.5 小时", abs(时长(f, 终) - 5400) < 1, 标签)

    f, 终, 标签 = 总结时间范围("总结一下")
    今天零点 = time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                           time.localtime().tm_mday, 0, 0, 0, 0, 0, -1))
    检查("没说时间 = 今天从 0 点到现在", abs(f - 今天零点) < 1 and 终 <= now + 1, 标签)

    f, 终, 标签 = 总结时间范围("总结这周")
    周一 = time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                       time.localtime().tm_mday - time.localtime().tm_wday, 0, 0, 0, 0, 0, -1))
    检查("这周从周一零点起", abs(f - 周一) < 1, 标签)

    f, 终, 标签 = 总结时间范围("总结前天上午")
    检查("前天上午 = 12 小时", abs(时长(f, 终) - 12 * 3600) < 1, 标签)
    检查("标签是前天上午", 标签 == "前天上午", 标签)

    f, 终, 标签 = 总结时间范围("总结昨天晚上8点")
    检查("单个时刻取一小时", abs(时长(f, 终) - 3600) < 1, 标签)
    检查("晚上 8 点 = 20:00", "20:00" in 标签, 标签)

    if 失败:
        print("\n有 %d 项没过：\n  %s" % (len(失败), "\n  ".join(失败)))
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
