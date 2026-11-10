/** 从 asar 里按前缀抽出整个目录到 dest。 */
import { openAsar } from './asar.mjs'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

const [, , asarPath, prefix, dest] = process.argv
if (!asarPath || !prefix || !dest) {
  console.error('用法: node asar-extract.mjs <asar> <内部前缀> <目标目录>')
  process.exit(2)
}

const a = openAsar(asarPath)
const hits = a.files.filter((f) => f.path === prefix || f.path.startsWith(prefix + '/'))
if (!hits.length) {
  console.error('没匹配到：' + prefix)
  process.exit(3)
}

let n = 0
for (const f of hits) {
  const rel = f.path.slice(prefix.length).replace(/^\/+/, '')
  const out = join(dest, rel)
  mkdirSync(dirname(out), { recursive: true })
  writeFileSync(out, a.read(f.path))
  n += 1
}
console.log('抽出 ' + n + ' 个文件 → ' + dest)
