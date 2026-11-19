<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useSessionStore } from '../stores/session'

const session = useSessionStore()
const router = useRouter()
const route = useRoute()
const username = ref('')

async function submit() {
  const ok = await session.login(username.value)
  if (!ok) return
  const redirect =
    typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  await router.push(redirect)
}
</script>

<template>
  <section class="login card" style="max-width: 26rem; margin: 2rem auto">
    <h1>Sign in</h1>
    <p class="muted">
      Development mode: enter a staff username (no password). Production
      identity is a follow-up decision; passwordless login only works with
      <code>auth_mode: dev</code>.
    </p>
    <form @submit.prevent="submit">
      <label for="login-username">Username</label>
      <div style="display: flex; gap: 0.5rem; margin-top: 0.25rem">
        <input
          id="login-username"
          v-model="username"
          name="username"
          autocomplete="username"
          required
          style="flex: 1; padding: 0.35rem 0.5rem"
        />
        <button type="submit" class="primary" :disabled="session.loading">
          Sign in
        </button>
      </div>
    </form>
    <p
      v-if="session.loginError"
      class="error-text"
      role="alert"
      data-testid="login-error"
    >
      {{ session.loginError.message }} (HTTP {{ session.loginError.status }})
    </p>
  </section>
</template>
