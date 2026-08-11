import { createRouter, createWebHistory } from 'vue-router'
import PortfolioLayout from '@/views/PortfolioLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: PortfolioLayout,
      redirect: '/overview',
      children: [
        {
          path: 'overview',
          name: 'overview',
          component: () => import('@/views/OverviewView.vue'),
        },
        {
          path: 'stats',
          name: 'stats',
          component: () => import('@/views/StatsView.vue'),
        },
        {
          path: 'holdings',
          name: 'holdings',
          component: () => import('@/views/HoldingsView.vue'),
        },
        {
          path: 'trades',
          name: 'trades',
          component: () => import('@/views/TradesView.vue'),
        },
        {
          path: 'prices',
          name: 'prices',
          component: () => import('@/views/HoldingsView.vue'),
        },
        {
          path: 'analysis',
          name: 'analysis',
          component: () => import('@/views/AnalysisView.vue'),
        },
        {
          path: 'vibe',
          name: 'vibe',
          component: () => import('@/views/VibeTradingView.vue'),
        },
      ],
    },
  ],
})

export default router
