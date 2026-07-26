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
          path: 'analysis',
          name: 'analysis',
          component: () => import('@/views/AnalysisView.vue'),
        },
      ],
    },
  ],
})

export default router
