// DSH「通到微信」· 网页半边
//
// 在 DSH 左侧插件栏提供「通到微信」独立页面：
//   装依赖 → 启动桥接 → 选会话 → 开关 → 试一下。
//
// ⚠ 这里不碰会话、不发消息，只是往宿主那几条 /dsh-wechat-bridge/* 路由上读写。

window.__ModuleLoader__.load({
  id: 'dsh-wechat-bridge',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    const React = require('react')
    const inject = ['slots']

    const B = '/dsh-wechat-bridge'
    const get = (p) => fetch(B + p, { cache: 'no-store' }).then((r) => r.json()).catch(() => null)
    const post = (p, body) =>
      fetch(B + p, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
      })
        .then((r) => r.json())
        .catch(() => null)

    const h = React.createElement

    const S = {
      wrap: {
        padding: '12px 14px 24px',
        fontSize: 13,
        lineHeight: 1.7,
        background: 'var(--dsw-alias-bg-layer-1, rgba(255,255,255,.92))',
        border: '1px solid var(--dsw-alias-border-l1, rgba(0,0,0,.08))',
        borderRadius: 12,
        boxSizing: 'border-box',
        width: '100%',
        height: '100%',
        maxHeight: '100%',
        overflowY: 'auto',
        overflowX: 'hidden',
      },
      h: { fontSize: 14, fontWeight: 600, margin: '18px 0 8px' },
      dim: { opacity: 0.62, fontSize: 12 },
      card: {
        border: '1px solid var(--dsw-alias-border-l2, rgba(0,0,0,.12))',
        borderRadius: 8,
        padding: '10px 12px',
        marginBottom: 10,
        background: 'var(--dsw-alias-bg-layer-1)',
      },
      row: { display: 'flex', gap: 10, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' },
      lab: { minWidth: 170, fontSize: 12.5 },
      input: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 6,
        padding: '4px 8px',
        background: 'transparent',
        color: 'inherit',
        fontSize: 12.5,
        boxSizing: 'border-box',
      },
      area: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 6,
        padding: '6px 8px',
        background: 'transparent',
        color: 'inherit',
        fontSize: 12.5,
        lineHeight: 1.6,
        boxSizing: 'border-box',
        resize: 'vertical',
        fontFamily: 'inherit',
      },
      hint: { opacity: 0.6, fontSize: 11.5, marginTop: 2 },
      ok: { color: 'var(--dsw-alias-state-business-primary)', fontSize: 12 },
      bad: { color: '#c0392b', fontSize: 12, fontWeight: 600 },
      pre: {
        whiteSpace: 'pre-wrap',
        fontSize: 11.5,
        lineHeight: 1.5,
        maxHeight: 220,
        overflow: 'auto',
        background: 'rgba(127,127,127,.08)',
        borderRadius: 6,
        padding: '8px 10px',
        fontFamily: 'ui-monospace, Consolas, monospace',
      },
      btn: (primary) => ({
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 6,
        padding: '5px 12px',
        background: primary ? 'var(--dsw-alias-state-business-primary)' : 'transparent',
        color: primary ? '#fff' : 'inherit',
        cursor: 'pointer',
        fontSize: 12.5,
      }),
      tag: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 12,
        padding: '1px 4px 1px 8px',
        borderRadius: 999,
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'rgba(127,127,127,.08)',
      },
      tagX: {
        border: 'none',
        background: 'transparent',
        color: 'inherit',
        cursor: 'pointer',
        fontSize: 13,
        lineHeight: 1,
        padding: '2px 3px',
        opacity: 0.7,
      },
    }

    const 开关 = (值, 改) =>
      h('input', { type: 'checkbox', checked: !!值, onChange: (e) => 改(e.target.checked) })

    // 会话 id 有的带 session- 前缀、有的不带，比的时候统一剥掉
    const 键 = (id) => String(id == null ? '' : id).trim().replace(/^session-/, '')

    function Section() {
      const [态, 设态] = React.useState(null)
      const [脏, 设脏] = React.useState(false)
      const [说, 设说] = React.useState('')
      const [忙, 设忙] = React.useState(false)
      const [新屏蔽, 设新屏蔽] = React.useState('')
      const [视图, 设视图] = React.useState('设置')
      const [设置页, 设设置页] = React.useState('启动')
      const [更新, 设更新] = React.useState(null)
      const [查中, 设查中] = React.useState(false)
      const [选中群, 设选中群] = React.useState('')
      const [白名单草稿, 设白名单草稿] = React.useState({})
      // 轮询是异步的：脏这个 state 到回调里可能还是旧值，所以拿 ref 兜一份
      const 脏ref = React.useRef(false)
      const 设脏2 = (v) => {
        脏ref.current = !!v
        设脏(!!v)
      }

      const 拉 = React.useCallback(async () => {
        const d = await get('/state')
        if (!d || !d.ok) return
        // 手上有没保存的改动时，只更新状态（桥接/日志/会话清单），**别拿服务器那份配置盖掉**。
        // 以前就是这里每 4 秒盖一次：刚加的绑定行、刚打的聊天名、刚敲进屏蔽框的字，全被冲掉，
        // 于是「单独绑定根本用不了」「不回谁加不进去」。
        设态((旧) => (旧 && 脏ref.current ? { ...d, 配置: 旧.配置 } : d))
      }, [])

      React.useEffect(() => {
        拉()
        const t = setInterval(拉, 4000)
        return () => clearInterval(t)
      }, [拉])

      const 改配置 = (补) => {
        设态((旧) => (旧 ? { ...旧, 配置: { ...旧.配置, ...补 } } : 旧))
        设脏2(true)
      }

      const 存 = async () => {
        const 绑 = 态.配置.绑定 || []
        // 只填了半边（聊天名没填 / 会话没选）的行会被服务端丢掉 —— 先拦住，别无声消失
        const 缺 = 绑
          .map((b, i) => ({ i: i + 1, 空: !String(b.聊天 || '').trim() || !String(b.会话 || '').trim() }))
          .filter((x) => x.空)
        if (缺.length) {
          设说(`第 ${缺.map((x) => x.i).join('、')} 行还缺聊天名 / 会话 —— 补上再存，或者点「删」去掉这一行`)
          return
        }
        const 缺群 = (态.配置.群聊管理 || []).findIndex((x) => !String(x.群 || '').trim())
        if (缺群 >= 0) {
          设说(`群聊管理第 ${缺群 + 1} 行还没填群名或群 ID`)
          return
        }
        设忙(true)
        const r = await post('/save', 态.配置)
        if (r && r.ok) {
          设态(r)
          设白名单草稿({})
          设脏2(false)
          设说(r.说 || '保存好了')
        } else {
          设说('保存失败：' + ((r && r.说) || '未知'))
        }
        设忙(false)
      }

      const 装依赖 = async () => {
        设忙(true)
        const r = await post('/bridge/install')
        设说((r && r.说) || '')
        设忙(false)
        拉()
      }

      const 起 = async () => {
        设忙(true)
        const r = await post('/bridge/start')
        设说((r && r.说) || '')
        设忙(false)
        拉()
      }

      const 停 = async () => {
        设忙(true)
        const r = await post('/bridge/stop')
        设说((r && r.说) || '')
        设忙(false)
        拉()
      }

      const 测发 = async () => {
        设忙(true)
        const r = await post('/test-send', {})
        设说((r && r.说) || '')
        设忙(false)
      }

      const 测收 = async () => {
        设忙(true)
        const r = await post('/test-inbound', {})
        设说((r && r.说) || '')
        设忙(false)
      }

      if (!态) return h('div', { style: S.wrap }, '读取中…')

      const 配置 = 态.配置 || {}
      const 桥 = 态.桥接 || {}
      const 会话们 = 态.活着的会话 || []
      const 绑定 = 配置.绑定 || []
      const 群聊管理 = 配置.群聊管理 || []
      const 群初值 = (群, 群名) => ({
        群, 群名, 群聊权限: '所有人可以聊', 所有人可聊: true, 群白名单: [], 固定回复: '',
        群上下文条数: 6, 群里只认点名: true, 追话句数: 3,
        点名别名: ['大肥鱼', '肥鱼', '小肥鱼', '小客服'], 话题规则: [],
        群回复间隔秒: 30, 日记: true, 总结提示词: '', 成员快照: true,
        入群提醒: '', 退群提醒: '',
      })
      const 改群规则 = (i, 补) => 改配置({ 群聊管理: 群聊管理.map((x, j) => j === i ? { ...x, ...补 } : x) })
      const 最近群 = (态.最近聊天 || []).filter((x) => x.类型 === '群' && String(x.id || x.名 || '').trim())
      const 群候选 = []
      const 群候选键 = new Set()
      const 加群候选 = (id, 名, 已有索引) => {
        const k = String(id || 名 || '').trim()
        if (!k || 群候选键.has(k)) return
        群候选键.add(k)
        群候选.push({ id: k, 名: 名 || k, 已有索引 })
      }
      群聊管理.forEach((x, i) => 加群候选(x.群, x.群名 || x.群, i))
      最近群.forEach((x) => 加群候选(x.id || x.名, x.名, 群聊管理.findIndex((r) => r.群 === x.id || r.群 === x.名)))
      const 当前群 = 群候选.find((x) => x.id === 选中群) || null
      const 当前群规则 = 当前群
        ? 群聊管理.find((x) => x.群 === 当前群.id) || 群聊管理.find((x) => x.群 === 当前群.名) || null
        : null
      const 当前群索引 = 当前群 ? 群聊管理.findIndex((x) => x === 当前群规则) : -1
      const 选群 = (群) => {
        const i = 群聊管理.findIndex((x) => x.群 === 群.id || x.群 === 群.名)
        if (i < 0) 改配置({ 群聊管理: 群聊管理.concat([群初值(群.id, 群.名)]) })
        设选中群(群.id)
      }
      const 屏蔽 = 配置.屏蔽 || []

      // 「不回谁」：一条一条加、点 × 放出来。以前是个整块 textarea，
      // 打字打到一半就被轮询冲掉，所以怎么都加不进去。
      const 加屏蔽 = () => {
        const 名 = 新屏蔽.trim()
        if (!名) return
        设新屏蔽('')
        if (屏蔽.includes(名)) return
        改配置({ 屏蔽: 屏蔽.concat([名]) })
      }
      const 删屏蔽 = (名) => 改配置({ 屏蔽: 屏蔽.filter((x) => x !== 名) })

      const 会话下拉 = (值, 改, 允许空) => {
        const 中 = 会话们.find((x) => 键(x.id) === 键(值))
        return h(
          'select',
          {
            style: { ...S.input, minWidth: 280 },
            value: 中 ? 中.id : 值 || '',
            onChange: (e) => 改(e.target.value),
          },
          允许空 ? h('option', { value: '' }, '（不选）') : null,
          会话们.map((s) =>
            h('option', { key: s.id, value: s.id }, (s.标题 || '（没标题）') + '  ·  ' + String(s.id).slice(-8)),
          ),
          !中 && 值 ? h('option', { value: 值 }, '（已不活）' + String(值).slice(-8)) : null,
        )
      }

      const 页签样式 = (开) => ({
        ...S.btn(false),
        fontSize: 12.5,
        opacity: 开 ? 1 : 0.6,
        borderColor: 开 ? 'var(--dsw-alias-state-business-primary)' : 'var(--dsw-alias-border-l2)',
        fontWeight: 开 ? 600 : 400,
      })

      const 查更新 = async () => {
        设查中(true)
        const d = await get('/check-update')
        设更新(d || { ok: false, 说: '没拿到结果' })
        设查中(false)
      }

      const 关于内容 = () =>
        h(
          'div',
          { style: { ...S.card, marginTop: 12, lineHeight: 1.7 } },
          h('div', { style: { fontSize: 14, fontWeight: 700 } }, '通到微信 · v' + (态.版本 || '?')),
          h(
            'div',
            { style: { ...S.dim, marginTop: 6, marginBottom: 10 } },
            '把 DSH 里的 AI 伙伴和微信打通：读微信本地库收消息、UI 自动化发消息，支持收图看表情包、群聊上下文与日记、主人权限、群回复频率。不注入、不 hook、不改微信。',
          ),
          h(
            'div',
            { style: { ...S.row, marginTop: 4 } },
            h('button', { style: S.btn(true), disabled: 查中, onClick: 查更新 }, 查中 ? '查着…' : '检查更新'),
          ),
          h(
            'div',
            { style: { display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 12 } },
            h(
              'a',
              {
                href: 'https://github.com/thawline/dsh-wechat-bridge',
                target: '_blank',
                rel: 'noreferrer',
                style: { color: 'var(--dsw-alias-state-business-primary)', fontSize: 12.5 },
              },
              '⭐ 去 GitHub 点个 Star',
            ),
            h(
              'a',
              {
                href: 'https://space.bilibili.com/366416862',
                target: '_blank',
                rel: 'noreferrer',
                style: { color: 'var(--dsw-alias-state-business-primary)', fontSize: 12.5 },
              },
              '📺 B 站：关注「你若化成冰」',
            ),
          ),
          更新
            ? h(
                'div',
                { style: { ...S.dim, marginTop: 8 } },
                更新.ok
                  ? 更新.有更新
                    ? '有新版本：v' + 更新.最新 + '（你现在 v' + 更新.当前 + '）。更新方式：在 profile 目录跑一次 pnpm add github:thawline/dsh-wechat-bridge，再重启 DSH。'
                    : '已经是最新版本：v' + 更新.当前
                  : '没查成：' + (更新.说 || ''),
              )
            : null,
          h(
            'div',
            { style: { ...S.dim, marginTop: 14, fontSize: 11.5, lineHeight: 1.7 } },
            '致谢：Python 那半的思路来自 wechatauto-replica；界面与宿主这半是为 DSH 重写的。',
          ),
        )

      return h(
        'div',
        { style: S.wrap },
        // ---------- 顶部 ----------
        h(
          'div',
          { style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' } },
          h('div', { style: { fontSize: 15, fontWeight: 700 } }, '通到微信'),
          h('span', { style: S.dim }, 'v' + (态.版本 || '?')),
          h('span', { style: S.dim }, 'DSH ' + (态.DSH地址 || '?')),
          h('span', { style: 桥.运行中 ? S.ok : S.dim }, 桥.运行中 ? '● 桥接在跑（' + (桥.运行秒 || 0) + ' 秒）' : '○ 桥接没跑'),
        ),
        h(
          'div',
          { style: { ...S.dim, marginTop: 4 } },
          '读微信本地库收消息、UI 自动化发消息。不注入、不 hook、不改微信。',
        ),

        // ---------- 页签 + 右上角保存 ----------
        h(
          'div',
          { style: { display: 'flex', gap: 6, marginTop: 14, alignItems: 'center', flexWrap: 'wrap' } },
          h('button', { style: 页签样式(视图 === '设置'), onClick: () => 设视图('设置') }, '设置'),
          h('button', { style: 页签样式(视图 === '关于'), onClick: () => 设视图('关于') }, '关于'),
          h('span', { style: { flex: 1 } }),
          视图 === '设置'
            ? h('button', { style: S.btn(脏), disabled: 忙 || !脏, onClick: 存 }, 脏 ? '保存设置' : '已保存')
            : null,
        ),
        说
          ? h('div', { style: { ...S.hint, marginTop: 4, textAlign: 'right' } }, 说)
          : null,

        视图 === '关于'
          ? 关于内容()
          : h('div', { style: { display: 'contents' } },
          h('div', { role: 'tablist', style: { display: 'flex', gap: 6, margin: '14px 0 10px', overflowX: 'auto', borderBottom: '1px solid var(--dsw-alias-border-l2)' } },
            ['启动', '消息日志', '微信与群聊', '群聊配置'].map((name) => h('button', {
              key: name,
              role: 'tab',
              'aria-selected': 设置页 === name,
              style: { ...页签样式(设置页 === name), borderRadius: '6px 6px 0 0', whiteSpace: 'nowrap' },
              onClick: () => 设设置页(name),
            }, name)),
          ),
          设置页 === '启动' ? h('div', { role: 'tabpanel' },
        // ---------- 一、依赖 ----------
        h('div', { style: S.h }, '一、装依赖'),
        h(
          'div',
          { style: S.card },
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '虚拟环境'),
            h('span', { style: 桥.venv就绪 ? S.ok : S.dim }, 桥.venv就绪 ? '✓ 已建好' : '还没建（用现成解释器就不需要）'),
            h('span', { style: { ...S.dim, marginLeft: 8 } }, 桥.虚拟环境 || ''),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '微信自动化库'),
            h(
              'span',
              { style: 桥.python ? S.ok : S.dim },
              桥.依赖就绪 ? '✓ 已验证' : 桥.python ? '（会用现成的解释器）' : '（还没就绪）',
            ),
          ),
          h(
            'div',
            { style: S.row },
            h('button', { style: S.btn(true), disabled: 忙 || 桥.安装?.进行中, onClick: 装依赖 },
              桥.安装?.进行中 ? '正在装…' : '装依赖（第一次几分钟）'),
            h('span', { style: S.dim }, '会用系统里的 Python 建一个独立环境，不动你别的环境。'),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '解释器（可选）'),
            h('input', {
              style: { ...S.input, width: 380 },
              value: 配置.解释器 || '',
              placeholder: '留空 = 自动找；或填一个已装好 wechatauto 的 python.exe',
              onChange: (e) => 改配置({ 解释器: e.target.value }),
            }),
          ),
          h('div', { style: S.hint }, '实际在用的：' + (桥.python || '（还没有）')),
          桥.安装?.进行中 || 桥.安装?.日志?.length
            ? h('div', null,
                h('div', { style: { ...S.dim, marginBottom: 4 } }, '进度：' + (桥.安装?.步骤 || '')),
                h('div', { style: S.pre }, (桥.安装?.日志 || []).join('\n') || '（还没输出）'))
            : null,
          桥.安装?.失败 ? h('div', { style: { ...S.bad, marginTop: 6 } }, '失败：' + 桥.安装.失败) : null,
        ),

        // ---------- 二、启动 ----------
        h('div', { style: S.h }, '二、启动桥接'),
        h(
          'div',
          { style: S.card },
          h(
             'div',
             { style: S.row },
             h('span', { style: S.lab }, '底层后端'),
             h(
               'select',
               {
                 style: { ...S.input, minWidth: 190 },
                 value: 配置.后端 || 'sqlite_uia',
                 onChange: (e) => 改配置({ 后端: e.target.value }),
               },
               h('option', { value: 'sqlite_uia' }, 'SQLite + UIA（当前）'),
               h('option', { value: 'auto' }, '自动（当前等同 SQLite + UIA）'),
               h('option', { value: 'native_hook' }, 'Native Hook（开发中）'),
             ),
             h('span', { style: S.dim }, '切换后重启桥接生效；Native Hook 暂未启用。'),
           ),
        ),
        h(
          'div',
          { style: S.card },
          h(
            'div',
            { style: S.row },
            h('button', { style: S.btn(true), disabled: 忙 || 桥.运行中 || !桥.python, onClick: 起 }, '启动'),
            h('button', { style: S.btn(false), disabled: 忙 || !桥.运行中, onClick: 停 }, '停止'),
            h('span', { style: S.dim }, 桥.运行中 ? '日志会实时滚在下面' : 桥.python ? '可以启动了' : '先点「装依赖」，或者填一个能用的解释器'),
          ),
          h('div', { style: S.pre }, (桥.进程日志 || []).join('\n') || '（还没输出）'),
        ),

        ) : null,
        设置页 === '微信与群聊' ? h('div', { role: 'tabpanel' },
        // ---------- 三、怎么接 ----------
        h('div', { style: S.h }, '三、接到哪间会话'),
        h(
          'div',
          { style: S.card },
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '总开关'),
            开关(配置.总开关, (v) => 改配置({ 总开关: v })),
            h('span', { style: S.dim }, '关着 = 一句都不发'),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '试运行'),
            开关(配置.试运行, (v) => 改配置({ 试运行: v })),
            h('span', { style: S.dim }, '开着 = 只在日志里记「该发什么」，不真发'),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '默认会话'),
            会话下拉(配置.默认会话, (v) => 改配置({ 默认会话: v }), true),
          ),
           h('div', { style: S.hint }, '微信里来的消息默认进这一间；没单独绑定的聊天都走它。'),

          h(
            'datalist',
            { id: 'wx-chat-names' },
            (态.最近聊天 || []).map((x, i) => h('option', { key: i, value: x.名 }, x.类型 || '')),
          ),

          h('div', { style: { ...S.h, marginTop: 12 } }, '单独绑定（可选）'),
          h('div', { style: S.hint },
            '一行 = 一个微信聊天 → 一间会话。左边填微信里显示的名字；下面的候选直接点，改过备注也认 wxid。'),
          绑定.map((b, i) => {
            const 好 = (态.绑定情况 || []).find((x) => x.聊天 === b.聊天)
            return h(
              'div',
              {
                key: i,
                style: {
                  marginTop: 8,
                  padding: '8px 10px',
                  border: '1px solid var(--dsw-alias-border-l2)',
                  borderRadius: 8,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 6,
                },
              },
              // 第一行：微信这一侧
              h(
                'div',
                { style: { display: 'flex', gap: 8, alignItems: 'center' } },
                h('span', { style: { minWidth: 64, fontSize: 12, opacity: 0.7 } }, '微信会话'),
                h('input', {
                  style: { ...S.input, width: 240 },
                  list: 'wx-chat-names',
                  value: b.聊天,
                  placeholder: '微信聊天名（或 wxid / xxx@chatroom）',
                  onChange: (e) => {
                    const 新 = 绑定.map((x, j) => (j === i ? { ...x, 聊天: e.target.value } : x))
                    改配置({ 绑定: 新 })
                  },
                }),
                h('button', {
                  style: { ...S.btn(false), marginLeft: 'auto' },
                  onClick: () => 改配置({ 绑定: 绑定.filter((_, j) => j !== i) }),
                }, '删'),
              ),
              // 第二行：DSH 这一侧
              h(
                'div',
                { style: { display: 'flex', gap: 8, alignItems: 'center' } },
                h('span', { style: { minWidth: 64, fontSize: 12, opacity: 0.7 } }, 'AI 会话'),
                会话下拉(b.会话, (v) => {
                  const 新 = 绑定.map((x, j) => (j === i ? { ...x, 会话: v } : x))
                  改配置({ 绑定: 新 })
                }, false),
              ),
              // 第三行：接上没有
              h(
                'div',
                { style: 好 ? S.ok : S.hint },
                好
                  ? `已接上：${好.会话标题 || String(b.会话).slice(-8)}${好.活着 ? '' : '（那间现在没开）'}`
                  : b.聊天 && b.会话
                    ? '还没保存 —— 点右上角「保存设置」'
                    : '还差聊天名 / 会话',
              ),
            )
          }),
          h(
            'div',
            { style: { marginTop: 8, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' } },
            h('button', {
              style: S.btn(false),
              onClick: () => 改配置({ 绑定: 绑定.concat([{ 聊天: '', 会话: 配置.默认会话 || '' }]) }),
            }, '＋ 加一条绑定'),
            h('span', { style: S.dim }, '加完要点右上角「保存设置」才生效'),
          ),
          (态.最近聊天 || []).length
            ? h(
                'div',
                { style: { marginTop: 10 } },
                h('div', { style: S.hint }, '最近来找过的（点一下绑到默认会话，✓ 的再点一下解绑）：'),
                h(
                  'div',
                  { style: { display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 4 } },
                  (态.最近聊天 || []).slice(0, 10).map((x) => {
                    const 已有 = 绑定.some((b) => b.聊天 === x.名)
                    return h(
                      'button',
                      {
                        key: x.名,
                        style: { ...S.btn(false), padding: '2px 8px', opacity: 已有 ? 0.55 : 1 },
                        title: `${x.类型 || ''}${x.id ? ' · ' + x.id : ''}`,
                        onClick: () =>
                          改配置({
                            绑定: 绑定
                              .filter((b) => b.聊天 !== x.名)
                              .concat(已有 ? [] : [{ 聊天: x.名, 会话: 配置.默认会话 || '' }]),
                          }),
                      },
                      (已有 ? '✓ ' : '＋ ') + x.名,
                    )
                  }),
                ),
              )
            : h('div', { style: S.hint }, '（等桥接跑起来、有人来找过我之后，这里会列出这些名字，直接点就行）'),

        ),
        ) : null,
        设置页 === '群聊配置' ? h('div', { role: 'tabpanel' },
          h('div', { style: { ...S.h, marginTop: 18 } }, '群聊管理'),
          h('div', { style: S.hint }, '选择群聊后编辑该群配置。群 ID 优先，改群名后仍能匹配。'),
          h('div', { style: { display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0, marginTop: 8 } },
            h('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, minWidth: 0 } },
              群候选.map((群) => h('button', {
                key: 群.id,
                style: { ...S.btn(当前群 && 当前群.id === 群.id), maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                title: 群.id,
                onClick: () => 选群(群),
              }, (群.名 || 群.id) + (群.id !== 群.名 ? ' · ' + 群.id.slice(-10) : ''))),
              !群候选.length ? h('div', { style: S.hint }, '还没有见过群。可用下面按钮添加，或等群里来一条消息。') : null,
              h('button', { style: S.btn(false), onClick: () => {
                const n = '新群-' + String(Date.now()).slice(-6)
                const 新 = 群聊管理.concat([群初值(n, n)])
                改配置({ 群聊管理: 新 })
                设选中群(n)
              } }, '＋ 添加群'),
            ),
            当前群 && !当前群规则 ? h('div', { style: S.hint }, '该群尚未配置。选择上方群名创建独立规则。') : null,
            当前群规则 ? h('div', { style: { ...S.card, marginBottom: 0 } },
              h('div', { style: { ...S.row, marginBottom: 10 } },
                h('input', { style: { ...S.input, flex: 1, minWidth: 180 }, list: 'wx-group-names', value: 当前群规则.群 || '', placeholder: '群 ID 或群名', onChange: (e) => {
                  const 新值 = e.target.value
                  if (当前群索引 >= 0) 改群规则(当前群索引, { 群: 新值 })
                  else 改配置({ 群聊管理: 群聊管理.concat([{ ...当前群规则, 群: 新值 }]) })
                } }),
                h('button', { style: S.btn(false), onClick: () => { 改配置({ 群聊管理: 群聊管理.filter((_, j) => j !== 当前群索引) }); 设选中群('') } }, '删群'),
              ),
              h('div', { style: S.hint }, '显示名：' + (当前群规则.群名 || 当前群.名 || '（未命名）') + '；规则保存后才会生效。'),
              h('div', { style: { ...S.row, marginTop: 10 } },
                h('span', { style: S.lab }, '聊天权限'),
                h('select', { style: { ...S.input, minWidth: 180 }, value: 当前群规则.群聊权限 || (当前群规则.所有人可聊 === false ? '所有人不可以聊' : '所有人可以聊'), onChange: (e) => 改群规则(当前群索引, { 群聊权限: e.target.value, 所有人可聊: e.target.value === '所有人可以聊' }) },
                  ['所有人可以聊', '所有人不可以聊', '仅白名单可聊'].map((v) => h('option', { key: v, value: v }, v))),
              ),
              (当前群规则.群聊权限 === '仅白名单可聊') ? h('div', { style: { marginBottom: 8 } },
                h('span', { style: S.lab }, '本群白名单'),
                h('textarea', { style: { ...S.area, width: '100%', height: 68 }, value: 白名单草稿[当前群.id] ?? (当前群规则.群白名单 || []).join('\n'), placeholder: '成员 wxid 或昵称：每行一个，或用英文/中文逗号分隔；优先填 wxid', onChange: (e) => {
                  const 输入 = e.target.value
                  设白名单草稿((旧) => ({ ...旧, [当前群.id]: 输入 }))
                  改群规则(当前群索引, { 群白名单: 输入.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean) })
                } }),
                h('div', { style: S.hint }, '只有名单中的成员和主人能与 AI 聊天；昵称可能被群名片冒用，建议用 wxid。'),
              ) : null,
              (当前群规则.群聊权限 || (当前群规则.所有人可聊 === false ? '所有人不可以聊' : '所有人可以聊')) !== '所有人可以聊' ? h('textarea', { style: { ...S.area, width: '100%', minHeight: 96, marginBottom: 8, resize: 'vertical' }, value: 当前群规则.固定回复 || '', placeholder: '无权限成员点名时的固定回复（留空 = 静默）', onChange: (e) => 当前群索引 >= 0 && 改群规则(当前群索引, { 固定回复: e.target.value }) }) : null,
              (() => {
                const 字段 = (k, label, body) => h('div', { style: { borderTop: '1px solid var(--dsw-alias-border-l2)', paddingTop: 8, marginTop: 8 } },
                  h('div', { style: { ...S.row, marginBottom: 5 } }, h('span', { style: { ...S.lab, minWidth: 150 } }, label)),
                  body(当前群规则[k] ?? 配置[k], (v) => 改群规则(当前群索引, { [k]: v })))
                return h('div', null,
                  字段('群上下文条数', '群上下文条数', (v, set) => h('input', { style: { ...S.input, width: 90 }, type: 'number', min: 0, max: 50, value: v == null ? 0 : v, onChange: (e) => set(Math.max(0, Math.min(50, Number(e.target.value) || 0))) })),
                  字段('群里只认点名', '群里只认点名', (v, set) => 开关(v, set)),
                  字段('追话句数', '追话句数', (v, set) => h('input', { style: { ...S.input, width: 70 }, type: 'number', min: 0, max: 20, value: v == null ? 0 : v, onChange: (e) => set(Math.max(0, Math.min(20, Number(e.target.value) || 0))) })),
                  字段('点名别名', '点名别名', (v, set) => h('textarea', { style: { ...S.area, width: '100%', height: 64 }, value: (v || []).join('\n'), onChange: (e) => set(e.target.value.split(/[\n,，]/).map((s) => s.trim()) ) })),
                  字段('话题规则', '话题规则', (v, set) => h('div', null, (v || []).map((规, j) => h('div', { key: j, style: { display: 'flex', gap: 5, alignItems: 'flex-start', marginBottom: 5 } }, h('input', { type: 'checkbox', checked: (规 && 规.启用) !== false, onChange: (e) => set((v || []).map((x, k) => k === j ? { ...x, 启用: e.target.checked } : x)) }), h('textarea', { style: { ...S.area, width: 100, height: 52 }, value: (规 && 规.词) || '', placeholder: '词', onChange: (e) => set((v || []).map((x, k) => k === j ? { ...x, 词: e.target.value } : x)) }), h('textarea', { style: { ...S.area, flex: 1, minWidth: 120, height: 52 }, value: (规 && 规.回) || '', placeholder: '回复提示词', onChange: (e) => set((v || []).map((x, k) => k === j ? { ...x, 回: e.target.value } : x)) }), h('input', { style: { ...S.input, width: 52 }, type: 'number', min: 0, value: (规 && 规.窗口) || 0, title: '窗口秒', onChange: (e) => set((v || []).map((x, k) => k === j ? { ...x, 窗口: Math.max(0, Number(e.target.value) || 0) } : x)) }), h('input', { style: { ...S.input, width: 44 }, type: 'number', min: 0, value: (规 && 规.次数) || 0, title: '窗口内次数', onChange: (e) => set((v || []).map((x, k) => k === j ? { ...x, 次数: Math.max(0, Number(e.target.value) || 0) } : x)) }), h('button', { style: S.btn(false), onClick: () => set((v || []).filter((_, k) => k !== j)) }, '删'))), h('button', { style: S.btn(false), onClick: () => set((v || []).concat([{ 词: '', 回: '', 启用: true, 窗口: 0, 次数: 0 }])) }, '＋ 加一条'))),
                  字段('群回复间隔秒', '群回复间隔秒', (v, set) => h('input', { style: { ...S.input, width: 90 }, type: 'number', min: 0, value: v == null ? 0 : v, onChange: (e) => set(Math.max(0, Number(e.target.value) || 0)) })),
                  字段('日记', '群聊日记', (v, set) => 开关(v, set)),
                  字段('总结提示词', '总结提示词', (v, set) => h('textarea', { style: { ...S.area, width: '100%', height: 80 }, value: v || '', onChange: (e) => set(e.target.value) })),
                  字段('成员快照', '成员快照', (v, set) => 开关(v !== false, set)),
                  字段('入群提醒', '入群提醒', (v, set) => h('textarea', { style: { ...S.area, width: '100%', height: 64 }, value: v || '', onChange: (e) => set(e.target.value) })),
                  字段('退群提醒', '退群提醒', (v, set) => h('textarea', { style: { ...S.area, width: '100%', height: 64 }, value: v || '', onChange: (e) => set(e.target.value) })),
                )
              })(),
            ) : h('div', { style: S.hint }, '先从上方选一个群。'),
          ),
          h('datalist', { id: 'wx-group-names' }, 最近群.map((x) => h('option', { key: x.id || x.名, value: x.id || x.名 }, x.名))),
        ) : null,
        设置页 === '微信与群聊' ? h('div', { role: 'tabpanel' },
          h(
            'div',
            { style: { ...S.row, marginTop: 14 } },
            h('span', { style: S.lab }, '接口令牌'),
            h('input', {
              style: { ...S.input, width: 240 },
              value: 配置.令牌 || '',
              placeholder: '留空 = 不校验（只建议本机）',
              onChange: (e) => 改配置({ 令牌: e.target.value }),
            }),
          ),

          h('div', { style: { ...S.h, marginTop: 18 } }, '看图（微信里的图片和表情包）'),
          h(
            'div',
            { style: S.card },
            h(
              'div',
              { style: S.row },
              h('span', { style: S.lab }, '识图'),
              开关(配置.看图 !== false, (v) => 改配置({ 看图: v })),
              h('span', { style: S.dim }, '收到图片 / 表情包先让视觉模型看一遍，再把描述带进对话'),
            ),
            h(
              'div',
              { style: S.row },
              h('span', { style: S.lab }, '视觉密钥'),
              h('input', {
                style: { ...S.input, width: 300 },
                type: 'password',
                value: 配置.视觉密钥 || '',
                placeholder: '留空 = 用环境变量 / 鲸鱼娘设置里的 DeepSeek key',
                onChange: (e) => 改配置({ 视觉密钥: e.target.value }),
              }),
            ),
            h(
              'div',
              { style: S.row },
              h('span', { style: S.lab }, '视觉模型'),
              h('input', {
                style: { ...S.input, width: 200 },
                value: 配置.视觉模型 || 'deepseek-flash',
                onChange: (e) => 改配置({ 视觉模型: e.target.value }),
              }),
              h('span', { style: S.dim }, '默认 deepseek-flash，支持 GIF'),
            ),
            h(
              'div',
              { style: S.row },
              h('span', { style: S.lab }, '备用视觉密钥（智谱）'),
              h('input', {
                style: { ...S.input, width: 300 },
                type: 'password',
                value: 配置.视觉备用密钥 || '',
                placeholder: '可选：DeepSeek 看失败时退到 glm-4v-flash',
                onChange: (e) => 改配置({ 视觉备用密钥: e.target.value }),
              }),
            ),
            h(
              'div',
              { style: S.hint },
              '同一张图按内容哈希缓存，只花一次钱；没配密钥也不会挡消息，只是标一句「没看清」。',
            ),
          ),

          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '外人只能聊天'),
            开关(配置.外人只能聊天, (v) => 改配置({ 外人只能聊天: v })),
            h('span', { style: S.dim }, '只有「主人」能动电脑；别人发的那一轮按下面的可用工具白名单放行，其余硬拦'),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '外人可用工具'),
            h('input', {
              style: { ...S.input, width: 380 },
              value: (配置.外人可用工具 || []).join(', '),
              placeholder: 'web_search, web_fetch, send_meme（留空 = 只能聊天）',
              onChange: (e) =>
                改配置({
                  外人可用工具: e.target.value
                    .split(/[,，\n]/)
                    .map((s) => s.trim())
                    .filter(Boolean),
                }),
            }),
          ),
          h(
            'div',
            { style: S.hint },
            '只读 / 联网类工具才适合往这里放；跑命令、读写文件、改设置的工具别加，加了就是给外人开电脑。',
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '群聊日记'),
            开关(配置.日记, (v) => 改配置({ 日记: v })),
            h('span', { style: S.dim }, '把群里的对话按天写成 markdown（不点名的也记）'),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '日记目录'),
            h('input', {
              style: { ...S.input, width: 380 },
              value: 配置.日记目录 || '',
              placeholder: '留空 = ~/.dsh/wechat-diary',
              onChange: (e) => 改配置({ 日记目录: e.target.value }),
            }),
          ),
          h(
            'div',
            { style: { ...S.row, alignItems: 'flex-start' } },
            h('span', { style: S.lab }, '总结提示词'),
            h('textarea', {
              style: { ...S.area, width: 520, height: 96 },
              value: 配置.总结提示词 || '',
              placeholder:
                '留空 = 用内置的写法：分组讲事、抓原话和梗、分清玩笑和事实、别写成流水账。想换口味就整段写在这里',
              onChange: (e) => 改配置({ 总结提示词: e.target.value }),
            }),
          ),
          h(
            'div',
            { style: S.hint },
            '「总结一下今天的瓜」这类请求，桥接会按时间从微信库取记录，连同这段写法一起交给模型。',
          ),
          h('div', { style: { ...S.h, marginTop: 14 } }, '记忆'),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '记忆密钥'),
            h('input', {
              style: { ...S.input, width: 300 },
              type: 'password',
              value: 配置.记忆密钥 || '',
              placeholder: '留空 = 用 DSH 自己的 DEEPSEEK_API_KEY',
              onChange: (e) => 改配置({ 记忆密钥: e.target.value }),
            }),
            h('span', { style: S.dim }, '成员画像 / 群像合成用'),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '记忆模型'),
            h('input', {
              style: { ...S.input, width: 180 },
              value: 配置.记忆模型 || 'deepseek-flash',
              onChange: (e) => 改配置({ 记忆模型: e.target.value }),
            }),
            h('input', {
              style: { ...S.input, width: 260 },
              value: 配置.记忆地址 || 'https://api.deepseek.com',
              onChange: (e) => 改配置({ 记忆地址: e.target.value }),
            }),
          ),
          h(
            'div',
            { style: S.hint },
            '这里只给成员画像、群像摘要、证据校验用；聊天本身还是 DSH 那边的模型。密钥留空时读 '
              + '~/.dsh/.credentials.yaml。',
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '主人'),
            h('input', {
              style: { ...S.input, width: 380 },
              value: (配置.主人 || []).join(', '),
              placeholder: 'wxid_xxx 或昵称，逗号分隔；主人说话不点名也进',
              onChange: (e) =>
                改配置({
                  主人: e.target.value
                    .split(/[,，]/)
                    .map((s) => s.trim())
                    .filter(Boolean),
                }),
            }),
          ),
          h(
            'div',
            { style: S.row },
            h('span', { style: S.lab }, '入站白名单'),
            h('input', {
              style: { ...S.input, width: 380 },
              value: (配置.入站白名单 || []).join(', '),
              placeholder: '留空 = 谁都能发进来；填了只放行名单里的（wxid 或昵称，逗号分隔）',
              onChange: (e) =>
                改配置({
                  入站白名单: e.target.value
                    .split(/[,，]/)
                    .map((s) => s.trim())
                    .filter(Boolean),
                }),
            }),
          ),
          h(
            'div',
            { style: { ...S.row, alignItems: 'flex-start', marginBottom: 2 } },
            h('span', { style: S.lab }, '不回谁（屏蔽）'),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 6, flex: 1, minWidth: 320 } },
              h(
                'div',
                { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
                h('input', {
                  style: { ...S.input, width: 240 },
                  list: 'wx-chat-names',
                  value: 新屏蔽,
                  placeholder: '微信名 / 群名（回车也行）',
                  onChange: (e) => 设新屏蔽(e.target.value),
                  onKeyDown: (e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      加屏蔽()
                    }
                  },
                }),
                h('button', { style: S.btn(false), onClick: 加屏蔽 }, '加上'),
                h('span', { style: S.dim }, `现在 ${屏蔽.length} 个`),
              ),
              h(
                'div',
                { style: { display: 'flex', flexWrap: 'wrap', gap: 6 } },
                屏蔽.length
                  ? 屏蔽.map((名, i) =>
                      h(
                        'span',
                        { key: 名 + i, style: S.tag },
                        名,
                        h('button', { style: S.tagX, title: '放出来', onClick: () => 删屏蔽(名) }, '×'),
                      ),
                    )
                  : h('span', { style: S.dim }, '（空 —— 谁的都回）'),
              ),
            ),
          ),
          h(
            'div',
            { style: S.hint },
            '这些人 / 群发消息不进对话、也不写群日记。改完点右上角「保存设置」，桥接最多 30 秒认；'
              + '也可以直接跟我说「以后别理某某」，它自己会加。',
          ),

        ) : null,
        设置页 === '消息日志' ? h('div', { role: 'tabpanel' },
        // ---------- 四、试一试 ----------
        h('div', { style: S.h }, '四、试一试'),
        h(
          'div',
          { style: S.card },
          h(
            'div',
            { style: S.row },
            h('button', { style: S.btn(false), disabled: 忙, onClick: 测发 }, '测：发到微信'),
            h('button', { style: S.btn(false), disabled: 忙, onClick: 测收 }, '测：灌一条进会话'),
            h('span', { style: S.dim }, '「发到微信」会真的发一条固定测试句。'),
          ),
          h('div', { style: S.hint },
            '正式用法：在微信里跟「文件传输助手」说话，就等于跟我说话；别人发给你的私聊、群里的话也会转进来。'),
        ),
        h('div', { style: { ...S.h, marginTop: 18 } }, '消息日志'),
        h('div', { style: S.pre }, (态.日志 || []).join('\n') || '（还空着）'),
        ) : null,
        )
      )
    }

    function apply(ctx) {
      const slots = ctx.get('slots')
      if (slots === undefined) return

      // 左侧插件栏入口 + 对应主面板。配置页本身复用原来的 Section，
      // 这样所有状态轮询、保存和启动逻辑只有一份，不会出现两套页面互相覆盖。
      slots.inject('sidebar.panellist', () =>
        slots.register(
          { name: 'sidebar.panellist', id: 'wechat-bridge', order: 25, label: () => '通到微信' },
          () => h('span', { 'aria-hidden': true, style: { fontSize: 18, lineHeight: 1 } }, '🐋'),
        ),
      )
      slots.inject('main', () =>
        slots.register(
          { name: 'main', key: 'wechat-bridge' },
          Section,
        ),
      )
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
