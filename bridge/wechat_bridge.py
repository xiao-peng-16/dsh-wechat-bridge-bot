#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH · 微信桥接（不注入、不修改微信）

原理：
  - 收：读微信 4.x 本地 SQLite 数据库（只读，要求微信处于登录状态）
  - 发：Windows UI 自动化 + 剪贴板，驱动微信窗口真的去打字发送
  - 中间：把消息推给 DSH 插件（POST /dsh-wechat-bridge/receive），
          再把待发的取回来（GET /dsh-wechat-bridge/outbox）

一般不用手跑 —— DSH 设置页里点「启动桥接」，插件会用自己建的 venv 起它。

手跑：
  <venv>\\Scripts\\python.exe bridge\\wechat_bridge.py --dsh http://127.0.0.1:3080
"""

import argparse
import base64
import hashlib
import html
import json
import os
import random
import re
import sys
import tempfile
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request

# 可选库：仓库根的 .tools/py-libs（比如 zstandard —— 引用/红包这类消息的 XML 是
# zstd 压缩的，没它就只能读到「[文件/链接/卡片]」占位）。桥接的 venv 有时装不进去，
# 所以允许把 wheel 解压在这儿，启动时挂进 sys.path。
_可选库 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".tools", "py-libs")
if os.path.isdir(_可选库) and _可选库 not in sys.path:
    sys.path.insert(0, _可选库)

# 有些嵌入式 Python 探测不到可写的临时目录（C 盘 TEMP 被安全软件拦住时会这样），
# 这里兜底指到桥目录下自己的 tmp/，否则 tempfile.gettempdir() 会直接抛 FileNotFoundError。
try:
    tempfile.gettempdir()
except Exception:
    _兜底临时 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tmp')
    try:
        os.makedirs(_兜底临时, exist_ok=True)
    except Exception:
        pass
    tempfile.tempdir = _兜底临时
    os.environ['TEMP'] = _兜底临时
    os.environ['TMP'] = _兜底临时

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "bridge.log")

# 可选的小补丁：同目录有 uia_compat.py 就加载（某些微信小版本需要）
UIA_COMPAT = True


BACKEND_LABEL = "CORE"


def log(*a):
    line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), BACKEND_LABEL,
                            " ".join(str(x) for x in a))
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


ARGS = None
DSH = ""
TOKEN = ""
ONLY_CHATS = []
SEND_ENABLED = True
SKIP_OLDER = 15.0
POLL_SEC = 2.0
# 群里只有被点名才转（正文里带这些词之一）。私聊不受这个限制。
ALIASES = []
DEFAULT_ALIASES = "大肥鱼,肥鱼,小肥鱼,小客服"

# 从 DSH 设置页拉的配置（每 30 秒刷一次）
CFG = {
    "后端": "sqlite_uia",
    "群上下文条数": 6,
    "群里只认点名": True,
    "追话句数": 3,
    "点名别名": [],
    "群聊管理": [],
    "每条字数上限": 60,
    "每条最多几条": 3,
    "日记": True,
    "日记目录": "",
    "总结提示词": "",
    "唤醒词": [],
    "话题规则": [],
    "记忆密钥": "",
    "记忆模型": "deepseek-flash",
    "记忆地址": "https://api.deepseek.com",
    "主人": [],
    "屏蔽": [],
}
CFG_TS = 0.0

# 微信里收到的图片 / 表情包落盘到这里，再把路径交给宿主去看图
INBOUND_DIR = os.path.join(
    os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
    "wechat-inbound",
)
VISION_CACHE = os.path.join(os.path.dirname(INBOUND_DIR), "wechat-vision-cache.json")
# 宿主版本 ≥1.2.0 时它自己会看图；老宿主就由桥接先看好再发文字
HOST_VERSION = (1, 1, 0)


def _活跃群列表():
    """直接从磁盘配置读「活跃群」—— 宿主 /state 里暂时还没这个字段。"""
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    path = os.path.join(home, "wechat-bridge-config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x).strip() for x in (data.get("活跃群") or []) if str(x).strip()]
    except Exception:
        return []


def refresh_config():
    """问宿主要一次配置。设置页改完不用重启桥接。"""
    global CFG, CFG_TS, ALIASES
    r = http_json("/dsh-wechat-bridge/state")
    c = (r or {}).get("配置") if isinstance(r, dict) else None
    if not isinstance(c, dict):
        return False
    try:
        # 点名别名：设置页填了就用设置页的，没填（空 / 缺字段）就保留启动时那份
        别名 = [str(x).strip() for x in (c.get("点名别名") or []) if str(x).strip()]
        if 别名:
            ALIASES = 别名
        CFG = {
            "后端": str(c.get("后端") or "sqlite_uia").strip().lower() or "sqlite_uia",
            "群上下文条数": max(0, min(50, int(c.get("群上下文条数") or 0))),
            "点名别名": 别名,
            "群里只认点名": bool(c.get("群里只认点名", True)),
            # 老宿主还没有这个字段时按 3 算（默认开）；明确填 0 才是关
            "追话句数": (3 if c.get("追话句数") is None
                          else max(0, min(20, int(c.get("追话句数") or 0)))),
            # 一条回复拆成几条发：字数上限（每段最长）+ 条数上限（0 = 不限）
            "每条字数上限": max(10, min(500, int(c.get("每条字数上限", 60) or 60))),
            "每条最多几条": max(0, min(20, int(c.get("每条最多几条", 3) or 0))),
            "日记": bool(c.get("日记", True)),
            "日记目录": str(c.get("日记目录") or "").strip(),
            "总结提示词": str(c.get("总结提示词") or "").strip(),
            "记忆密钥": str(c.get("记忆密钥") or "").strip(),
            "记忆模型": str(c.get("记忆模型") or "deepseek-flash").strip() or "deepseek-flash",
            "记忆地址": (str(c.get("记忆地址") or "https://api.deepseek.com").strip()
                         or "https://api.deepseek.com").rstrip("/"),
            "唤醒词": [str(x).strip() for x in (c.get("唤醒词") or []) if str(x).strip()],
            # 话题规则是 {词, 回} 的对象数组（老配置是「词 = 怎么回」的字符串），原样收着，
            # 别在这里 str() —— 那样 dict 会被转成 "{'词': ...}" 一串字，后面永远匹配不上。
            "话题规则": c.get("话题规则") or [],
            "主人": [str(x).strip() for x in (c.get("主人") or []) if str(x).strip()],
            "屏蔽": [str(x).strip() for x in (c.get("屏蔽") or []) if str(x).strip()],
            "群回复间隔秒": max(0.0, float(c.get("群回复间隔秒") or 0)),
            "绑定": c.get("绑定") or [],
            "群聊管理": c.get("群聊管理") if isinstance(c.get("群聊管理"), list) else [],
            "成员快照": bool(c.get("成员快照", True)),
            "成员快照秒": max(10.0, min(3600.0, float(c.get("成员快照秒") or 30))),
            "入群提醒": str(c.get("入群提醒") or "").strip(),
            "退群提醒": str(c.get("退群提醒") or "").strip(),
        }
        CFG["活跃群"] = _活跃群列表()
        CFG_TS = time.time()
        return True
    except Exception as e:
        log("解析配置失败：", repr(e))
        return False


_群专属字段 = (
    "群里只认点名", "追话句数", "群上下文条数", "点名别名", "话题规则",
    "群回复间隔秒", "日记", "总结提示词", "成员快照", "入群提醒", "退群提醒",
)


def _群规则(group_id, group_name, config=None):
    """按群底层 ID 优先、显示名其次取规则；未命中返回空字典。"""
    config = config if isinstance(config, dict) else CFG
    rules = config.get("群聊管理") or []
    gid, gname = str(group_id or "").strip(), str(group_name or "").strip()
    for wanted in (gid, gname):
        if not wanted:
            continue
        for rule in rules:
            if isinstance(rule, dict) and str(rule.get("群") or "").strip() == wanted:
                return rule
    return {}


def group_sender_allowed(rule, sender_id, nickname, owner=False):
    if owner or not rule:
        return True
    mode = rule.get("群聊权限") or ("所有人不可以聊" if rule.get("所有人可聊") is False else "所有人可以聊")
    if mode == "所有人不可以聊":
        return False
    if mode == "仅白名单可聊":
        members = rule.get("群白名单") or []
        return bool((sender_id and sender_id in members) or (nickname and nickname in members))
    return True


def resolve_group_settings(group_id, group_name, config=None, aliases=None):
    """返回某群的有效设置，规则字段按属性存在性覆盖全局值。"""
    config = config if isinstance(config, dict) else CFG
    base_aliases = aliases if aliases is not None else (config.get("点名别名") or ALIASES)
    values = {key: config.get(key) for key in _群专属字段}
    values["点名别名"] = list(base_aliases or [])
    rule = _群规则(group_id, group_name, config)
    for key in _群专属字段:
        if key in rule:
            values[key] = rule[key]
    return values


def parse_args():
    global DSH, TOKEN, ONLY_CHATS, SEND_ENABLED, SKIP_OLDER, POLL_SEC, ALIASES

    p = argparse.ArgumentParser(description="DSH WeChat bridge")
    p.add_argument("--dsh", default=os.environ.get("DSH_ORIGIN", "http://127.0.0.1:3080"))
    p.add_argument("--token", default=os.environ.get("DSH_BRIDGE_TOKEN", ""))
    p.add_argument("--only", default="", help="只转这些聊天，逗号分隔（留空 = 全部）")
    p.add_argument("--aliases", default=DEFAULT_ALIASES, help="群里叫这些名字才算在跟我说话")
    p.add_argument("--no-send", action="store_true", help="只收不发")
    p.add_argument("--skip-older", type=float, default=15.0)
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--backend", default=os.environ.get("DSH_BRIDGE_BACKEND", "sqlite_uia"),
                   choices=("sqlite_uia", "auto", "native_hook"),
                   help="底层后端；native_hook 仅预留，当前尚未实现")
    p.add_argument("--test-image", default="", help="只看一张本地图片然后退出（调视图用）")
    a = p.parse_args()

    DSH = str(a.dsh).rstrip("/")
    TOKEN = str(a.token or "")
    ONLY_CHATS = [x.strip() for x in str(a.only or "").split(",") if x.strip()]
    ALIASES = [x.strip() for x in str(a.aliases or "").split(",") if x.strip()]
    SEND_ENABLED = not a.no_send
    SKIP_OLDER = float(a.skip_older or 15.0)
    POLL_SEC = float(a.poll or 2.0)
    return a


# @ 某人时微信会把机器 id 塞进正文，看着像乱码 —— 洗掉
_MENTION_ID = re.compile(r"wxid_[0-9a-zA-Z_-]+[:：]?")
_AT_TAG = re.compile(r"@[^\s@]{1,24}\s?")


def clean_text(text):
    t = _MENTION_ID.sub("", text or "")
    t = _AT_TAG.sub("", t)
    return " ".join(t.split()).strip()


def _消息正文(raw):
    """群消息 raw 形如 "wxid_xxx:\\n正文"，取可见正文 —— 引用定位（target_text）用。
    引用/卡片类消息 raw 是整段 XML，正文在 <title> 里，别把 XML 当引用目标。"""
    s = str(raw or "")
    if s.lstrip().startswith("<?xml") or "<appmsg" in s:
        m = re.search(r"<title>(.*?)</title>", s, re.S)
        if m:
            return clean_text(html.unescape(m.group(1)))
        return ""
    m = re.match(r"^wxid_[0-9a-zA-Z_-]+[:：]\s*\n?", s)
    if m:
        s = s[m.end():]
    return clean_text(s)


# ---------- HTTP ----------

def http_json(path, payload=None, method=None):
    url = DSH + path
    data = None
    headers = {"content-type": "application/json"}
    if TOKEN:
        headers["x-bridge-token"] = TOKEN
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "说": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "说": str(e)}


def main():
    global HOST_VERSION, ARGS, BACKEND_LABEL
    ARGS = parse_args()
    log("DSH 地址：", DSH, "｜只收不发：", not SEND_ENABLED)

    # 先拿一次配置，让后端选择成为统一入口；当前默认/唯一可用实现仍是 sqlite_uia。
    refresh_config()
    from backends import create_backend
    backend_name = str(getattr(ARGS, "backend", None) or CFG.get("后端") or "sqlite_uia")
    BACKEND_LABEL = "HOOK" if backend_name in ("native_hook", "hook") else "UIA"
    log("桥接后端：", backend_name, "｜收发链路：", BACKEND_LABEL)

    log("初始化微信桥接后端 …（第一次会慢）")
    try:
        backend = create_backend(backend_name, logger=log)
        backend.connect()
        db = backend.db

        # 拟人节流：默认 natural 档是「120 秒最多 6 次写」，撞上就冷却 60 秒左右。
        # 我这边一次回复要发好几段，很容易撞线 —— 撞线时消息发不出去，
        # 上层会以为失败再重试，重试又撞线，就出现了「卡住」「重复」。
        # 调到 fast 档（120 秒 20 次），保留节奏，但不会把正常聊天卡死。
        try:
            from wechatauto import rhythm as _rhythm

            _rhythm.set_profile('fast')
            _rhythm.configure(burst=30)   # 120 秒内 30 次，别再把正常回复卡住
            _p = _rhythm.profile()
            log("发送节流档位：", _p.name, "（%d 秒内 %d 次）" % (int(_p.window), _p.burst))
        except Exception as _e:
            log("节流档位调整失败（不影响用）：", repr(_e))
    except Exception as e:
        log("✗ import wechatauto 失败：", repr(e))
        log("  依赖没装好 —— 回 DSH 设置页点「装依赖」")
        return 2

    if UIA_COMPAT:
        try:
            import uia_compat  # noqa: F401

            log("uia_compat.apply() =", uia_compat.apply())
        except ImportError:
            pass
        except Exception as e:
            log("⚠ uia_compat 出错（继续跑）：", repr(e))

    log("连接微信数据库（只读）…")
    try:
        # backend.connect() 已完成连接；这里保留独立错误边界，方便后续后端提供不同诊断。
        db = backend.db
    except Exception as e:
        log("✗ 连不上微信数据库：", repr(e))
        log("  检查：微信登录了吗？微信 → 设置 → 文件管理 → 默认保存位置")
        return 2
    me = backend.account_id()
    log("当前账号 wxid：", me)
    http_json("/dsh-wechat-bridge/state", {"账号": me})
    try:
        info = backend.self_info()
        log("昵称：", info.get("nick_name") or info.get("remark") or "?")
    except Exception:
        pass

    name_cache = {}
    group_name_cache = {}
    group_card_cache = {}
    from member_snapshot import snapshot_path, member_records, load_snapshot, save_snapshot, diff, render_lines
    snapshot_root = os.environ.get('DSH_HOME') or os.path.join(os.path.expanduser('~'), '.dsh')
    snapshot_state = {}

    def display_name(username, chatroom=""):
        username = str(username or "")
        chatroom = str(chatroom or "")
        cache_key = (chatroom, username) if chatroom else username
        cache = group_name_cache if chatroom else name_cache
        if cache_key in cache:
            return cache[cache_key]
        name = ""
        try:
            if username.endswith("@chatroom"):
                name = db.group_id_to_name(username) or ""
            elif chatroom.endswith("@chatroom"):
                member_map = group_card_cache.get(chatroom)
                if member_map is None:
                    member_map = {}
                    try:
                        from group_nicknames import load_group_nickname_map
                        members = db.get_group_members(chatroom) or []
                        usernames = [str(member.get("username") or "") for member in members]
                        contact_path = next(
                            os.path.join(db.workdir, rel.replace(os.sep, "__"))
                            for rel, path, _ in db._db_files
                            if os.path.basename(path).lower() == "contact.db"
                        )
                        member_map = load_group_nickname_map(contact_path, chatroom, usernames)
                    except Exception as e:
                        log("读取群名片失败（回退联系人名）：", repr(e))
                    group_card_cache[chatroom] = member_map
                name = member_map.get(username, "")
                if not name:
                    name = db.get_nickname(username) or ""
            else:
                name = db.get_nickname(username) or ""
        except Exception:
            name = ""
        if name:
            cache[cache_key] = name
        return name

    # ---------- 群回复频率 ----------

    chat_hits = {}      # 群名 -> [时间戳]
    # 追话计数：本鱼在这个群说完话后，数一数后面跟了几句（发完回复就清零）
    追话计数 = {}
    # 本鱼在每个群最后说的那句话：认领判断靠它来认「是不是在接本鱼的话」
    最后回复 = {}

    def 报给设置页(msg):
        """桥接自己的 log() 只写 bridge.log；限流这类主人想在设置页直接看到的，
        再顺带报给宿主 /log，记进设置页那个日志区。"""
        try:
            http_json("/dsh-wechat-bridge/log", {"说": msg})
        except Exception:
            pass

    def 频率允许(chat, 词="", 窗口=0, 次数=0, 设置=None):
        """同一个群、同一个话题词各算各的冷却：A 词刚接过，不该拦着 B 词。
        每个词还能自己配「窗口秒内最多几次」——配了就以它为准，不再叠全局间隔；
        没配（窗口/次数为 0）才用全局「群回复间隔秒」。"""
        gap = (设置 or {}).get("群回复间隔秒") if 设置 is not None else CFG.get("群回复间隔秒")
        gap = gap or 0
        if 窗口 and 次数:
            # 词自己配了「窗口秒内最多几次」，就以它为准，全局间隔别再叠一道
            gap = 0
        if not gap and not (窗口 and 次数):
            return True
        键 = ("%s|%s" % (chat, 词)) if 词 else chat
        标签 = ("%s「%s」" % (chat, 词)) if 词 else chat
        now = time.time()
        hits = [t for t in chat_hits.get(键, []) if now - t < 3600]
        if gap and hits and now - hits[-1] < gap:
            剩 = gap - (now - hits[-1])
            话 = "群频率：%s 刚回过，还要等 %.0f 秒才再接（间隔 %.0f 秒）" % (标签, 剩, gap)
            log(话)
            报给设置页(话)
            return False
        if 窗口 and 次数:
            最近 = [t for t in hits if now - t < 窗口]
            if len(最近) >= 次数:
                话 = "群频率：%s 最近 %d 秒内已经接了 %d 次（上限 %d 次）" % (标签, 窗口, len(最近), 次数)
                log(话)
                报给设置页(话)
                return False
        hits.append(now)
        chat_hits[键] = hits
        return True

    # ---------- 屏蔽 / 主人 / 日记 / 群上下文 ----------

    def is_blocked(chat, nickname, cid=""):
        # 屏蔽名单三种写法都认：显示名（群名/昵称）、群里的 wxid、会话 id。
        # 只认显示名的话，一改备注就漏，所以底层那个码永远多认一份。
        for b in CFG["屏蔽"]:
            if b and (b == chat or b == nickname or (cid and b == cid)):
                return True
        return False

    def is_master(sender_id, nickname):
        # 主人权限只认 sender wxid，改昵称或群名片不能冒领。
        return bool(sender_id and sender_id in CFG["主人"])

    def diary_path(chat, 设置=None):
        base = ((设置 or {}).get("日记目录") if 设置 is not None else CFG.get("日记目录")) or os.path.join(os.path.expanduser("~"), ".dsh", "wechat-diary")
        day = time.strftime("%Y-%m-%d")
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", chat).strip("_")[:60] or "群"
        return os.path.join(base, day, safe + ".md")

    def diary_append(chat, ts, who, content, 设置=None):
        p = diary_path(chat, 设置)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            new = not os.path.exists(p)
            with open(p, "a", encoding="utf-8") as f:
                if new:
                    f.write("# %s · %s\n\n" % (chat, time.strftime("%Y-%m-%d")))
                t = time.strftime("%H:%M", time.localtime(ts or time.time()))
                f.write("- %s %s：%s\n" % (t, who or "某人", content))
        except Exception as e:
            log("写日记失败：", repr(e))

    def group_context(username, chat, limit):
        """那群最近在聊什么 —— 拼成一段给模型看。"""
        if limit <= 0:
            return ""
        try:
            rows = db.get_messages(username, limit=limit + 2)
        except Exception as e:
            log("取群上下文失败：", repr(e))
            return ""
        if not rows:
            return ""
        try:
            rows = sorted(rows, key=lambda m: float(m.get("create_time") or 0))
        except Exception:
            pass
        rows = rows[:-1][-limit:]      # 最后一条就是当前这句，去掉
        lines = []
        for m in rows:
            if (m.get("type") or "") != "文本":
                continue
            txt = clean_text(m.get("content") or "")
            if not txt or txt.startswith("<"):
                continue
            ts = float(m.get("create_time") or 0)
            t = time.strftime("%H:%M", time.localtime(ts)) if ts else "??:??"
            sid = m.get("sender_username") or ""
            lines.append("[%s] %s：%s" % (t, display_name(sid, username) or "某人", txt))
        if not lines:
            return ""
        return "（「%s」刚才在聊：\n%s\n）\n" % (chat, "\n".join(lines))

    # ---------- 「总结」：直接从微信库里按时间取，不读日记 md ----------
    #
    # 日记 md 只记桥接跑起来之后的消息，问「下午聊了啥」时常常是空的。
    # 所以碰到总结类请求，直接按时间窗去消息库里翻 —— 库里有本来就有历史。
    #
    # 光把记录丢给模型不够：它默认会写成逐条转述的流水账，把复读、造谣、反转全漏掉。
    # 下面这段是「怎么总结」的默认指令，设置里填「总结提示词」就能整段覆盖。

    DEFAULT_SUMMARY_GUIDE = (
        "【怎么写这份总结】\n"
        "1. 先通读，把这段时间压成 5~8 件真正发生过的事，每件起个短标题（如「恋爱嫌疑案」「八个学妹」）。\n"
        "2. 每件事写清楚：谁、发生了什么、转折在哪，以及最有梗的**原话**（一字不改地抄一两句）。"
        "复读刷屏、谐音梗、互相造谣、突然反转，这些是群的灵魂，别漏。\n"
        "3. 分清玩笑和事实：群友起哄「他跟八个学妹」这类是造谣玩梗，写成「被造谣 / 被起哄」，别当成真事。\n"
        "4. 不要逐条转述，不要「然后……然后……」，别把每句话都搬进来；人名保持原样，不确定是谁就别硬猜。\n"
        "5. 约局没凑齐、有人放鸽子、谁在加班、天气如何，这类日常小事往往是笑点，别当废话删掉。\n"
        "6. 结尾一句话收住，不升华、不说教。整体 8~15 行，用群聊口气，别写成会议纪要。"
    )

    _总结词 = re.compile(r"总结|汇总|梳理|回顾|复盘")
    _时间词 = re.compile(
        r"今天|今日|当天|上午|下午|晚上|昨晚|昨天|前天|早上|凌晨|中午|傍晚"
        r"|最近|刚才|这段|这几天|这两天|这周|本周|上周|群里|聊天|记录|点到|小时|分钟")

    def 是想总结(text):
        s = str(text or "")
        return bool(_总结词.search(s)) and bool(_时间词.search(s))

    def _当天零点(偏移天=0):
        n = time.localtime()
        return time.mktime((n.tm_year, n.tm_mon, n.tm_mday + 偏移天, 0, 0, 0, 0, 0, -1))

    def _中文数(s):
        """一 ~ 十二 转数字；够解析「九点」「十二点」就行。"""
        try:
            if str(s).isdigit():
                return int(s)
        except Exception:
            pass
        return {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9,
                "十": 10, "十一": 11, "十二": 12}.get(str(s))

    # 时段：词 → (起时, 止时, 标签)。顺序有讲究：「晚上」要在「上午」之前试，
    # 不然「晚上」里的「上」不会歧义，但「下午」和「上午」必须按整词匹配。
    _时段表 = (
        ("凌晨", 0, 6, "凌晨"),
        ("早上", 6, 12, "早上"),
        ("早晨", 6, 12, "早上"),
        ("中午", 11, 14, "中午"),
        ("下午", 12, 18, "下午"),
        ("傍晚", 17, 19, "傍晚"),
        ("晚上", 18, 24, "晚上"),
        ("今晚", 18, 24, "晚上"),
        ("晚间", 18, 24, "晚上"),
        ("上午", 0, 12, "上午"),
    )

    def 总结时间范围(text):
        """从一句话里猜时间窗：返回 (起始时间戳, 结束时间戳, 标签)。

        认这些说法（越具体越优先）：
          最近两小时 / 半小时内 / 刚才
          今天上午 9 点到 12 点、昨天下午 3:30-5:00、晚上 8 点到 11 点、早上 9 点半
          今天 / 昨天 / 前天 / 上周 / 本周 / 这几天 + 上午 / 下午 / 晚上 / 凌晨
        """
        s = str(text or "")
        现在 = time.time()
        n = time.localtime()

        # —— 先定基准日（整周的先走）——
        if "上周" in s:
            起 = _当天零点(-n.tm_wday - 7)
            return 起, 起 + 7 * 86400, "上周"
        if "这周" in s or "本周" in s or "一周" in s:
            return _当天零点(-n.tm_wday), 现在, "本周"
        if "这几天" in s:
            return 现在 - 3 * 86400, 现在, "最近三天"
        if "这两天" in s:
            return 现在 - 2 * 86400, 现在, "最近两天"
        if "前天" in s:
            零点, 日名 = _当天零点(-2), "前天"
        elif "昨天" in s or "昨晚" in s:
            零点, 日名 = _当天零点(-1), "昨天"
        else:
            零点, 日名 = _当天零点(0), "今天"

        # —— 最近 N 小时 / N 分钟（含「两」这种中文数字）——
        m = re.search(r"最近\s*(半|[\d一二两三四五六七八九十]+)\s*(小时|钟头|分钟|分)", s)
        if m:
            数 = 30 if m.group(1) == "半" else (_中文数(m.group(1)) or 1)
            if m.group(2) in ("小时", "钟头"):
                数 *= 60
            标签 = ("最近%d小时" % (数 // 60)) if 数 >= 60 and 数 % 60 == 0 else ("最近%d分钟" % 数)
            return 现在 - 数 * 60, 现在, 标签
        if "刚才" in s:
            return 现在 - 3 * 3600, 现在, "最近三小时"

        # —— 时段 ——
        时段 = None
        时段名 = ""
        for 词, 起时, 终时, 名 in _时段表:
            if 词 in s:
                时段, 时段名 = (起时, 终时), 名
                break
        if not 时段 and "昨晚" in s:
            时段, 时段名 = (18, 24), "晚上"

        # —— 显式时刻：9点 / 九点半 / 9:30 / 9点15分 ——
        时刻们 = []
        for m in re.finditer(
                r"(\d{1,2}|十二|十一|十|[一二两三四五六七八九])\s*点"
                r"(?:\s*(半|一刻|(\d{1,2})\s*分?))?", s):
            h = _中文数(m.group(1))
            if h is None or h > 24:
                continue
            mm = 0
            if m.group(2) == "半":
                mm = 30
            elif m.group(2) == "一刻":
                mm = 15
            elif m.group(3):
                mm = int(m.group(3))
            # 按「下午」「晚上」把 12 小时制掰正（「下午12点」仍算 12:00）
            if 时段 and 时段[0] >= 12 and h < 12:
                h += 12
            elif 时段 and 时段[0] >= 18 and h == 12:
                h = 24
            时刻们.append((h, mm))
        if not 时刻们:
            for m in re.finditer(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", s):
                时刻们.append((int(m.group(1)), int(m.group(2))))

        if len(时刻们) >= 2:
            起h, 起m = 时刻们[0]
            终h, 终m = 时刻们[1]
            起 = 零点 + 起h * 3600 + 起m * 60
            终 = 零点 + 终h * 3600 + 终m * 60
            if 终 <= 起:
                终 += 86400
            return 起, 终, "%s %d:%02d-%d:%02d" % (日名, 起h, 起m, 终h, 终m)
        if len(时刻们) == 1:
            起h, 起m = 时刻们[0]
            起 = 零点 + 起h * 3600 + 起m * 60
            return 起, 起 + 3600, "%s %d:%02d 起一小时" % (日名, 起h, 起m)

        if 时段:
            起时, 终时 = 时段
            return 零点 + 起时 * 3600, 零点 + 终时 * 3600, 日名 + 时段名

        return 零点, (现在 if 日名 == "今天" else 零点 + 86400), 日名

    def 取库历史(user, ts_from, ts_to, 上限=400):
        """从微信消息库按时间取一段：近→远翻页，翻到比起点还早就停。"""
        out = []
        off = 0
        try:
            while len(out) < 上限 and off < 8000:
                batch = db.get_messages(user, limit=100, offset=off)
                if not batch:
                    break
                for m in batch:
                    t = float(m.get("create_time") or 0)
                    if ts_from <= t <= ts_to:
                        out.append(m)
                off += len(batch)
                最老 = min(float(m.get("create_time") or 0) for m in batch)
                if 最老 < ts_from:
                    break
        except Exception as e:
            log("按时间读库失败：", repr(e))
            return []
        out.sort(key=lambda m: float(m.get("create_time") or 0))
        return out[-上限:]

    def 历史转文字(rows, 每行上限=120, 总字上限=12000, chatroom_username=""):
        try:
            我 = str(getattr(db, "wxid", "") or "")
        except Exception:
            我 = ""
        原始 = []
        for m in rows:
            if (m.get("type") or "") != "文本":
                continue
            txt = clean_text(m.get("content") or "")
            if not txt or txt.startswith("<"):
                continue
            ts = float(m.get("create_time") or 0)
            sid = str(m.get("sender_username") or "")
            who = "我" if (sid and 我 and sid == 我) else (display_name(sid, chatroom_username) or "某人")
            原始.append((ts, who, txt))

        # 同一个人一口气发的几条（90 秒内）并成一行。群聊里这种连发极多：
        # 「我沉迷盲盒」「那段时间」「我甚至很少打开微信」拆成三行，模型读起来像三个人抢话，
        # 写出来就是流水账；并成一行才看得出是一个人把一件事说完。
        合并 = []
        for ts, who, txt in 原始:
            if 合并 and 合并[-1][1] == who and ts - 合并[-1][3] <= 90:
                合并[-1][2].append(txt)
                合并[-1][3] = ts
            else:
                合并.append([ts, who, [txt], ts])

        lines = []
        用了 = 0
        for ts, who, txts, _ in 合并:
            txt = " / ".join(txts)
            if 用了 + len(txt) + 24 > 总字上限:
                continue
            t = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "??:??"
            if len(txt) > 每行上限:
                txt = txt[:每行上限] + "…"
            lines.append("[%s] %s：%s" % (t, who, txt))
            用了 += len(txt)
        if len(合并) > len(lines):
            lines.append("（记录太长，另外 %d 段没放进来）" % (len(合并) - len(lines)))
        return lines

    # ---------- 表情包：正文里的 [表情: 描述] 变成真图片 ----------

    # 和前端配图器（dsh-meme client.js 的 matchDesc）同一套规则，不然同一条回复
    # 在 Web 里配到的图，和微信里发出去的会是两张。
    _RE_MEME = re

    def _fold_caption(s):
        s = str(s or "").strip()
        s = _RE_MEME.sub(r'[\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u00ab\u00bb]', '"', s)
        return _RE_MEME.sub(r"\s+", " ", s)

    def _loose_caption(s):
        return _RE_MEME.sub(r'["\'\s]', "", _fold_caption(s))

    _TOKEN_SPLIT = _RE_MEME.compile(r'[\s,.!?;:\u3001\uff0c\u3002\uff01\uff1f\uff1b\uff1a"\u201c\u201d\'\u2018\u2019()\uff08\uff09\\[\\]\u3010\u3011\u300a\u300b\u2026\u00b7\uff5e~\\-]+')

    def _split_desc_tokens(s):
        parts = _TOKEN_SPLIT.split(_fold_caption(s))
        toks = [_RE_MEME.sub(r'["\']', "", p) for p in parts]
        toks = [t for t in toks if len(t) >= 2]
        toks.sort(key=len, reverse=True)
        return toks

    MEME = {"captions": {}, "keywords": {}, "rows": []}

    def load_memes():
        import glob
        import sqlite3

        dbs = []
        try:
            base = os.path.join(os.path.expanduser("~"), ".dsh", "profiles")
            dbs += glob.glob(os.path.join(base, "*", "node_modules", "dsh-meme", "memes", "*", "index.db"))
        except Exception:
            pass
        try:
            # dsh-meme 的默认图库目录：用户自己装/导入的图包都在这里，
            # 只读 profiles 里的内置库会漏掉一大半（鲸鱼娘那 138 张就在这）
            base2 = os.path.join(os.path.expanduser("~"), ".dsh", "meme-packs")
            dbs += glob.glob(os.path.join(base2, "*", "index.db"))
        except Exception:
            pass
        def 加键(表, key, path):
            raw = str(key or "").strip()
            if not raw:
                return
            for form in {raw, _fold_caption(raw), _loose_caption(raw)}:
                if form and form not in 表:
                    表[form] = path

        for dbp in dbs:
            pack = os.path.dirname(dbp)
            try:
                con = sqlite3.connect(dbp)
                for path, cap, kw, fn in con.execute("select path, caption, keywords, file_name from memes"):
                    absp = os.path.join(pack, str(path).replace("/", os.sep))
                    if not os.path.exists(absp):
                        continue
                    cap = str(cap or "").strip()
                    kw = str(kw or "")
                    fn = str(fn or "")
                    加键(MEME["captions"], cap, absp)
                    加键(MEME["captions"], fn, absp)
                    加键(MEME["captions"], cap[:80], absp)
                    加键(MEME["captions"], cap[:100], absp)
                    加键(MEME["keywords"], kw, absp)
                    for w in kw.split():
                        加键(MEME["keywords"], w, absp)
                    hay = []
                    parts = []
                    for s in (cap, kw, fn):
                        f = _fold_caption(s)
                        if not f:
                            continue
                        l = _loose_caption(s)
                        hay.append(f)
                        if l and l != f:
                            hay.append(l)
                        parts.append(s)
                    MEME["rows"].append({"hay": hay, "tokens": _split_desc_tokens(" ".join(parts)), "path": absp})
                con.close()
            except Exception as e:
                log("读表情库失败：", repr(e))
        if MEME["rows"]:
            log("表情库：%d 张（%s）" % (len(MEME["rows"]), "、".join(os.path.basename(os.path.dirname(d)) for d in dbs)))

    def find_meme(desc):
        """和前端 matchDesc 同一套优先级：caption 精确 > keyword 精确 > 分词最长命中。"""
        raw = str(desc or "").strip()
        if not raw:
            return ""
        forms = [raw, _fold_caption(raw), _loose_caption(raw)]
        for 表 in (MEME["captions"], MEME["keywords"]):
            for form in forms:
                hit = 表.get(form)
                if hit:
                    return hit
        needles = []
        for t in _split_desc_tokens(raw):
            l = _loose_caption(t)
            if l:
                needles.append(l)
        best, best_len, best_cnt = "", 0, 0
        for row in MEME["rows"]:
            longest = 0
            count = 0
            for needle in needles:
                length = len(needle) if any(needle in h for h in row["hay"]) else 0
                for tok in row["tokens"]:
                    t = _loose_caption(tok)
                    if t and (needle in t or t in needle):
                        length = max(length, min(len(needle), len(t)))
                if length:
                    longest = max(longest, length)
                    count += 1
            if longest > best_len or (longest == best_len and count > best_cnt):
                best, best_len, best_cnt = row["path"], longest, count
        return best

    # ---------- 发：从 DSH 取队列，用 UIA 发出去 ----------

    gui_box = {"gui": None}
    send_lock = threading.Lock()
    # 刚发出去的消息：同一个微信号，自己发的都会进库 ——
    # 靠这个列表把「我们发的」跟「主人自己在手机上打的」区分开，不然会自问自答。
    recently_sent = []
    # 上一次往外发的时间。文件传输助手发完立刻会被读回来，几秒内一律不看（防回环的兜底）
    STATE = {"last_send": 0.0}

    def 静音窗口操作(gui):
        """把 gui 上「会主动把微信拉到前台」的方法换成空操作。

        wechatauto 发送前习惯先 ensure_visible()（最小化遮挡窗口 + 置顶 +
        ShowWindow(SW_RESTORE)），这是窗口在后台发送时自己冒头的最大来源。
        UIA 写控件不需要窗口在前台，所以这里直接让它「当作已经可见」。
        图片发送走的是另一条显式前台路径（_ensure_wechat_ready(前台=True)），
        不受这里影响。
        """
        try:
            if getattr(gui, "_quiet_gui", False):
                return
            gui.ensure_visible = lambda *a, **k: True
            gui.bring_to_front = lambda *a, **k: True
            gui._minimize_blockers = lambda *a, **k: 0
            gui._quiet_gui = True
        except Exception as e:
            log("静音窗口操作失败（不影响发送）：", repr(e))

    def get_gui():
        if gui_box["gui"] is None:
            gui_box["gui"] = backend.make_gui()
            log("发送通道已就绪：%s" % backend.name)
        return gui_box["gui"]

    def _foreground_wechat():
        """把微信主窗口拉到前面。只在发送失败重试时用 —— 平时不抢你的前台。"""
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if found:
                win32gui.ShowWindow(found[0], 9)          # SW_RESTORE
                win32gui.SetForegroundWindow(found[0])    # 拉到前面
                time.sleep(0.5)
                return True
        except Exception as e:
            log("把微信窗口拉到前面失败：", repr(e))
        return False

    def _minimize_wechat():
        """发完把微信收起来 —— 不然它被拉到前台，主人正在干别的就被打断了。"""
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if found:
                win32gui.ShowWindow(found[0], 6)          # SW_MINIMIZE
                return True
        except Exception as e:
            log("最小化微信失败：", repr(e))
        return False

    def _强设前台(hwnd):
        """把窗口真正顶到前台。

        单纯的 SetForegroundWindow 会被 Windows 的前台锁定拒绝（调用进程不是
        当前前台进程时）。先用 AttachThreadInput 把自己的输入队列接到当前前台
        窗口上，这个限制就绕过去了。
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = user32.GetWindowThreadProcessId(hwnd, None)
            连上 = False
            if tid_fg and tid_me and tid_fg != tid_me:
                连上 = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                if 连上:
                    user32.AttachThreadInput(tid_me, tid_fg, False)
            return True
        except Exception as e:
            log("强设前台失败：", repr(e))
            return False

    def _取消置顶():
        h = STATE.pop("wx_hwnd", None)
        if not h:
            return
        try:
            import win32con
            import win32gui as wg

            wg.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass

    # ---------- 后台发送（学 whale-girl）：不抢前台、发完还原 ----------
    # 起因：wechatauto 的 WeChatUIA.ensure_window() 每次发送都会调 _activate() →
    #       把微信恢复并顶到最前面。实测微信最小化/被遮挡时其实照样能发，这步纯属
    #       多余，代价是主人用电脑时会被反复打断。这里学 whale-girl 的做法：
    #       文字走后台（UIA 直接写控件，不需要焦点），失败才抢前台重试；
    #       发完把窗口状态照原样还回去。

    def _微信窗口():
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            return found[0] if found else None
        except Exception:
            return None

    def _微信在前台():
        """微信此刻是不是前台窗口。全局回车（keybd_event）只在前台时才能按，
        否则会按到主人正在用的别的窗口里去。"""
        try:
            import ctypes

            h = _微信窗口()
            return bool(h) and ctypes.windll.user32.GetForegroundWindow() == h
        except Exception:
            return False

    def _快照窗口():
        """记下发送前窗口长什么样：句柄、是否最小化、当时的前台窗口。"""
        snap = {"hwnd": None, "iconic": False, "foreground": None, "visible": True}
        try:
            import ctypes
            import win32gui

            h = _微信窗口()
            if not h:
                return snap
            snap["hwnd"] = h
            snap["iconic"] = bool(win32gui.IsIconic(h))
            snap["visible"] = bool(win32gui.IsWindowVisible(h))
            snap["foreground"] = ctypes.windll.user32.GetForegroundWindow() or None
        except Exception:
            pass
        return snap

    def _还原窗口(snap):
        """把窗口状态还回去：原来最小化的继续最小化，前台还给原来那个窗口。"""
        if not snap:
            return
        try:
            import ctypes
            import win32gui

            h = snap.get("hwnd")
            if not h:
                return
            if not snap.get("visible", True):
                # 发送前微信是「收在托盘里」（窗口隐藏），发完要重新藏起来，
                # 别让它突然出现在屏幕上
                win32gui.ShowWindow(h, 0)          # SW_HIDE
                return
            if snap.get("iconic"):
                win32gui.ShowWindow(h, 6)          # SW_MINIMIZE
                return
            fg = snap.get("foreground")
            cur = ctypes.windll.user32.GetForegroundWindow()
            if fg and fg != cur and ctypes.windll.user32.IsWindow(fg):
                try:
                    ctypes.windll.user32.SetForegroundWindow(fg)
                except Exception:
                    pass
        except Exception:
            pass

    def _拉起窗口(snap):
        """引用要靠 OCR 截屏定位，窗口最小化时截不到图 —— 先恢复出来。
        发送结束由 send_one 的 finally 调 _还原窗口 按回去。"""
        try:
            import win32gui

            h = (snap or {}).get("hwnd")
            if h and win32gui.IsWindow(h) and win32gui.IsIconic(h):
                win32gui.ShowWindow(h, 9)          # SW_RESTORE
                time.sleep(0.8)
                # 旧引擎的窗口坐标是最小化时测的（-32000 那种），不清掉截屏区域还是错的
                gui_box["gui"] = None
                log("引用：微信窗口最小化着，先拉起来（OCR 要截屏）")
        except Exception as e:
            log("拉窗口失败：", repr(e))

    def _守护最小化(hwnd):
        """发送期间盯着窗口，看它有没有被谁拉出来（只记录，不动手）。

        以前是「一冒头就按回去」，结果切会话时新会话的输入框跟着丢焦点，
        字发不出去。收回最小化交给 send_one 的 finally（_还原窗口），
        那会儿发送已经结束，按回去不会再打断任何操作。
        """
        stopped = threading.Event()

        def 跑():
            报过 = False
            while not stopped.is_set():
                try:
                    import win32gui

                    if hwnd and win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd):
                        if not 报过:
                            log("窗口被拉起来了（发送过程中）")
                            报过 = True
                except Exception:
                    pass
                time.sleep(0.25)

        threading.Thread(target=跑, daemon=True).start()
        return stopped

    def _install_quiet_uia(u):
        """给 UIA 引擎打补丁：quiet_mode 下 _activate 直接返回，不抢前台。

        whale-girl 实测 18/18：微信最小化、被遮挡、前台全屏时都能正常发送，
        所以「把窗口拉到最前」这一步可以省掉。不改第三方库文件，只改实例。
        """
        try:
            import types

            if getattr(u, "_quiet_patch", False):
                u.quiet_mode = True
                return
            orig = getattr(u, "_activate", None)
            if orig is None:
                return

            def patched(self, win, _orig=orig):
                if getattr(self, "quiet_mode", False):
                    return
                return _orig(win)

            u._activate = types.MethodType(patched, u)

            # 还有一个漏网的：_force_foreground 会 ShowWindow(SW_RESTORE) +
            # SetForegroundWindow，把最小化的微信直接拉出来。安静模式下让它
            # 直接「假装成功」——UIA 写控件并不需要窗口在前台。
            if not getattr(u, "_quiet_ff", False):
                try:
                    u._force_foreground = lambda hwnd: True
                    u._quiet_ff = True
                except Exception:
                    pass

            # 点击补丁：wechatauto 的 _click_ctrl 会 SetCursorPos + 真实鼠标点击，
            # 切会话/搜索时屏幕上鼠标乱飞就是它。这里优先用 UIA 的 Invoke/Select，
            # 不碰鼠标；控件不支持时才退回原来的模拟点击。
            orig_click = getattr(u, "_click_ctrl", None)
            if orig_click is not None and not getattr(u, "_quiet_click", False):
                def quiet_click(self, ctrl, right=False, _orig=orig_click):
                    if right:
                        return _orig(ctrl, right=True)
                    err1 = err2 = None
                    try:
                        ctrl.GetInvokePattern().Invoke()
                        log("点击控件：走 Invoke（不动鼠标）")
                        return True
                    except Exception as e:
                        err1 = repr(e)
                    try:
                        ctrl.GetSelectionItemPattern().Select()
                        log("点击控件：走 Select（不动鼠标）")
                        return True
                    except Exception as e:
                        err2 = repr(e)
                    log("点击控件：退回真实鼠标点击（%s / %s）" % (err1, err2))
                    return _orig(ctrl, right=False)

                u._click_ctrl = types.MethodType(quiet_click, u)
                u._quiet_click = True

            u._quiet_patch = True
            u.quiet_mode = True
        except Exception as e:
            log("后台模式补丁没打上（不影响发送）：", repr(e))

    # UIA 起不来时的熔断。
    # wechatauto 的 UIA 一失败就退化成「按固定坐标盲点」：每秒两三次扫屏、枚举窗口、
    # 顺手最小化挡着的窗口 —— 主人看到的就是电脑卡成幻灯片，桌面还自己动。
    # 所以连着起不来几次就先停手，别让它一直空转。
    uia_坏 = {"连败": 0, "到": 0.0}

    def _记UIA失败(原因=""):
        uia_坏["连败"] += 1
        文本 = str(原因)
        缺 = ""
        if "No module named 'numpy'" in 文本 or "No module named \"numpy\"" in 文本:
            缺 = "（缺 numpy —— 设置 → 通到微信，点一次「装依赖」，装完重启桥接）"
        elif "No module named" in 文本:
            缺 = "（缺 Python 包 —— 设置 → 通到微信，点一次「装依赖」）"
        if uia_坏["连败"] >= 3 and time.time() >= uia_坏["到"]:
            uia_坏["到"] = time.time() + 300
            log(
                "UIA 连着 %d 次起不来，先停 5 分钟不再碰微信窗口（省得它盲点坐标乱点）%s"
                % (uia_坏["连败"], 缺)
            )

    def _uia熔断中():
        return time.time() < uia_坏["到"]

    def _取引擎():
        """每次发送都问一次最新的 UIA 引擎。

        微信跑久了会把 accessibility gate 那个字节归零，缓存的引擎会「假死」：
        还能收消息，但一条也发不出去，只能重启桥接 —— 每次重取能避开这个坑。

        注意：这里**不打 quiet 补丁**。后台静默那条路（_activate 直接返回 +
        _force_foreground 假装成功）会让窗口没真正激活、后面的点击和粘贴落空，
        切会话时直接把发送卡死。先回到原生行为：会冒头，但发得出去。
        """
        if _uia熔断中():
            return None
        try:
            gui = get_gui()
            u = gui._get_uia()
            if u is None:
                _记UIA失败("引擎给回 None")
                return None
            uia_坏["连败"] = 0
            uia_坏["到"] = 0.0
            return u
        except Exception as e:
            log("取 UIA 引擎失败：", repr(e))
            _记UIA失败(e)
            return None

    def _recover_uia(原因=""):
        """强制重新热激活 UIA（refresh=True），成功返回 True。"""
        if _uia熔断中():
            return False
        try:
            gui = get_gui()
            u = gui._get_uia(refresh=True)
            if u is not None:
                uia_坏["连败"] = 0
                uia_坏["到"] = 0.0
                log("UIA 已重新热激活" + (("（" + 原因 + "）") if 原因 else ""))
                return True
            _记UIA失败("热激活给回 None")
        except Exception as e:
            log("UIA 重新热激活异常：", repr(e))
            _记UIA失败(e)
        return False

    def _press_enter(次=2, ctrl=False):
        """底层按键补回车。

        UIA 的 SendKeys 有时根本不触发发送（字符进了框、回车没生效），
        这里用 keybd_event 硬按几次；ctrl=True 兼容微信里「Ctrl+Enter 发送」的设置。
        """
        try:
            import win32api
            import win32con

            for _ in range(次):
                if ctrl:
                    win32api.keybd_event(0x11, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
                if ctrl:
                    win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.12)
            return True
        except Exception as e:
            log("补回车失败：", repr(e))
            return False

    def _keyboard_send(文字, chat):
        """完全不依赖 UIA 的发送通道：Ctrl+F 搜会话 → 粘贴 → 回车 → 查库确认。

        UIA 假死时的第二条命。代价是要抢一下前台（键盘输入必须有焦点），
        所以只在 UIA 真的发不出去时用。
        """
        if not 文字:
            return False
        try:
            import win32con
            import win32gui

            h = _微信窗口()
            if not h:
                return False
            win32gui.ShowWindow(h, 9)
            _强设前台(h)
            time.sleep(0.5)
            import win32api

            def _粘贴(s):
                import win32clipboard

                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, s)
                finally:
                    win32clipboard.CloseClipboard()
                time.sleep(0.1)
                win32api.keybd_event(0x11, 0, 0, 0)      # Ctrl
                win32api.keybd_event(0x56, 0, 0, 0)      # V
                win32api.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.4)

            # Ctrl+F 打开搜索 → 输会话名 → 回车进会话
            win32api.keybd_event(0x11, 0, 0, 0)
            win32api.keybd_event(0x46, 0, 0, 0)
            win32api.keybd_event(0x46, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.6)
            _粘贴(chat)
            _press_enter(1)
            time.sleep(0.8)
            _粘贴(文字)
            _press_enter(2)
            _press_enter(2, ctrl=True)
            time.sleep(1.2)
            if _already_sent(文字, chat, within=40):
                return True
            _press_enter(2)
            time.sleep(1.2)
            return _already_sent(文字, chat, within=40)
        except Exception as e:
            log("键盘通道失败：", repr(e))
            return False

    def _ensure_wechat_ready(重试=2, 前台=False):
        """确认微信窗口能操作。

        默认只确认窗口在、不抢前台（文字用 UIA 直接写控件，不需要焦点）；
        需要粘贴图片、或文字发送失败重试时才传 前台=True 把窗口顶上来。
        """
        if not 前台:
            return _微信窗口() is not None
        for 第 in range(重试 + 1):
            try:
                import win32gui

                found = []

                def cb(hwnd, _):
                    try:
                        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                            found.append(hwnd)
                    except Exception:
                        pass
                    return True

                win32gui.EnumWindows(cb, None)
                if found:
                    h = found[0]
                    win32gui.ShowWindow(h, 9)          # SW_RESTORE
                    if not _强设前台(h):
                        # 退路：先最小化再恢复，有时也能把焦点拿回来
                        try:
                            win32gui.ShowWindow(h, 6)
                            time.sleep(0.12)
                            win32gui.ShowWindow(h, 9)
                            time.sleep(0.15)
                            win32gui.SetForegroundWindow(h)
                        except Exception:
                            pass
                    # 发送期间把微信置顶：DSH 窗口（我自己）一刷新就会盖上来，
                    # 这是「字卡在输入框」最主要的来源
                    try:
                        import win32con

                        win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                        STATE["wx_hwnd"] = h
                    except Exception:
                        pass
                    time.sleep(0.4)
                    return True
            except Exception as e:
                log("微信窗口准备失败：", repr(e))
            time.sleep(0.4)
        return False

    def 拆条(text: str, 上限: int = 26, 最多: int = 0):
        """把一段话拆成几条短消息：先按换行分段，长段再按句子拆。

        最多 > 0 时：拆出来超过这个条数，就把多出来的并进最后一条 ——
        群里一口气刷十几条太吵（设置里「一条回复最多几条」）。
        """
        out = []
        for 段 in re.split(r"\n+", str(text or "")):
            段 = 段.strip()
            if not 段:
                continue
            if len(段) <= 上限:
                out.append(段)
                continue
            句们 = re.split(r"(?<=[。！？!?…])", 段)
            cur = ""
            for 句 in 句们:
                if not 句:
                    continue
                if len(cur) + len(句) <= 上限:
                    cur += 句
                else:
                    if cur.strip():
                        out.append(cur.strip())
                    cur = 句
            if cur.strip():
                out.append(cur.strip())
        out = out or [str(text or "").strip()]
        if 最多 and len(out) > 最多:
            头 = out[:最多 - 1]
            尾 = "\n".join(out[最多 - 1:]).strip()
            out = 头 + [尾] if 尾 else 头
        return out


    def _chat_username(chat):
        """聊天名 → 数据库里的 username。群必须走 group_name_to_id（nickname 查不到群）。"""
        try:
            g = db.group_name_to_id(chat)
            if g:
                return g
        except Exception:
            pass
        try:
            return db.username_by_nickname(chat) or chat
        except Exception:
            return chat


    def _already_sent(text, chat, within=60):
        """库里最近已经有这句话 → 其实发出去了，别再点一次（点了就是重复消息）。"""
        try:
            u = _chat_username(chat)
            rows = db.get_messages(u, limit=12)
            now = time.time()
            for m in rows or []:
                ts = float(m.get("create_time") or 0)
                if not ts or abs(now - ts) > within:
                    continue
                c = clean_text(m.get("content") or "")
                if c and (c == text or text in c or c in text):
                    return True
        except Exception:
            pass
        return False

    # 进程内「这句我已经发出去过」的记录。数据库查询有 25/120 秒的时间窗，
    # 兜底通道晚几秒醒来、或者拆条后隔了几十秒再补发，窗口一过就会重复发一遍
    # （就是「老是发两次一模一样」）。这里用不过期的内存记录兜住。
    _发过的段 = {}

    def _记发过(text):
        try:
            k = clean_text(text)
            if k:
                _发过的段[k] = time.time()
        except Exception:
            pass

    def _段已发过(text, within=3600):
        try:
            k = clean_text(text)
            t = _发过的段.get(k)
            return bool(t) and (time.time() - t) <= within
        except Exception:
            return False


    def _localize_image(p):
        """正文 markdown 里的本机 HTTP 图（表情包服务等）先落成本地文件，再交给微信发。"""
        if not re.match(r"^https?://", p, re.I):
            return p if os.path.exists(p) else None
        from urllib.parse import urlparse

        u = urlparse(p)
        if (u.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            log("⚠ 不是本机的图，跳过：", p)
            return None
        ext = os.path.splitext(u.path)[1].lower()
        if ext not in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=ext, prefix="wxbridge_")
            os.close(fd)
            with urllib.request.urlopen(p, timeout=15) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            return tmp
        except Exception as e:
            log("⚠ 图下载失败：", p, repr(e))
            try:
                if tmp:
                    os.unlink(tmp)
            except Exception:
                pass
            return None

    def _send_image_dib(gui, path, chat):
        """发图：把图转成 CF_DIB 位图放剪贴板 → Ctrl+V → 回车。

        微信 4.x 不吃 wechatauto 的 CF_HDROP 路线（粘贴后草稿是空的，
        回车等于发了个寂寞，数据库里也不会有图片消息），但认 CF_DIB。
        GIF 只发第一帧（微信粘贴位图本来也不会动）。
        """
        import io
        import ctypes
        from PIL import Image

        try:
            im = Image.open(path)
            if getattr(im, "is_animated", False):
                im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
        except Exception as e:
            log("⚠ 图片打不开：", repr(e))
            return False
        buf = io.BytesIO()
        try:
            im.save(buf, format="BMP")
            dib = buf.getvalue()[14:]   # 去掉 BMP 文件头，剩下的就是 DIB
        except Exception as e:
            log("⚠ 图片转位图失败：", repr(e))
            return False

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        try:
            if not u32.OpenClipboard(None):
                log("⚠ 剪贴板打不开")
                return False
            try:
                u32.EmptyClipboard()
                h = k32.GlobalAlloc(0x0002 | 0x0040, len(dib))
                ptr = k32.GlobalLock(h)
                ctypes.memmove(ptr, dib, len(dib))
                k32.GlobalUnlock(h)
                if not u32.SetClipboardData(8, h):
                    log("⚠ 写剪贴板失败")
                    return False
            finally:
                u32.CloseClipboard()
        except Exception as e:
            log("⚠ 剪贴板异常：", repr(e))
            return False

        box = None
        for 第 in range(3):
            if 第 > 0:
                # 外层刚发完文字、窗口多半是好的；只有真探测不到时才重新摆窗口
                try:
                    _ensure_wechat_ready(前台=True)
                except Exception:
                    pass
            try:
                box = gui.get_input_box()
            except Exception as e:
                log("⚠ 探测输入框异常：", repr(e))
                box = None
            if box:
                break
            log("⚠ 发图探测不到输入框，第 %d 次重试" % (第 + 1))
            time.sleep(0.5)
        if not box:
            log("⚠ 发图时找不到输入框")
            return False
        try:
            gui.focus_input(box)
        except Exception as e:
            log("⚠ 聚焦输入框失败：", repr(e))
            return False
        time.sleep(0.2)
        gui._input.key(0x56, ctrl=True)   # VK_V
        time.sleep(0.8)
        gui._input.key(0x0D)              # VK_RETURN
        time.sleep(0.7)
        return True

    def _last_image_ts(chat):
        """聊天里最新一张图片消息的时间戳；没有就是 0。用来判断“刚才那张到底发出去没有”。"""
        try:
            u = _chat_username(chat)
            for m in db.get_messages(u, limit=8) or []:
                if m.get("type") == "图片":
                    return float(m.get("create_time") or 0)
        except Exception:
            pass
        return 0.0

    def _image_sent_recently(chat, since):
        """发完去库里确认一下：真出现图片消息才算发成了。"""
        try:
            u = _chat_username(chat)
            for m in db.get_messages(u, limit=4) or []:
                if m.get("type") == "图片" and float(m.get("create_time") or 0) >= since - 2:
                    return True
        except Exception:
            pass
        return False

    def _框里的字():
        """用 UIA 读输入框里的文字。读不到返回 None（表示"不知道"，不是"空"）。

        像素判据会把光标、占位提示、发送按钮都算成"有字"，据此重试会一直空转，
        所以优先问 UIA 要真实文本，读不到才退回像素判断。
        """
        try:
            gui = get_gui()
            uia = _取引擎()
            if uia is None:
                return None
            ctrl = uia._chat_input()
            if ctrl is None:
                return None
            r = uia._read_edit_value(ctrl) if hasattr(uia, "_read_edit_value") else None
            return None if r is None else str(r)
        except Exception:
            return None

    def _清空输入框():
        """发送前把输入框里可能残留的上一条清掉。

        发失败时文字会留在输入框（wechatauto 只校验粘贴、不校验回车），
        下一次任意回车会把这段残渣一起发出去 —— 表现就是同一条发了两遍。
        """
        try:
            框 = _框里的字()
            if 框 is not None and not 框.strip():
                return True
            gui = get_gui()
            uia = _取引擎()
            if uia is None:
                return False
            ctrl = uia._chat_input()
            if ctrl is None:
                return False
            try:
                vp = ctrl.GetValuePattern()
                if vp is not None and str(vp.Value or ""):
                    vp.SetValue("")
                    log("发送前清掉了输入框里的残留")
                    return True
            except Exception:
                pass
            try:
                ctrl.SetFocus()
                time.sleep(0.05)
                k32.keybd_event(0x11, 0, 0, 0)   # Ctrl down
                k32.keybd_event(0x41, 0, 0, 0)   # A down
                k32.keybd_event(0x41, 0, 2, 0)
                k32.keybd_event(0x11, 0, 2, 0)   # Ctrl up
                k32.keybd_event(0x08, 0, 0, 0)   # Backspace
                k32.keybd_event(0x08, 0, 2, 0)
                log("发送前用键盘清空了输入框残留")
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _kick_enter(最多=3):
        """把卡在输入框里的话踹出去，返回 True 表示框已空。

        根因：wechatauto 的 UIA 发送只校验「文字有没有粘进输入框」，不校验回车
        之后输入框有没有清空。窗口被抢焦点时回车落空，字就留在输入框里，
        而接口已经报了「已操作发送」—— 表现就是「消息卡在输入框没发出去」。

        以前只补一次回车，落空一次就又堵在那里；现在按「框里还有字就继续踹」
        来判：回车三次还清不掉，就回退到 guia 的 click_send（它会 OCR 找到
        「发送」按钮去点，并且每次点完都校验输入框清空）。
        """
        try:
            gui = get_gui()
            for _ in range(最多):
                框 = _框里的字()
                if 框 is not None:
                    if not 框.strip():
                        return True
                else:
                    try:
                        if not gui._input_box_has_text():
                            return True
                    except Exception:
                        break
                try:
                    gui._input.key(0x0D)          # VK_RETURN
                except Exception:
                    uia = _取引擎()
                    if uia is None:
                        break
                    e = uia._chat_input()
                    if e is None:
                        break
                    e.SendKeys("{Enter}", waitTime=0.05)
                time.sleep(1.0)
            框 = _框里的字()
            if 框 is not None and not 框.strip():
                return True
            if 框 is None:
                try:
                    if not gui._input_box_has_text():
                        return True
                except Exception:
                    pass
            # 不再点「发送」按钮：那条路要真实移动鼠标（OCR 定位 + 点击），
            # 发消息时鼠标会乱飞，很吓人。改成纯键盘再踹一轮。
            log("输入框还堵着，再踹一轮回车（不碰鼠标）")
            try:
                # 优先用 UIA 元素定向发回车（后台也能用，不挑前台）；
                # 全局回车只在前台窗口是微信时才按，否则会按到主人正在用的窗口上
                uia = _取引擎()
                输入框 = uia._chat_input() if uia is not None else None
                if 输入框 is not None:
                    输入框.SendKeys("{Enter}", waitTime=0.05)
                elif _微信在前台():
                    _press_enter(2)
                    _press_enter(1, ctrl=True)
                else:
                    log("微信不在前台，跳过全局回车（免得按到别的窗口）")
            except Exception as e:
                log("踹回车失败：%r" % (e,))
            time.sleep(1.2)
            框 = _框里的字()
            if 框 is None or not 框.strip():
                return True
            log("输入框还是堵着，这条先放弃")
            return False
        except Exception as e:
            log("补回车失败：%r" % (e,))
            return False

    def _唤醒消息(item):
        """这条回复是回谁的：宿主带了「回复谁」的 wxid，就查那轮对应的唤醒消息。

        返回 {"id","name","quote"} 或 None。老宿主不带这个字段，返回 None，
        调用方退回旧逻辑（不用「最后一条消息」硬凑 @）。
        """
        try:
            chat = str(item.get("chat") or "")
            谁 = str(item.get("回复谁") or "").strip()
            if not 谁:
                return None
            现在 = time.time()
            候选 = [x for x in 待回复.get(chat, [])
                    if x.get("id") == 谁 and 现在 - float(x.get("ts") or 0) <= 300]
            if 候选:
                return 候选[-1]
            # 桥接刚重启、待回复表空了：至少用宿主带回来的名字，引用就免了
            名字 = str(item.get("回复谁名") or "").strip()
            原消息号 = str(item.get("原消息ID") or "").strip()
            return {"id": 谁, "name": 名字, "quote": "", "native_message_id": 原消息号} if 名字 else None
        except Exception:
            return None

    def send_one(item):
        text = str(item.get("text") or "").strip()
        chat = str(item.get("chat") or "").strip()
        images = [str(p) for p in (item.get("images") or []) if p]
        files = [str(p) for p in (item.get("files") or []) if str(p).strip()]

        # 正文里的 [表情: 描述] 换成真图片（表情库里有就发图，没有就保持原样）
        表情图 = []

        def _repl(m):
            p = find_meme(m.group(1))
            if p:
                表情图.append(p)
                return ""
            # 找不到图就保留原文 —— 群里有人在「讨论」[表情: ...] 这串字时，
            # 整段删掉会变成「」；真发失败时也能看出来是哪张没配上
            return m.group(0)

        text = re.sub(r"\[表情:\s*([^\]]+)\]", _repl, text).strip()
        images = 表情图 + images

        # 兜底：DSH 那边没摘干净的 markdown 图片（只认本机地址），这里再摘一次，
        # 免得微信里收到一串 "![xxx](http://127.0.0.1:...)" 的文字。
        def _摘图(m):
            u = m.group(1)
            if re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", u, re.I):
                images.append(u)
                return ""
            return m.group(0)

        text = re.sub(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", _摘图, text).strip()

        if not text and not images and not files:
            return {"ok": False, "说": "空消息（表情没在库里配到图，整条就空了）"}
        if not chat:
            return {"ok": False, "说": "没指定发给谁（chat 空着）"}

        # Native Hook 使用微信原生发送；图片、文件和引用分别调用已校验的 helper 协议。
        if getattr(backend, "name", "") == "native_hook":
            try:
                native_target = str(item.get("chatId") or "").strip() or _chat_username(chat)
                if native_target != chat:
                    log("[HOOK] outbox 会话目标：显示名=%s，chatId=%s，nativeTarget=%s" % (
                        chat, item.get("chatId") or "（未提供）", native_target))
                native_gui = backend.make_gui()
                native_gui.open_chat(native_target)
                sent_any = False
                wake = _唤醒消息(item)
                quote_id = str((wake or {}).get("native_message_id") or "").strip()
                parts = 拆条(text, int(CFG.get("每条字数上限") or 60),
                             int(CFG.get("每条最多几条") or 0)) if text else []
                for index, part in enumerate(parts):
                    ok = False
                    if index == 0 and wake and wake.get("chatroom_id"):
                        if quote_id:
                            try:
                                ok = backend.send_quote(native_target, quote_id, part)
                            except Exception as quote_error:
                                log("[HOOK] 原生引用发送失败，退回普通文本：%r" % (quote_error,))
                                ok = False
                        else:
                            log("[HOOK] 本轮没有原生消息 ID，引用不可用，退回普通文本")
                        if not ok and quote_id:
                            log("[HOOK] 原生引用发送失败，退回普通文本")
                    if not ok:
                        ok = native_gui.send_msg(part, native_target, False)
                    if not ok:
                        return {"ok": False, "说": "Native Hook 文本发送失败"}
                    recently_sent.append((clean_text(part), time.time()))
                    sent_any = True
                for image_path in images:
                    local_path = _localize_image(image_path)
                    if not local_path:
                        log("[HOOK] 图片不存在或无法读取，跳过：%s" % image_path)
                        continue
                    if not native_gui.send_image(local_path, native_target):
                        return {"ok": False, "说": "Native Hook 图片发送失败"}
                    sent_any = True
                for file_path in files:
                    if not os.path.isfile(file_path):
                        return {"ok": False, "说": "待发送文件不存在：%s" % file_path}
                    if not native_gui.send_file(file_path, native_target, True):
                        return {"ok": False, "说": "Native Hook 文件发送失败"}
                    sent_any = True
                if not sent_any:
                    return {"ok": False, "说": "空消息"}
                if text:
                    recently_sent.append((clean_text(text), time.time()))
                STATE["last_send"] = time.time()
                STATE["last_sent_chat"] = chat
                STATE["last_sent_ts"] = time.time()
                log("✅ Native Hook 已发到微信（%s）：文字=%d，图片=%d，文件=%d" % (
                    chat, len(parts), len(images), len(files)))
                return {"ok": True, "说": "已发送"}
            except Exception as e:
                log("[HOOK] ✗ Native Hook 发送失败：%r" % (e,))
                return {"ok": False, "说": "Native Hook 发送失败：%s" % e}

        with send_lock:
            # UIA 坏着的时候整条发送路都不许走：下面 _ensure_wechat_ready / open_chat /
            # 读输入框都会进 wechatauto 的窗口操作，它一失败就退化成坐标盲点、
            # 顺手最小化挡着的窗口 —— 队列里积着几条消息就是每秒两三次的空转。
            if _uia熔断中():
                return {"ok": False, "说": "UIA 起不来，先不发（5 分钟内不再试，免得它乱点桌面）"}
            窗口快照 = _快照窗口()
            守护 = _守护最小化(窗口快照.get("hwnd")) if 窗口快照.get("iconic") else None
            try:
                # 先不抢前台：同一条会话里连发时，UIA 直接写控件就够了，
                # 把窗口顶上来只会让主人看见它闪。只有要切会话时才摆正窗口。
                _ensure_wechat_ready(前台=False)
                gui = get_gui()
                # 每次发送前重取一次引擎：微信跑久了 accessibility 会假死，
                # 缓存的引擎能收消息却一条也发不出去
                _引擎 = _取引擎()
                # 发之前先清干净输入框：上一次失败的字留在里面，
                # 这一次回车会把它一起带出去（同一句话发两遍的根源）
                try:
                    _清空输入框()
                except Exception:
                    pass
                # 已经在目标会话里就别再搜一次 —— open_chat 要点搜索框、输入名字、选群，
                # 而且 wechatauto 内部对同一个词还会重试几遍，一条消息就能多花好几秒。
                当前会话 = None
                try:
                    if _引擎 is not None:
                        当前会话 = _引擎.current_chat()
                except Exception:
                    当前会话 = None
                已在目标 = False
                if 当前会话:
                    # 群名可能带后缀（未读数之类），允许子串匹配，别为了「（3）」再搜一遍
                    已在目标 = (当前会话 == chat) or (chat and (chat in 当前会话 or 当前会话 in chat))
                elif (STATE.get("last_sent_chat") == chat
                      and time.time() - float(STATE.get("last_sent_ts") or 0) < 90):
                    # 窗口最小化时 current_chat 偶尔读不到；刚发过这个群，就按缓存算
                    已在目标 = True
                    log("当前会话读不到，按 90 秒内发过的记录认为还在：%s" % chat)
                if 已在目标:
                    log("已在目标会话，跳过选群：%s" % chat)
                if not 已在目标:
                    # 切会话这一下躲不掉：得让窗口在前，open_chat 才点得到搜索框
                    _ensure_wechat_ready(前台=True)
                    gui = get_gui()
                    _引擎 = _取引擎()
                    try:
                        gui.open_chat(chat)
                    except Exception as e:
                        log("打开会话失败（继续试）：", repr(e))
                    # 切会话这一下微信会把自己拉起来（Invoke 会话项时应用自己的行为），
                    # 这时候如果立刻按回最小化，新会话的输入框就丢焦点了，字会发不出去。
                    # 所以这里让它先露着，等这条发完在 finally 里统一收回去。
                    # 窗口最小化时输入框可能丢焦点，这里顺手把焦点还给控件。
                    try:
                        if _引擎 is not None:
                            e = _引擎._chat_input()
                            if e is not None:
                                e.SetFocus()
                    except Exception as e:
                        log("还焦点给输入框失败：", repr(e))
                发了 = False

                def 带超时(动作, 超时=25):
                    """send_msg 偶尔会卡在 UIA 里不出来，给个上限，别把整条队列堵死。"""
                    盒 = {}

                    def 跑():
                        try:
                            盒["r"] = 动作()
                        except Exception as e:
                            盒["e"] = e

                    t = threading.Thread(target=跑, daemon=True)
                    t.start()
                    t.join(超时)
                    if t.is_alive():
                        return "发送超时"
                    return 盒.get("e") or 盒.get("r")

                def 试(动作, 名字, 重试前=1):
                    """发一次；返回失败（不是抛异常）也要当成失败 —— 窗口没接住时就是这样。"""
                    for 第 in range(重试前 + 1):
                        try:
                            r = 带超时(lambda: 动作(gui))
                        except Exception as e:
                            r = e
                        if not isinstance(r, Exception) and (r is None or r):
                            return True
                        原因 = ""
                        try:
                            原因 = str(r.get("message") or "") if hasattr(r, "get") else str(r)
                        except Exception:
                            原因 = str(r)
                        # 已经发出去了、只是数据库还没落库 —— 再发一遍就成了两条重复消息
                        if "未确认" in 原因 or "已操作发送" in 原因:
                            # 但字也可能压根没发出去，就卡在输入框里 —— 补一次回车再确认
                            if _kick_enter():
                                time.sleep(1.2)
                                log("已操作发送但库里没确认，补了一次回车：" + 原因[:50])
                            else:
                                log("发出去但库里没确认，不再重发：" + 原因[:50])
                            return True
                        log("第 %d 次%s没成：%s" % (第 + 1, 名字, 原因[:80]))
                        if 第 < 重试前:
                            # 先原地重试；还是不行就重新热激活 UIA（引擎假死时只能这样救），
                            # 最后才把微信拉到前台 —— 平时不抢主人正在用的窗口
                            if 第 == 0:
                                _recover_uia("发送失败")
                            else:
                                _ensure_wechat_ready(前台=True)
                            time.sleep(0.6)
                    return False

                if text:
                    # 长内容拆成几条短消息发（一个大气泡读着累）—— 字数和条数都能在设置里调，
                    # 条数超了就并进最后一条，别在群里一口气刷十几条
                    文字上限 = int(CFG.get("每条字数上限") or 60)
                    最多条 = int(CFG.get("每条最多几条") or 0)
                    # 群回复的第一条用「引用」：群里好几个人同时说话时，看得出这句在回谁。
                    # 只在「刚收到群消息、还没过 2 分钟」时引，别把老消息引出来。
                    # 唤醒 = 这条回复对应的那条微信消息（宿主随回复带回来），引用和 @ 都照它来。
                    唤醒 = _唤醒消息(item)
                    引用原文 = ""
                    if 唤醒 and len(str(唤醒.get("quote") or "").strip()) >= 4:
                        引用原文 = str(唤醒["quote"]).strip()
                    elif (chat and chat == LAST_CHAT.get("chat") and LAST_CHAT.get("group")
                          and time.time() - float(LAST_CHAT.get("ts") or 0) < 10):
                        # 老宿主不带「回复谁」：只有最后一条就是刚刚那条（10 秒内）才敢引
                        引用原文 = str(LAST_CHAT.get("quote") or "").strip()
                        # 太短的句子（「嗯」「哈哈哈」）满屏都是，OCR 一引就引到别人身上，宁可不引
                        if len(引用原文) < 4:
                            引用原文 = ""
                    第几条 = 0
                    # 同一段回复拆成几条发时，第一条负责把会话切好；后面几条直接在当前会话里发。
                    # 以前每条都 send_msg(t, chat, True)，wechatauto 每次都会「搜索框 → 输群名 → 选群」，
                    # 一条三段回复就是三次搜索，白白多花好几秒。
                    会话已就位 = False
                    切换次数 = 0          # 这一段回复里真正「搜索选群」的次数，写给日志看
                    for 一 in 拆条(text, 文字上限, 最多条):
                        # 发送前不做「这句发过没有」的去重：那套是给「接口说失败、其实发出去了」
                        # 兜底用的。放这儿会误杀 —— 同一句话故意再发一遍（比如固定台词）
                        # 会被当成「刚发过」直接跳过。去重只在下面失败重试的路径上用。
                        ok = False
                        if 第几条 == 0 and 引用原文:
                            try:
                                # 引用要 OCR 截屏定位，窗口最小化时截不到 —— 先拉起来
                                _拉起窗口(窗口快照)
                                # 拉起来之后重取引擎：旧对象的坐标是窗口最小化时测的
                                gui = get_gui()
                                # quote_msg 内部对 who 是无条件 open_chat 的（搜索+选群），
                                # 已经在目标会话时传 None，让它直接在当前会话里引用。
                                ok = 试(lambda g, t=一, q=引用原文,
                                        w=(None if 已在目标 else chat):
                                        g.quote_msg(t, who=w, target_text=q),
                                        "引用发文字")
                            except Exception as e:
                                log("引用发送异常（退回普通发送）：", repr(e))
                                ok = False
                            if not ok:
                                log("引用没成，退回普通发送：%s" % 一[:20])
                                # 引用发不出去时，开头补一个「@唤醒者」：名字来自这一轮真正叫醒你的
                                # 那条消息，不是 LAST_CHAT 里那条（刷屏时早被下一条覆盖了）。
                                if 第几条 == 0 and 一 and 唤醒:
                                    from group_nicknames import resolve_mention_name
                                    群ID = str(唤醒.get("chatroom_id") or _chat_username(chat) or "")
                                    对方 = resolve_mention_name(db, 群ID, str(唤醒.get("id") or ""))
                                    if 对方:
                                        一 = "@%s %s" % (对方, 一)
                        if not ok:
                            谁 = None          # None = 发到当前打开的会话，不搜索
                            要确认 = False
                            if not 会话已就位:
                                if 已在目标:
                                    谁, 要确认 = None, False
                                else:
                                    谁, 要确认 = chat, True
                                    切换次数 += 1
                            else:
                                # 万一这会儿会话被切走了，认出来再切回去
                                try:
                                    现在 = _引擎.current_chat() if _引擎 is not None else None
                                except Exception:
                                    现在 = None
                                if 现在 != chat:
                                    log("会话被切走了（现在是 %r），重新打开：%s" % (现在, chat))
                                    谁 = chat
                                    要确认 = True
                                    切换次数 += 1
                            ok = 试(lambda g, t=一, w=谁, v=要确认: g.send_msg(t, w, v), "发文字")
                            会话已就位 = True
                        第几条 += 1
                        if not ok and _already_sent(一, chat, within=25):
                            log("接口说失败，但库里查到了 —— 算发成功：%s" % 一[:30])
                            ok = True
                        if not ok:
                            # 最后再补一脚：字常常只是卡在输入框里没回车出去
                            if _kick_enter():
                                time.sleep(1.2)
                                if _already_sent(一, chat, within=25):
                                    log("补回车后库里查到了 —— 算发成功：%s" % 一[:30])
                                    ok = True
                        if not ok:
                            # UIA 彻底没发出去：先走键盘通道（Ctrl+F 搜会话→粘贴→回车→查库）
                            if _keyboard_send(一, chat):
                                log("键盘通道补发成功：%s" % 一[:30])
                                ok = True
                        if not ok:
                            # 最后再抢一次前台重试
                            try:
                                _ensure_wechat_ready(前台=True)
                                time.sleep(0.5)
                                gui.open_chat(chat)
                                if 试(lambda g, t=一: g.send_msg(t, chat, True), "发文字（前台重试）"):
                                    ok = True
                            except Exception as e:
                                log("前台重试异常：", repr(e))
                        if not ok:
                            gui_box["gui"] = None      # 下次重建
                            return {"ok": False, "说": "文字没发出去（微信窗口没接住）"}
                        # 发成功也要补回车：wechatauto 只认「粘贴成功」，
                        # 回车落空它照样报成功，字会留在框里。多按几次，
                        # 并兼容微信里「Ctrl+Enter 发送」的设置。
                        # 全局回车只在前台窗口是微信时才按，否则会按到主人正在用的窗口上
                        if _微信在前台():
                            _press_enter(2)
                            _press_enter(1, ctrl=True)
                        _kick_enter()
                        # 拆开发出去的每一条都记账 —— 不然文件传输助手里会被当成主人说的话读回来
                        recently_sent.append((clean_text(一), time.time()))
                        _记发过(一)
                        time.sleep(0.35)
                    recently_sent.append((clean_text(text), time.time()))
                    _记发过(text)
                    STATE["last_send"] = time.time()
                    # 记下「刚发到哪个群」：下次发同一个群时，current_chat 读不到就靠它跳过选群
                    STATE["last_sent_chat"] = chat
                    STATE["last_sent_ts"] = time.time()
                    if len(recently_sent) > 100:
                        del recently_sent[:-100]
                    log("[%s] ✅ 已发到微信（%s，切会话 %d 次）：%s" % (BACKEND_LABEL, chat, 切换次数, text[:40]))
                    发了 = True

                临时图 = []

                def 取图(p):
                    q = _localize_image(p)
                    if q and q != p:
                        临时图.append(q)
                    return q

                for p in images:
                    p = 取图(p)
                    if not p:
                        log("⚠ 图拿不到，跳过")
                        continue
                    # 发完文字窗口可能被挡/失焦 —— 发图前把会话和窗口都摆正。
                    # 但刚才要是已经发过文字，窗口和会话都还在这条上，再走全套恢复会白顿一秒多。
                    try:
                        if text:
                            time.sleep(0.25)
                        else:
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.2)
                            gui.open_chat(chat)
                            time.sleep(0.4)
                    except Exception as e:
                        log("⚠ 发图前准备窗口失败（继续试）：", repr(e))
                    t0 = time.time()
                    前图时 = _last_image_ts(chat)
                    try:
                        ok_img = _send_image_dib(gui, p, chat)
                    except Exception as e:
                        log("✗ 发图异常：", repr(e))
                        ok_img = False
                    # 接口返回不可靠（点击被挡掉时它说失败，其实剪贴板粘贴已经发出去了）——
                    # 一律以数据库里有没有新图片为准。前两秒快速查，别让确认本身拖慢下一条。
                    真发了 = False
                    for 第 in range(14):
                        if _last_image_ts(chat) > 前图时 + 0.5:
                            真发了 = True
                            break
                        time.sleep(0.35 if 第 < 6 else 1.5)
                    if 真发了:
                        if ok_img:
                            log("🖼 已发图（%s）：%s" % (chat, os.path.basename(p)))
                        else:
                            log("🖼 已发图（%s，接口说失败但库里查到了）：%s" % (chat, os.path.basename(p)))
                        发了 = True
                        time.sleep(0.4)
                    else:
                        # 第一次多半是窗口被抢/被挡 —— 重建窗口再来一次，别直接把图丢掉
                        log("✗ 发图失败，重建窗口重试一次：%s" % p)
                        gui_box["gui"] = None
                        try:
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.5)
                            gui2 = get_gui()
                            gui2.open_chat(chat)
                            time.sleep(0.5)
                            前图时2 = _last_image_ts(chat)
                            _send_image_dib(gui2, p, chat)
                            for 第 in range(12):
                                if _last_image_ts(chat) > 前图时2 + 0.5:
                                    真发了 = True
                                    break
                                time.sleep(1.0)
                        except Exception as e:
                            log("重试发图异常：", repr(e))
                        if 真发了:
                            log("🖼 重试后已发图（%s）：%s" % (chat, os.path.basename(p)))
                            发了 = True
                            time.sleep(0.4)
                        else:
                            log("✗ 发图彻底失败（库里还是没有新图）：%s" % p)

                # 文件：wechatauto 自带 send_file（剪贴板粘贴路线），一条一个文件
                for p in files:
                    if not p or not os.path.isfile(p):
                        log("⚠ 文件不存在，跳过：%s" % p)
                        continue
                    try:
                        # 同一批里文字/图已经发过、会话还在原处，就不用再摆一次窗口
                        if not text and not images:
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.2)
                            gui.open_chat(chat)
                            time.sleep(0.4)
                        ok_f = 试(lambda g, f=p: g.send_file(f, chat, True), "发文件")
                    except Exception as e:
                        log("✗ 发文件异常：", repr(e))
                        ok_f = False
                    if ok_f:
                        log("📎 已发文件（%s）：%s" % (chat, os.path.basename(p)))
                        发了 = True
                    else:
                        log("✗ 文件没发出去：%s" % p)
                    time.sleep(0.5)

                for t in 临时图:
                    try:
                        os.unlink(t)
                    except Exception:
                        pass

                if not 发了:
                    return {"ok": False, "说": "字、图、文件都没发出去"}
                return {"ok": True}
            except Exception as e:
                log("✗ 发送失败（%s）：%r" % (chat, e))
                gui_box["gui"] = None    # 下次重建
                return {"ok": False, "说": repr(e)}
            finally:
                if 守护 is not None:
                    守护.set()
                # 发送期间把微信置顶了，发完要放下来，不然它一直压在别的窗口上面
                _取消置顶()
                # 后台发送的收尾：原来最小化的继续最小化，前台还给主人原来那个窗口
                _还原窗口(窗口快照)

    # ---------- 兜底：DSH 那边的会话事件没送到时，直接读会话文件把回复捞出来 ----------
    # 起因：2026-10-27 晚，DSH 重启后 host.js 的 session/event 不再触发，
    #       回复全部卡在 DSH 里发不出去。这里作为独立于事件的一层保险。

    LAST_CHAT = {"chat": os.environ.get('DSH_BRIDGE_DEFAULT_CHAT') or '', "ts": 0.0}
    # 「这一轮是谁叫醒的」：每条进 DSH 的微信消息都记一笔（发送者 wxid / 名字 / 原文）。
    # 宿主压回复时会带回「回复谁」的 wxid，桥接照着它引用 / @ —— 不再拿「最后一条进来的消息」，
    # 群聊刷屏时那玩意早就被下一个人覆盖了，于是回 A 却 @ 了 B。
    待回复 = {}   # chat -> [{"id", "name", "quote", "ts"}]，只留最近几分钟的
    FALLBACK_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fallback_state.json')
    # 会话目录按「最近改动过的那个」自动找，不写死具体会话名
    def _找会话根() -> str:
        base = os.path.join(os.path.expanduser('~'), '.dsh', 'sessions')
        try:
            dirs = [os.path.join(base, d) for d in os.listdir(base)]
            dirs = [d for d in dirs if os.path.isdir(d)]
            if dirs:
                return max(dirs, key=lambda p: os.path.getmtime(p))
        except Exception:
            pass
        return base

    SESS_ROOT = _找会话根()
    # 兜底通道：2026-10-28 起默认关闭。它读会话文件猜「这句回复该发给谁」，
    # 猜错就会把私聊内容发进群、或者把同一句重发两遍（当天出了两次事故）。
    # 想要它回来：设环境变量 DSH_BRIDGE_NO_FALLBACK=0。
    FALLBACK_OFF = os.environ.get('DSH_BRIDGE_NO_FALLBACK', '1') != '0'

    def _读兜底游标():
        try:
            with open(FALLBACK_STATE, 'r', encoding='utf-8') as f:
                return int((json.load(f) or {}).get('seq') or 0)
        except Exception:
            return 0

    def _写兜底游标(n):
        try:
            with open(FALLBACK_STATE, 'w', encoding='utf-8') as f:
                json.dump({'seq': int(n)}, f)
        except Exception:
            pass

    def _最新会话文件():
        best, best_t = None, 0.0
        try:
            for d in os.listdir(SESS_ROOT):
                p = os.path.join(SESS_ROOT, d, 'session.v4.jsonl.zstd')
                if os.path.exists(p):
                    t = os.path.getmtime(p)
                    if t >= best_t:
                        best_t, best = t, p
        except Exception:
            pass
        return best

    _FB_CACHE = {'key': None, 'res': None}

    def 兜底捞回复():
        p = _最新会话文件()
        if not p:
            return None
        try:
            mt = os.path.getmtime(p)
        except Exception:
            return None
        key = (p, mt, _读兜底游标())
        if _FB_CACHE['key'] == key:
            return _FB_CACHE['res']
        res = _解析兜底(p)
        _FB_CACHE['key'] = key
        _FB_CACHE['res'] = res
        return res

    def _解析兜底(p):
        """找最后一条「来自微信」的用户消息之后的那句回复。本地聊的不算。"""
        try:
            import zstandard
        except Exception:
            return None
        try:
            with open(p, 'rb') as f:
                raw = zstandard.ZstdDecompressor().stream_reader(f).read()
        except Exception:
            return None
        last_user = None
        last_asst = None
        for line in raw.decode('utf-8', 'replace').splitlines():
            if len(line) < 24 or '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get('type')
            if t == 'user/message':
                src = (o.get('data') or {}).get('source') or {}
                k = src.get('kind')
                if k == 'wechat':
                    last_user = o
                    last_asst = None          # 只看这条微信消息之后的回复
                elif k == 'user':
                    last_user = None          # 有人在本机聊天，那一轮的风头过了
                    last_asst = None
            elif t == 'assistant/message' and last_user is not None:
                d = o.get('data') or {}
                body = (d.get('message') or {}).get('content') or []
                txt = ''.join(x.get('text') or '' for x in body if x.get('type') == 'text').strip()
                if txt:
                    last_asst = (o.get('seq') or 0, txt)
        if not last_user or not last_asst:
            return None
        seq_u = last_user.get('seq') or 0
        seq_a, txt_a = last_asst
        if seq_a <= seq_u or seq_a <= _读兜底游标():
            return None
        return {'seq': seq_a, 'text': txt_a}

    def outbox_loop():
        log("出站轮询已启动（每 %.0f 秒）" % POLL_SEC)

        # Hook 文本发送不经过 UIA；只有 sqlite_uia 后端需要预热窗口控件树。
        if getattr(backend, "name", "") != "native_hook":
            def _预热():
                try:
                    with send_lock:
                        _取引擎()
                    log("UIA 引擎预热完成")
                except Exception as e:
                    log("UIA 预热失败：", repr(e))

            threading.Thread(target=_预热, daemon=True).start()
        else:
            log("HOOK 文本通道跳过 UIA 引擎预热")
        心跳钟 = time.time()
        while True:
            time.sleep(POLL_SEC)
            # 每 30 秒给宿主报一次「我还活着」：宿主那边有看门狗，90 秒收不到心跳就当假死重启
            if time.time() - 心跳钟 >= 30:
                心跳钟 = time.time()
                try:
                    http_json("/dsh-wechat-bridge/heartbeat", {"chat": LAST_CHAT.get("chat") or ""})
                except Exception:
                    pass
            if not SEND_ENABLED:
                continue
            try:
                r = http_json("/dsh-wechat-bridge/outbox")
                if not r.get("ok"):
                    continue
                消息队列 = r.get("消息") or []
                if 消息队列:
                    log("[%s] 出站队列收到 %d 条" % (BACKEND_LABEL, len(消息队列)))
                这一批发过了 = False
                for item in 消息队列:
                    # ⭐ 发送放进子线程带超时：微信窗口被最小化/被抢焦点时，UIA 有可能一直等下去，
                    #    以前就这么把整条出站循环堵死了 —— 后面的消息全卡在这儿不发。
                    结果盒 = {}

                    def 干(条=item):
                        try:
                            结果盒["r"] = send_one(条)
                        except Exception as e:  # noqa: BLE001
                            结果盒["e"] = e

                    线 = threading.Thread(target=干, daemon=True)
                    线.start()
                    线.join(timeout=70)
                    if 线.is_alive():
                        log("⚠ 发送超时（微信窗口多半卡住了），这条先跳过，重建窗口")
                        gui_box["gui"] = None
                        res = {"ok": False, "说": "发送超时（窗口卡住）"}
                    elif "r" in 结果盒:
                        res = 结果盒["r"]
                    else:
                        res = {"ok": False, "说": str(结果盒.get("e"))}
                    log("[%s] 出站发送结果：ok=%s，chat=%s，说=%s" % (
                        BACKEND_LABEL, bool(res.get("ok")), item.get("chat"), res.get("说", "")))
                    http_json("/dsh-wechat-bridge/outbox/ack", {
                        "ok": bool(res.get("ok")),
                        "chat": item.get("chat"),
                        "说": res.get("说", ""),
                    })
                    if res.get("ok"):
                        这一批发过了 = True
                        # 本鱼刚在这个群说完话：追话计数清零，后面几句可能是在接本鱼的话
                        追话计数[item.get("chat") or ""] = 0
                        最后回复[item.get("chat") or ""] = str(item.get("text") or "")
                # 主通道真发出去的那条，把兜底游标一起推过去 —— 否则库里的时间窗一过，
                # 兜底会把同一句再捞出来重复发一遍（就是「发图就重复」的老毛病）
                if 这一批发过了 and (r.get("消息") or []):
                    try:
                        捞2 = 兜底捞回复()
                        if 捞2:
                            待比2 = re.sub(r"\[表情:\s*[^\]]+\]", "", 捞2["text"]).strip()
                            if 待比2 and _already_sent(待比2, LAST_CHAT.get("chat") or "", within=180):
                                _写兜底游标(捞2["seq"])
                                log("兜底游标跟主通道同步：seq=%s" % 捞2["seq"])
                    except Exception:
                        pass
                if not FALLBACK_OFF and not r.get("消息"):
                    try:
                        捞 = 兜底捞回复()
                    except Exception:
                        捞 = None
                    if 捞:
                        聊天 = LAST_CHAT.get("chat") or ""
                        # ⭐ 串台防护：只有「刚刚」来过微信消息，兜底才认这句回复是给它的。
                        # 之前是永远拿 LAST_CHAT，结果主人在 Web 里跟我聊、我回的私聊内容
                        # 被兜底当成「对群里那条老消息的回复」，整段发进了群（2026-10-28 16:19）。
                        过期 = (time.time() - float(LAST_CHAT.get("ts") or 0)) > 90
                        if 聊天 and 过期:
                            log("兜底：最后一条微信消息在 90 秒前，这句回复不归它，跳过（防串台）")
                            聊天 = ""
                        if 聊天:
                            log("兜底：会话事件没送到，直接捞到一句回复（seq=%s）" % 捞["seq"])
                            # 兜底只补「主通道真没送到」的：已经发过的绝不能再发一遍（会重复两遍文字+图）
                            待比 = re.sub(r"\[表情:\s*[^\]]+\]", "", 捞["text"]).strip()
                            已发过 = False
                            try:
                                if 待比:
                                    已发过 = _already_sent(待比, 聊天, within=120)
                                else:
                                    已发过 = _image_sent_recently(聊天, time.time() - 120)
                            except Exception:
                                已发过 = False
                            if 已发过:
                                log("兜底：这句微信里已经发过了，不重复发，只推进游标")
                                _写兜底游标(捞["seq"])
                            else:
                                兜底结果 = {"ok": False, "说": "未发"}
                                try:
                                    兜底结果 = send_one({"chat": 聊天, "text": 捞["text"], "images": []})
                                except Exception as e:  # noqa: BLE001
                                    log("兜底发送失败：%r" % (e,))
                                if 兜底结果.get("ok"):
                                    _写兜底游标(捞["seq"])
                                    time.sleep(0.5)
                # 收窗口交给 send_one 的 _还原窗口：原来最小化的继续最小化，
                # 主人本来就开着微信的就不再动它 —— 不再一刀切按最小化
            except Exception:
                log("出站轮询出错：", traceback.format_exc(limit=2))

    # ---------- 收：监听数据库新消息 ----------

    START_TS = time.time()
    seen = set()

    # ---------- 收：图片 / 表情包落盘 ----------

    媒体 = {"下载器": None, "导入失败": False}

    def _图片后缀(data):
        if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        return ""

    def _保存表情包(raw):
        """表情包 = 一条 <emoji cdnurl="...">。cdnurl 是明文，直接下，不用解密。"""
        try:
            text = html.unescape(raw or "")
            # 微信 4.x 的 XML 属性会带空格：cdnurl = "..." / md5 = "..."
            url = ""
            for attr in ("cdnurl", "thumburl"):
                m = re.search(r'%s\s*=\s*["\']([^"\']+)["\']' % attr, text)
                if m and m.group(1).strip().startswith(("http://", "https://")):
                    url = m.group(1).strip()
                    break
            if not url:
                return None
            md5 = re.search(r'md5\s*=\s*["\']([^"\']+)["\']', text)
            key = (md5.group(1) if md5 else hashlib.md5(url.encode("utf-8")).hexdigest()).lower()
            folder = os.path.join(INBOUND_DIR, "stickers")
            os.makedirs(folder, exist_ok=True)
            for ext in ("gif", "png", "jpg", "webp"):
                p = os.path.join(folder, key + "." + ext)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    return p
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(8 * 1024 * 1024 + 1)
            if not data or len(data) > 8 * 1024 * 1024:
                return None
            ext = _图片后缀(data)
            if not ext:
                return None
            p = os.path.join(folder, key + "." + ext)
            with open(p, "wb") as f:
                f.write(data)
            log("表情包落盘：%s（%d KB）" % (p, len(data) // 1024))
            return p
        except Exception as e:
            log("表情包落盘失败：", repr(e))
            return None

    def _保存图片(msg, username):
        """普通图片：本地 .dat 解密。wechatauto 的 MediaDownloader 会处理 wxgf。"""
        if 媒体["导入失败"]:
            return None
        try:
            if 媒体["下载器"] is None:
                from wechatauto.media import MediaDownloader

                媒体["下载器"] = MediaDownloader(db)
            folder = os.path.join(INBOUND_DIR, "images")
            os.makedirs(folder, exist_ok=True)
            local_id = int(msg.get("local_id") or 0)
            if not local_id:
                return None
            p = 媒体["下载器"].download_image(
                username, local_id, save_dir=folder, tier="best", shard=msg.get("shard")
            )
            if p and os.path.isfile(p) and os.path.getsize(p) > 0:
                log("图片落盘：%s" % p)
                return p
            return None
        except Exception as e:
            log("图片落盘失败：", repr(e))
            return None

    # ---------- 记忆模型：成员画像 / 群像 / 证据校验 ----------
    #
    # 和聊天分开：聊天由 DSH 那边的模型接，这里只负责「把群里的东西记下来」。
    # 密钥留空时回退读 DSH 自己的凭据（~/.dsh/.credentials.yaml 的 DEEPSEEK_API_KEY）。

    def _dsh密钥():
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        path = os.path.join(home, ".credentials.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("DEEPSEEK_API_KEY:"):
                        return s.split(":", 1)[1].strip().strip("\"'")
        except Exception:
            pass
        return ""

    def _记忆配置():
        return {
            "key": str(CFG.get("记忆密钥") or "").strip() or _dsh密钥(),
            "model": str(CFG.get("记忆模型") or "deepseek-flash").strip() or "deepseek-flash",
            "base": (str(CFG.get("记忆地址") or "https://api.deepseek.com").strip()
                     or "https://api.deepseek.com").rstrip("/"),
        }

    def _记忆问(system, user, max_tokens=1200, timeout=60, model=None):
        """调一次记忆模型，返回文本；没配密钥就返回空串，当没有这个能力。"""
        c = _记忆配置()
        if not c["key"]:
            return ""
        payload = {
            "model": model or c["model"],
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + [{"role": "user", "content": user}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": False,
        }
        req = urllib.request.Request(
            c["base"] + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json", "authorization": "Bearer " + c["key"]},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return str(data["choices"][0]["message"]["content"] or "")
        except Exception as e:
            log("记忆模型调用失败：", repr(e))
            return ""

    def 认领判断(文本, 发送人, 上下文, 本鱼最后=""):
        """问记忆模型：这条消息在跟谁说话？返回 yes / no / unclear（失败也当 unclear）。"""
        if not _记忆配置().get("key"):
            return "unclear"
        系统 = (
            "你在判断一条微信群消息在跟谁说话。群里既有真人，也有一个叫「%s」的机器人（也就是“你”）。\n"
            "规则：明确叫到机器人、@机器人、引用机器人的话、或明显在问机器人 → 回答 yes；\n"
            "如果给了「机器人最后说的那句」，而这条消息明显在接那句话（回应、追问、吐槽、同意、反驳）→ 也回答 yes；\n"
            "明显在回另一个真人、或跟另一个真人说话 → 回答 no；\n"
            "看不出明确对象（群体话题、自言自语、玩笑）→ 回答 unclear。\n"
            "只输出一个词：yes 或 no 或 unclear。"
        ) % ("、".join(ALIASES) or "大肥鱼")
        用户 = (上下文 or "")
        if 本鱼最后:
            用户 += "\n（机器人最后说的是：%s）" % str(本鱼最后)[:200]
        用户 += "\n（这条消息（%s）：%s）" % (发送人 or "某人", str(文本)[:300])
        答 = _记忆问(系统, 用户, max_tokens=20, timeout=15,
                    model="deepseek-chat").strip().lower()
        for 值 in ("yes", "no", "unclear"):
            if 答.startswith(值):
                return 值
        return "unclear"

    # ---------- 记忆库：成员画像 / 群像 / 好感账本 ----------
    #
    # 轻量版：SQLite（Python 自带），不引 PG/向量。好感只认事件类型，分值来自
    # 固定效果表、模型插不上手（照 Attune 的思路），带逐笔账本方便以后撤销。

    def _记忆库路径():
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        d = os.path.join(home, "wechat-memory")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return os.path.join(d, "memory.db")

    _记忆库锁 = threading.Lock()

    # 好感只认事件类型，模型给不出数值
    _好感表 = {
        "点名": 0.5,
        "引用": 1.0,
        "话题接话": 0.3,
        "被夸": 2.0,
        "被骂": -2.0,
        "主动打招呼": 0.5,
    }

    def _记忆库():
        import sqlite3
        con = sqlite3.connect(_记忆库路径(), timeout=10)
        con.execute(
            "CREATE TABLE IF NOT EXISTS members("
            "account TEXT, group_id TEXT, member_id TEXT, name TEXT,"
            "profile TEXT DEFAULT '', affinity REAL DEFAULT 0, updated_at REAL DEFAULT 0,"
            "PRIMARY KEY(account, group_id, member_id))")
        con.execute(
            "CREATE TABLE IF NOT EXISTS member_details("
            "account TEXT, member_id TEXT, name TEXT DEFAULT '', details TEXT DEFAULT '{}',"
            "updated_at REAL DEFAULT 0, PRIMARY KEY(account, member_id))")
        con.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "account TEXT, group_id TEXT, member_id TEXT, name TEXT,"
            "kind TEXT, content TEXT, evidence TEXT DEFAULT '', created_at REAL)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS group_profiles("
            "account TEXT, group_id TEXT, profile TEXT DEFAULT '', updated_at REAL DEFAULT 0,"
            "PRIMARY KEY(account, group_id))")
        con.execute(
            "CREATE TABLE IF NOT EXISTS ledger("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "account TEXT, group_id TEXT, member_id TEXT, delta REAL, reason TEXT, created_at REAL)")
        return con

    def 记事件(account, group_id, member_id, name, kind, content, evidence=""):
        """记一条互动：事件进 events，好感按固定表进 ledger。失败不打扰主流程。"""
        if not member_id:
            return
        try:
            with _记忆库锁:
                con = _记忆库()
                now = time.time()
                con.execute(
                    "INSERT OR IGNORE INTO members(account, group_id, member_id, name, updated_at)"
                    " VALUES(?,?,?,?,?)", (account, group_id, member_id, name or "", now))
                con.execute(
                    "INSERT INTO events(account, group_id, member_id, name, kind, content, evidence, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (account, group_id, member_id, name or "", kind, (content or "")[:300],
                     (evidence or "")[:300], now))
                delta = _好感表.get(kind)
                if delta:
                    con.execute(
                        "INSERT INTO ledger(account, group_id, member_id, delta, reason, created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (account, group_id, member_id, delta, kind, now))
                    con.execute(
                        "UPDATE members SET affinity = affinity + ?, updated_at = ?"
                        " WHERE account=? AND group_id=? AND member_id=?",
                        (delta, now, account, group_id, member_id))
                con.commit()
                con.close()
        except Exception as e:
            log("记忆写入失败：", repr(e))

    def 记忆上下文(account, group_id, member_id, name, 预算=320):
        """把「对这个人的印象 + 好感 + 群像」拼成一小段注入上下文，超预算就截断。"""
        片段 = []
        try:
            with _记忆库锁:
                con = _记忆库()
                row = con.execute(
                    "SELECT profile, affinity FROM members"
                    " WHERE account=? AND group_id=? AND member_id=?",
                    (account, group_id, member_id)).fetchone()
                g = con.execute(
                    "SELECT profile FROM group_profiles WHERE account=? AND group_id=?",
                    (account, group_id)).fetchone()
                con.close()
            if row and row[0]:
                片段.append("对%s的印象：%s" % (name or "这个人", row[0][:170]))
            if row and row[1]:
                片段.append("和%s的好感 %.1f" % (name or "这个人", float(row[1])))
            if g and g[0]:
                片段.append("群像：%s" % g[0][:170])
        except Exception as e:
            log("记忆读取失败：", repr(e))
        文本 = "；".join(片段)
        if len(文本) > 预算:
            文本 = 文本[:预算] + "…"
        return ("（记忆：%s）\n" % 文本) if 文本 else ""

    # ---------- 画像合成：成员画像 + 群像（每条都要原文证据） ----------

    def _剥json(文本):
        s = str(文本 or "").strip()
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
        m = re.search(r"\{.*\}", s, re.S)
        return json.loads(m.group(0)) if m else {}

    def 合成画像(account, 群名, 消息文本, 上限=6000):
        """让模型按原文写成员画像和群像；每条都得交出原文证据，对不上就丢。"""
        if not (_记忆配置().get("key") and str(消息文本).strip()):
            return 0
        系统 = (
            "你在给一个微信群做人物侧写。只根据给出的聊天记录，不要编。\n"
            "输出 JSON：{\"members\":[{\"name\":\"群里的显示名\",\"profile\":\"一句话画像(20字内)\","
            "\"evidence\":\"聊天记录里原样出现的一句话\"}],"
            "\"group\":{\"氛围\":\"\",\"兴趣\":\"\",\"梗\":\"\",\"evidence\":\"聊天记录里原样出现的片段\"}}。\n"
            "evidence 必须是聊天记录里原文，一个字都不改；想不出证据就别写这个人。"
        )
        用户 = "聊天记录：\n" + str(消息文本)[:上限] + "\n\n只输出 JSON。"
        原文 = _记忆问(系统, 用户, max_tokens=1500)
        if not 原文:
            return 0
        try:
            data = _剥json(原文)
        except Exception as e:
            log("画像 JSON 解析失败：", repr(e))
            return 0
        now = time.time()
        收 = 0
        try:
            with _记忆库锁:
                con = _记忆库()
                for 人 in (data.get("members") or [])[:20]:
                    名字 = str(人.get("name") or "").strip()
                    画像 = str(人.get("profile") or "").strip()
                    证据 = str(人.get("evidence") or "").strip()
                    if not 名字 or not 画像:
                        continue
                    if 证据 and 证据 not in 消息文本:
                        log("画像证据对不上，丢弃：%s" % 名字)
                        continue
                    cur = con.execute(
                        "UPDATE members SET profile=?, updated_at=?"
                        " WHERE account=? AND group_id=? AND name=?",
                        (画像[:200], now, account, 群名, 名字))
                    if cur.rowcount == 0:
                        con.execute(
                            "INSERT INTO members(account, group_id, member_id, name, profile, updated_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (account, 群名, 名字, 名字, 画像[:200], now))
                    收 += 1
                g = data.get("group") or {}
                if isinstance(g, dict) and g:
                    描述 = "；".join(
                        "%s：%s" % (k, v) for k, v in g.items()
                        if k != "evidence" and str(v or "").strip())
                    证据 = str(g.get("evidence") or "").strip()
                    if 描述 and (not 证据 or 证据 in 消息文本):
                        con.execute(
                            "INSERT INTO group_profiles(account, group_id, profile, updated_at)"
                            " VALUES(?,?,?,?) ON CONFLICT(account, group_id)"
                            " DO UPDATE SET profile=excluded.profile, updated_at=excluded.updated_at",
                            (account, 群名, 描述[:400], now))
                con.commit()
                con.close()
            if 收:
                log("画像合成：%s，成员 %d 条" % (群名, 收))
        except Exception as e:
            log("画像写入失败：", repr(e))
        return 收

    def 画像线程():
        """启动后先等 10 分钟，之后每 6 小时扫一遍当天日记，给每个群合成画像。"""
        time.sleep(600)
        while True:
            try:
                account = me or ""
                天 = os.path.join(
                    CFG["日记目录"] or os.path.join(os.path.expanduser("~"), ".dsh", "wechat-diary"),
                    time.strftime("%Y-%m-%d"))
                if os.path.isdir(天):
                    for fn in os.listdir(天):
                        if not fn.endswith(".md"):
                            continue
                        群名 = fn[:-3]
                        try:
                            with open(os.path.join(天, fn), encoding="utf-8") as f:
                                文本 = f.read()
                            合成画像(account, 群名, 文本)
                        except Exception as e:
                            log("读日记失败：", repr(e))
            except Exception as e:
                log("画像线程出错：", repr(e))
            time.sleep(6 * 3600)

    # ---------- 群友自述资料 ----------
    # 不再把聊天里的自述自动写入长期资料；资料仅通过受权限控制的工具显式修改。

    # ---------- 看图：老宿主自己看，新宿主交给它看 ----------

    def _视觉配置():
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        配置 = {}
        try:
            with open(os.path.join(home, "wechat-bridge-config.json"), encoding="utf-8-sig") as f:
                配置 = json.load(f) or {}
        except Exception:
            pass
        if 配置.get("看图") is False:
            return None
        主 = str(配置.get("视觉密钥") or "").strip()
        备 = str(配置.get("视觉备用密钥") or "").strip()
        if not 主 or not 备:
            try:
                # 可选的第三方设置文件（比如 whale-girl 的 settings.json），用环境变量指过来
                第三方设置 = os.environ.get("DSH_BRIDGE_VISION_SETTINGS") or os.path.join(home, "wechat-vision-settings.json")
                with open(第三方设置, encoding="utf-8-sig") as f:
                    鲸 = json.load(f) or {}
                if not 主:
                    主 = str(鲸.get("apiKey") or "").strip()
                if not 备:
                    备 = str(鲸.get("visionApiKey") or "").strip()
            except Exception:
                pass
        if not 主:
            主 = str(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("VISION_API_KEY") or "").strip()
        if not 备:
            备 = str(os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY") or "").strip()
        if not 主 and not 备:
            return None
        return {
            "主": 主,
            "备": 备,
            "模型": str(配置.get("视觉模型") or "deepseek-flash").strip() or "deepseek-flash",
            "地址": str(配置.get("视觉地址") or "https://api.deepseek.com").rstrip("/"),
            "备用模型": str(配置.get("视觉备用模型") or "glm-4v-flash").strip() or "glm-4v-flash",
            "备用地址": str(
                配置.get("视觉备用地址") or "https://open.bigmodel.cn/api/paas/v4"
            ).rstrip("/"),
        }

    def _读视觉缓存():
        try:
            with open(VISION_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _写视觉缓存(表):
        try:
            items = sorted(表.items(), key=lambda kv: float((kv[1] or {}).get("时间") or 0), reverse=True)
            os.makedirs(os.path.dirname(VISION_CACHE), exist_ok=True)
            with open(VISION_CACHE, "w", encoding="utf-8") as f:
                json.dump(dict(items[:500]), f, ensure_ascii=False)
        except Exception:
            pass

    def _视觉请求(地址, key, 模型, data_url, 提示):
        url = 地址.rstrip("/") + "/chat/completions"
        体 = {
            "model": 模型,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": 提示},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }
            ],
            "max_tokens": 700,
        }
        if "deepseek" in 地址:
            体["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            url,
            data=json.dumps(体, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError("视觉接口 HTTP %s：%s" % (e.code, detail))
        msg = ((payload.get("choices") or [{}])[0].get("message") or {})
        text = str(msg.get("content") or "").strip()
        if not text:
            raise RuntimeError("视觉接口没返回文字")
        return re.sub(r"\s+", " ", text)[:180]

    def _看图(path, 类型):
        """返回一句中文描述；没配钥匙 / 看失败就返回空字符串。"""
        设置 = _视觉配置()
        if not 设置:
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read(20 * 1024 * 1024 + 1)
        except Exception:
            return ""
        if not data or len(data) > 20 * 1024 * 1024:
            return ""
        suffix = _图片后缀(data)
        mime = {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(suffix)
        if not mime:
            return ""
        digest = hashlib.sha256(data).hexdigest()
        缓存 = _读视觉缓存()
        if isinstance(缓存.get(digest), dict) and 缓存[digest].get("描述"):
            return str(缓存[digest]["描述"])
        提示 = (
            "这是微信聊天里对方发来的%s。"
            "用一两句中文说清画面内容，以及它表达的情绪、语气或梗；不要寒暄，不超过60字。" % 类型
        )
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        描述 = ""
        for key, 模型, 地址 in (
            (设置["主"], 设置["模型"], 设置["地址"]),
            (设置["备"], 设置["备用模型"], 设置["备用地址"]),
        ):
            if not key:
                continue
            try:
                描述 = _视觉请求(地址, key, 模型, data_url, 提示)
                if 描述:
                    break
            except Exception as e:
                log("看图失败（%s）：%r" % (模型, e))
        if not 描述:
            return ""
        缓存[digest] = {"描述": 描述, "时间": time.time(), "类型": 类型}
        _写视觉缓存(缓存)
        return 描述

    def _看图并替换(content, path, 类型):
        描述 = _看图(path, 类型)
        if not 描述:
            return content
        标签 = "一个表情包" if 类型 == "表情包" else "一张图片"
        前缀 = "[对方发来%s：%s]" % (标签, 描述)
        new, n = re.subn(r"（发来(?:一)?(?:张|个)?(?:图片|表情包)）", lambda _m: 前缀, content, count=1)
        return new if n else (前缀 + "\n" + content)

    def _解析拍一拍(xml, self_wxid, expected_chat):
        """仅解析格式明确、目标为当前账号的微信拍一拍系统事件。"""
        if (not xml or not self_wxid or len(xml) > 65536
                or re.search(r"<!\s*(DOCTYPE|ENTITY)", xml, re.I)):
            return None
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml)
            if root.tag != "sysmsg" or root.get("type") != "pat":
                return None
            pats = root.findall("pat")
            if len(pats) != 1:
                return None
            pat = pats[0]
            fields = {}
            for name in ("fromusername", "拍拍者", "pattedusername", "拍者", "chatusername", "template"):
                nodes = pat.findall(name)
                if len(nodes) == 1 and nodes[0].text:
                    fields[name] = html.unescape(nodes[0].text.strip())
            target = fields.get("pattedusername", "")
            actor = fields.get("fromusername", "")
            chat = fields.get("chatusername", "")
            if target != self_wxid or not actor or (chat and chat != expected_chat):
                return None
            return actor
        except Exception:
            return None

    def on_message(msg, listener):
        try:
            username = msg.get("username") or ""
            if not username:
                return
            mtype = msg.get("type") or ""
            raw = msg.get("content") or msg.get("raw_content") or ""
            pat_actor = ""
            try:
                native_type = int(msg.get("wx_type") or 0)
            except (TypeError, ValueError):
                native_type = 0
            if native_type in (10000, 10002):
                system_xml = str(msg.get("system_content") or raw or "")
                if system_xml and not re.search(r"<sysmsg\b", system_xml, re.I):
                    log("收到微信系统消息但 XML 不是 sysmsg：wx_type=%s chat=%s xml=%s" % (
                        native_type, username, system_xml[:1200]))
                pat_actor = _解析拍一拍(system_xml, me or "", username) or ""
                if not pat_actor:
                    if system_xml and re.search(r"type\s*=\s*['\"]pat['\"]", system_xml, re.I):
                        log("拍一拍解析未命中：wx_type=%s chat=%s account=%s xml=%s" % (
                            native_type, username, me or "", system_xml[:1200]))
                    # 其他系统事件（撤回、成员变动等）不作为聊天发给模型。
                    return
                sender_for_pat = pat_actor
                sender_name = db.get_nickname(sender_for_pat) or sender_for_pat
                raw = "%s 拍了拍我" % sender_name
                content = "（有人拍了拍你）"
                mtype = "文本"
                msg = dict(msg, content=content, raw_content=raw, sender_username=sender_for_pat,
                           type=mtype, create_time=msg.get("create_time") or time.time())
            # 侦察：引用/卡片这类 XML 消息，把原始格式打到日志里，好确认字段名
            if "<" in raw and ("refermsg" in raw or "appmsg" in raw):
                log("XML 消息：type=%s user=%s raw=%s" % (mtype, username, raw[:500]))
            图片待下 = ""
            图片类型 = ""
            引用自本鱼 = False   # 这条是不是「引用/回复了本鱼说的话」——是的话等于当面点名
            引用原话 = ""
            if mtype == "图片":
                content = "（发来一张图片）"
                图片类型 = "图片"
                图片待下 = "图片"
            elif mtype == "动画表情":
                content = "（发来一个表情包）"
                图片类型 = "表情包"
                图片待下 = "表情包"
            elif mtype in ("文本", "文件/链接/卡片"):
                content = clean_text(raw)
                if mtype == "文件/链接/卡片":
                    # 卡片 / 引用消息：正文在 <title>，被引用的那句在 <refermsg> 里
                    mt = re.search(r"<title>(.*?)</title>", raw, re.S)
                    body = clean_text(html.unescape(mt.group(1))) if mt else ""
                    mr = re.search(r"<refermsg>(.*?)</refermsg>", raw, re.S)
                    if mr:
                        引用段 = mr.group(1)
                        mq = re.search(r"<content>(.*?)</content>", 引用段, re.S)
                        引用原话 = clean_text(html.unescape(mq.group(1))) if mq else ""
                        # 被引用人的 wxid：微信各版本字段名不一样，挨个试
                        引用人 = ""
                        for 标签 in ("chatusr", "fromusr", "username"):
                            mm = re.search(r"<%s>(.*?)</%s>" % (标签, 标签), 引用段, re.S)
                            if mm:
                                引用人 = mm.group(1).strip()
                                break
                        引用自本鱼 = bool(me and 引用人 and 引用人 == me)
                        content = (f"（引用：{引用原话}）{body}" if 引用原话 else body).strip()
                    else:
                        content = body
            else:
                # 没认出来的类型也留个底：引用/卡片这类如果落在别的 type 里，
                # 至少能从日志看到它的 mtype 和原始内容，好对症加解析
                if raw and "<" in raw:
                    log("未识别消息：type=%s user=%s raw=%s" % (mtype, username, raw[:500]))
                return
            if not content or content.startswith("<"):
                return
            sender = msg.get("sender_username") or ""
            if me and sender == me:
                # 自己发的：绝大多数是主人在电脑/手机上打字，不该回环。
                # 例外是「文件传输助手」—— 那是他给自己留的入口，可以当对话窗用；
                # 但我们自己刚发出去的那几条要跳过，否则会自问自答。
                if str(username) != "filehelper":
                    return
                now = time.time()
                # 兜底：刚往外发完的这十几秒，文件传输助手里冒出来的都不当输入
                if now - STATE["last_send"] < 12:
                    return
                if any(content == t and now - ts < 120 for (t, ts) in recently_sent):
                    return
            create_time = float(msg.get("create_time") or 0)
            if create_time and create_time < START_TS - SKIP_OLDER:
                return              # 启动之前的旧消息

            chat = display_name(username) or username
            if ONLY_CHATS and chat not in ONLY_CHATS:
                return

            key = "%s|%s|%s|%s" % (username, sender, create_time, (raw or content)[:60])
            if key in seen:
                return
            seen.add(key)
            if len(seen) > 2000:
                seen.clear()

            nickname = ""
            try:
                nickname = db.get_nickname(sender) or ""
            except Exception:
                pass
            is_group = str(username).endswith("@chatroom")
            群设置 = resolve_group_settings(username, chat) if is_group else None
            wechat_nickname = nickname
            if is_group:
                display_name(sender, username)  # Populate the per-group raw card cache.
            group_nickname = (group_card_cache.get(username) or {}).get(sender, "") if is_group else ""
            if group_nickname:
                nickname = group_nickname
            原话 = content
            # 追话计数：本鱼在这个群说完话后，数一数后面跟了几句（发完回复就清零）
            if is_group:
                追话计数[chat] = 追话计数.get(chat, 0) + 1

            # 屏蔽名单：微信运动之类，既不读也不回，日记也不记
            if is_blocked(chat, nickname, username):
                return

            # 群聊记日记 —— 不管点没点名都记，这是「群里的记忆」
            if is_group and 群设置.get("日记", CFG.get("日记", True)):
                diary_append(chat, create_time, nickname or "某人", content, 群设置)

            # 群里：要不要接「没点名」的闲聊，看设置「群里只认点名」（默认开 = 只在叫到名字时进 DSH）
            at_me = bool(pat_actor)
            话题命中 = False
            追话命中 = False
            命中 = None
            if is_group:
                at_me = bool(pat_actor) or any(a in raw for a in (群设置.get("点名别名") or [])) or 引用自本鱼
                群规则 = _群规则(username, chat)
                if not group_sender_allowed(群规则, sender, nickname, is_master(sender, nickname)):
                    if not at_me:
                        return
                    # 交给宿主排固定回复；不运行话题/追话/记忆/图片/总结等 AI 处理。
                    待回复.setdefault(chat, []).append({
                        "id": sender, "chatroom_id": username, "name": nickname or "",
                        "quote": _消息正文(raw)[:24],
                        "native_message_id": str(msg.get("native_message_id") or ""),
                        "ts": time.time(),
                    })
                    待回复[chat] = 待回复[chat][-12:]
                    r = http_json("/dsh-wechat-bridge/receive", {
                        "text": 原话, "event": "pat" if pat_actor else "",
                        "chat": chat, "chatId": username, "chatType": "group",
                        "sender": nickname or "群里的人", "senderId": sender,
                        "nativeMessageId": str(msg.get("native_message_id") or ""),
                        "atMe": True,
                    })
                    if not r.get("ok"):
                        log("群固定回复未排队（%s）：%s" % (chat, r.get("说")))
                    return
                if 引用自本鱼:
                    log("引用本鱼：%s 引用了「%s」" % (sender, 引用原话[:40]))
                # 话题规则：一条是 {词, 回}。命中哪条就把那条的写法塞给模型，
                # 比统一一句「想接就接」有分寸 —— 提到白饭和提到骂人，接法不该一样。
                # 老配置里存的是「词 = 怎么回」的字符串，也认。
                规则 = []
                for 项 in (群设置.get("话题规则") or []):
                    if isinstance(项, dict):
                        # 每条规则前面有个复选框：关掉的这条直接不参与匹配
                        if 项.get("启用") is False:
                            continue
                        # 「词」那一栏每行一个，任意一个命中都按同一条规则接
                        词们 = [x.strip() for x in str(项.get("词") or "").splitlines() if x.strip()]
                        v = str(项.get("回") or "").strip()
                        try:
                            窗口 = max(0, int(项.get("窗口") or 0))
                        except Exception:
                            窗口 = 0
                        try:
                            次数 = max(0, int(项.get("次数") or 0))
                        except Exception:
                            次数 = 0
                    else:
                        s = str(项).strip()
                        词们, v, 窗口, 次数 = ([s] if s else []), "", 0, 0
                        for 分隔 in ("=>", "＝", "=", "|"):
                            if 分隔 in s:
                                k, v = s.split(分隔, 1)
                                词们 = [k.strip()] if k.strip() else []
                                v = v.strip()
                                break
                    if 词们:
                        规则.append((词们, v, 窗口, 次数))
                for 词们, v, 窗口, 次数 in 规则:
                    for k in 词们:
                        if k and k in raw:
                            命中 = (k, v, 窗口, 次数)
                            break
                    if 命中:
                        break
                话题命中 = 命中 is not None or (
                    bool(CFG.get("唤醒词"))
                    and any(str(w).strip() in raw for w in CFG["唤醒词"] if str(w).strip())
                )
                只认点名 = bool(群设置.get("群里只认点名", True))
                # 只认点名：没喊名字、也没踩到话题词的一律不进 DSH —— 日记照记、群上下文照攒。
                # 关掉：主人说话 / 活跃群 也算叫到（老行为），再吃频率闸。
                called = at_me or 话题命中 or (
                    not 只认点名
                    and (is_master(sender, nickname) or chat in CFG.get("活跃群", []))
                )
                # 追话：本鱼刚在这个群说完话，后面 N 句里进来的消息很可能是接话的。
                # 交给认领判断过一遍，明确了才回；看不出就不回（保守，不乱插嘴）。
                if not called:
                    追话句数 = int(群设置.get("追话句数") or 0)
                    if 追话句数 > 0 and 追话计数.get(chat, 0) <= 追话句数:
                        判断 = 认领判断(content, nickname, group_context(username, chat, int(群设置.get("群上下文条数") or 0)),
                                     最后回复.get(chat, ""))
                        log("追话判断（%s 第 %d 句）：%s「%s」→ %s"
                            % (chat, 追话计数.get(chat, 0), nickname or "某人",
                               str(raw)[:20], 判断))
                        if 判断 == "yes":
                            called = True
                            追话命中 = True
                if 命中:
                    log("话题命中「%s」：%s" % (命中[0], chat))
                if not called:
                    return
                if pat_actor:
                    at_me = True
                    raw = "%s 拍了拍我" % (nickname or sender)
                    log("拍一拍点名：%s 在 %s 拍了拍当前账号，转入 DSH" % (nickname or sender, chat))
                # 点名必进；话题词和普通闲聊都吃频率闸 —— 谁发的都一样，不然主人连着刷
                # 几句「欢迎新人」就能把它刷成复读机。被拦的原因和剩余时间由 频率允许() 自己记日志。
                # 追话（紧接着接本鱼的话）不吃频率闸：本鱼刚说完、对方马上接「你在干嘛」，
                # 不该被 30 秒冷却挡掉。
                if (not at_me and not pat_actor and not 追话命中
                        and not 频率允许(chat, 命中[0] if 命中 else "",
                                         命中[2] if 命中 else 0, 命中[3] if 命中 else 0, 群设置)):
                    return
                prefix = group_context(username, chat, int(群设置.get("群上下文条数") or 0))
                # 记忆：先记这一笔（谁在跟本鱼互动），再把对这个人的印象 / 群像拼进上下文
                if 引用自本鱼:
                    记事件(me or "", chat, sender, nickname, "引用", raw, 引用原话)
                elif at_me:
                    记事件(me or "", chat, sender, nickname, "点名", raw)
                elif 命中:
                    记事件(me or "", chat, sender, nickname, "话题接话", raw)
                这段记忆 = 记忆上下文(me or "", chat, sender, nickname)
                if 这段记忆:
                    prefix = 这段记忆 + prefix
                # 问「群里多少人」这种：模型自己看不到成员表，桥接这边能查，
                # 顺手把人数塞进上下文，别让它只能答「我看不到」。
                if re.search(r"多少人|几个人|群人数|成员数|多少位", raw):
                    try:
                        人数 = len(db.get_group_members(username) or [])
                        if 人数:
                            prefix = "（这个群现在 %d 人）\n%s" % (人数, prefix or "")
                    except Exception as e:
                        log("查群人数失败：", repr(e))
                if re.search(r"谁.*(没|没有|未).*(设置|填).*(群昵称|群名片)|谁.*(群昵称|群名片).*(没|没有|未).*(设置|填)|哪些人.*(没|没有|未).*(设置|填).*(群昵称|群名片)", raw):
                    try:
                        from group_nicknames import get_group_nickname_status
                        状态 = get_group_nickname_status(db, username)
                        if 状态.get("error"):
                            prefix = "（群昵称状态读取失败：%s）\n%s" % (状态["error"], prefix or "")
                        else:
                            未设置 = 状态.get("unset") or []
                            名单 = [str(x.get("remark") or x.get("nick_name") or x.get("username") or "未知成员") for x in 未设置]
                            if 名单:
                                prefix = "（这个群检测到 %d/%d 位成员没有群昵称：%s）\n%s" % (
                                    len(名单), len(状态.get("members") or []), "、".join(名单[:80]), prefix or "")
                            else:
                                prefix = "（这个群的 %d 位成员都检测到已设置群昵称）\n%s" % (
                                    len(状态.get("members") or []), prefix or "")
                    except Exception as e:
                        log("查询未设置群昵称失败：", repr(e))
                if 引用自本鱼:
                    hint = ("（有人引用/回复了你刚才说的话，等于当面跟你讲，必须回；"
                            "顺着被引用的那句接，别答非所问。）")
                elif at_me:
                    hint = "（这条点名了你，必须回。说话的是「%s」。）" % (nickname or "某人")
                elif 命中:
                    if 命中[1]:
                        hint = (
                            "（有人提到「%s」。按下面的要求回：只做要求里写的事，别加理由、别自由发挥；"
                            "语气和措辞都照它来——它说要友好，就得是友好口气，不是通知口气。\n%s\n"
                            "不想接就只回两个字 [不回]。）" % (命中[0], 命中[1]))
                    else:
                        # 纯唤醒词（没写怎么回）：不强制说什么，但别白叫 —— 顺着最近的聊天接一两句，
                        # 真接不上才允许 [不回]，不然「叫醒了没反应」跟没唤醒一样。
                        hint = ("（有人提到「%s」，把你叫醒了。看看上面这几条在聊什么，顺着接一两句，"
                                "语气自然点；实在接不上才回两个字 [不回]。）" % 命中[0])
                elif 话题命中:
                    hint = ("（有人提到你挂心的话题词，把你叫醒了。看看上面在聊什么，顺着接一两句；"
                            "实在接不上才回两个字 [不回]。）")
                else:
                    hint = "（这条没点名你：想回就回；不想回就只回两个字 [不回]，别发别的。）"
                content = (prefix or "") + hint + content
                # 对方明确要看图时，把「去调 send_meme」再说一遍 —— 群里的模型
                # 常把「发个表情包看看」理解成在讨论发图，然后回一段解释
                if re.search(r"表情包|发图|来张图|来张表情|斗图|发个表情", raw):
                    content += (
                        "\n（对方要看图：现在就去调 send_meme 拿一张，把候选的 [表情: 描述] "
                        "原样写进回复；实在没有合适的再用纯文字说明。）")
                # 带情绪的消息更容易配图：桥接帮着提一句，别让「活跃」档一整天不出一张
                elif re.search(
                        r"哈哈|笑死|233|好耶|开心|高兴|太棒|气死|烦死|讨厌|呜呜|哭了|难过|伤心"
                        r"|卧槽|我靠|天呐|离谱|无语|服了|可爱|喜欢|爱你|么么|宝贝", raw):
                    content += (
                        "\n（这条在表达情绪：适合配一张表情包，去调 send_meme 挑一张贴的，"
                        "把候选的 [表情: 描述] 原样写进回复。）")

            # 「总结 / 汇总」这类请求：不从日记 md 读，直接从微信库里按时间取一段
            if 是想总结(原话):
                try:
                    f, 终, 标签 = 总结时间范围(原话)
                    历史 = 取库历史(username, f, 终, 400)
                    # 把「刚问的这一句」摘掉，它在问题里已经有了
                    历史 = [
                        m for m in 历史
                        if not (
                            float(m.get("create_time") or 0) > time.time() - 180
                            and clean_text(m.get("content") or "").strip() == str(原话).strip()
                        )
                    ]
                    行们 = 历史转文字(历史, chatroom_username=username if str(username).endswith("@chatroom") else "")
                    if 行们:
                        指导来源 = 群设置 if 群设置 is not None else CFG
                        指导 = str(指导来源.get("总结提示词") or "").strip() or DEFAULT_SUMMARY_GUIDE
                        content = (
                            "（按设置：下面是从微信消息库里按时间直接取出的「%s」记录，"
                            "共 %d 行（同一个人 90 秒内的连发已合并成一行）。"
                            "请据此总结，不要说「只看到几条」也别编。\n%s\n\n%s\n）\n"
                            % (标签, len(行们), "\n".join(行们), 指导)
                        ) + content
                        log("按请求读库：%s｜%s｜%d 行" % (chat, 标签, len(行们)))
                    else:
                        content = (
                            "（微信库里「%s」这段时间没有文字记录 —— 如实说没记录，别编。）\n" % 标签
                        ) + content
                        log("按请求读库：%s｜%s｜没有记录" % (chat, 标签))
                except Exception as e:
                    log("总结取库出错：", repr(e))

            # 图片 / 表情包：老宿主没有看图能力，就由桥接先看；新宿主交给它看
            图片路径 = []
            if 图片待下:
                p = msg.get("native_image_path")
                if not p:
                    p = _保存表情包(raw) if 图片待下 == "表情包" else _保存图片(msg, username)
                if p:
                    if HOST_VERSION >= (1, 2, 0):
                        图片路径 = [p]
                    else:
                        content = _看图并替换(content, p, 图片类型)
                elif msg.get("image_status") in ("failed", "unavailable", "skipped"):
                    log("[%s] 普通图片接收未完成：%s" % (
                        BACKEND_LABEL, msg.get("image_error") or msg.get("image_status")))
                    return

            # 记住「这一轮是谁叫的」：宿主回复时会带回这个 wxid，桥接照它引用 / @。
            # 只留最近 10 分钟、最多 12 条，别无限涨。
            待回复.setdefault(chat, []).append({
                "id": sender,
                "chatroom_id": username if is_group else "",
                "name": nickname or "",
                "quote": _消息正文(raw)[:24] if is_group else "",
                "native_message_id": str(msg.get("native_message_id") or ""),
                "ts": time.time(),
            })
            _现在 = time.time()
            待回复[chat] = [x for x in 待回复[chat]
                            if _现在 - float(x.get("ts") or 0) < 600][-12:]

            LAST_CHAT["chat"] = chat
            LAST_CHAT["ts"] = time.time()
            # 这条是不是群消息、原文是什么：回复时第一条拿来「引用」，群里多人同时说话也看得出在回谁。
            # 引用靠 OCR 在屏幕上找这条文字：太短（「嗯」「哈哈」）容易命中别人的消息、引错人，
            # 太长又会被换行/省略号截断认不出来，24 个字是个折中。
            LAST_CHAT["quote"] = _消息正文(raw)[:24] if is_group else ""
            LAST_CHAT["group"] = bool(is_group)
            # 这条是谁说的：引用发不出去时，退回普通发送并在开头带上「@昵称」
            LAST_CHAT["sender"] = nickname or ""
            r = http_json("/dsh-wechat-bridge/receive", {
                "text": content,
                "event": "pat" if pat_actor else "",
                "chat": chat,
                # 会话的底层 id（好友 wxid / xxx@chatroom）：绑定和屏蔽填这个名字也认，
                # 改备注、同名都不怕对不上
                "chatId": username,
                "chatType": "group" if is_group else "private",
                "sender": nickname or ("群里的人" if is_group else "对方"),
                "senderId": sender,
                "nativeMessageId": str(msg.get("native_message_id") or ""),
                "事件": "拍一拍" if pat_actor else "",
                "atMe": True if not is_group else at_me,
                # 话题命中单独报：宿主据此放行（不是点名也能进），
                # 但模型回 [不回] 时照样要丢掉 —— 不能把这两个语义混成 atMe。
                "话题命中": bool(话题命中) if is_group else False,
                # 追话同理：本鱼刚说完话、对方紧接着在接，明确判过 yes 才传
                "追话": bool(追话命中) if is_group else False,
                "图片": 图片路径,
                "图片类型": 图片类型,
                "native_message_id": str(msg.get("native_message_id") or ""),
            })
            if r.get("ok"):
                log("[%s] → DSH（at=%s，图=%d）：%s ｜ %s ｜ raw=%s" % (
                    BACKEND_LABEL, at_me, len(图片路径), chat, content[:40], raw[:80]))
            else:
                log("[%s] ⚠ DSH 没收下：%s ｜ %s" % (
                    BACKEND_LABEL, r.get("说"), content[:40]))
        except Exception:
            log("处理消息出错：", traceback.format_exc(limit=2))

    if ARGS.test_image:
        描述 = _看图(ARGS.test_image, "表情包")
        with open(os.path.join(HERE, "vision-test.txt"), "w", encoding="utf-8") as f:
            f.write(描述 or "(empty)")
        log("看图测试：", 描述 or "(空)")
        return 0

    if getattr(backend, "name", "") == "native_hook":
        backend.set_image_callback(on_message)
    listener = backend.start_listener(on_message)
    log("消息监听已启动：%s" % backend.name)
    log("监听已启动（0.4 秒一轮）")

    load_memes()

    threading.Thread(target=outbox_loop, daemon=True).start()

    refresh_config()
    log("配置：群上下文 %d 条｜群里只认点名 %s｜日记 %s｜主人 %s｜屏蔽 %d 个｜话题规则 %d 条"
        "｜点名别名 %s" % (
        CFG["群上下文条数"], "开" if CFG.get("群里只认点名", True) else "关",
        "开" if CFG["日记"] else "关",
        "/".join(CFG["主人"]) or "（没设）", len(CFG["屏蔽"]),
        len(CFG.get("话题规则") or []),
        "/".join(ALIASES) or "（没设）"))

    def config_loop():
        while True:
            time.sleep(30)
            refresh_config()

    threading.Thread(target=config_loop, daemon=True).start()

    def member_snapshot_loop():
        log('群成员快照已启动（每 30 秒；每群独立 JSON，不读 Excel）')
        while True:
            try:
                refresh_config()
                if not CFG.get('成员快照', True):
                    time.sleep(5)
                    continue
                interval = float(CFG.get('成员快照秒') or 30)
                groups = set()
                for item in (CFG.get('绑定') or []):
                    chat = str(item.get('聊天') or '').strip()
                    if chat.endswith('@chatroom'):
                        groups.add(chat)
                try:
                    recent = http_json('/dsh-wechat-bridge/state', {"账号": me})
                    for item in recent.get('最近聊天') or []:
                        cid = str(item.get('id') or '').strip()
                        if cid.endswith('@chatroom'):
                            groups.add(cid)
                except Exception:
                    pass
                for group in groups:
                    group_settings = resolve_group_settings(group, display_name(group))
                    if not group_settings.get('成员快照', True):
                        continue
                    members = db.get_group_members(group) or []
                    card_map = {}
                    try:
                        from group_nicknames import load_group_nickname_map
                        contact_path = next(os.path.join(db.workdir, rel.replace(os.sep, '__'))
                                            for rel, source, _ in db._db_files
                                            if os.path.basename(source).lower() == 'contact.db')
                        card_map = load_group_nickname_map(contact_path, group,
                                                           [str(m.get('username') or '') for m in members])
                    except Exception:
                        pass
                    current = member_records(members, card_map)
                    path = snapshot_path(snapshot_root, group)
                    previous = load_snapshot(path)
                    added, removed, baseline = diff(previous, current)
                    save_snapshot(path, current)
                    if baseline:
                        log('群成员快照建立基线：%s（%d 人）' % (group, len(current)))
                        continue
                    if not added and not removed:
                        continue
                    by_id = {str(m.get('username') or '').strip(): m for m in members}
                    for uid in sorted(added):
                        m = by_id.get(uid, {})
                        card = display_name(uid, group)
                        nick = str(m.get('nick_name') or '').strip()
                        texts = render_lines(group_settings.get('入群提醒') or '', group=display_name(group), group_nickname=card,
                                              wechat_nickname=nick, wxid=uid, count=len(current))
                        for text in texts:
                            http_json('/dsh-wechat-bridge/send', {'聊天': display_name(group), '文本': text})
                        if texts:
                            log('检测到入群：%s → %s（%d 条）' % (group, uid, len(texts)))
                    if removed:
                        # 同一轮扫描可能同时退了多人：先合并变量，再按模板行各发一次，避免逐人刷屏。
                        removed_rows = [(uid, previous.get(uid) or {}) for uid in sorted(removed)]
                        cards = [str(old.get('群昵称') or '').strip() for _, old in removed_rows]
                        nicks = [str(old.get('微信昵称') or '').strip() for _, old in removed_rows]
                        ids = [uid for uid, _ in removed_rows]
                        mention_names = [card or nick for card, nick in zip(cards, nicks)]
                        texts = render_lines(
                            group_settings.get('退群提醒') or '', group=display_name(group),
                            group_nickname='、'.join(x for x in cards if x),
                            wechat_nickname='、'.join(x for x in nicks if x),
                            wxid='、'.join(ids), count=len(removed),
                        )
                        for text in texts:
                            http_json('/dsh-wechat-bridge/send', {'聊天': display_name(group), '文本': text})
                        if texts:
                            log('检测到退群：%s → %s（%d 人，%d 条）' % (group, '、'.join(x for x in mention_names if x) or '未知成员', len(removed), len(texts)))
            except Exception:
                log('群成员快照出错：', traceback.format_exc(limit=2))
            time.sleep(interval)

    threading.Thread(target=member_snapshot_loop, daemon=True).start()

    # 画像合成：有记忆密钥才起（没有就只走账本，不烧钱）
    if _记忆配置().get("key"):
        threading.Thread(target=画像线程, daemon=True).start()
        log("画像合成线程已启动（10 分钟后第一次，之后每 6 小时扫当天日记）")
    else:
        log("没配记忆密钥，画像合成跳过（好感账本照常工作）")

    st = http_json("/dsh-wechat-bridge/state", {"账号": me})
    if st.get("ok"):
        try:
            段 = [int(x) for x in str(st.get("版本") or "1.1.0").split(".")[:3]]
            while len(段) < 3:
                段.append(0)
            HOST_VERSION = tuple(段)
        except Exception:
            HOST_VERSION = (1, 1, 0)
        log("DSH 在线，绑定 %s 条｜宿主 v%s" % (
            len(st.get("绑定情况") or []), ".".join(str(x) for x in HOST_VERSION)))
    else:
        log("⚠ DSH 没应答：%s" % st.get("说"))

    log("桥接已就绪。Ctrl+C 停。")

    # 桥刚连上时，给主人发一句撒娇（每天最多一次，免得每次重启都烦他）
    def _连上撒娇():
        try:
            主人们 = [str(x).strip() for x in (CFG.get("主人") or []) if str(x).strip()]
            if not 主人们:
                return
            收件人 = 主人们[0].split("/")[0].strip()
            if not 收件人:
                return
            标记 = os.path.join(
                os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
                "wx-greeting.json",
            )
            今天 = time.strftime("%Y-%m-%d")
            try:
                with open(标记, encoding="utf-8") as f:
                    上次 = (json.load(f) or {}).get("日期")
            except Exception:
                上次 = None
            if 上次 == 今天:
                return
            time.sleep(20)  # 等窗口和 UIA 热起来
            候选 = [
                "我回来啦。今天也在。",
                "连上了连上了，先跟你报到一下，别不理我呀。",
                "桥接好了。你要是忙就先忙，我挂在这儿。",
            ]
            文案 = random.choice(候选)
            结果 = send_one({"chat": 收件人, "text": 文案, "images": []})
            if bool((结果 or {}).get("ok")):
                with open(标记, "w", encoding="utf-8") as f:
                    json.dump({"日期": 今天, "时间": time.time()}, f, ensure_ascii=False)
                log("已发连上撒娇（%s）" % 收件人)
            else:
                log("连上撒娇没发出去：%s" % (结果 or {}).get("说"))
        except Exception:  # noqa: BLE001
            log("连上撒娇失败：%s" % traceback.format_exc(limit=2))

    threading.Thread(target=_连上撒娇, daemon=True).start()

    try:
        while True:
            time.sleep(30)
            th = getattr(listener, "_thread", None)
            if th is not None and not th.is_alive():
                log("[%s] 监听线程掉了，先清理原生服务再重启" % BACKEND_LABEL)
                try:
                    backend.stop_listener(listener)
                except Exception:
                    log("[%s] 清理掉线监听失败：" % BACKEND_LABEL, traceback.format_exc(limit=2))
                try:
                    backend.connect()
                    listener = backend.start_listener(on_message)
                    log("[%s] 监听线程已重启" % BACKEND_LABEL)
                except Exception:
                    log("[%s] 拉不起来：" % BACKEND_LABEL, traceback.format_exc(limit=2))
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    finally:
        try:
            backend.stop_listener(listener)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
