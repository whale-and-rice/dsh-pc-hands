/**
 * 极简 asar 读取器：只做我们要的两件事——列目录、取文件。
 * 格式：8 字节 pickle 头 + JSON 目录树 + 数据区（偏移以 8+headerPickleSize 为基）。
 */
import { readFileSync } from 'node:fs'

export function openAsar(path) {
  const buf = readFileSync(path)

  // 第一层 pickle：UInt32(size=4) + UInt32(headerPickleSize)
  if (buf.readUInt32LE(0) !== 4) throw new Error('不是 asar：头 4 字节不是 4')
  const headerPickleSize = buf.readUInt32LE(4)
  const headerBuf = buf.subarray(8, 8 + headerPickleSize)

  // 第二层 pickle：UInt32(strLen+4) + UInt32(strLen) + JSON
  const jsonLen = headerBuf.readUInt32LE(4)
  const json = headerBuf.subarray(8, 8 + jsonLen).toString('utf8')
  const tree = JSON.parse(json)

  const dataBase = 8 + headerPickleSize

  function walk(node, prefix, out) {
    for (const [name, entry] of Object.entries(node.files || {})) {
      const rel = prefix ? prefix + '/' + name : name
      if (entry.files) walk(entry, rel, out)
      // 注意：asar 里 size/offset 是字符串，必须转数字，否则加法变拼接、读出来是 0 字节
      else out.push({ path: rel, size: Number(entry.size), offset: Number(entry.offset), unpacked: !!entry.unpacked })
    }
    return out
  }

  return {
    tree,
    files: walk(tree, '', []),
    read(rel) {
      const f = this.files.find((x) => x.path === rel)
      if (!f) throw new Error('asar 里没有：' + rel)
      if (f.unpacked) throw new Error('这是 unpacked 文件，去 app.asar.unpacked 取：' + rel)
      return buf.subarray(dataBase + f.offset, dataBase + f.offset + f.size)
    },
  }
}

// 命令行：node asar.mjs list <asar> [过滤词]  /  node asar.mjs cat <asar> <内部路径>
if (process.argv[1] && process.argv[1].endsWith('asar.mjs')) {
  const [, , cmd, asarPath, arg] = process.argv
  const a = openAsar(asarPath)
  if (cmd === 'list') {
    const hits = a.files.filter((f) => !arg || f.path.includes(arg))
    console.log('总文件数=' + a.files.length + '  命中=' + hits.length)
    for (const f of hits.slice(0, 200)) console.log(f.size + '\t' + f.path)
  } else if (cmd === 'cat') {
    process.stdout.write(a.read(arg))
  } else {
    console.error('用法: node asar.mjs list|cat <asar> [过滤词|内部路径]')
    process.exit(2)
  }
}
