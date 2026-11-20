/**
 * Section map for the settings UI.
 *
 * The configuration file is one flat JSON object; the settings page
 * presents it as a list of sections, each with its own subscreen.  A
 * section groups the top-level keys that belong to one topic, so the
 * index stays short and every subscreen holds a coherent amount of
 * settings.
 */

/** One group of configuration keys with its own subscreen. */
export type SettingsSection = {
  /** URL slug of the section. */
  slug: string
  /** Human title shown in the section list. */
  title: string
  /** One-line explanation shown under the title. */
  summary: string
  /** Top-level configuration keys the section owns. */
  keys: string[]
}

export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    slug: 'general',
    title: 'General',
    summary: 'Season, Christmas date, and the pipeline file paths.',
    keys: ['season', 'xmas_date', 'excel_file', 'log_file', 'match_report_csv'],
  },
  {
    slug: 'sources',
    title: 'Zite sources',
    summary: 'Source table URLs the importer reads.',
    keys: ['zite'],
  },
  {
    slug: 'tables',
    title: 'Tables & columns',
    summary: 'Columns imported per table and how each one is validated.',
    keys: ['tables'],
  },
  {
    slug: 'vocabularies',
    title: 'Vocabularies',
    summary: 'Allowed values for the enum columns.',
    keys: ['vocabularies'],
  },
  {
    slug: 'locations',
    title: 'Location groups',
    summary: 'Pickup and drop-off locations grouped by area.',
    keys: ['location_groups'],
  },
  {
    slug: 'screening',
    title: 'Screening',
    summary: 'Screening rules and the external checks they run.',
    keys: ['screening', 'acquire', 'email_check', 'address_check'],
  },
  {
    slug: 'ai',
    title: 'AI & prompts',
    summary: 'Models and system prompts for screening and clarification.',
    keys: ['ai_screening', 'clarify'],
  },
  {
    slug: 'matching',
    title: 'Matchmaking & sheets',
    summary: 'Match scoring weights and information-sheet generation.',
    keys: ['matchmaking', 'info_sheet'],
  },
  {
    slug: 'output',
    title: 'Reports & packets',
    summary: 'Report and sponsor-packet output settings.',
    keys: ['report', 'sponsor_packets'],
  },
  {
    slug: 'server',
    title: 'Server',
    summary: 'Storage, API, and background-worker settings.',
    keys: ['storage', 'api', 'worker'],
  },
]

/** Slug of the catch-all section for keys no named section owns. */
export const OTHER_SECTION_SLUG = 'other'

/** Find one section by slug. */
export function sectionBySlug(slug: string): SettingsSection | undefined {
  return SETTINGS_SECTIONS.find((section) => section.slug === slug)
}

/**
 * Every section that applies to a configuration tree.
 *
 * Keys no named section owns are collected into a trailing "Other"
 * section, so a configuration file that grows a new key stays
 * reachable without a frontend change.
 */
export function sectionsFor(
  keys: string[],
): SettingsSection[] {
  const owned = new Set(SETTINGS_SECTIONS.flatMap((section) => section.keys))
  const rest = keys.filter((key) => !owned.has(key))
  const sections = [...SETTINGS_SECTIONS]
  if (rest.length > 0) {
    sections.push({
      slug: OTHER_SECTION_SLUG,
      title: 'Other',
      summary: 'Settings no named section covers yet.',
      keys: rest,
    })
  }
  return sections
}
