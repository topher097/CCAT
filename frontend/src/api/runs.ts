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

/** The screening command's global AI flag. */
export type ScreenRunSettings = {
  overwrite_new: boolean
}

/** The commands the run menu drives. */
export type RunCommand = 'match' | 'generate'

/** The screening command's own settings command. */
export type ScreenCommand = 'screen'

/** GET one command's effective run settings. */
export function getRunSettings(
  command: RunCommand | ScreenCommand,
): Promise<RunSettingsView> {
  return api.get(`/api/v1/runs/${command}/settings`)
}

/** Persist one command's run settings to the configuration file. */
export function saveRunSettings(
  command: RunCommand | ScreenCommand,
  body: MatchRunSettings | GenerateRunSettings | ScreenRunSettings,
): Promise<RunSettingsView> {
  return api.patch(`/api/v1/runs/${command}/settings`, body)
}
