from bridge.wechat_bridge import group_sender_allowed, resolve_group_settings


def run():
    config = {
        "群里只认点名": True,
        "追话句数": 3,
        "群上下文条数": 6,
        "点名别名": ["默认别名"],
        "话题规则": [{"词": "默认", "回": "默认回答"}],
        "群回复间隔秒": 10,
        "日记": True,
        "总结提示词": "默认总结",
        "成员快照": True,
        "入群提醒": "欢迎",
        "退群提醒": "再见",
        "群聊管理": [
            {"群": "同名群", "群里只认点名": False},
            {"群": "123@chatroom", "点名别名": [], "话题规则": [],
             "群回复间隔秒": 0, "入群提醒": "", "日记": False},
        ],
    }
    by_id = resolve_group_settings("123@chatroom", "同名群", config)
    assert by_id["群里只认点名"] is True
    assert by_id["点名别名"] == [] and by_id["话题规则"] == []
    assert by_id["群回复间隔秒"] == 0 and by_id["入群提醒"] == ""
    assert by_id["日记"] is False and by_id["追话句数"] == 3
    by_name = resolve_group_settings("456@chatroom", "同名群", config)
    assert by_name["群里只认点名"] is False
    unknown = resolve_group_settings("789@chatroom", "其他群", config)
    assert unknown["点名别名"] == ["默认别名"]
    assert unknown["入群提醒"] == "欢迎"
    assert group_sender_allowed({}, "wxid_a", "甲")
    assert not group_sender_allowed({"所有人可聊": False}, "wxid_a", "甲")
    assert group_sender_allowed({"所有人可聊": False}, "wxid_owner", "主人", owner=True)
    whitelist = {"群聊权限": "仅白名单可聊", "群白名单": ["wxid_a", "小明"]}
    assert group_sender_allowed(whitelist, "wxid_a", "甲")
    assert group_sender_allowed(whitelist, "wxid_b", "小明")
    assert not group_sender_allowed(whitelist, "wxid_b", "乙")
    assert not group_sender_allowed({"群聊权限": "仅白名单可聊", "群白名单": []}, "wxid_a", "甲")
    assert group_sender_allowed({"群聊权限": "所有人可以聊"}, "wxid_b", "乙")
    assert not group_sender_allowed({"群聊权限": "所有人不可以聊"}, "wxid_b", "乙")
    print("group settings regression passed")


if __name__ == "__main__":
    run()
