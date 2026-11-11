"""Per-group member snapshots. Deliberately independent from the Excel member workbook."""
import json
import os
import re


def safe_filename(chatroom):
    return re.sub(r'[^0-9A-Za-z._-]+', '_', str(chatroom or 'group'))[:120] or 'group'


def snapshot_path(root, chatroom):
    return os.path.join(root, 'wechat-member-snapshots', safe_filename(chatroom) + '.json')


def member_records(members, card_lookup=None):
    card_lookup = card_lookup or {}
    records = {}
    for member in members:
        uid = str(member.get('username') or '').strip()
        if not uid:
            continue
        records[uid] = {
            '群昵称': str(card_lookup.get(uid) or '').strip(),
            '微信昵称': str(member.get('nick_name') or '').strip(),
        }
    return records


def member_ids(members):
    return set(member_records(members))


def load_snapshot(path):
    try:
        with open(path, encoding='utf-8') as f:
            value = json.load(f)
        raw = value.get('members', {})
        # Compatibility with v1 snapshots that stored a list of wxids.
        if isinstance(raw, list):
            return {str(x).strip(): {} for x in raw if str(x).strip()}
        return {str(k).strip(): (v if isinstance(v, dict) else {}) for k, v in raw.items() if str(k).strip()}
    except Exception:
        return None


def save_snapshot(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'version': 2, 'members': records}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def diff(previous, current):
    if previous is None:
        return set(), set(), True
    return set(current) - set(previous), set(previous) - set(current), False


def render_lines(template, *, group='', group_nickname='', wechat_nickname='', wxid='', count=0):
    mention_name = str(group_nickname or '').strip() or str(wechat_nickname or '').strip()
    values = {'群名': group, '群昵称': group_nickname, '微信昵称': wechat_nickname,
              '昵称': mention_name, 'wxid': wxid, '人数': count}
    lines = []
    for line in str(template or '').splitlines():
        if not line.strip():
            continue
        try:
            text = line.format(**values).strip()
        except (KeyError, ValueError):
            text = line.strip()
        if text:
            lines.append(text)
    return lines


def render(template, **kwargs):
    return '\\n'.join(render_lines(template, **kwargs))
