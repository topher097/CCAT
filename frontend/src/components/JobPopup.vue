<script setup lang="ts">
import { computed } from 'vue'

import PopupFrame from './PopupFrame.vue'
import { useJobPopup } from '../composables/useJobPopup'

const { jobId, closeJob } = useJobPopup()
const title = computed(() => (jobId.value === null ? '' : `Job #${jobId.value}`))
// embed=1 drops the section navigation inside the framed job page.
const src = computed(() => `/jobs/${jobId.value}?embed=1`)
</script>

<template>
  <PopupFrame
    :open="jobId !== null"
    :title="title"
    :src="src"
    @close="closeJob"
  />
</template>
