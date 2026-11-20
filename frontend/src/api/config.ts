/** Pipeline configuration API (admin only). */

import { api } from './client'
import type { JsonObject } from './client'

export type ConfigEnvelope = {
  path: string
  config: JsonObject
}

/** GET the active pipeline configuration file. */
export function getConfig(): Promise<ConfigEnvelope> {
  return api.get<ConfigEnvelope>('/api/v1/config')
}

/** PUT the whole configuration tree. */
export function saveConfig(config: JsonObject): Promise<ConfigEnvelope> {
  return api.put<ConfigEnvelope>('/api/v1/config', config)
}
