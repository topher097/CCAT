/** Route map from refactor §7 plus auth/dirty guards (§7.1.1, §7.2). */

import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
} from 'vue-router'

import { useSessionStore } from '../stores/session'
import { formDirty, setDirty } from '../composables/useDirtyGuard'
import HomeView from '../views/HomeView.vue'
import LoginView from '../views/LoginView.vue'
import AuditLandingView from '../views/AuditLandingView.vue'
import AuditListView from '../views/AuditListView.vue'
import FamilyDetailView from '../views/FamilyDetailView.vue'
import SponsorDetailView from '../views/SponsorDetailView.vue'
import ChildDetailView from '../views/ChildDetailView.vue'
import StubView from '../views/StubView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** Reachable without a session. */
    public?: boolean
    title?: string
  }
}

export const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: HomeView, meta: { title: 'Overview' } },
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { public: true, title: 'Login' },
  },
  {
    path: '/acquire',
    name: 'acquire',
    component: StubView,
    meta: { title: 'Acquire' },
  },
  {
    path: '/screen',
    name: 'screen',
    component: StubView,
    meta: { title: 'Screen' },
  },
  {
    path: '/audit',
    name: 'audit',
    component: AuditLandingView,
    meta: { title: 'Audit' },
  },
  {
    path: '/audit/sponsors',
    name: 'sponsors',
    component: AuditListView,
    props: { segment: 'sponsors' },
    meta: { title: 'Sponsors' },
  },
  {
    path: '/audit/sponsors/:id',
    name: 'sponsor',
    component: SponsorDetailView,
    meta: { title: 'Sponsor' },
  },
  {
    path: '/audit/families',
    name: 'families',
    component: AuditListView,
    props: { segment: 'families' },
    meta: { title: 'Families' },
  },
  {
    path: '/audit/families/:id',
    name: 'family',
    component: FamilyDetailView,
    meta: { title: 'Family' },
  },
  {
    path: '/audit/families/:familyId/children/:childId',
    name: 'family-child',
    component: ChildDetailView,
    props: true,
    meta: { title: 'Child' },
  },
  {
    path: '/audit/children',
    name: 'children',
    component: AuditListView,
    props: { segment: 'children' },
    meta: { title: 'Children' },
  },
  {
    path: '/audit/children/:id',
    name: 'child',
    component: ChildDetailView,
    meta: { title: 'Child' },
  },
  // Phase 4b command areas: entries exist; pages are stubs by design.
  {
    path: '/match',
    name: 'match',
    component: StubView,
    meta: { title: 'Match' },
  },
  {
    path: '/generate',
    name: 'generate',
    component: StubView,
    meta: { title: 'Generate' },
  },
  {
    path: '/stats',
    name: 'stats',
    component: StubView,
    meta: { title: 'Stats' },
  },
  {
    path: '/clarify',
    name: 'clarify',
    component: StubView,
    meta: { title: 'Clarify' },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: StubView,
    meta: { title: 'Jobs' },
  },
  {
    path: '/admin',
    name: 'admin',
    component: StubView,
    meta: { title: 'Admin' },
  },
]

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory(),
    routes,
  })

  router.beforeEach(async (to) => {
    const session = useSessionStore()

    if (to.meta.public) {
      if (!session.authenticated) await session.restore()
      return session.authenticated ? { path: '/' } : true
    }

    if (!session.authenticated && !(await session.restore())) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    if (formDirty.value) {
      // Browser confirm is the last-resort guard; editors also expose
      // explicit reload/rebase actions for version conflicts.
      if (!window.confirm('Discard unsaved changes?')) return false
      setDirty(false)
    }
    return true
  })

  router.afterEach((to) => {
    document.title = to.meta.title ? `${to.meta.title} · CCAT` : 'CCAT'
  })

  return router
}

export const router = createAppRouter()
