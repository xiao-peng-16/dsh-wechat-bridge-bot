"""Workbook identity matching: wxid > WeChat nickname > group card."""


def find_member_row(ws, wxid='', wechat_nickname='', group_nickname=''):
    headers = {str(c.value or '').strip(): c.column for c in ws[1]}
    nick_header = '微信昵称' if '微信昵称' in headers else '昵称'
    rows = list(ws.iter_rows(min_row=2))
    tiers = [('wxid', wxid), (nick_header, wechat_nickname), ('群昵称', group_nickname)]
    for heading, value in tiers:
        value = str(value or '').strip()
        col = headers.get(heading)
        if not value or not col:
            continue
        matches = []
        for row in rows:
            stored_id = str(row[headers['wxid']-1].value or '').strip() if 'wxid' in headers else ''
            # A known different ID is never overridden by a name match.
            if heading != 'wxid' and wxid and stored_id and stored_id != wxid:
                continue
            stored = str(row[col-1].value or '').strip()
            aliases = [stored] if heading == 'wxid' else [stored] + [x.strip() for x in stored.split('/')]
            if value in aliases:
                matches.append(row)
        if len(matches) > 1:
            raise ValueError('身份匹配不唯一：%s，行 %s' % (heading, ','.join(str(r[0].row) for r in matches)))
        if matches:
            return matches[0][0].row
    return None
