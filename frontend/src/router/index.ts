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
import DuplicationListView from '../views/DuplicationListView.vue'
import DuplicationDetailView from '../views/DuplicationDetailView.vue'

import PipelineCommandView from '../views/PipelineCommandView.vue'
import MatchView from '../views/MatchView.vue'
import ManualMatchView from '../views/ManualMatchView.vue'
import DispatchView from '../views/DispatchView.vue'
import StatsView from '../views/StatsView.vue'
import ClarifyListView from '../views/ClarifyListView.vue'
import ClarifyDetailView from '../views/ClarifyDetailView.vue'
import JobsListView from '../views/JobsListView.vue'
import JobDetailView from '../views/JobDetailView.vue'
import AdminView from '../views/AdminView.vue'
import DatabaseView from '../views/DatabaseView.vue'
import SettingsLayout from '../views/settings/SettingsLayout.vue'
import SettingsIndexView from '../views/settings/SettingsIndexView.vue'
import SettingsSectionView from '../views/settings/SettingsSectionView.vue'
import { useSettingsStore } from '../stores/settings'
import { sectionBySlug } from '../settings/sections'

declare module 'vue-router' {
  interface RouteMeta {
    /** Reachable without a session. */
    public?: boolean
    /** Admin-only route (nav hidden and guard enforced). */
    admin?: boolean
    title?: string
    /** Page uses the full viewport width (see .app-main--wide). */
    wide?: boolean
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
    component: PipelineCommandView,
    props: { command: 'acquire' },
    meta: { title: 'Acquire' },
  },
  {
    path: '/screen',
    name: 'screen',
    component: PipelineCommandView,
    props: { command: 'screen' },
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
  {
    path: '/audit/duplications',
    name: 'duplications',
    component: DuplicationListView,
    meta: { title: 'Duplications' },
  },
  {
    path: '/audit/duplications/:id',
    name: 'duplication',
    component: DuplicationDetailView,
    meta: { title: 'Duplication' },
  },
  {
    path: '/match',
    name: 'match',
    component: MatchView,
    meta: { title: 'Match' },
  },
  {
    path: '/match/manual',
    name: 'match-manual',
    component: ManualMatchView,
    meta: { title: 'Manual matching', wide: true },
  },
  {
    path: '/dispatch',
    name: 'dispatch',
    component: DispatchView,
    meta: { title: 'Dispatch' },
  },
  { path: '/generate', redirect: '/dispatch' },
  {
    path: '/stats',
    name: 'stats',
    component: StatsView,
    meta: { title: 'Stats' },
  },
  {
    path: '/clarify',
    name: 'clarify',
    component: ClarifyListView,
    meta: { title: 'Clarify' },
  },
  {
    path: '/clarify/:familyId',
    name: 'clarify-family',
    component: ClarifyDetailView,
    meta: { title: 'Clarification family' },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: JobsListView,
    meta: { title: 'Jobs' },
  },
  {
    path: '/jobs/:id',
    name: 'job',
    component: JobDetailView,
    meta: { title: 'Job' },
  },
  {
    path: '/admin',
    name: 'admin',
    component: AdminView,
    meta: { title: 'Admin', admin: true },
  },
  {
    path: '/admin/database',
    name: 'admin-database',
    component: DatabaseView,
    meta: { title: 'Database', admin: true },
  },
  {
    path: '/settings',
    component: SettingsLayout,
    meta: { title: 'Settings', admin: true },
    children: [
      {
        path: '',
        name: 'settings',
        component: SettingsIndexView,
        meta: { title: 'Settings', admin: true },
      },
      {
        path: ':section',
        name: 'settings-section',
        component: SettingsSectionView,
        meta: { title: 'Settings', admin: true },
      },
    ],
  },
]

export function createAppRouter() {
  const router = createRouter({
    history: createWebHistory(),
    routes,
  })

  router.beforeEach(async (to, from) => {
    const session = useSessionStore()
    const settings = useSettingsStore()

    if (to.meta.public) {
      if (!session.authenticated) await session.restore()
      return session.authenticated ? { path: '/' } : true
    }

    if (!session.authenticated && !(await session.restore())) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    if (to.meta.admin && !session.hasRole('admin')) return { path: '/' }

    // Moving between settings sections keeps the same draft, so only
    // leaving the settings pages can lose the edits.
    const staysInSettings =
      to.path.startsWith('/settings') && from.path.startsWith('/settings')

    if ((formDirty.value || settings.dirty) && !staysInSettings) {
      // Browser confirm is the last-resort guard; editors also expose
      // explicit reload/rebase actions for version conflicts.
      if (!window.confirm('Discard unsaved changes?')) return false
      setDirty(false)
    }

    if (
      from.path.startsWith('/settings') &&
      !to.path.startsWith('/settings')
    ) {
      // The prompt above promises the edits are dropped; drop them.
      settings.discard()
    }

    return true
  })

  router.afterEach((to) => {
    const title =
      to.name === 'settings-section'
        ? sectionBySlug(String(to.params.section))?.title
        : to.meta.title
    document.title = title ? `${title} · CCAT` : 'CCAT'
  })

  return router
}

export const router = createAppRouter()
