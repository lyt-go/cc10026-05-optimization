import { createRouter, createWebHistory } from 'vue-router'

import LateAdjustmentView from '@/views/LateAdjustmentView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径需排在动态模块路由之前，否则会被 /:moduleKey 抢先匹配。
    { path: '/usage_billing/late-adjustments', name: 'late-adjustments', component: LateAdjustmentView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
