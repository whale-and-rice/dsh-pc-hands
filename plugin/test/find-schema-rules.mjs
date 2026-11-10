// 在新版 app.asar 里找「工具参数 schema 规则」到底搬到哪个文件了
import { openAsar } from './asar.mjs'

const ASAR = 'D:\\dsh2\\resources\\app.asar'
const NEEDLES = [
  'additionalProperties must be explicitly true or false',
  'type arrays are not supported',
  'CONSTRAINT_KEYWORDS',
  'SCHEMA_TYPES',
  'unsupported JSON schema',
]

const a = openAsar(ASAR)
const hits = Object.create(null)
for (const n of NEEDLES) hits[n] = []

for (const f of a.files) {
  const path = f.path
  if (!/\.(js|cjs|mjs)$/.test(path)) continue
  let buf
  try { buf = a.read(path) } catch { continue }
  if (!buf || buf.length > 40 * 1024 * 1024) continue
  let s
  try { s = buf.toString('utf8') } catch { continue }
  for (const n of NEEDLES) if (s.includes(n)) hits[n].push(path)
}

for (const [n, list] of Object.entries(hits)) {
  console.log(`\n=== ${n} ===`)
  if (!list.length) console.log('  （没有命中）')
  else for (const p of list.slice(0, 8)) console.log('  ' + p)
}
