"""Conservative self-reported profile hints from group cards, not verified facts."""
import re
import unicodedata

REGIONS = ('黑龙江 河北 山西 辽宁 吉林 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 广西 海南 四川 贵州 云南 陕西 甘肃 青海 台湾 内蒙古 宁夏 新疆 西藏 香港 澳门 北京 天津 上海 重庆 哈尔滨 深圳 广州 佛山 中山 杭州 南昌 兰州 济南 武汉 徐州 苏州 成都 福州 襄阳'.split())


def extract_group_profile(card):
    text = unicodedata.normalize('NFKC', str(card or '')).strip()
    result = {}
    # Reject age/decade wording and ambiguous multiple numeric runs.
    numbers = list(re.finditer(r'(?<!\d)(\d{2}|(?:19|20)\d{2})(?!\d)', text))
    all_runs = re.findall(r'\d+', text)
    if len(numbers) == 1 and len(all_runs) == 1:
        match = numbers[0]
        token = match.group()
        suffix = text[match.end():]
        if not re.match(r'\s*(岁|后|cm|厘米|kg|公斤)', suffix, re.I):
            year = int(token) if len(token) == 4 else (1900+int(token) if int(token)>=70 else 2000+int(token))
            if 1970 <= year <= 2010:
                result['年龄_出生年份'] = '%02d' % (year % 100)
    found = [name for name in REGIONS if name in text]
    # Do not guess residence vs hometown or parse free-text mentions later in a card.
    if len(found) == 1 and text.startswith(found[0]):
        result['地区'] = found[0]
    elif len(found) == 1 and re.match(r'^\d{2}[- _]+%s' % re.escape(found[0]), text):
        result['地区'] = found[0]
    return result
