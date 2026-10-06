#!/usr/bin/env node
/**
 * 为已打包的产物生成 / 校验 electron-updater 差分更新所需的 `.blockmap`。
 *
 * 构建走 `--dir` + `ditto`，electron-builder 自己没有产出 zip，也就不会附带
 * blockmap；而 electron-updater 的 MacUpdater 只有拿到 `<artifact>.blockmap`
 * 才会走 `differentialDownloadInstaller`，否则整包重下。这里直接调用
 * app-builder-lib 内部的同一个函数，保证与 electron-builder 自己产出的格式一致，
 * 不重新实现分块算法。
 *
 * 用法：
 *   node .ci/make-blockmap.mjs <artifact>          生成旁挂的 .blockmap（macOS zip）
 *   node .ci/make-blockmap.mjs --verify <artifact>  只校验已有的 <artifact>.blockmap
 *   node .ci/make-blockmap.mjs --embed <artifact>   把 blockmap 内嵌进产物末尾（Windows NSIS）
 *
 * `--embed` 会**修改文件**：NSIS 的差分下载器
 * `FileWithEmbeddedBlockMapDifferentialDownloader` 从产物末尾读
 * `blockMapSize + 4` 字节（末 4 字节是 BE 长度头），所以必须先内嵌、
 * 再算 sha512 与 size，顺序反了清单里的校验和就对不上。
 */

import { createRequire } from 'node:module'
import { closeSync, openSync, readFileSync, readSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { gunzipSync, inflateRawSync } from 'node:zlib'
import process from 'node:process'

// app-builder-lib 只存在于 apps/desktop 的依赖树里（pnpm 严格布局，根目录解析不到）。
const desktopRequire = createRequire(new URL('../apps/desktop/package.json', import.meta.url))
const blockmapEntry = desktopRequire.resolve('app-builder-lib/out/targets/blockmap/blockmap.js')
// 分块校验和是 blake2b(dkLen=18)，与 blockmap.js 里 emitChunk 用的完全一致。
const { blake2b } = createRequire(blockmapEntry)('@noble/hashes/blake2.js')

/**
 * 校验一个 blockmap 是否确实是某个产物的、且内容完整。
 * @param {string} artifact 被覆盖的产物路径。
 * @param {string} blockmapFile blockmap 路径。
 * @returns {{ entries: number, chunks: number, covered: number, bytes: number }} 统计信息。
 */
function verify(artifact, blockmapFile) {
  const raw = readFileSync(blockmapFile)
  const parsed = JSON.parse(gunzipSync(raw).toString('utf8'))
  if (!Array.isArray(parsed.files) || parsed.files.length === 0) {
    throw new Error(`blockmap ${blockmapFile} 里没有 files 列表`)
  }
  for (const entry of parsed.files) {
    if (entry.checksums.length !== entry.sizes.length || entry.checksums.length === 0) {
      throw new Error(`blockmap 条目 ${entry.name} 的 checksums/sizes 不匹配`)
    }
  }
  const covered = parsed.files.reduce((total, entry) => total + entry.sizes.reduce((a, b) => a + b, 0), 0)
  const bytes = statSync(artifact).size
  if (covered !== bytes) {
    throw new Error(`blockmap 覆盖 ${covered} 字节，产物是 ${bytes} 字节 —— 不是同一个文件`)
  }
  // 逐块核对：blockmap 自身不记录整文件的 sha512，只有 sizes 对上但内容换了
  // （同尺寸的不同产物）这种错配，只有逐块比对才拦得住。
  // 只读前若干块覆盖到的字节，不把 393MB 产物整个读进内存。
  const first = parsed.files[0]
  const span = first.sizes.slice(0, 8).reduce((a, b) => a + b, 0)
  const head = Buffer.allocUnsafe(span)
  const handle = openSync(artifact, 'r')
  try {
    readSync(handle, head, 0, span, 0)
  }
  finally {
    closeSync(handle)
  }
  let cursor = 0
  for (let index = 0; index < Math.min(first.checksums.length, 8); index += 1) {
    const slice = head.subarray(cursor, cursor + first.sizes[index])
    const digest = Buffer.from(blake2b(slice, { dkLen: 18 })).toString('base64')
    if (digest !== first.checksums[index]) {
      throw new Error(`blockmap 第 ${index} 块校验和对不上 —— 内容不匹配`)
    }
    cursor += first.sizes[index]
  }
  return {
    entries: parsed.files.length,
    chunks: parsed.files.reduce((total, entry) => total + entry.checksums.length, 0),
    covered,
    bytes,
    size: raw.length,
  }
}

/**
 * 校验一个内嵌在产物末尾的 blockmap：读末 blockMapSize+4 字节，
 * 末 4 字节是压缩长度的 BE 头，前面是 deflateRaw 压缩的 JSON。
 * @param {string} artifact 被内嵌的产物路径。
 * @param {number} blockMapSize 压缩块的长度。
 * @returns {{ chunks: number, covered: number, bytes: number }} 统计信息。
 */
function verifyEmbedded(artifact, blockMapSize) {
  if (!Number.isSafeInteger(blockMapSize) || blockMapSize < 1) {
    throw new Error(`内嵌 blockMapSize 非法：${blockMapSize}`)
  }
  const bytes = statSync(artifact).size
  const tail = Buffer.allocUnsafe(blockMapSize + 4)
  const handle = openSync(artifact, 'r')
  try {
    readSync(handle, tail, 0, tail.length, bytes - tail.length)
  }
  finally {
    closeSync(handle)
  }
  const header = tail.readUInt32BE(blockMapSize)
  if (header !== blockMapSize) {
    throw new Error(`内嵌 blockmap 长度头是 ${header}，与 blockMapSize ${blockMapSize} 不符`)
  }
  const parsed = JSON.parse(inflateRawSync(tail.subarray(0, blockMapSize)).toString('utf8'))
  if (!Array.isArray(parsed.files) || parsed.files.length === 0) {
    throw new Error('内嵌 blockmap 里没有 files 列表')
  }
  const covered = parsed.files.reduce((total, entry) => total + entry.sizes.reduce((a, b) => a + b, 0), 0)
  if (covered !== bytes - blockMapSize - 4) {
    throw new Error(`内嵌 blockmap 覆盖 ${covered} 字节，产物去掉 blockmap 后是 ${bytes - blockMapSize - 4} 字节`)
  }
  return {
    chunks: parsed.files.reduce((total, entry) => total + entry.checksums.length, 0),
    covered,
    bytes,
  }
}

const mode = process.argv[2]
const target = process.argv[mode?.startsWith('--') === true ? 3 : 2]
if (target === undefined) {
  throw new Error('usage: make-blockmap.mjs [--verify|--embed] <artifact>')
}

if (mode === '--embed') {
  const { buildBlockMap } = desktopRequire('app-builder-lib/out/targets/blockmap/blockmap.js')
  // 不传 outFile 即内嵌模式：写 deflateRaw 压缩块 + 4 字节 BE 长度头。
  const result = await buildBlockMap(target, 'deflate')
  if (typeof result.blockMapSize !== 'number') {
    throw new Error('buildBlockMap 没有返回 blockMapSize，内嵌可能没生效')
  }
  const verified = verifyEmbedded(target, result.blockMapSize)
  process.stdout.write(
    `内嵌 blockmap: ${result.blockMapSize} 字节  ${verified.chunks} 个块  `
    + `覆盖 ${verified.covered} + ${result.blockMapSize} + 4 = ${verified.bytes} 字节  回读校验通过\n`,
  )
  // workflow 用这一行把 blockMapSize 填进 electron-updater 清单
  process.stdout.write(`blockMapSize=${result.blockMapSize}\n`)
  process.exit(0)
}

// 非 --embed：旁挂一个 .blockmap 文件（macOS zip 的路径）。
const outFile = `${target}.blockmap`
if (mode !== '--verify') {
  const { buildBlockMap } = desktopRequire('app-builder-lib/out/targets/blockmap/blockmap.js')
  await buildBlockMap(target, 'gzip', outFile)
}

const result = verify(target, outFile)
process.stdout.write(
  `blockmap: ${outFile}  ${result.size} 字节  ${result.entries} 个条目 / ${result.chunks} 个块  `
  + `覆盖 ${result.covered}/${result.bytes} 字节  逐块校验通过\n`,
)