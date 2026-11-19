<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'

import { useSessionStore } from './stores/session'

const session = useSessionStore()
const router = useRouter()
const online = ref(typeof navigator === 'undefined' ? true : navigator.onLine)

const nav = [
  { to: '/', label: 'Overview' },
  { to: '/acquire', label: 'Acquire' },
  { to: '/screen', label: 'Screen' },
  { to: '/audit', label: 'Audit' },
  { to: '/match', label: 'Match' },
  { to: '/generate', label: 'Generate' },
  { to: '/stats', label: 'Stats' },
  { to: '/clarify', label: 'Clarify' },
  { to: '/jobs', label: 'Jobs' },
  { to: '/admin', label: 'Admin' },
]

const seasonName = computed(() => session.season?.name ?? '—')

function onOnline() {
  online.value = true
}

function onOffline() {
  online.value = false
}

async function logout() {
  await session.logout()
  await router.push('/login')
}

onMounted(() => {
  window.addEventListener('online', onOnline)
  window.addEventListener('offline', onOffline)
  if (session.authenticated && !session.season) void session.loadSeason()
})

onUnmounted(() => {
  window.removeEventListener('online', onOnline)
  window.removeEventListener('offline', onOffline)
})
</script>

<template>
  <div class="app">
    <header v-if="session.authenticated" class="app-header">
      <div class="brand">
        <strong>CCAT</strong>
        <span class="season" data-testid="header-season">{{ seasonName }}</span>
      </div>
      <div class="identity">
        <span data-testid="header-user">{{ session.username }}</span>
        <span class="roles">{{ session.roles.join(', ') || 'no role' }}</span>
        <button type="button" data-testid="logout" @click="logout">Log out</button>
      </div>
    </header>

    <nav v-if="session.authenticated" class="app-nav" aria-label="Sections">
      <RouterLink v-for="item in nav" :key="item.to" :to="item.to">
        {{ item.label }}
      </RouterLink>
    </nav>

    <div v-if="!online" class="banner banner-offline" role="status">
      Offline — showing the last loaded data; changes will fail to save.
    </div>
    <div v-if="session.loading" class="banner banner-loading" role="status">
      Loading…
    </div>

    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>
