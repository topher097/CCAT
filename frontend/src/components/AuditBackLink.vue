<script setup lang="ts">
/** The audit back button: it returns to the list view the user came from. */
defineProps<{ to: string; label?: string }>()
</script>

<template>
  <router-link class="back-link" :to="to" data-testid="back-link">
    ← {{ label ?? 'Back' }}
  </router-link>
</template>
