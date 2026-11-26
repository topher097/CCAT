/** Per-session "progress to next on audit" flag (never persisted). */

import { ref } from 'vue'

/** Module singleton: survives route remounts, resets on reload. */
export const autoAdvance = ref(true)
