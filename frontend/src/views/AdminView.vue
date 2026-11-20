<script setup lang="ts">
/** /admin: staff accounts and roles (admin only; §7.1.11, §8). */

import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError } from '../api/client'
import {
  ROLES,
  createUser,
  listUsers,
  patchUser,
  type User,
  type UserCreate,
  type UserPatch,
} from '../api/users'
import { useSessionStore } from '../stores/session'

type RowDraft = {
  roles: string[]
  active: boolean
  display_name: string
}

const session = useSessionStore()
const isAdmin = computed(() => session.hasRole('admin'))

const users = ref<User[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const drafts = reactive<Record<number, RowDraft>>({})
const rowError = ref<Record<number, string>>({})
const savingId = ref<number | null>(null)

const creating = ref(false)
const createError = ref<string | null>(null)
const createMessage = ref<string | null>(null)
const newUser = reactive({
  username: '',
  display_name: '',
  roles: [] as string[],
  active: true,
})

function seedDrafts(rows: User[]): void {
  for (const user of rows) {
    drafts[user.id] = {
      roles: [...(user.roles ?? [])],
      active: user.active,
      display_name: user.display_name ?? '',
    }
  }
}

async function load(): Promise<void> {
  if (!isAdmin.value) return
  loading.value = true
  error.value = null
  try {
    users.value = await listUsers()
    seedDrafts(users.value)
  } catch (failure) {
    error.value =
      failure instanceof ApiError ? failure.message : 'user request failed'
  } finally {
    loading.value = false
  }
}

function toggleRole(list: string[], role: string): void {
  const index = list.indexOf(role)
  if (index >= 0) list.splice(index, 1)
  else list.push(role)
}

async function create(): Promise<void> {
  createError.value = null
  createMessage.value = null
  creating.value = true
  try {
    const user = await createUser({
      username: newUser.username.trim(),
      display_name: newUser.display_name.trim() || null,
      roles: [...newUser.roles] as UserCreate['roles'],
      active: newUser.active,
    })
    users.value = [...users.value, user]
    seedDrafts([user])
    createMessage.value = `Created ${user.username}.`
    newUser.username = ''
    newUser.display_name = ''
    newUser.roles = []
    newUser.active = true
  } catch (failure) {
    createError.value =
      failure instanceof ApiError ? failure.message : 'create request failed'
  } finally {
    creating.value = false
  }
}

async function save(user: User): Promise<void> {
  const draft = drafts[user.id]
  if (!draft) return
  savingId.value = user.id
  rowError.value = { ...rowError.value, [user.id]: '' }
  try {
    const updated = await patchUser(user.id, {
      display_name: draft.display_name.trim() || null,
      active: draft.active,
      roles: [...draft.roles] as UserPatch['roles'],
    })
    users.value = users.value.map((row) =>
      row.id === updated.id ? updated : row,
    )
    seedDrafts([updated])
  } catch (failure) {
    rowError.value = {
      ...rowError.value,
      [user.id]:
        failure instanceof ApiError ? failure.message : 'save request failed',
    }
  } finally {
    savingId.value = null
  }
}

onMounted(() => void load())
</script>

<template>
  <section>
    <h1 style="margin-top: 0">Admin</h1>

    <p v-if="!isAdmin" class="muted" data-testid="admin-forbidden">
      Admin role required. <RouterLink to="/">Back to overview</RouterLink>
    </p>

    <template v-else>
      <p class="muted">
        Staff accounts and the fixed role matrix. The API enforces the same
        admin gate.
      </p>

      <section class="card">
        <h2 style="margin-top: 0">Create user</h2>
        <form @submit.prevent="create">
          <p>
            <label>
              Username
              <input v-model="newUser.username" type="text" name="username" />
            </label>
          </p>
          <p>
            <label>
              Display name
              <input
                v-model="newUser.display_name"
                type="text"
                name="display_name"
              />
            </label>
          </p>
          <fieldset style="border: 0; padding: 0">
            <legend class="muted">Roles</legend>
            <label v-for="role in ROLES" :key="role" style="margin-right: 1rem">
              <input
                type="checkbox"
                :value="role"
                :checked="newUser.roles.includes(role)"
                @change="toggleRole(newUser.roles, role)"
              />
              {{ role.replaceAll('_', ' ') }}
            </label>
          </fieldset>
          <p>
            <label>
              <input v-model="newUser.active" type="checkbox" />
              Active
            </label>
          </p>
          <p v-if="createError" class="error-text" role="alert">
            {{ createError }}
          </p>
          <p v-if="createMessage" class="notice" role="status">
            {{ createMessage }}
          </p>
          <button
            type="submit"
            class="primary"
            :disabled="creating || !newUser.username.trim()"
          >
            {{ creating ? 'Creating…' : 'Create user' }}
          </button>
        </form>
      </section>

      <p v-if="loading" role="status">Loading users…</p>
      <p v-else-if="error" class="error-text" role="alert" data-testid="admin-error">
        {{ error }} <button type="button" @click="load">Retry</button>
      </p>
      <p v-else-if="users.length === 0" class="muted">No users stored.</p>
      <table v-else class="data" data-testid="admin-users">
        <thead>
          <tr>
            <th>User</th>
            <th>Display name</th>
            <th>Roles</th>
            <th>Active</th>
            <th>Save</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td>
              {{ user.username }}
              <span v-if="user.dev_no_password" class="badge">dev</span>
            </td>
            <td>
              <label>
                <span class="muted">Display name</span>
                <input
                  v-if="drafts[user.id]"
                  v-model="drafts[user.id]!.display_name"
                  type="text"
                />
              </label>
            </td>
            <td>
              <span
                v-for="role in ROLES"
                :key="role"
                style="display: block; white-space: nowrap"
              >
                <label>
                  <input
                    type="checkbox"
                    :value="role"
                    :checked="drafts[user.id]?.roles.includes(role) ?? false"
                    @change="
                      drafts[user.id] &&
                        toggleRole(drafts[user.id]!.roles, role)
                    "
                  />
                  {{ role.replaceAll('_', ' ') }}
                </label>
              </span>
            </td>
            <td>
              <label>
                <input
                  v-if="drafts[user.id]"
                  v-model="drafts[user.id]!.active"
                  type="checkbox"
                />
                active
              </label>
            </td>
            <td>
              <button
                type="button"
                :disabled="savingId === user.id"
                @click="save(user)"
              >
                {{ savingId === user.id ? 'Saving…' : 'Save' }}
              </button>
              <p v-if="rowError[user.id]" class="error-text" role="alert">
                {{ rowError[user.id] }}
              </p>
            </td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>
