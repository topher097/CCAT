import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import { router } from './router'
import { configureClient } from './api/client'
import { useSessionStore } from './stores/session'
import './assets/base.css'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)

const session = useSessionStore(pinia)
configureClient({
  csrfToken: () => session.csrfToken,
  onUnauthorized: () => {
    session.clear()
    if (router.currentRoute.value.path !== '/login') {
      void router.push({ path: '/login' })
    }
  },
})

app.mount('#app')
