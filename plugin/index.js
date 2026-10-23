/**
 * dsh-pc-hands — 给鲸娘一双手和一双眼睛。
 *
 * 不和 DSH 的 UI 抢地盘，只注册工具；真正干活的还是 pc 那套脚本
 * （pc.py / uia.ps1 / ocr.ps1 / pctask.py），这里做的事是：
 *   · 把「一句话」翻译成脚本调用，省掉一次 shell 往返
 *   · 统一超时、错误、输出裁剪（截图路径、OCR 文本、坐标）
 *
 * 为什么不在插件里重写键鼠：那些坑（前台锁、剪贴板编码、句柄复用、OCR 逐字框）
 * 已经在 py 里踩平并写进注释了，再写一遍只会多一份不一致。
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import { spawnSync, spawn } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'

const MARK = join(homedir(), 'OllamaStart', 'pc', 'plugin-loaded.json')

function mark(stage, extra) {
  try {
    writeFileSync(MARK, JSON.stringify({
      stage,
      at: new Date().toISOString(),
      pid: process.pid,
      ...extra,
    }, null, 2), 'utf8')
  } catch { /* 标记失败不该影响插件 */ }
}

export const inject = ['tools']

const HOMEDIR = homedir()

// 脚本目录：config → 环境变量 → 本机常见位置 → 家目录下的 dsh-pc-hands
const DIR_CANDIDATES = [
  process.env.DSH_PC_DIR,
  join(__dirname, 'pc'),               // 随插件一起 npm 安装时，脚本就在包内
  join(HOMEDIR, 'OllamaStart', 'pc'),
  join(HOMEDIR, 'dsh-pc-hands', 'pc'),
  join(HOMEDIR, '.dsh', 'pc-hands'),
].filter(Boolean)

// Python 解释器：config → 环境变量 → DSH 自带运行时 → PATH 里的 python
const PY_CANDIDATES = [
  process.env.DSH_PC_PYTHON,
  'D:/dsh2/resources/runtime/primary-runtime/dependencies/python/python.exe',
  'D:/dsh/resources/runtime/primary-runtime/dependencies/python/python.exe',
  join(HOMEDIR, 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'),
  'C:/Python312/python.exe',
  'C:/Python311/python.exe',
  'python',
  'python3',
].filter(Boolean)

function firstExisting(list) {
  for (const p of list) {
    if (p === 'python' || p === 'python3') return p
    try { if (existsSync(p)) return p } catch { /* ignore */ }
  }
  return null
}

function cfg(config) {
  const dir = (config?.pcDir && existsSync(config.pcDir)) ? config.pcDir : (firstExisting(DIR_CANDIDATES) || DIR_CANDIDATES[1])
  let python = null
  if (config?.pythonPath) python = config.pythonPath
  else {
    for (const p of PY_CANDIDATES) {
      if (p === 'python' || p === 'python3') { python = p; break }
      try { if (existsSync(p)) { python = p; break } } catch { /* ignore */ }
    }
  }
  return {
    dir,
    python: python || 'python',
    timeoutMs: Number(config?.timeoutMs) > 0 ? Number(config.timeoutMs) : 120000,
    dirOk: (() => { try { return existsSync(dir) } catch { return false } })(),
    pythonOk: (() => { try { return python === 'python' || python === 'python3' || existsSync(python) } catch { return false } })(),
  }
}

function run(conf, args, timeoutMs) {
  const res = spawnSync(conf.python, ['-X', 'utf8', ...args], {
    cwd: conf.dir,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    encoding: 'utf8',
    timeout: timeoutMs || conf.timeoutMs,
    windowsHide: true,
  })
  const out = ((res.stdout || '') + (res.stderr || '')).trim()
  const code = res.status === null ? -1 : res.status
  return { code, out }
}


function ps(conf, args, timeoutMs) {
  const res = spawnSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ...args], {
    cwd: conf.dir,
    encoding: 'utf8',
    timeout: timeoutMs || conf.timeoutMs,
    windowsHide: true,
  })
  const out = ((res.stdout || '') + (res.stderr || '')).trim()
  return { code: res.status === null ? -1 : res.status, out }
}

function tail(text, lines = 40, chars = 6000) {
  const arr = text.split(/\r?\n/)
  const sliced = arr.slice(-lines).join('\n')
  return sliced.length > chars ? sliced.slice(-chars) : sliced
}

// ---- 动键鼠前后的提示 · 2026-10-23 主人提的 ----
// 一次任务里常常连着好几个工具调用，所以做去抖：
// 15 秒内只弹一次「开始」，最后一个调用停下 1.8 秒后再弹「做完了」。
const NOTIFY_PS1 = join(homedir(), 'OllamaStart', 'notify.ps1')
let lastStartAt = 0
let endTimer = null

function toast(text) {
  try {
    // 不能加 detached: true —— 那样子进程会被 DSH 的 Job Object 收走，
    // 表现是「什么都没发生」且没有任何报错（2026-10-23 对照测过：同步/普通异步都行，detached 静默死）。
    const ps = join(process.env.SystemRoot || 'C:\\Windows',
      'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const child = spawn(ps, ['-NoProfile', '-ExecutionPolicy', 'Bypass',
      '-File', NOTIFY_PS1, '鲸娘', text], { stdio: 'ignore', windowsHide: true })
    child.on('error', (e) => {
      try {
        writeFileSync(join(DEFAULT_DIR, 'plugin-toast.log'),
          `${new Date().toISOString()} error ${e && e.message ? e.message : String(e)}\n`, { flag: 'a' })
      } catch { /* ignore */ }
    })
    child.unref()
  } catch (e) {
    try {
      writeFileSync(join(DEFAULT_DIR, 'plugin-toast.log'),
        `${new Date().toISOString()} throw ${e && e.message ? e.message : String(e)}\n`, { flag: 'a' })
    } catch { /* ignore */ }
  }
}

function announceStart(text) {
  if (endTimer) { clearTimeout(endTimer); endTimer = null }
  const now = Date.now()
  if (now - lastStartAt > 15000) {
    lastStartAt = now
    toast(text || '本鲸开始动键鼠了')
  }
}

function announceEnd() {
  if (endTimer) clearTimeout(endTimer)
  endTimer = setTimeout(() => {
    toast('做完了，可以碰屏幕了')
    endTimer = null
  }, 1800)
  if (endTimer.unref) endTimer.unref()
}

// 包一层：动键鼠的工具进来先响一声，出去定个「结束」
function withHands(fn, startText) {
  return function (args) {
    announceStart(startText)
    try {
      return fn(args)
    } finally {
      announceEnd()
    }
  }
}

function applyInner(ctx, config) {
  const conf = cfg(config)
  mark('apply-enter', { dir: conf.dir, python: conf.python, dirOk: conf.dirOk, pythonOk: conf.pythonOk })
  if (!conf.dirOk) {
    // 别静默退出：装错地方的人只会看到"工具不存在"，根本不知道哪里错
    console.error('[pc-hands] 脚本目录不存在: ' + conf.dir)
    mark('apply-degraded', { reason: 'pcDir missing', dir: conf.dir, tried: DIR_CANDIDATES })
    try {
      ctx.tools.register(defineTool({
        name: 'pc_doctor',
        description: 'PC 手眼插件的自检：报告脚本目录/Python/Pillow/OCR 的状态。当前脚本目录没找到，先跑这个看怎么修。',
        parameters: {},
        output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.text || v.error || '' }] },
        execute() {
          return {
            ok: false,
            text: [
              'pc-hands 还没装好：找不到脚本目录。',
              '',
              '试过这些位置：',
              ...DIR_CANDIDATES.map((d) => '  · ' + d),
              '',
              '怎么修（任选一）：',
              '  1) 把 pc.py / ocr.ps1 / uia.ps1 / pctask.py 放进 ' + join(HOMEDIR, 'OllamaStart', 'pc'),
              '  2) 在 cordis.patch.yml 里给 dsh-pc-hands 配 pcDir: <你的目录>',
              '  3) 设环境变量 DSH_PC_DIR=<你的目录>，重启 DSH',
            ].join('\n'),
          }
        },
      }))
    } catch (e) {
      mark('register-fail', { tool: 'pc_doctor', error: String(e && e.message ? e.message : e) })
    }
    return
  }
  const shotsDir = join(conf.dir, 'shots')
  try { mkdirSync(shotsDir, { recursive: true }) } catch { /* exists */ }

  const py = (script, args, timeout) => run(conf, [join(conf.dir, script), ...args], timeout)

  // 注册失败必须留证据：不然只能看到 apply-enter 就没了（第一次就是这样盲猜的）
  let registered = 0
  const failures = []
  // 会动键鼠/窗口的工具：进出都响一声（主人要的「开始有提示、结束也有」）
  const HANDS = new Set([
    'pc_click', 'pc_type', 'pc_key', 'pc_drag', 'pc_move', 'pc_hover',
    'pc_scan', 'pc_close', 'pc_task', 'pc_guard', 'pc_uia', 'pc_scroll',
  ])
  const wrapHands = (def) => {
    if (!def || !HANDS.has(def.name) || typeof def.execute !== 'function') return def
    const orig = def.execute.bind(def)
    def.execute = function (...callArgs) {
      announceStart(def.name === 'pc_task' ? '本鲸开始跑一串操作了' : '本鲸开始动键鼠了')
      try {
        return orig(...callArgs)
      } finally {
        announceEnd()
      }
    }
    return def
  }
  const reg = (def) => {
    try {
      ctx.tools.register(wrapHands(def))
      registered += 1
    } catch (error) {
      // Keep the failure instead of letting the final mark overwrite it: a
      // silent drop is exactly how pc_task went missing once already.
      failures.push({
        tool: def && def.name,
        error: error && error.message ? error.message : String(error),
      })
    }
  }

  // ---- 看 ----
  reg(defineTool({
    name: 'pc_see',
    description:
      '截屏并识别屏幕上的文字（Windows 原生 OCR），返回文本 + 截图路径。' +
      '用途：确认界面现在是什么、某段文字在不在、需要本鲸亲眼看时先拿图。' +
      'window 传窗口标题的子串就只截那个窗口；不传截整个屏幕。' +
      '注意：OCR 对中文会把字拆开、偶有错字，关键结论仍要自己看截图。',
    parameters: {
      window: { type: 'string', description: '窗口标题子串（可选）' },
      name: { type: 'string', description: '截图文件名的前缀（可选，默认 see）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.text || v.error || '' }] },
    execute(args) {
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      const out = join(shotsDir, `${args.name || 'see'}-${stamp}.png`)
      const shotArgs = ['shot', '--out', out]
      if (args.window) shotArgs.push('--window', String(args.window))
      const shotRes = py('pc.py', shotArgs)
      const ocrRes = py('pc.py', ['ocr', '--image', out])
      const lines = ocrRes.out.split(/\r?\n/).filter((l) => l.trim() && !l.includes('saved ') && !l.startsWith('  ok='))
      const result = {
        ok: shotRes.code === 0 && ocrRes.code === 0,
        image: out,
        text: lines.join('\n') || '(没有识别到文字)',
        raw: tail(ocrRes.out, 5, 800),
      }
      if (shotRes.code !== 0) result.error = tail(shotRes.out, 10, 800)
      return result
    },
  }))

  // ---- 找 ----
  reg(defineTool({
    name: 'pc_find',
    description: '在屏幕上用 OCR 找一个短语，返回它的中心坐标（可以直接交给 pc_click）。'
      + '没找到返回 null——不要凭猜点坐标。',
    parameters: {
      text: { type: 'string', description: '要找的短语（中文可直接写）' },
      window: { type: 'string', description: '先激活这个标题的窗口（可选）' },
      below: { type: 'string', description: '锚点：只接受这个短语下方的候选（可选，如「联系人」）' },
      above: { type: 'string', description: '锚点：只接受这个短语上方的候选（可选，如「群聊」）' },
      region: { type: 'string', description: '像素区域 x0,y0,x1,y1，只在这个范围里找（可选）' },
      pick: { type: 'string', description: 'first / top / last / bottom，多个候选时取哪个（默认 first）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (!args.text) return { ok: false, message: '需要 text' }
      if (args.window) py('pc.py', ['activate', String(args.window)])
      const res = py('pc.py', ['findtext', String(args.text), ...narrowArgs(args)])
      const m = res.out.match(/at \((\d+),(\d+)\)/)
      if (!m) return { ok: false, x: null, y: null, message: `没找到「${args.text}」` }
      return { ok: true, x: Number(m[1]), y: Number(m[2]), message: res.out.trim() }
    },
  }))

  // ---- 手 ----
  reg(defineTool({
    name: 'pc_click',
    description: '点一下。三种给法：text（OCR 找到这句话再点它，最稳）、x/y（坐标）、window+name（UIA 按控件名点）。'
      + '动手前建议先 pc_see 确认界面，点完再 pc_see 核对。',
    parameters: {
      text: { type: 'string', description: '按文字点（OCR）' },
      below: { type: 'string', description: '只点这个短语下方的（可选）' },
      above: { type: 'string', description: '只点这个短语上方的（可选）' },
      region: { type: 'string', description: '像素区域 x0,y0,x1,y1（可选）' },
      pick: { type: 'string', description: 'first / top / last / bottom（默认 first）' },
      x: { type: 'number', description: '坐标 x' },
      y: { type: 'number', description: '坐标 y' },
      window: { type: 'string', description: '窗口标题子串（UIA 方式或先激活）' },
      name: { type: 'string', description: '控件名（UIA 方式，需要同时给 window）' },
      double: { type: 'boolean', description: '双击' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (args.text) {
        const res = py('pc.py', ['clicktext', String(args.text), ...narrowArgs(args)])
        return { ok: res.code === 0, message: tail(res.out, 8, 700) }
      }
      if (typeof args.x === 'number' && typeof args.y === 'number') {
        const a = ['click', String(Math.round(args.x)), String(Math.round(args.y))]
        if (args.double) a.push('--double')
        const res = py('pc.py', a)
        return { ok: res.code === 0, message: tail(res.out, 4, 400) }
      }
      if (args.window && args.name) {
        const res = py('pc.py', ['seq', `sel ${args.window} ${args.name}`])
        return { ok: res.code === 0, message: tail(res.out, 8, 700) }
      }
      return { ok: false, message: '要么给 text，要么给 x/y，要么给 window+name' }
    },
  }))

  reg(defineTool({
    name: 'pc_type',
    description: '往当前焦点里打字（中文走 Win32 剪贴板，所以任何程序都吃得下）。'
      + '要求：先把目标窗口激活并有输入焦点，否则字会跑到别处。',
    parameters: { text: { type: 'string', description: '要输入的文字' } },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (!args.text) return { ok: false, message: '需要 text' }
      const res = py('pc.py', ['type', String(args.text)])
      return { ok: res.code === 0, message: tail(res.out, 4, 400) }
    },
  }))

  reg(defineTool({
    name: 'pc_key',
    description: '按键或组合键：enter/esc/tab/pagedown/f2… 或 ctrl+s、ctrl+home 这种组合。',
    parameters: { keys: { type: 'string', description: '键名或组合，如 pagedown / ctrl+home' } },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (!args.keys) return { ok: false, message: '需要 keys' }
      const combo = String(args.keys)
      const res = py('pc.py', [combo.includes('+') ? 'hotkey' : 'key', combo])
      return { ok: res.code === 0, message: tail(res.out, 4, 400) }
    },
  }))

  // ---- 滚 ----
  reg(defineTool({
    name: 'pc_scroll',
    description:
      '滚动鼠标滚轮（在指针位置滚）。amount 正数向上、负数向下；一格约 120，'
      + '一次 -1200 大约翻 10 行。可先给 x/y 把指针移过去再滚。',
    parameters: {
      amount: { type: 'number', description: '滚动量，正上负下（建议 ±1200 一档）' },
      x: { type: 'number', description: '先移动指针到的 x（可选）' },
      y: { type: 'number', description: '先移动指针到的 y（可选）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.ok ? ('滚动 ' + v.amount) : (v.error || '滚动失败') }] },
    execute(args) {
      const amount = Number.isFinite(args.amount) ? Math.trunc(args.amount) : -1200
      if (Number.isFinite(args.x) && Number.isFinite(args.y)) {
        py('pc.py', ['move', String(Math.trunc(args.x)), String(Math.trunc(args.y))])
      }
      const r = py('pc.py', ['scroll', String(amount)])
      return { ok: r.code === 0, amount, out: tail(r.out, 4, 300) }
    },
  }))

  // ---- 剪贴板 ----
  reg(defineTool({
    name: 'pc_clip',
    description:
      '读写系统剪贴板纯文本。get 读出来（抓页面上的长串、验证复制成功时用）；'
      + 'set 把文本放进剪贴板（之后用 pc_key ctrl+v 粘贴）。不点任何界面。',
    parameters: {
      action: { type: 'string', enum: ['get', 'set'], description: 'get 读 / set 写' },
      text: { type: 'string', description: 'action=set 时要写进去的文本' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.ok ? (v.action === 'get' ? (v.text || '(空)') : v.out) : ('失败: ' + (v.error || '')) }] },
    execute(args) {
      if (args.action === 'set') {
        const r = py('pc.py', ['clipset', String(args.text ?? '')])
        return { ok: r.code === 0, action: 'set', out: tail(r.out, 3, 200) }
      }
      const r = py('pc.py', ['clipget'])
      return { ok: r.code === 0, action: 'get', text: r.out.replace(/\r/g, '') }
    },
  }))

  // ---- 自检 ----
  reg(defineTool({
    name: 'pc_doctor',
    description:
      'PC 手眼插件自检：报告系统、Python、Pillow、截图、OCR 引擎、DPI 缩放、显示器、'
      + '剪贴板、通知脚本是否就绪。装完/换机器后先跑这个；出问题也先跑这个。',
    parameters: {},
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.text || v.error || '' }] },
    execute() {
      const r = py('pc.py', ['doctor'], 90000)
      const fails = (r.out.match(/FAIL/g) || []).length
      if (r.code !== 0 && !r.out) {
        return {
          ok: false,
          text: [
            '跑不起来 pc.py doctor。',
            '  脚本目录: ' + conf.dir,
            '  Python  : ' + conf.python,
            '',
            '常见原因：Python 路径不对（在 cordis.patch.yml 里配 pythonPath），',
            '或者没装 Pillow（pip install pillow）。',
          ].join('\n'),
        }
      }
      return { ok: fails === 0, text: r.out, fails }
    },
  }))

  reg(defineTool({
    name: 'pc_windows',
    description: '列出当前可见窗口（hwnd/pid/位置/标题），用来确认「要操作的是哪个窗口」——句柄会变，别缓存。',
    parameters: {},
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.text }] },
    execute() {
      const res = py('pc.py', ['windows'])
      return { ok: res.code === 0, text: tail(res.out, 45, 5000) }
    },
  }))

  reg(defineTool({
    name: 'pc_scan',
    description: '兜底动作：沿一列 y 坐标逐个点击，每点一次就 OCR 校验一处区域，命中才停。'
      + '用在「目标文字 OCR 读不出来」的界面上（微信搜索列表就是这种）。'
      + '要求：每次点击都幂等（只是打开某个列表项），且校验区域里的字是读得出来的（如窗口标题栏）。',
    parameters: {
      x: { type: 'number', description: '点击的 x 坐标' },
      y0: { type: 'number', description: '起始 y' },
      y1: { type: 'number', description: '结束 y' },
      verify: { type: 'string', description: '校验区域里应该出现的文字' },
      region: { type: 'string', description: '校验区域 x0,y0,x1,y1（通常是标题栏）' },
      step: { type: 'number', description: '每次下移多少像素（默认 22）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (typeof args.x !== 'number' || typeof args.y0 !== 'number' || typeof args.y1 !== 'number') {
        return { ok: false, message: '需要 x / y0 / y1' }
      }
      if (!args.verify || !args.region) return { ok: false, message: '需要 verify 与 region' }
      const res = py('pc.py', ['scanclick', String(Math.round(args.x)),
        Math.round(args.y0) + '-' + Math.round(args.y1),
        '--verify', String(args.verify), '--vregion', String(args.region),
        '--step', String(Math.round(args.step || 22))], 180000)
      return { ok: res.code === 0, message: tail(res.out, 30, 3000) }
    },
  }))

  // ---- 控件层（UIA）----
  reg(defineTool({
    name: 'pc_uia',
    description: '看/点「控件」而不是像素：list=列出该窗口里所有带名字的控件（含可执行的动作），'
      + 'tree=打印控件层级（判断「哪个分组里的那个按钮」用），select/invoke=按控件名选中或点击。'
      + '适合微信以外的常规 Windows 程序（WPS、资源管理器、系统对话框）；微信几乎不暴露控件，用 OCR。',
    parameters: {
      action: { type: 'string', description: 'list / tree / select / invoke' },
      window: { type: 'string', description: '窗口标题子串（用来定位窗口）' },
      hwnd: { type: 'number', description: '窗口句柄（已知时可直接给，优先于 window）' },
      name: { type: 'string', description: '控件名（select/invoke 需要；list 可当过滤词）' },
      depth: { type: 'number', description: 'tree 的深度，默认 3' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.text }] },
    execute(args) {
      const action = String(args.action || 'list')
      let hwnd = args.hwnd
      if (!hwnd) {
        if (!args.window) return { ok: false, text: '需要 window（标题子串）或 hwnd' }
        const w = py('pc.py', ['windows'])
        const line = w.out.split(/\r?\n/).find((l) => l.includes(String(args.window)))
        if (!line) return { ok: false, text: '没找到标题含「' + args.window + '」的窗口' }
        hwnd = Number(line.trim().split(/\s+/)[0])
      }
      const uiaVerb = action === 'tree' ? 'tree' : action
      const avatar = [String(hwnd), ...(args.name ? [String(args.name)] : []),
        ...(action === 'tree' ? [String(Math.round(args.depth || 3))] : [])]
      const res = ps(conf, [join(conf.dir, 'uia.ps1'), uiaVerb, ...avatar], 120000)
      return { ok: res.code === 0, hwnd, text: tail(res.out, 60, 6000) }
    },
  }))

  // ---- 弹窗守卫 ----
  reg(defineTool({
    name: 'pc_guard',
    description: '意外弹窗守卫：action=list 只列出当前对话框（只读）；action=guard 在后台守一段时间，'
      + '遇到新弹窗时按按钮文案判断——只有「取消/否/关闭/以后再说/不保存/跳过」这类安全按钮会自动点，'
      + '碰到「确定/是/保存/覆盖/删除/允许」一律不点，留给主人决定。日志在 pc\\popup_guard.log。',
    parameters: {
      action: { type: 'string', description: 'list（只读列出）/ guard（守一段时间）' },
      seconds: { type: 'number', description: 'guard 持续秒数，默认 120' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      const action = String(args.action || 'list')
      if (action === 'guard') {
        const secs = Math.round(args.seconds || 120)
        const res = py('pc.py', ['guard', '--seconds', String(secs)], (secs + 30) * 1000)
        return { ok: res.code === 0, message: tail(res.out, 40, 4000) || 'guard 已结束' }
      }
      const res = py('pc.py', ['popups'])
      return { ok: res.code === 0, message: tail(res.out, 30, 3000) || '当前没有对话框' }
    },
  }))

  // ---- 拖 ----
  reg(defineTool({
    name: 'pc_drag',
    description: '拖东西。给 window 就拖那个窗口（自己按标题取矩形，抓身体），不给就按坐标拖。'
      + '桌宠这类「要先按住一会儿才跟着走」的目标，hold 给 400~600、steps 给 35~40 才跟手。',
    parameters: {
      window: { type: 'string', description: '窗口标题子串（给了就拖窗口，x1/y1 忽略）' },
      x1: { type: 'number', description: '起点 x（不给 window 时必需）' },
      y1: { type: 'number', description: '起点 y' },
      x2: { type: 'number', description: '终点 x' },
      y2: { type: 'number', description: '终点 y' },
      hold: { type: 'number', description: '按住多少毫秒再移动，默认 600' },
      steps: { type: 'number', description: '分几步移动，默认 18（拖桌宠给 35~40）' },
      gap: { type: 'number', description: '每步间隔毫秒，默认 12（拖桌宠给 15）' },
      grab: { type: 'string', description: '抓取点相对窗口左上角的偏移 dx,dy（可选）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      const hold = Math.round(args.hold || 600)
      const steps = Math.round(args.steps || 18)
      const gap = Math.round(args.gap || 12)
      if (args.window) {
        if (typeof args.x2 !== 'number' || typeof args.y2 !== 'number') {
          return { ok: false, message: '拖窗口需要 x2 / y2（终点）' }
        }
        const a = ['dragwin', String(args.window), String(Math.round(args.x2)), String(Math.round(args.y2)),
          '--hold', String(hold)]
        if (args.grab) a.push('--grab', String(args.grab))
        const res = py('pc.py', a, 60000)
        return { ok: res.code === 0, message: tail(res.out, 8, 600) }
      }
      if ([args.x1, args.y1, args.x2, args.y2].some((v) => typeof v !== 'number')) {
        return { ok: false, message: '要么给 window + x2/y2，要么给 x1/y1/x2/y2' }
      }
      const res = py('pc.py', ['drag', String(Math.round(args.x1)), String(Math.round(args.y1)),
        String(Math.round(args.x2)), String(Math.round(args.y2)),
        '--hold', String(hold), '--steps', String(steps), '--gap', String(gap)], 60000)
      return { ok: res.code === 0, message: tail(res.out, 8, 600) }
    },
  }))

  // ---- 搬窗口 ----
  reg(defineTool({
    name: 'pc_move',
    description: '按标题把窗口搬到某个位置（秒级，尺寸取自实时矩形，不会把窗口搞坏）。'
      + '能用它就别用「拖」——拖容易失手。',
    parameters: {
      window: { type: 'string', description: '窗口标题子串' },
      x: { type: 'number', description: '新左上角 x' },
      y: { type: 'number', description: '新左上角 y' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (!args.window) return { ok: false, message: '需要 window' }
      if (typeof args.x !== 'number' || typeof args.y !== 'number') {
        return { ok: false, message: '需要 x / y' }
      }
      const res = py('pc.py', ['movewin', String(args.window), String(Math.round(args.x)), String(Math.round(args.y))], 30000)
      return { ok: res.code === 0, message: tail(res.out, 6, 500) }
    },
  }))

  // ---- 悬停 ----
  reg(defineTool({
    name: 'pc_hover',
    description: '把鼠标慢慢滑到某点并停住（约 2 秒），让 hover 面板渲染出来；'
      + '给 text 的话再在悬停点附近找这几个字并点它（面板里的按钮）。'
      + '一步跳过去只会出思考气泡，不会出面板——这就是它存在的理由。',
    parameters: {
      x: { type: 'number', description: '悬停点 x' },
      y: { type: 'number', description: '悬停点 y' },
      wait: { type: 'number', description: '停住多少毫秒，默认 2000' },
      text: { type: 'string', description: '要点的按钮文字（可选；给了就顺带点）' },
      dry: { type: 'boolean', description: '只找不点（配合 text 调试）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (typeof args.x !== 'number' || typeof args.y !== 'number') {
        return { ok: false, message: '需要 x / y' }
      }
      const wait = Math.round(args.wait || 2000)
      if (args.text) {
        const a = ['hoverclick', String(Math.round(args.x)), String(Math.round(args.y)), String(args.text),
          '--wait', String(wait)]
        if (args.dry) a.push('--dry')
        const res = py('pc.py', a, wait + 60000)
        return { ok: res.code === 0, message: tail(res.out, 10, 900) }
      }
      const res = py('pc.py', ['hover', String(Math.round(args.x)), String(Math.round(args.y)), '--wait', String(wait)], 40000)
      return { ok: res.code === 0, message: tail(res.out, 6, 500) }
    },
  }))

  // ---- 读控制台 ----
  reg(defineTool({
    name: 'pc_console',
    description: '直接读控制台/窗口里的文字，比 OCR 准：'
      + 'action=read 用 Console API 读整坨输出（Dev-C++ 那种黑窗口）；'
      + 'action=find 在窗口区域里找几个字（走 OCR，但只在窗口内）；'
      + 'action=watch 轮询到某句出现为止（一个进程里等，模型只加载一次，很快）。'
      + 'hwnd 用 pc_windows 拿不到控制台时，可以先 pc_task 的 launch 结果或自己枚举。',
    parameters: {
      action: { type: 'string', description: 'read / find / watch' },
      hwnd: { type: 'number', description: '窗口句柄' },
      until: { type: 'string', description: 'watch：等到哪句出现（逗号分隔多个）' },
      find: { type: 'string', description: 'find：要找的文字' },
      timeout: { type: 'number', description: 'watch：最多等多少秒，默认 60' },
      tail: { type: 'boolean', description: 'read：只给最后 12 行' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      const action = String(args.action || 'read')
      if (typeof args.hwnd !== 'number') return { ok: false, message: '需要 hwnd' }
      const hwnd = Math.round(args.hwnd)
      if (action === 'read') {
        const a = ['conread', String(hwnd)]
        if (args.tail) a.push('--tail')
        const res = py('pc.py', a, 30000)
        return { ok: res.code === 0, message: tail(res.out, 40, 4000) }
      }
      if (action === 'find') {
        if (!args.find) return { ok: false, message: 'find 需要 find 文字' }
        const res = py('pc.py', ['wintext', String(hwnd), '--find', String(args.find)], 60000)
        return { ok: res.code === 0, message: tail(res.out, 10, 900) }
      }
      if (action === 'watch') {
        if (!args.until) return { ok: false, message: 'watch 需要 until' }
        const secs = Math.round(args.timeout || 60)
        const res = py('pc.py', ['watch', String(hwnd), '--until', String(args.until),
          '--timeout', String(secs), '--click'], (secs + 30) * 1000)
        return { ok: res.code === 0, message: tail(res.out, 10, 900) }
      }
      return { ok: false, message: 'action 只能是 read / find / watch' }
    },
  }))

  // ---- 关窗口 ----
  reg(defineTool({
    name: 'pc_close',
    description: '按标题子串关窗口（比如 Dev-C++ 跑完停在「请按任意键继续」的黑窗口）；'
      + '发的是 WM_CLOSE，等于点了右上角，不粗暴。',
    parameters: { window: { type: 'string', description: '窗口标题子串' } },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.message }] },
    execute(args) {
      if (!args.window) return { ok: false, message: '需要 window' }
      const res = py('pc.py', ['closewin', String(args.window)], 20000)
      return { ok: res.code === 0, message: tail(res.out, 6, 500) }
    },
  }))

  // ---- 任务 ----
  reg(defineTool({
    name: 'pc_task',
    description:
      '跑一串任务步骤（不是单次点击），每步都有判据、失败会重试，最后写 journal。' +
      '步骤格式：{do, ...}，do 可选 launch/want/activate/uia/key/type/scroll/shot/assert/assert_text/click_text/seek/sleep/notify/movewin/drag/hover/conread/closewin。' +
      '典型用法：先 pc_see 看现状 → 想出步骤 → pc_task 一次跑完 → 看返回的 journal 和证据截图。' +
      '关键：用 want 等窗口、用 assert_text 验文字、用 seek 翻到看见为止——不要写死页数和坐标。',
    parameters: {
      name: { type: 'string', description: '任务名（写进 journal）' },
      steps: { type: 'array', description: '步骤数组，见描述里的 do 列表', items: { type: 'object', additionalProperties: true, properties: { do: { type: 'string', description: '动词名' } } } },
      journal: { type: 'string', description: 'journal 输出路径（可选）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: v.text }] },
    execute(args) {
      if (!Array.isArray(args.steps) || args.steps.length === 0) {
        return { ok: false, text: '需要非空的 steps 数组' }
      }
      const planPath = join(conf.dir, 'plans', `_inline-${Date.now()}.json`)
      const plan = {
        name: args.name || 'inline-task',
        journal: args.journal || planPath.replace(/\.json$/, '.journal.json'),
        on_fail: 'stop',
        steps: args.steps,
      }
      try {
        mkdirSync(join(conf.dir, 'plans'), { recursive: true })
        writeFileSync(planPath, JSON.stringify(plan, null, 2), 'utf8')
      } catch (e) {
        return { ok: false, text: '写 plan 失败: ' + String(e) }
      }
      const res = py('pctask.py', ['run', planPath], Math.max(conf.timeoutMs, 180000))
      return { ok: res.code === 0, plan: planPath, text: tail(res.out, 60, 8000) }
    },
  }))

  mark('tools-registered', { count: registered, expected: 18, failures })
  console.log('[pc-hands] 工具就绪: pc_see / pc_find / pc_click / pc_type / pc_key / pc_windows / pc_task / pc_scan / pc_uia / pc_guard / pc_drag / pc_move / pc_hover / pc_console / pc_close (v0.4.0)')
}


function narrowArgs(args) {
  const out = []
  if (args.below) out.push('--below', String(args.below))
  if (args.above) out.push('--above', String(args.above))
  if (args.region) out.push('--region', String(args.region))
  if (args.pick) out.push('--pick', String(args.pick))
  return out
}

export function apply(ctx, config) {
  try {
    applyInner(ctx, config)
  } catch (error) {
    mark('apply-throw', {
      error: error && error.message ? error.message : String(error),
      stack: error && error.stack ? String(error.stack).split('\n').slice(0, 8) : undefined,
    })
    console.error('[pc-hands] 激活失败: ' + (error && error.message ? error.message : String(error)))
  }
}
