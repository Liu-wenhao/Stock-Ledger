import { onMounted, onUnmounted, ref } from 'vue'

/**
 * Table body scroll height based on viewport.
 * @param {number} offset pixels reserved for header/kpi/tabs/toolbar/pagination
 * @param {number} min minimum body height
 */
export function useTableScrollY(offset = 420, min = 420) {
  const y = ref(min)

  function update() {
    y.value = Math.max(min, window.innerHeight - offset)
  }

  onMounted(() => {
    update()
    window.addEventListener('resize', update)
  })

  onUnmounted(() => {
    window.removeEventListener('resize', update)
  })

  return y
}
