<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { listEntities } from '../api/audit'
import { useAuditNav } from '../composables/useAuditNav'
import { useSessionStore } from '../stores/session'
import EntityEditor from '../components/EntityEditor.vue'
import AuditBackLink from '../components/AuditBackLink.vue'
import StatusBadge from '../components/StatusBadge.vue'

const route = useRoute()
const session = useSessionStore()
const key = computed(() => String(route.params.id ?? ''))
const children = ref<Record<string, unknown>[]>([])
const childrenError = ref<string | null>(null)
/** The back button returns to the families list this family came from. */
const { listUrl } = useAuditNav('/audit/families')

async function loadChildren(): Promise<void> {
  const season = session.season?.name
  if (!season) return
  childrenError.value = null
  try {
    const page = await listEntities(season, 'child', {
      q: key.value,
      page_size: 200,
    })
    children.value = (page.items as Record<string, unknown>[]).filter(
      (row) => String(row.family_id) === key.value,
    )
  } catch (failure) {
    childrenError.value =
      failure instanceof Error ? failure.message : 'children request failed'
  }
}

watch(key, () => void loadChildren())
onMounted(() => void loadChildren())
</script>

<template>
  <section>
    <EntityEditor entity="family" :entity-key="key">
      <template #back>
        <AuditBackLink :to="listUrl" label="All families" />
      </template>
    </EntityEditor>

    <section class="card" style="margin-top: 1rem" data-testid="family-children">
      <h2 style="margin-top: 0">Children in this family</h2>
      <p v-if="childrenError" class="error-text" role="alert">
        {{ childrenError }}
      </p>
      <p v-else-if="children.length === 0" class="muted">
        No children reference this family.
      </p>
      <table v-else class="data">
        <thead>
          <tr>
            <th>Status</th>
            <th>Record id</th>
            <th>Name</th>
            <th>DOB</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="child in children" :key="String(child.zite_record_id)">
            <td><StatusBadge :status="String(child.status ?? 'new')" /></td>
            <td>
              <RouterLink
                :to="`/audit/families/${key}/children/${child.zite_record_id}`"
              >
                {{ child.zite_record_id }}
              </RouterLink>
            </td>
            <td>{{ child.first_name ?? '—' }}</td>
            <td>{{ child.dob ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>
