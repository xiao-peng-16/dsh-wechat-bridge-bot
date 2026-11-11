"""Regression: workbook columns may move without mixing names or profile fields."""
import ast
import contextlib
import io
import json
import tempfile
from pathlib import Path
from openpyxl import Workbook, load_workbook
from bridge import member_edit
import sys
sys.path.insert(0, str(Path('bridge').resolve()))
from bridge.member_identity import find_member_row


def run():
    probe = Workbook().active
    probe.append(['wxid', '群昵称', '微信昵称'])
    probe.append(['id_a', '同名', '甲'])
    probe.append(['id_b', '乙群名', '同名'])
    probe.append([None, '丙群名', '丙'])
    assert find_member_row(probe, 'id_a', '同名', '丙群名') == 2
    assert find_member_row(probe, wechat_nickname='同名', group_nickname='同名') == 3
    assert find_member_row(probe, group_nickname='丙群名') == 4
    assert find_member_row(probe, 'id_new', '甲', '同名') is None
    probe.append([None, '丁群名', '丙'])
    try:
        find_member_row(probe, wechat_nickname='丙')
    except ValueError:
        pass
    else:
        raise AssertionError('Ambiguous names must not select the first row')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'members.xlsx'
        wb = Workbook()
        ws = wb.active
        ws.append(['wxid', '群昵称', '微信昵称', '星座', '性别'])
        ws.append(['wxid_one', '群内名', '原名 / 别名', '', '女'])
        wb.save(path)
        assert member_edit.找行(ws, '别名')[0][0].value == 'wxid_one'
        results, _, _ = member_edit.查(ws, {'关键词': '群内名'}, [], 30)
        assert results[0]['微信昵称'] == '原名 / 别名'
        for name, mode in [('wxid_one', 'update'), ('新人', 'upsert')]:
            payload = {'表': str(path), '名': name, '字段': '星座', '值': '天秤座', '模式': mode}
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import sys
                previous = sys.stdin
                try:
                    sys.stdin = io.StringIO(json.dumps(payload))
                    assert member_edit.main() == 0
                finally:
                    sys.stdin = previous
        saved = load_workbook(path)
        assert saved.active['B2'].value == '群内名'
        assert saved.active['C2'].value == '原名 / 别名'
        assert saved.active['D2'].value == '天秤座'
        assert saved.active['C3'].value == '新人'
        assert saved.active['D3'].value == '天秤座'
        # Execute the bridge's actual nested updater against the same reordered workbook.
        tree = ast.parse(Path('bridge/wechat_bridge.py').read_text(encoding='utf-8'))
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_改群友资料')
        import re
        import time
        env = {'os': __import__('os'), 're': re, 'time': time, '_群友表路径': lambda: str(path),
               '_星名们': ['天秤'], '_MBTI_RE': re.compile(r'(?!)'), 'log': lambda *args: None}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<bridge-updater>', 'exec'), env)
        env['_改群友资料']('wxid_one', '别名', '', '新群名')
        saved = load_workbook(path)
        assert saved.active['B2'].value == '新群名'
        assert saved.active['C2'].value == '原名 / 别名'
        env['_改群友资料']('wxid_two', '微信新名', '', '群新名')
        saved = load_workbook(path)
        assert [saved.active.cell(4,c).value for c in range(1,4)] == ['wxid_two', '群新名', '微信新名']
    print('Member-column query/update/upsert and bridge regression passed')


if __name__ == '__main__':
    run()
