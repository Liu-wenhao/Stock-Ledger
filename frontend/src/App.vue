<script setup>
import { computed } from 'vue'
import { theme } from 'ant-design-vue'
import { useTheme } from '@/composables/useTheme'

const { isDark } = useTheme()
const themeConfig = computed(() => ({
  algorithm: isDark.value ? theme.darkAlgorithm : theme.defaultAlgorithm,
  token: {
    colorPrimary: '#14977d',
    colorSuccess: '#089981',
    colorError: '#f23645',
    colorBgBase: isDark.value ? '#131722' : '#ffffff',
    colorTextBase: isDark.value ? '#d1d4dc' : '#131722',
    colorBorder: isDark.value ? '#363a45' : '#e0e3eb',
    borderRadius: 8,
    fontFamily: '"Trebuchet MS", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
  },
}))
</script>

<template>
  <a-config-provider :theme="themeConfig">
    <router-view />
  </a-config-provider>
</template>

<style>
html,
body,
#app {
  width: 100%;
  max-width: none;
  max-height: none;
}
</style>
