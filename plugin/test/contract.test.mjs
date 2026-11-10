/**
 * dsh-pc-hands 的契约测试。
 *
 * 守三件事：
 *   1. 工具定义本身干净（名字不重、数量对、schema 合法）——schema 非法时 DSH 会
 *      **静默丢掉整条工具**，当年 pc_task 就是这么消失的，而且没有任何报错。
 *   2. 校验规则没跑偏——规则不是本测试自己编的，是从本机真实 app.asar 里的
 *      @deepseek-ai/dsh-tools 抄下来并逐条对账（DSH 一改规则这里就红）。
 *   3. 活着的 DSH 确实收下了它们——插件自己会写 plugin-loaded.json，
 *      里面记着 count / expected / failures。
 *
 * 跑法：node --test test/
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, dirname } from 'node:path'
import { openAsar } from './asar.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_DIR = join(HERE, '..')
const INDEX_JS = join(PLUGIN_DIR, 'index.js')
const MARKER = join(PLUGIN_DIR, '..', 'plugin-loaded.json')
const ASAR = process.env.DSH_ASAR || 'D:/dsh2/resources/app.asar'
// 2026-11-07 新版 DSH 之后，规则从 lib/index.js 搬到了 lib/types/json-schema.js
// （还有一份共享 chunk）。这里按顺序找，第一个存在的就用。
const DSH_TOOLS_CANDIDATES = [
  'dsh/node_modules/@deepseek-ai/dsh-tools/lib/types/json-schema.js',
  'dsh/node_modules/@deepseek-ai/dsh-tools/lib/shared-DTdn5dXO.js',
  'dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js',
]
const DSH_TOOLS_IN_ASAR = DSH_TOOLS_CANDIDATES.find((p) => {
  try {
    const a = openAsar(ASAR)
    return a.files.some((f) => f.path === p)
  } catch { return false }
}) || DSH_TOOLS_CANDIDATES[DSH_TOOLS_CANDIDATES.length - 1]

const SRC = readFileSync(INDEX_JS, 'utf8')

// ---------------------------------------------------------------------------
// 从插件源码里把工具定义抠出来（不执行插件：它要 DSH 的 cordis 才能跑）
// ---------------------------------------------------------------------------

/** 从 open 位置（指向 '{'）开始，按括号配平取出一段对象字面量，跳过字符串与注释。 */
function sliceLiteral(src, open) {
  assert.equal(src[open], '{', 'sliceLiteral 期望从 { 开始')
  let depth = 0
  let i = open
  while (i < src.length) {
    const c = src[i]
    if (c === "'" || c === '"' || c === '`') {
      const quote = c
      i += 1
      while (i < src.length) {
        if (src[i] === '\\') { i += 2; continue }
        if (src[i] === quote) break
        i += 1
      }
      i += 1
      continue
    }
    if (c === '/' && src[i + 1] === '/') { while (i < src.length && src[i] !== '\n') i += 1; continue }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 2; continue }
    if (c === '{') depth += 1
    else if (c === '}') { depth -= 1; if (depth === 0) return src.slice(open, i + 1) }
    i += 1
  }
  throw new Error('对象字面量没有配平')
}

/** 找出所有 defineTool 调用，抠出 name 与 parameters（parameters 是作者侧的简化写法，不是 JSON Schema）。 */
function extractToolDefs(src) {
  const defs = []
  const nameRe = /name:\s*'([a-z0-9_]+)'/g
  const paramRe = /parameters:\s*\{/g
  const names = []
  for (let m; (m = nameRe.exec(src));) names.push({ name: m[1], at: m.index })
  const params = []
  for (let m; (m = paramRe.exec(src));) params.push({ at: m.index, open: m.index + m[0].length - 1 })

  for (const p of params) {
    // 配到「它前面最近的那个 name」
    let owner = null
    for (const n of names) if (n.at < p.at) owner = n
    const literal = sliceLiteral(src, p.open)
    defs.push({ name: owner ? owner.name : '(unknown)', literal })
  }
  return defs
}

const TOOL_LITERALS = extractToolDefs(SRC)
const TOOL_NAMES = TOOL_LITERALS.map((d) => d.name).filter((n) => n !== '(unknown)')

// 19 个工具是「对外契约」：少一个就是坏了，多一个要显式改这里
const EXPECTED_NAMES = [
  'pc_click', 'pc_clip', 'pc_close', 'pc_console', 'pc_doctor', 'pc_drag',
  'pc_find', 'pc_guard', 'pc_hover', 'pc_key', 'pc_move', 'pc_scan',
  'pc_see', 'pc_scroll', 'pc_task', 'pc_type', 'pc_uia', 'pc_wait', 'pc_windows',
]

test('工具数量与名字：19 个，且与对外契约一致', () => {
  const unique = [...new Set(TOOL_NAMES)].sort()
  assert.deepEqual(unique, [...EXPECTED_NAMES].sort(),
    '工具集合变了。真删/真加都行，但必须同步改 EXPECTED_NAMES，并重启 DSH 让 marker 更新')

  // 允许的重复只有一种：pc_doctor 在「脚本目录不存在」的降级分支里另注册一份
  //（两条分支互斥，运行时只会走一条）。别的重复就是真覆盖。
  const seen = new Set()
  const dups = new Set()
  for (const n of TOOL_NAMES) { if (seen.has(n)) dups.add(n); else seen.add(n) }
  assert.deepEqual([...dups].filter((n) => n !== 'pc_doctor'), [],
    '有非预期重名：重名会让其中一条被静默覆盖')
})

// ---------------------------------------------------------------------------
// schema 校验：规则全部抄自 dsh-tools（见下方「规则对账」测试）
// ---------------------------------------------------------------------------

const CONSTRAINT_KEYWORDS = new Set(['type', 'oneOf', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'const'])
const ANNOTATION_KEYWORDS = new Set(['description', 'title', 'default', 'examples'])
const SCHEMA_TYPES = ['object', 'array', 'string', 'number', 'integer', 'boolean', 'null']
const KEYWORD_ONLY_ON = {
  properties: ['object'], required: ['object'], additionalProperties: ['object'],
  items: ['array'], enum: ['string', 'number', 'integer', 'boolean', 'null'],
  const: ['string', 'number', 'integer', 'boolean', 'null'],
}
const ONE_OF_SIBLINGS = ['properties', 'required', 'additionalProperties', 'items', 'enum', 'const']

/** 把插件作者的 parameters 写法（{ 参数名: {type, description} }）转成 object schema。 */
function paramsToSchema(params) {
  return {
    type: 'object',
    additionalProperties: false,
    properties: params,
    ...(Object.keys(params).length ? {} : {}),
  }
}

/** 逐条检查一个 JSON Schema 子集，返回违规列表（空 = 合规）。 */
function lintSchema(node, path = '$', out = []) {
  if (!node || typeof node !== 'object' || Array.isArray(node)) {
    out.push(`${path} 不是对象`)
    return out
  }
  for (const key of Object.keys(node)) {
    if (!CONSTRAINT_KEYWORDS.has(key) && !ANNOTATION_KEYWORDS.has(key)) {
      out.push(`${path}.${key} 不是受支持的关键字（只支持 type/oneOf/properties/required/additionalProperties/items/enum/const + 注解）`)
    }
  }
  if (Object.hasOwn(node, 'oneOf')) {
    for (const k of ONE_OF_SIBLINGS) if (Object.hasOwn(node, k)) out.push(`${path}: oneOf 旁边不能有 ${k}`)
    if (!Array.isArray(node.oneOf) || !node.oneOf.length) out.push(`${path}.oneOf 必须是非空数组`)
    else node.oneOf.forEach((sub, i) => lintSchema(sub, `${path}.oneOf[${i}]`, out))
    return out
  }
  if (Object.hasOwn(node, 'type')) {
    if (Array.isArray(node.type)) out.push(`${path}.type 不支持数组写法`)
    else if (!SCHEMA_TYPES.includes(node.type)) out.push(`${path}.type=${node.type} 不在允许列表里`)
    else {
      for (const [key, types] of Object.entries(KEYWORD_ONLY_ON)) {
        if (Object.hasOwn(node, key) && !types.includes(node.type)) out.push(`${path}.${key} 不能用在 type="${node.type}" 上`)
      }
      if (node.type === 'object') {
        // ★ 核心那条：对象必须显式写 additionalProperties，否则 DSH 编译不过、整条工具被静默丢弃
        if (!Object.hasOwn(node, 'additionalProperties')) out.push(`${path} 是 object 但没写 additionalProperties（DSH 会拒收，且静默）`)
        else if (typeof node.additionalProperties !== 'boolean') out.push(`${path}.additionalProperties 必须是布尔`)
        const props = node.properties
        if (props !== undefined) {
          if (typeof props !== 'object' || Array.isArray(props)) out.push(`${path}.properties 必须是对象`)
          else for (const [k, v] of Object.entries(props)) lintSchema(v, `${path}.properties.${k}`, out)
        }
        if (Object.hasOwn(node, 'required')) {
          if (!Array.isArray(node.required) || node.required.some((x) => typeof x !== 'string')) out.push(`${path}.required 必须是字符串数组`)
          else for (const k of node.required) if (!props || !Object.hasOwn(props, k)) out.push(`${path}.required 里的 "${k}" 不在 properties 里`)
        }
      }
      if (node.type === 'array' && Object.hasOwn(node, 'items')) lintSchema(node.items, `${path}.items`, out)
    }
  }
  return out
}

test('每个工具的 schema 都能过 DSH 的子集校验', () => {
  const problems = []
  for (const { name, literal } of TOOL_LITERALS) {
    let params
    try {
      // 只求值这段字面量：它由字符串/数字/嵌套对象组成，不含函数调用
      params = new Function('return (' + literal + ')')()
    } catch (e) {
      problems.push(`${name}: parameters 不是可求值的字面量（${e.message}）`)
      continue
    }
    problems.push(...lintSchema(paramsToSchema(params), name).map((p) => `${name}: ${p}`))
  }
  assert.deepEqual(problems, [], 'schema 有问题：\n' + problems.join('\n'))
})

test('schema 校验器自身不空转：故意写坏一个，必须被抓出来', () => {
  const bad = paramsToSchema({ x: { type: 'object', properties: { y: { type: 'string' } } } })
  const out = lintSchema(bad)
  assert.ok(out.some((m) => m.includes('additionalProperties')), '没抓到「object 缺 additionalProperties」——校验器形同虚设')

  const bad2 = paramsToSchema({ x: { type: 'string', items: { type: 'string' } } })
  assert.ok(lintSchema(bad2).some((m) => m.includes('items')), '没抓到「items 用在 string 上」')

  const bad3 = paramsToSchema({ x: { type: 'array', items: { type: 'object', properties: {} } } })
  assert.ok(lintSchema(bad3).some((m) => m.includes('additionalProperties')), '没抓到「数组元素是 object 但缺 additionalProperties」')
})

// ---------------------------------------------------------------------------
// 规则对账：上面那几条规则是不是真的还是 DSH 的规则
// ---------------------------------------------------------------------------

test('校验规则与真实 dsh-tools 对账（DSH 改规则这里就红）', { skip: !existsSync(ASAR) && '找不到 app.asar' }, () => {
  const a = openAsar(ASAR)
  const src = a.read(DSH_TOOLS_IN_ASAR).toString('utf8')

  // 1) 允许的关键字清单没变
  const constraint = src.match(/const CONSTRAINT_KEYWORDS = new Set\(\[([\s\S]*?)\]\)/)
  assert.ok(constraint, 'dsh-tools 里找不到 CONSTRAINT_KEYWORDS，结构变了，需要重抄规则')
  const realKeywords = [...constraint[1].matchAll(/["']([a-zA-Z]+)["']/g)].map((m) => m[1]).sort()
  assert.deepEqual(realKeywords, [...CONSTRAINT_KEYWORDS].sort(), 'DSH 支持的关键字变了')

  // 2) 类型清单没变
  const types = src.match(/const SCHEMA_TYPES = \[([\s\S]*?)\]/)
  assert.ok(types, '找不到 SCHEMA_TYPES')
  const realTypes = [...types[1].matchAll(/["']([a-z]+)["']/g)].map((m) => m[1]).sort()
  assert.deepEqual(realTypes, [...SCHEMA_TYPES].sort(), 'DSH 允许的 type 变了')

  // 3) 「对象必须显式写 additionalProperties（且必须是布尔）」这条还在
  //    2026-11-07 新版措辞从 "explicitly true or false" 改成了 "must be a boolean"，
  //    语义没变，所以两种说法都认。
  const AP_WORDINGS = [
    'additionalProperties must be a boolean',
    'additionalProperties must be explicitly true or false',
  ]
  assert.ok(AP_WORDINGS.some((w) => src.includes(w)),
    '「对象必须显式写 additionalProperties」这条规则变了 —— 我们的核心断言要跟着改')

  // 4) type 数组仍不被支持
  assert.ok(src.includes('type arrays are not supported'), 'type 数组的支持情况变了')
})

// ---------------------------------------------------------------------------
// 跨语言契约：pc.py 打印的东西，插件在解析
// ---------------------------------------------------------------------------

test('pc.py 的 findtext 输出格式与插件的解析仍然对得上', () => {
  // 两种目录布局都要能跑：仓库里是 plugin/pc/pc.py，本机装法是 pc/pc.py 与 plugin/ 平级
  const PC_PY_CANDIDATES = [join(PLUGIN_DIR, 'pc', 'pc.py'), join(PLUGIN_DIR, '..', 'pc.py')]
  const PC_PY = PC_PY_CANDIDATES.find((p) => existsSync(p))
  assert.ok(PC_PY, '找不到 pc.py（查过：' + PC_PY_CANDIDATES.join(' / ') + '）')
  const py = readFileSync(PC_PY, 'utf8')

  // 1) pc.py 还在打这一行（格式串里是 {cx},{cy}）
  const printRe = /find_text: \{needle!r\} at \(\{(\w+)\},\{(\w+)\}\)/
  assert.ok(printRe.test(py),
    'pc.py 不再打印 "find_text: ... at ({cx},{cy})" —— 插件的坐标解析会永远失败（表现为「找不到」，但屏幕上明明有）')

  // 2) 插件还在用同一个正则
  const REGEX_SRC = '/at \\((\\d+),(\\d+)\\)/'
  assert.ok(SRC.includes(REGEX_SRC),
    '插件的 /at \\((\\d+),(\\d+)\\)/ 变了；如果是有意改的，这里也要一起改')

  // 3) 拿一行「真样子」的输出喂给那个正则，确认能取出坐标
  const sample = "  find_text: '许海宁' at (412,166)"
  const m = new RegExp('at \\((\\d+),(\\d+)\\)').exec(sample)
  assert.ok(m, '样例输出没被解析出来')
  assert.deepEqual([Number(m[1]), Number(m[2])], [412, 166], '解析出的坐标不对')
})

// ---------------------------------------------------------------------------
// 活体证据：真的 DSH 收下了吗
// ---------------------------------------------------------------------------

test('活着的 DSH 确实注册了全部工具（读插件自己写的 marker）', (t) => {
  // 这条是「活体证据」：只有在真正装过、跑过这个插件的机器上才有意义。
  // 在别人的机器 / 干净的 CI 里没有 marker 是正常的，跳过而不是判死。
  if (!existsSync(MARKER)) {
    t.skip('这台机器上没有 plugin-loaded.json（没装过或没跑过这个插件），跳过活体检查')
    return
  }
  const m = JSON.parse(readFileSync(MARKER, 'utf8'))
  assert.equal(m.stage, 'tools-registered', 'marker 停在 ' + m.stage + '：没走到注册完成那一步，看 failures')
  assert.deepEqual(m.failures, [], '注册时有工具失败：' + JSON.stringify(m.failures))
  assert.equal(m.count, EXPECTED_NAMES.length, `DSH 收到的工具数 ${m.count} ≠ 契约 ${EXPECTED_NAMES.length}`)

  if (Array.isArray(m.names)) {
    assert.deepEqual([...m.names].sort(), [...EXPECTED_NAMES].sort(), 'DSH 收到的工具名与契约不一致')
  } else {
    // 老 marker 只记数量；提示一句，不算失败
    console.error('提示：marker 还没记 names（重启一次 DSH 后就会有），这次只校验了数量')
  }

  const srcTime = statSync(INDEX_JS).mtimeMs
  const markTime = new Date(m.at).getTime()
  if (markTime < srcTime) {
    console.error('提示：index.js 比 marker 新 —— 当前 DSH 跑的是旧代码，重启后这条才有意义')
  }
})
