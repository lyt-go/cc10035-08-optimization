import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import EstimateDetailView from '@/views/ts-store/EstimateDetailView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    {
      path: '/ts_store/estimates/:estimateRef',
      name: 'ts-store-estimate-detail',
      component: EstimateDetailView,
      props: true,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
