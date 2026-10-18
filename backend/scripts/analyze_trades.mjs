// Replay trades_backup.csv with the same weighted-average cost basis as
// backend/app/services/ledger.py and emit the metrics the strategy page needs.
// Usage: node analyze_trades.mjs [csvPath]
import fs from 'node:fs';

const path = process.argv[2] || 'D:/LWH/web/project/stock-ledger/backend/trades_backup.csv';
const raw = fs.readFileSync(path, 'utf8').replace(/^\uFEFF/, '');
const lines = raw.split(/\r?\n/).filter((l) => l.trim());

function parseCsvLine(line) {
  const out = [];
  let cur = '';
  let q = false;
  for (const ch of line) {
    if (ch === '"') { q = !q; continue; }
    if (ch === ',' && !q) { out.push(cur); cur = ''; continue; }
    cur += ch;
  }
  out.push(cur);
  return out;
}

const cleanSymbol = (s) =>
  (s || '').trim().replace(/^(NYSE|NASDAQ|AMEX):/i, '').toUpperCase();

function parseTime(s) {
  // "2026/05/04 23:30"
  const [date, time] = s.trim().split(/\s+/);
  const [y, m, d] = date.split('/').map(Number);
  const [hh, mm] = (time || '00:00').split(':').map(Number);
  return new Date(Date.UTC(y, m - 1, d, hh, mm));
}

const rows = [];
lines.slice(1).forEach((line, i) => {
  const r = parseCsvLine(line);
  const side = (r[1] || '').trim();
  if (!side) return;
  rows.push({
    id: i + 2,
    symbol: cleanSymbol(r[0]),
    side,
    qty: Number(r[2]),
    price: Number(r[3]),
    comm: Number(r[4] || 0),
    ts: parseTime(r[5]),
    note: (r[6] || '').trim(),
  });
});
rows.sort((a, b) => a.ts - b.ts || a.id - b.id);

const positions = new Map(); // symbol -> {qty, avg, cost}
const openCycle = new Map(); // symbol -> cycle
const cycles = [];
let deposits = 0, buyNotional = 0, sellNotional = 0, commissions = 0, depositTs = null;
const daySides = new Map(); // "SYM|YYYY-MM-DD" -> Set
const hourBuckets = new Map(); // beijing hour -> count of filled intraday orders

for (const t of rows) {
  commissions += t.comm;
  if (t.side === 'Deposit') {
    deposits += t.price || t.qty;
    depositTs = t.ts;
    continue;
  }
  if (!t.symbol) continue;
  const dayKey = `${t.symbol}|${t.ts.toISOString().slice(0, 10)}`;
  if (t.side === 'Buy' || t.side === 'Sell') {
    if (!daySides.has(dayKey)) daySides.set(dayKey, new Set());
    daySides.get(dayKey).add(t.side[0]);
  }

  let p = positions.get(t.symbol);
  if (!p) { p = { qty: 0, avg: 0, cost: 0 }; positions.set(t.symbol, p); }

  const notional = t.qty * t.price;

  if (t.side === 'Buy') {
    if (p.qty <= 1e-9) {
      openCycle.set(t.symbol, {
        symbol: t.symbol, started: t.ts, ended: t.ts,
        buys: 0, sells: 0, buyQty: 0, sellQty: 0,
        buyAmount: 0, sellAmount: 0, realized: 0, cost: 0,
        addonBelow: 0, peakQty: 0,
      });
    }
    const c = openCycle.get(t.symbol);
    if (p.qty > 1e-9 && t.price < p.avg) c.addonBelow += 1;
    buyNotional += notional;
    c.buys += 1; c.buyQty += t.qty; c.buyAmount += notional;
    p.cost += notional; p.qty += t.qty;
    p.avg = p.cost / p.qty;
    c.peakQty = Math.max(c.peakQty, p.qty);
  } else if (t.side === 'Sell') {
    let c = openCycle.get(t.symbol);
    if (!c) {
      c = { symbol: t.symbol, started: t.ts, ended: t.ts, buys: 0, sells: 0,
            buyQty: 0, sellQty: 0, buyAmount: 0, sellAmount: 0, realized: 0,
            cost: 0, addonBelow: 0, peakQty: 0 };
      openCycle.set(t.symbol, c);
    }
    const sellQty = p.qty > 0 ? Math.min(t.qty, p.qty) : t.qty;
    const costRemoved = p.avg * sellQty;
    let pnl = sellQty * t.price - costRemoved;
    if (p.qty > 0 && t.qty > p.qty) pnl += (t.qty - p.qty) * t.price;
    sellNotional += notional;
    c.sells += 1; c.sellQty += sellQty; c.sellAmount += notional;
    c.realized += pnl; c.cost += costRemoved; c.ended = t.ts;
    p.qty -= sellQty;
    p.cost = p.avg * p.qty;
    if (p.qty <= 1e-9) {
      p.qty = 0; p.cost = 0; p.avg = 0;
      cycles.push({ ...c, days: Math.max(0, Math.round((c.ended - c.started) / 86400000)) });
      openCycle.delete(t.symbol);
    }
  }
}

const closed = cycles.filter((c) => c.cost > 0);
const wins = closed.filter((c) => c.realized > 0);
const losses = closed.filter((c) => c.realized <= 0);
const realized = closed.reduce((s, c) => s + c.realized, 0);
const winSum = wins.reduce((s, c) => s + c.realized, 0);
const lossSum = losses.reduce((s, c) => s + c.realized, 0);
const avgWin = wins.length ? winSum / wins.length : 0;
const avgLoss = losses.length ? Math.abs(lossSum / losses.length) : 0;

const bySymbol = new Map();
for (const c of closed) {
  const b = bySymbol.get(c.symbol) || { symbol: c.symbol, realized: 0, cost: 0, cycles: 0, days: 0, addonBelow: 0, trades: 0 };
  b.realized += c.realized; b.cost += c.cost; b.cycles += 1;
  b.days += c.days; b.addonBelow += c.addonBelow;
  bySymbol.set(c.symbol, b);
}
for (const t of rows) {
  if (t.symbol && (t.side === 'Buy' || t.side === 'Sell')) {
    const b = bySymbol.get(t.symbol);
    if (b) b.trades += 1;
  }
}

const monthly = new Map();
for (const t of rows) {
  if (t.symbol && (t.side === 'Buy' || t.side === 'Sell')) {
    const m = t.ts.toISOString().slice(0, 7);
    const rec = monthly.get(m) || { month: m, trades: 0, realized: 0 };
    rec.trades += 1;
    monthly.set(m, rec);
  }
}
for (const c of closed) {
  const m = c.ended.toISOString().slice(0, 7);
  const rec = monthly.get(m) || { month: m, trades: 0, realized: 0 };
  rec.realized += c.realized;
  monthly.set(m, rec);
}

const flags = [...daySides.entries()]
  .filter(([, s]) => s.has('B') && s.has('S'))
  .map(([k]) => { const [symbol, date] = k.split('|'); return { symbol, date }; });

const openPositions = [...positions.entries()]
  .filter(([, p]) => p.qty > 1e-9)
  .map(([symbol, p]) => ({ symbol, qty: +p.qty.toFixed(4), avg: +p.avg.toFixed(4), cost: +p.cost.toFixed(2) }))
  .sort((a, b) => b.cost - a.cost);

const r2 = (n) => Math.round(n * 100) / 100;

const out = {
  meta: {
    rows: rows.length,
    first: rows[0].ts.toISOString().slice(0, 10),
    last: rows[rows.length - 1].ts.toISOString().slice(0, 10),
    symbols: new Set(rows.filter((t) => t.symbol).map((t) => t.symbol)).size,
  },
  capital: {
    deposits: r2(deposits),
    buyNotional: r2(buyNotional),
    sellNotional: r2(sellNotional),
    turnoverX: r2(buyNotional / deposits),
    commissions: r2(commissions),
    realized: r2(realized),
    realizedPct: r2((realized / deposits) * 100),
  },
  behavior: {
    closedCycles: closed.length,
    wins: wins.length,
    losses: losses.length,
    winRate: r2((wins.length / closed.length) * 100),
    avgWin: r2(avgWin),
    avgLoss: r2(avgLoss),
    payoff: r2(avgWin / avgLoss),
    profitFactor: r2(winSum / Math.abs(lossSum)),
    winSum: r2(winSum),
    lossSum: r2(lossSum),
    avgWinDays: r2(wins.reduce((s, c) => s + c.days, 0) / (wins.length || 1)),
    avgLossDays: r2(losses.reduce((s, c) => s + c.days, 0) / (losses.length || 1)),
    addonBelowTotal: closed.reduce((s, c) => s + c.addonBelow, 0),
    sameDayFlips: flags.length,
    sameDayFlipSymbols: [...new Set(flags.map((f) => f.symbol))].sort(),
  },
  symbols: [...bySymbol.values()]
    .map((b) => ({
      symbol: b.symbol,
      realized: r2(b.realized),
      cost: r2(b.cost),
      retPct: b.cost ? r2((b.realized / b.cost) * 100) : 0,
      cycles: b.cycles,
      trades: b.trades,
      avgDays: b.cycles ? r2(b.days / b.cycles) : 0,
      addonBelow: b.addonBelow,
    }))
    .sort((a, b) => a.realized - b.realized),
  monthly: [...monthly.values()].sort((a, b) => a.month.localeCompare(b.month))
    .map((m) => ({ ...m, realized: r2(m.realized) })),
  openPositions,
  openCycles: [...openCycle.values()].map((c) => ({
    symbol: c.symbol, realized: r2(c.realized), addonBelow: c.addonBelow,
    days: Math.round((new Date('2026-10-09') - c.started) / 86400000),
  })),
};

console.log(JSON.stringify(out, null, 2));
