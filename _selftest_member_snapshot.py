import json
import tempfile
from pathlib import Path
from bridge.member_snapshot import snapshot_path, member_records, load_snapshot, save_snapshot, diff, render, render_lines

def run():
    with tempfile.TemporaryDirectory() as d:
        p=snapshot_path(d,'49676485022@chatroom')
        old=member_records([{'username':'a','nick_name':'微信甲'},{'username':'b','nick_name':'微信乙'}], {'a':'群甲','b':'群乙'})
        assert load_snapshot(p) is None
        save_snapshot(p,old)
        assert load_snapshot(p)['a']=={'群昵称':'群甲','微信昵称':'微信甲'}
        added,removed,baseline=diff(load_snapshot(p),{'b':old['b'],'c':{}})
        assert added=={'c'} and removed=={'a'} and not baseline
        legacy=Path(d)/'legacy.json'; legacy.write_text(json.dumps({'members':['x']}),encoding='utf-8')
        assert load_snapshot(str(legacy))=={'x':{}}
        assert render('欢迎 {昵称}（{微信昵称}） {wxid} {人数}',group_nickname='卡片',wechat_nickname='昵称',wxid='c',count=2)=='欢迎 卡片（昵称） c 2'
        assert render('{昵称}', group_nickname='', wechat_nickname='微信名') == '微信名'
        assert render_lines('第一条 {昵称}\n\n第二条 {人数}', group_nickname='卡片', wechat_nickname='微信名', count=2) == ['第一条 卡片', '第二条 2']
        assert render_lines('{昵称} 已离开群聊（共 {人数} 人）', group_nickname='群甲、群乙、群丙', count=3) == ['群甲、群乙、群丙 已离开群聊（共 3 人）']
        assert snapshot_path(d,'room/a').endswith('room_a.json')
    print('member snapshot regression passed')
if __name__=='__main__':run()
