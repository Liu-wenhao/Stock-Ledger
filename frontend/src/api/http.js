import axios from 'axios'
import { message } from 'ant-design-vue'

const http = axios.create({
  baseURL: '/',
  timeout: 60000,
})

http.interceptors.request.use((config) => {
  const market = localStorage.getItem('ledger-market') || 'us'
  config.params = { ...(config.params || {}), market }
  return config
})

http.interceptors.response.use(
  (res) => res,
  (err) => {
    const msg = err?.response?.data?.detail || err.message || '请求失败'
    message.error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    return Promise.reject(err)
  },
)

export default http
