import { readonly, ref } from 'vue'

const state = ref<number | null>(null)
const jobId = readonly(state)

export function useJobPopup() {
  return {
    jobId,
    openJob(id: number): void {
      state.value = id
    },
    closeJob(): void {
      state.value = null
    },
  }
}
