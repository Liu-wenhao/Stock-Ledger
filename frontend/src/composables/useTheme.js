import { ref } from 'vue'

const isDark = ref(false)
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
  applyTheme(darkByClock())
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
  return { isDark }
}
