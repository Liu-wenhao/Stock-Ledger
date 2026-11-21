import { ref } from 'vue'

const isDark = ref(false)
const savedTheme = typeof localStorage === 'undefined' ? null : localStorage.getItem('ledger-theme')
const themeChoice = ref(['light', 'dark'].includes(savedTheme) ? savedTheme : 'auto')
let timer

export function darkByClock(date = new Date()) {
  const hour = date.getHours()
  return hour >= 20 || hour < 8
}

function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  isDark.value = dark
}

function syncTheme() {
  applyTheme(themeChoice.value === 'auto' ? darkByClock() : themeChoice.value === 'dark')
}

function toggleTheme() {
  themeChoice.value = isDark.value ? 'light' : 'dark'
  localStorage.setItem('ledger-theme', themeChoice.value)
  syncTheme()
}

function startThemeClock() {
  syncTheme()
  if (timer) return
  timer = window.setInterval(syncTheme, 60_000)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') syncTheme()
  })
}

if (typeof document !== 'undefined') startThemeClock()

export function useTheme() {
  return { isDark, toggleTheme }
}
