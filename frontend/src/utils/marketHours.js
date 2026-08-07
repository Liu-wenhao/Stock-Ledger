function dateKeyUtc(date) {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(
    date.getUTCDate(),
  ).padStart(2, '0')}`
}

function observedFixedHoliday(year, month, day) {
  const date = new Date(Date.UTC(year, month - 1, day))
  const weekday = date.getUTCDay()
  if (weekday === 6) date.setUTCDate(date.getUTCDate() - 1)
  if (weekday === 0) date.setUTCDate(date.getUTCDate() + 1)
  return dateKeyUtc(date)
}

function nthWeekday(year, month, weekday, nth) {
  const date = new Date(Date.UTC(year, month - 1, 1))
  const offset = (weekday - date.getUTCDay() + 7) % 7
  date.setUTCDate(1 + offset + (nth - 1) * 7)
  return dateKeyUtc(date)
}

function lastWeekday(year, month, weekday) {
  const date = new Date(Date.UTC(year, month, 0))
  date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() - weekday + 7) % 7))
  return dateKeyUtc(date)
}

function easterSunday(year) {
  const a = year % 19
  const b = Math.floor(year / 100)
  const c = year % 100
  const d = Math.floor(b / 4)
  const e = b % 4
  const f = Math.floor((b + 8) / 25)
  const g = Math.floor((b - f + 1) / 3)
  const h = (19 * a + b - d - g + 15) % 30
  const i = Math.floor(c / 4)
  const k = c % 4
  const l = (32 + 2 * e + 2 * i - h - k) % 7
  const m = Math.floor((a + 11 * h + 22 * l) / 451)
  const month = Math.floor((h + l - 7 * m + 114) / 31)
  const day = ((h + l - 7 * m + 114) % 31) + 1
  return new Date(Date.UTC(year, month - 1, day))
}

function usMarketHolidayKeys(year) {
  const goodFriday = easterSunday(year)
  goodFriday.setUTCDate(goodFriday.getUTCDate() - 2)
  return new Set([
    observedFixedHoliday(year, 1, 1),
    nthWeekday(year, 1, 1, 3),
    nthWeekday(year, 2, 1, 3),
    dateKeyUtc(goodFriday),
    lastWeekday(year, 5, 1),
    observedFixedHoliday(year, 6, 19),
    observedFixedHoliday(year, 7, 4),
    nthWeekday(year, 9, 1, 1),
    nthWeekday(year, 11, 4, 4),
    observedFixedHoliday(year, 12, 25),
  ])
}

/** Regular NYSE/Nasdaq session: 09:30-16:00 America/New_York. */
export function isUsMarketOpen(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    weekday: 'short',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(now)
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  if (values.weekday === 'Sat' || values.weekday === 'Sun') return false
  if (usMarketHolidayKeys(Number(values.year)).has(`${values.year}-${values.month}-${values.day}`)) {
    return false
  }
  const minutes = Number(values.hour) * 60 + Number(values.minute)
  return minutes >= 9 * 60 + 30 && minutes < 16 * 60
}
