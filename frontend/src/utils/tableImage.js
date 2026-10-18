// 把整张表格（全部行 + 汇总行，不受分页影响）画进 canvas 再下载成 PNG。
//
// 为什么不引 html2canvas：项目里没有这个依赖，为了截图装一个库不划算；
// 表格结构是固定的三行式（表头 / 数据行 / 汇总行），Canvas 2D 手绘足够，
// 顺带能把 pnl-up / pnl-down 的红绿配色和"代码 + 名称"两段式单元格还原出来。
//
// 单元格结构：每行是一个与 columns 等长的数组，元素可以是
//   { text, secondary?, tone? }
//   - text      主文本（必填）
//   - secondary 紧跟其后的灰色小字，用于股票名称、行情时间这类附属信息
//   - tone      直接传 pnlClass() 的返回值（'pnl-up' / 'pnl-down' / ''）
// 也可以直接传字符串，等价于只填 text 的简写。

const FONT_STACK = '"Trebuchet MS", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'

const COLORS = {
  text: '#131722',
  muted: '#787b86',
  border: '#e0e3eb',
  hairline: '#eef0f5',
  headerBg: '#f0f3fa',
  summaryBg: '#f0f3fa',
  up: '#089981',
  down: '#f23645',
}

const TITLE_FONT = `600 17px ${FONT_STACK}`
const SUBTITLE_FONT = `12px ${FONT_STACK}`
const HEAD_FONT = `600 13px ${FONT_STACK}`
const CELL_FONT = `13px ${FONT_STACK}`
const SECONDARY_FONT = `12px ${FONT_STACK}`
const SUMMARY_FONT = `600 13px ${FONT_STACK}`

const PAGE_PAD = 24
const CELL_PAD = 12
const ROW_H = 34
const HEAD_H = 38
const TITLE_H = 28
const MIN_COL_W = 64
const SECONDARY_GAP = 6
// 浏览器对 canvas 单边尺寸有上限（Chrome 是 32767），超了就整张图变白
const MAX_CANVAS_SIDE = 32000

function normalizeCell(cell) {
  if (cell == null) return { text: '' }
  if (typeof cell === 'object') return cell
  return { text: String(cell) }
}

function toneColor(tone) {
  if (tone === 'pnl-up') return COLORS.up
  if (tone === 'pnl-down') return COLORS.down
  return null
}

function textWidth(ctx, text, font) {
  ctx.font = font
  return ctx.measureText(text || '').width
}

function cellWidth(ctx, cell) {
  let w = textWidth(ctx, cell.text, CELL_FONT)
  if (cell.secondary) w += SECONDARY_GAP + textWidth(ctx, cell.secondary, SECONDARY_FONT)
  return w
}

// 单元格按列对齐：有 secondary 时把"主文本 + 小字"当成一个整体靠边，
// 否则右对齐的数字会和小字分家。
function drawCell(ctx, cell, left, colWidth, centerY, align, font, color) {
  const right = left + colWidth
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'

  const mainW = textWidth(ctx, cell.text, font)
  const secondaryW = cell.secondary
    ? SECONDARY_GAP + textWidth(ctx, cell.secondary, SECONDARY_FONT)
    : 0
  const x = align === 'right' ? right - CELL_PAD - (mainW + secondaryW) : left + CELL_PAD

  ctx.font = font
  ctx.fillStyle = toneColor(cell.tone) || color
  ctx.fillText(cell.text, x, centerY)

  if (cell.secondary) {
    ctx.font = SECONDARY_FONT
    ctx.fillStyle = COLORS.muted
    ctx.fillText(cell.secondary, x + mainW + SECONDARY_GAP, centerY)
  }
}

function sanitizeFilename(name) {
  const cleaned = String(name || 'table').replace(/[\\/:*?"<>|]+/g, '-').trim()
  return cleaned || 'table'
}

/**
 * 画出整张表并返回 canvas（可单独用于预览/测试）。
 * columns: [{ title, align }]，rows: 单元格二维数组，summary: 一行单元格或 null。
 */
export function buildTableImage({ title = '', subtitle = '', columns = [], rows = [], summary = null }) {
  const body = rows.map((row) => columns.map((_, i) => normalizeCell(row[i])))
  const summaryCells = summary ? columns.map((_, i) => normalizeCell(summary[i])) : null

  const measure = document.createElement('canvas').getContext('2d')

  // 列宽按内容撑开：表头、每条数据行、汇总行取最大，数字不会被挤在一起
  const widths = columns.map((col, i) => {
    let w = textWidth(measure, col.title, HEAD_FONT)
    body.forEach((cells) => {
      w = Math.max(w, cellWidth(measure, cells[i]))
    })
    if (summaryCells) w = Math.max(w, cellWidth(measure, summaryCells[i]))
    return Math.max(MIN_COL_W, Math.ceil(w) + CELL_PAD * 2)
  })

  const tableWidth = widths.reduce((a, b) => a + b, 0)
  const dataRowCount = Math.max(body.length, 1)
  const width = tableWidth + PAGE_PAD * 2
  const tableTop = PAGE_PAD + TITLE_H
  const tableHeight = HEAD_H + dataRowCount * ROW_H + (summaryCells ? ROW_H : 0)
  const height = tableTop + tableHeight + PAGE_PAD

  const canvas = document.createElement('canvas')
  const scale = Math.min(2, MAX_CANVAS_SIDE / width, MAX_CANVAS_SIDE / height)
  canvas.width = Math.round(width * scale)
  canvas.height = Math.round(height * scale)
  const ctx = canvas.getContext('2d')
  ctx.scale(scale, scale)

  const colLefts = []
  let cursor = PAGE_PAD
  widths.forEach((w) => {
    colLefts.push(cursor)
    cursor += w
  })
  const summaryTop = tableTop + HEAD_H + dataRowCount * ROW_H

  // 1. 底色
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, width, height)
  ctx.fillStyle = COLORS.headerBg
  ctx.fillRect(PAGE_PAD, tableTop, tableWidth, HEAD_H)
  if (summaryCells) {
    ctx.fillStyle = COLORS.summaryBg
    ctx.fillRect(PAGE_PAD, summaryTop, tableWidth, ROW_H)
  }

  // 2. 表格线（先画线再写字，避免压住文字）
  ctx.lineWidth = 1
  const hLine = (y, color) => {
    ctx.strokeStyle = color
    ctx.beginPath()
    ctx.moveTo(PAGE_PAD, Math.round(y) + 0.5)
    ctx.lineTo(PAGE_PAD + tableWidth, Math.round(y) + 0.5)
    ctx.stroke()
  }
  for (let i = 1; i < body.length; i += 1) {
    hLine(tableTop + HEAD_H + i * ROW_H, COLORS.hairline)
  }
  hLine(tableTop + HEAD_H, COLORS.border)
  if (summaryCells) hLine(summaryTop, COLORS.border)
  ctx.strokeStyle = COLORS.border
  ctx.strokeRect(PAGE_PAD + 0.5, tableTop + 0.5, tableWidth - 1, tableHeight - 1)

  // 3. 文字
  const titleBaseline = PAGE_PAD + TITLE_H / 2
  ctx.textBaseline = 'middle'
  ctx.textAlign = 'left'
  ctx.font = TITLE_FONT
  ctx.fillStyle = COLORS.text
  ctx.fillText(title, PAGE_PAD, titleBaseline)
  if (subtitle) {
    ctx.font = SUBTITLE_FONT
    ctx.fillStyle = COLORS.muted
    ctx.textAlign = 'right'
    ctx.fillText(subtitle, PAGE_PAD + tableWidth, titleBaseline)
    ctx.textAlign = 'left'
  }

  columns.forEach((col, i) => {
    drawCell(
      ctx,
      { text: col.title || '' },
      colLefts[i],
      widths[i],
      tableTop + HEAD_H / 2,
      col.align,
      HEAD_FONT,
      COLORS.text,
    )
  })

  if (!body.length) {
    ctx.font = CELL_FONT
    ctx.fillStyle = COLORS.muted
    ctx.textAlign = 'left'
    ctx.fillText('暂无数据', PAGE_PAD + CELL_PAD, tableTop + HEAD_H + ROW_H / 2)
  }
  body.forEach((cells, r) => {
    const centerY = tableTop + HEAD_H + r * ROW_H + ROW_H / 2
    columns.forEach((col, i) => {
      drawCell(ctx, cells[i], colLefts[i], widths[i], centerY, col.align, CELL_FONT, COLORS.text)
    })
  })
  if (summaryCells) {
    const centerY = summaryTop + ROW_H / 2
    columns.forEach((col, i) => {
      drawCell(ctx, summaryCells[i], colLefts[i], widths[i], centerY, col.align, SUMMARY_FONT, COLORS.text)
    })
  }

  return canvas
}

/**
 * 生成图片并触发下载。filename 不用带扩展名。
 * 返回 Promise<boolean>，false 表示浏览器没能出图（toBlob 失败）。
 */
export function downloadTableImage(options) {
  const canvas = buildTableImage(options)
  const filename = `${sanitizeFilename(options.filename || options.title)}.png`

  return new Promise((resolve) => {
    if (typeof canvas.toBlob !== 'function') {
      resolve(false)
      return
    }
    canvas.toBlob((blob) => {
      if (!blob) {
        resolve(false)
        return
      }
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      link.rel = 'noopener'
      document.body.appendChild(link)
      link.click()
      link.remove()
      // 立刻 revoke 在部分浏览器会打断下载，留一点时间
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      resolve(true)
    }, 'image/png')
  })
}
