// 临时自测：假 ctx 把 host.js 跑起来，验 保存/绑定/屏蔽/工具/最近聊天
import { readFileSync, writeFileSync, mkdirSync, rmSync, mkdtempSync } from 'node:fs'
import { join } from 'node:path'

// Isolate before importing host.js, which captures DSH_HOME at module evaluation.
process.env.DSH_HOME = mkdtempSync(join(process.cwd(), '.selftest-home-'))
const testPython = process.env.DSH_TEST_PYTHON || ''
const { apply } = await import('./lib/host.js')
const 家 = process.env.DSH_HOME
const 配置路径 = join(家, 'wechat-bridge-config.json')
const 聊天路径 = join(家, 'wechat-bridge-chats.json')

writeFileSync(
  配置路径,
  JSON.stringify({ 总开关: true, 试运行: false, 默认会话: 'session-aaa', 绑定: [], 屏蔽: ['微信运动'], 解释器: testPython }, null, 2),
  'utf8',
)
// 上一轮留下的「最近聊天」要清掉，不然「一开始是空的」这条永远过不去
try {
  rmSync(聊天路径, { force: true })
} catch (error) {}
const 期望版本 = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version

const 会话 = [
  { id: 'session-aaa', 标题: '主要聊天记忆' },
  { id: 'session-bbb', 标题: '第二个会话说说' },
]
const 标题表 = new Map(会话.map((s) => [s.id, { title: s.标题 }]))
const 工具 = []
const 路线 = new Map()
const 收到的 = []
const agent表 = new Map()

const 造agent = (id) => ({
  session: { id },
  followup: async (m) => 收到的.push({ id, m }),
  ctx: {
    tools: {
      guard: (cb) => {
        if (!guard回调们.has(id)) guard回调们.set(id, [])
        guard回调们.get(id).push(cb)
        return () => {}
      },
    },
  },
})
const guard回调们 = new Map()

const ctx = {
  get: (n) =>
    n === 'sessions'
      ? { list: () => 会话, flush: async () => {} }
      : n === 'sessionTitle'
        ? { get: (s) => 标题表.get(s.id) }
        : undefined,
  agents: {
    get: (id) => agent表.get(id),
    resume: async ({ resumeSessionId }) => {
      const a = 造agent(resumeSessionId)
      agent表.set(resumeSessionId, a)
      return a
    },
  },
  webServer: { register: (r) => 路线.set(r.path, r.handler), getOrigin: () => 'http://127.0.0.1:43134' },
  effect: (fn) => {
    fn()
    return () => {}
  },
  on: () => () => {},
  tools: { register: (t) => 工具.push(t) },
}

const 屏 = apply(ctx)

const 假req = (体) => ({
  headers: {},
  [Symbol.asyncIterator]: async function* () {
    yield Buffer.from(JSON.stringify(体))
  },
})
const 调 = async (路径, 体) => {
  const 处理器 = 路线.get(路径)
  if (!处理器) throw new Error('没这条路由：' + 路径)
  let 出 = null
  const res = { setHeader: () => {}, end: (s) => (出 = JSON.parse(s)) }
  await 处理器(假req(体), res)
  return 出
}
const 读配置 = () => JSON.parse(readFileSync(配置路径, 'utf8'))
const 断言 = (名, 条件, 细节) => {
  console.log((条件 ? '✓ ' : '✗ ') + 名 + (条件 ? '' : '  ← ' + JSON.stringify(细节)))
  if (!条件) process.exitCode = 1
}

// 1 路由与工具都挂上了
断言('注册了 4 个工具', 工具.length === 4, 工具.map((t) => t.name))
断言('注册了路由', 路线.has('/dsh-wechat-bridge/save') && 路线.has('/dsh-wechat-bridge/state'), [...路线.keys()])

// 2 概况
const 概 = await 屏.概况()
断言(`版本 ${期望版本}`, 概.版本 === 期望版本, 概.版本)
断言('最近聊天是数组', Array.isArray(概.最近聊天) && 概.最近聊天.length === 0, 概.最近聊天)

// 3 半截绑定行不收
const 存1 = await 调('/dsh-wechat-bridge/save', { 绑定: [{ 聊天: '', 会话: 'session-aaa' }] })
断言('半截行被拒（说里带「没填全」）', /没填全/.test(存1.说 || ''), 存1.说)
断言('半截行没落盘', 读配置().绑定.length === 0, 读配置().绑定)

// 4 正常保存：绑定 + 屏蔽
const 存2 = await 调('/dsh-wechat-bridge/save', {
  绑定: [{ 聊天: '测试群', 会话: 'session-aaa' }],
  屏蔽: ['微信运动', '测试群'],
})
断言('保存成功（绑定 1 条、不回 2 个）', /绑定 1 条、不回 2 个/.test(存2.说 || ''), 存2.说)
const 配2 = 读配置()
断言('绑定落盘了', 配2.绑定.length === 1 && 配2.绑定[0].聊天 === '测试群', 配2.绑定)
断言('屏蔽落盘了', 配2.屏蔽.join('|') === '微信运动|测试群', 配2.屏蔽)
断言('绑定情况带标题', (存2.绑定情况 || [])[0]?.会话标题 === '主要聊天记忆', 存2.绑定情况)

// 5 按底层 id 也能命中绑定（示例：群备注改了）
const 收1 = await 调('/dsh-wechat-bridge/receive', {
  text: '在吗',
  chat: '改了备注的群',
  chatId: '123@chatroom',
  chatType: 'group',
  sender: '张三',
  senderId: 'wxid_zhangsan',
})
断言('没绑定就回不行 —— 但默认会话兜着，应该能进', 收1.ok === true, 收1)
断言('消息喂进了默认会话', 收到的.length >= 1, 收到的.length)

// 5.5 外人轮的工具闸：联网 / 表情包放行，危险工具照拦
const 阀们 = guard回调们.get('session-aaa') || []
const 阀 = 阀们[0]
断言('外人轮挂上了工具闸', typeof 阀 === 'function', 阀们.length)
if (typeof 阀 === 'function') {
  断言('外人能联网搜索', 阀({ name: 'web_search' }) === undefined, 阀({ name: 'web_search' }))
  断言('外人能抓网页', 阀({ name: 'web_fetch' }) === undefined, 阀({ name: 'web_fetch' }))
  断言('外人能发表情包', 阀({ name: 'send_meme' }) === undefined, 阀({ name: 'send_meme' }))
  断言('外人不许跑命令', typeof 阀({ name: 'pwsh' }) === 'string', 阀({ name: 'pwsh' }))
  断言('外人不许写文件', typeof 阀({ name: 'write' }) === 'string', 阀({ name: 'write' }))

  // 5.6 白名单可配：改完配置，下一句工具就按新名单来
  await 调('/dsh-wechat-bridge/save', { ...读配置(), 外人可用工具: ['pwsh'] })
  断言('配了 pwsh 就放行 pwsh', 阀({ name: 'pwsh' }) === undefined, 阀({ name: 'pwsh' }))
  断言('没配 web_search 就拦', typeof 阀({ name: 'web_search' }) === 'string', 阀({ name: 'web_search' }))
  await 调('/dsh-wechat-bridge/save', {
    ...读配置(),
    外人可用工具: ['web_search', 'web_fetch', 'send_meme'],
  })
  断言('白名单改回来就恢复放行', 阀({ name: 'web_search' }) === undefined, 阀({ name: 'web_search' }))
}

// 6 给这个群单独绑一间，再按「显示名」进来
await 调('/dsh-wechat-bridge/save', {
  绑定: [{ 聊天: '测试群', 会话: 'session-aaa' }, { 聊天: '另一个群', 会话: 'session-bbb' }],
  屏蔽: ['微信运动'],
})
收到的.length = 0
const 收2 = await 调('/dsh-wechat-bridge/receive', {
  text: '单独的群',
  chat: '另一个群',
  chatId: '456@chatroom',
  chatType: 'group',
  sender: '李四',
  senderId: 'wxid_lisi',
})
断言('单独绑定的群进了自己那间', 收2.ok === true && 收到的[0]?.id === 'session-bbb', { 收2, 收到: 收到的.map((x) => x.id) })

// 每群规则：关闭后不进入 AI，仅明确点名才收到固定回复。
await 调('/dsh-wechat-bridge/save', { ...读配置(), 群回复间隔秒: 60,
  群聊管理: [{ 群: '456@chatroom', 所有人可聊: false, 固定回复: '请联系群主' }] })
断言('群规则落盘', 读配置().群聊管理[0].固定回复 === '请联系群主')
const 全局默认 = 读配置()
await 调('/dsh-wechat-bridge/save', { ...全局默认, 群聊管理: [
  { 群: '改名后的群', 所有人可聊: true },
  { 群: '456@chatroom', 所有人可聊: false, 固定回复: '请联系群主',
    群上下文条数: 0, 群里只认点名: false, 追话句数: 0,
    点名别名: ['只在这个群叫'], 话题规则: [], 群回复间隔秒: 0,
    日记: false, 成员快照: false, 入群提醒: '', 退群提醒: '', 总结提示词: '' },
] })
const 覆盖 = 读配置()
断言('逐群字段保存且全局默认不变', 覆盖.群聊管理[1].群上下文条数 === 0
  && 覆盖.群聊管理[1].群回复间隔秒 === 0 && 覆盖.群聊管理[1].日记 === false
  && 覆盖.群聊管理[1].话题规则.length === 0 && 覆盖.群里只认点名 === 全局默认.群里只认点名)
断言('同名与 ID 规则并存', 覆盖.群聊管理.length === 2)
断言('旧布尔权限迁移为三档模式', 覆盖.群聊管理[0].群聊权限 === '所有人可以聊'
  && 覆盖.群聊管理[1].群聊权限 === '所有人不可以聊')
const 群消息 = (补 = {}) => ({ text: '在吗', chat: '改名后的群', chatId: '456@chatroom',
  chatType: 'group', sender: '李四', senderId: 'wxid_lisi', ...补 })
const 入队前 = (await 屏.概况()).待发队列
const 模型前 = 收到的.length
const 未点名 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: false, 话题命中: true }))
断言('受限群未点名不进 AI 也不回复', !未点名.ok && 收到的.length === 模型前 && (await 屏.概况()).待发队列 === 入队前)
const 固定 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true, text: '用量' }))
断言('受限群点名仅排固定回复', 固定.ok && 收到的.length === 模型前 && (await 屏.概况()).待发队列 === 入队前 + 1, 固定)
const 冷却 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true }))
断言('群 ID 规则优先、独立冷却 0 生效', 冷却.ok && 收到的.length === 模型前
  && (await 屏.概况()).待发队列 === 入队前 + 2, 冷却)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 入站白名单: ['wxid_another'] })
const 白名单拦截 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true }))
断言('白名单先于固定回复', !白名单拦截.ok && /白名单/.test(白名单拦截.说), 白名单拦截)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 入站白名单: [],
  群聊管理: [{ 群: '456@chatroom', 群聊权限: '仅白名单可聊', 群白名单: ['wxid_alice', '小明'], 固定回复: '请联系群主', 群里只认点名: false, 群回复间隔秒: 0 }] })
断言('逐群白名单保存并与全局名单隔离', 读配置().群聊管理[0].群白名单.join('|') === 'wxid_alice|小明'
  && 读配置().入站白名单.length === 0)
const 白名单前 = 收到的.length
const 白名单队列前 = (await 屏.概况()).待发队列
const 白名单放行 = await 调('/dsh-wechat-bridge/receive', 群消息({ senderId: 'wxid_alice', atMe: true }))
断言('白名单 wxid 可与 AI 聊天', 白名单放行.ok && 收到的.length === 白名单前 + 1, 白名单放行)
const 昵称放行 = await 调('/dsh-wechat-bridge/receive', 群消息({ sender: '小明', senderId: 'wxid_ming', atMe: true }))
断言('白名单昵称可与 AI 聊天', 昵称放行.ok && 收到的.length === 白名单前 + 2, 昵称放行)
const 非白名单 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true }))
断言('非白名单点名只得到固定回复', 非白名单.ok && 收到的.length === 白名单前 + 2
  && (await 屏.概况()).待发队列 === 白名单队列前 + 1, 非白名单)
const 非白名单未点名 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: false }))
断言('非白名单未点名静默', !非白名单未点名.ok && 收到的.length === 白名单前 + 2, 非白名单未点名)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 主人: ['wxid_owner'],
  群聊管理: [{ 群: '456@chatroom', 群聊权限: '仅白名单可聊', 群白名单: [], 固定回复: '', 群里只认点名: false }] })
const 空名单陌生人 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true }))
断言('空群白名单不放行陌生人', !空名单陌生人.ok && 收到的.length === 白名单前 + 2, 空名单陌生人)
const 空名单主人 = await 调('/dsh-wechat-bridge/receive', 群消息({ sender: '主人', senderId: 'wxid_owner', atMe: true }))
断言('空群白名单仍放行主人', 空名单主人.ok && 收到的.length === 白名单前 + 3, 空名单主人)
await 调('/dsh-wechat-bridge/save', { ...读配置(),
  群聊管理: [{ 群: '456@chatroom', 所有人可聊: false, 固定回复: '' }] })
const 预期队列 = (await 屏.概况()).待发队列
const 空回复 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true }))
断言('空固定回复保持静默', !空回复.ok && (await 屏.概况()).待发队列 === 预期队列, 空回复)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 试运行: true,
  群聊管理: [{ 群: '456@chatroom', 所有人可聊: false, 固定回复: '试运行回复' }] })
const 试回复 = await 调('/dsh-wechat-bridge/receive', 群消息({ atMe: true }))
断言('试运行只记录不入队', 试回复.ok && (await 屏.概况()).待发队列 === 预期队列, 试回复)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 试运行: false,
  群聊管理: [{ 群: '456@chatroom', 所有人可聊: false, 固定回复: '请联系群主' }] })
const 别群 = await 调('/dsh-wechat-bridge/receive', 群消息({ chat: '另一群', chatId: '789@chatroom', atMe: true }))
断言('未配置的群沿用原有行为', 别群.ok && 收到的.length === 白名单前 + 4, 别群)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 主人: ['wxid_owner'] })
const 主人群 = await 调('/dsh-wechat-bridge/receive', 群消息({ sender: '主人', senderId: 'wxid_owner', atMe: true }))
断言('受限群主人仍进入 AI', 主人群.ok && 收到的.length === 白名单前 + 5, 主人群)
await 调('/dsh-wechat-bridge/save', { ...读配置(), 群聊管理: [], 群回复间隔秒: 30 })

// 7 最近聊过谁被记下来了
const 概2 = await 屏.概况()
const 名 = 概2.最近聊天.map((x) => x.名)
断言('最近聊天记到了两个群', 名.includes('改了备注的群') && 名.includes('另一个群'), 概2.最近聊天)
const 存盘 = JSON.parse(readFileSync(聊天路径, 'utf8'))
断言('聊天文件落了盘', (存盘.聊天 || 存盘 || []).length >= 2, 存盘)

// 8 工具：wechat_block
const 块 = 工具.find((t) => t.name === 'wechat_block')
const 块加 = await 块.execute({ action: 'add', who: '张三' })
断言('工具加了屏蔽', 读配置().屏蔽.includes('张三'), 读配置().屏蔽)
const 块列 = await 块.execute({ action: 'list' })
断言('工具能列出屏蔽', /张三/.test(块列.说 || ''), 块列.说)
await 块.execute({ action: 'remove', who: '张三' })
断言('工具能删屏蔽', !读配置().屏蔽.includes('张三'), 读配置().屏蔽)

// 9 工具：wechat_bind（按标题猜会话）
const 绑 = 工具.find((t) => t.name === 'wechat_bind')
const 绑列 = await 绑.execute({ action: 'list' })
断言('工具能列绑定', /另一个群/.test(绑列.说 || ''), 绑列.说)
const 绑加 = await 绑.execute({ action: 'add', chat: '第三个群', session: '第二个会话说说' })
断言('工具按标题猜到了会话', /第二个就位|第二个会话说说/.test(绑加.说 || ''), 绑加.说)
断言('工具写进了绑定', 读配置().绑定.some((b) => b.聊天 === '第三个群' && b.会话 === 'session-bbb'), 读配置().绑定)
await 绑.execute({ action: 'remove', chat: '第三个群' })
断言('工具能解绑', !读配置().绑定.some((b) => b.聊天 === '第三个群'), 读配置().绑定)

// 10 发文件：不存在的拒、存在的排队
const 假文件 = join(家, '发送测试.txt')
writeFileSync(假文件, 'hello', 'utf8')
const 缺文件 = await 调('/dsh-wechat-bridge/send', {
  聊天: '测试群',
  文件: [join(家, '并不存在.txt')],
})
断言('文件不存在时拒发', /文件不存在/.test(缺文件.说 || ''), 缺文件)
const 有文件 = await 调('/dsh-wechat-bridge/send', {
  聊天: '测试群',
  文件: [假文件],
})
断言('有文件时排队成功', 有文件.ok === true, 有文件)
const 只有文件没文字 = await 调('/dsh-wechat-bridge/send', {
  聊天: '测试群',
  文本: '',
  文件: [假文件],
})
断言('只发文件也行', 只有文件没文字.ok === true, 只有文件没文字)

// Owner privileges follow senderId, never a mutable display name.
await 调('/dsh-wechat-bridge/save', { 主人: ['wxid_owner'], 默认会话: 'session-aaa' })
await 调('/dsh-wechat-bridge/receive', {
  text: '主人权限测试', chat: '改了备注的群', chatId: '123@chatroom', chatType: 'group',
  sender: '改名后的主人', senderId: 'wxid_owner',
})
断言('主人微信轮允许跑命令', 阀({ name: 'pwsh' }) === undefined)
断言('主人微信轮允许写文件', 阀({ name: 'write' }) === undefined)
await 调('/dsh-wechat-bridge/receive', {
  text: '冒名测试', chat: '改了备注的群', chatId: '123@chatroom', chatType: 'group',
  sender: 'wxid_owner', senderId: 'wxid_stranger',
})
断言('昵称冒充主人仍被拦截', typeof 阀({ name: 'pwsh' }) === 'string')

// Member queries may run in a guest round; writes must not.
const 成员 = 工具.find((t) => t.name === 'wechat_member_update')
const 无账号 = await 成员.execute({ 动作: '查' })
断言('未连接微信账号时不能查资料', 无账号.ok === false, 无账号)
await 调('/dsh-wechat-bridge/state', { 账号: 'wxid_test_account' })
const 外人改 = await 成员.execute({ 动作: '改', 昵称: 'wxid_alice', 字段: '地区', 值: '上海', 模式: 'upsert' })
断言('外人不能改成员资料', 外人改.ok === false && /主人/.test(外人改.说), 外人改)
if (testPython) {
  const 外人查 = await 成员.execute({ 动作: '查' })
  断言('外人能查询成员资料', 外人查.ok === true && 外人查.人数 === 0, 外人查)
  await 调('/dsh-wechat-bridge/receive', {
    text: '主人修改资料', chat: '测试群', chatId: '123@chatroom', chatType: 'group',
    sender: '主人', senderId: 'wxid_owner',
  })
  const 主人改 = await 成员.execute({ 动作: '改', 昵称: 'wxid_alice', 字段: '地区', 值: '上海', 模式: 'upsert' })
  断言('主人修改资料成功', 主人改.ok === true, 主人改)
  const 查询 = await 成员.execute({ 动作: '查', 地区: '上海' })
  断言('更新后能查到资料', 查询.ok === true && 查询.人数 === 1, 查询)
}
console.log('\n日志尾巴：\n' + String(屏.日志 || '').split('\n').slice(-4).join('\n'))
