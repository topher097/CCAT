/** Typed run-settings endpoint helpers (types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

export type RunSettingsView = Schemas['RunSettingsView']

/**
 * The matcher's rememberable run flags.  The backend picks the request
 * model from the command, so these are request bodies the OpenAPI
 * schema does not name.
 */
export type MatchRunSettings = {
  test: boolean
  skip_match: boolean
}

/** The generation command's rememberable run flags. */
export type GenerateRunSettings = {
  test: boolean
  overwrite: boolean
  max_workers: number
}

/** The commands the run menu drives. */
export type RunCommand = 'match' | 'generate'

/** GET one command's effective run settings. */
export function getRunSettings(
  command: RunCommand,
): Promise<RunSettingsView> {
  return api.get(`/api/v1/runs/${command}/settings`)
}

/** Persist one command's run settings to the configuration file. */
export function saveRunSettings(
  command: RunCommand,
  body: MatchRunSettings | GenerateRunSettings,
): Promise<RunSettingsView> {
  return api.patch(`/api/v1/runs/${command}/settings`, body)
}
