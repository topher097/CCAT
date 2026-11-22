<script setup lang="ts">
/** The sponsor <select> with remaining capacity (plan D8). */

import type { ManualMatchSponsor } from '../api/manual-match'

defineProps<{
  sponsors: ManualMatchSponsor[]
  modelValue: number | null
  disabled?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: number | null] }>()

function remainingOf(sponsor: ManualMatchSponsor): number | null {
  if (sponsor.num_children === null || sponsor.num_children === undefined) {
    return null
  }
  return sponsor.remaining ?? sponsor.num_children
}

function capacityText(sponsor: ManualMatchSponsor): string {
  const remaining = remainingOf(sponsor)
  if (remaining === null) return 'unlimited'
  if (remaining <= 0) return 'full'
  return `${remaining} of ${sponsor.num_children} remaining`
}

function optionLabel(sponsor: ManualMatchSponsor): string {
  return `${sponsor.display_name} — ${capacityText(sponsor)}`
}

function isFull(sponsor: ManualMatchSponsor): boolean {
  return remainingOf(sponsor) === 0
}

function onChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  emit('update:modelValue', value === '' ? null : Number(value))
}
</script>

<template>
  <div class="sponsor-picker">
    <label for="sponsor-picker">Sponsor</label>
    <select
      id="sponsor-picker"
      data-testid="sponsor-picker"
      :value="modelValue ?? ''"
      :disabled="disabled"
      @change="onChange"
    >
      <option value="">Choose a sponsor…</option>
      <option
        v-for="sponsor in sponsors"
        :key="sponsor.sponsor_id"
        :value="sponsor.sponsor_id"
        :disabled="isFull(sponsor)"
      >
        {{ optionLabel(sponsor) }}
      </option>
    </select>
  </div>
</template>

<style scoped>
.sponsor-picker {
  display: inline-flex;
  gap: 0.4rem;
  align-items: center;
}

.sponsor-picker select {
  min-width: 16rem;
}
</style>
