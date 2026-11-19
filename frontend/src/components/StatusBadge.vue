<script setup lang="ts">
import { computed } from 'vue'

import { statusLabel } from '../utils/format'

const props = defineProps<{ status?: string | null }>()

const value = computed(() => props.status ?? 'new')
const css = computed(() => `status-${value.value.replaceAll('_', '-')}`)
</script>

<template>
  <span class="badge" :class="css" :data-status="value">
    {{ statusLabel(value) }}
  </span>
</template>
