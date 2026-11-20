/**
 * Path addressing and presentation metadata for the settings tree.
 *
 * A path is a list of segments: each segment is an object key or an
 * array index.  Paths are kept as arrays (not dotted strings) because
 * configuration keys may themselves contain dots, e.g. the template
 * variables under `info_sheet.mappings`.
 */

/** Location of one node in the configuration tree. */
export type Path = string[]

/** Presentation hints for one node. */
export type NodeMeta = {
  /** Human label; defaults to the key in title case. */
  label?: string
  /** One-line explanation shown under the label. */
  description?: string
  /** Value a newly added list item or map entry starts from. */
  newItem?: unknown
  /** The node's keys are user data, so it edits as a map of entries. */
  map?: boolean
  /** Field of the entry that holds its display title. */
  titleFrom?: 'rename'
}

export const NODE_META: Record<string, NodeMeta> = {
  season: {
    label: 'Season',
    description: 'Season label used in output filenames and the header.',
  },
  xmas_date: {
    label: 'Christmas date',
    description: 'Date every age is computed from (MM/DD/YYYY).',
  },
  excel_file: {
    label: 'Workbook',
    description: 'Spreadsheet the pipeline imports from.',
  },
  log_file: {
    label: 'Log file',
    description: 'Where the pipeline writes its run log.',
  },
  match_report_csv: {
    label: 'Match report',
    description: 'CSV the match step writes.',
  },
  zite: {
    label: 'Zite sources',
    description: 'Source table URLs the importer reads.',
  },
  'zite.sponsor': { label: 'Sponsor table' },
  'zite.family': { label: 'Family table' },
  'zite.child': { label: 'Child table' },
  tables: {
    label: 'Tables & columns',
    description: 'Columns imported per table and how each one is validated.',
    map: true,
    newItem: { columns: {} },
  },
  vocabularies: {
    label: 'Vocabularies',
    description: 'Allowed values for the enum columns.',
    map: true,
  },
  location_groups: {
    label: 'Location groups',
    description: 'Pickup and drop-off locations grouped by area.',
    map: true,
  },
  acquire: {
    label: 'Acquire',
    description: 'Settings for the acquisition stage.',
  },
  screening: {
    label: 'Screening',
    description: 'Rules applied while screening registrations.',
  },
  'screening.sibling_dob_max_gap_days': {
    label: 'Sibling gap (days)',
    description: 'Largest birth-date gap that still counts as siblings.',
  },
  email_check: {
    label: 'Email check',
    description: 'Bulk email validation through ZeroBounce.',
  },
  'email_check.enabled': { label: 'Run the email check' },
  'email_check.workers': {
    label: 'Workers',
    description: 'Concurrent checks when the bulk path is unavailable.',
  },
  'email_check.delay': {
    label: 'Delay (seconds)',
    description: 'Pacing between sequential checks.',
  },
  'email_check.api_key_env': {
    label: 'API key variable',
    description: 'Environment variable holding the ZeroBounce key.',
  },
  address_check: {
    label: 'Address check',
    description: 'Family address validation through Nominatim.',
  },
  'address_check.enabled': { label: 'Run the address check' },
  'address_check.base_url': { label: 'Server URL' },
  'address_check.delay': {
    label: 'Delay (seconds)',
    description: 'Seconds between requests (the OSM usage policy caps this).',
  },
  'address_check.user_agent': {
    label: 'User agent',
    description: 'Identifying header the service requires.',
  },
  'address_check.timeout': { label: 'Timeout (seconds)' },
  ai_screening: {
    label: 'AI screening',
    description: 'Model that reviews a child record for internal consistency.',
  },
  'ai_screening.model': { label: 'Model' },
  'ai_screening.system_prompt': {
    label: 'System prompt',
    description: 'Instructions the model screens records against.',
  },
  'ai_screening.temperature': {
    label: 'Temperature',
    description: 'Sampling temperature; 0 keeps screening deterministic.',
  },
  'ai_screening.max_workers': {
    label: 'Max workers',
    description: 'Concurrent screening requests.',
  },
  clarify: {
    label: 'Clarification emails',
    description: 'Model and files used to write family clarification emails.',
  },
  'clarify.model': { label: 'Model' },
  'clarify.system_prompt': {
    label: 'System prompt',
    description: 'Instructions for writing the clarification email.',
  },
  'clarify.email_template_html_file': { label: 'Email template' },
  'clarify.output_dir': { label: 'Output directory' },
  'clarify.clarifications_csv': { label: 'Clarifications CSV' },
  matchmaking: {
    label: 'Matchmaking',
    description: 'Weights the match score adds up.',
  },
  'matchmaking.location_weight': { label: 'Location weight' },
  'matchmaking.age_weight': { label: 'Age weight' },
  'matchmaking.gender_weight': { label: 'Gender weight' },
  info_sheet: {
    label: 'Information sheets',
    description: 'Child information sheet template and generation settings.',
  },
  'info_sheet.template_file': { label: 'Template file' },
  'info_sheet.output_dir': { label: 'Output directory' },
  'info_sheet.soffice_binary': { label: 'LibreOffice binary' },
  'info_sheet.max_workers': { label: 'Max workers' },
  'info_sheet.conversion_timeout_seconds': { label: 'Conversion timeout' },
  'info_sheet.contact_placeholder_length': {
    label: 'Placeholder length',
    description: 'Underscores used for the blank sponsor contact line.',
  },
  'info_sheet.contact_placeholder_affiliations': {
    label: 'Placeholder affiliations',
    description: 'Sponsor affiliations whose contact line stays blank.',
  },
  'info_sheet.mappings': {
    label: 'Template mappings',
    description: 'Template variable on the left, record field on the right.',
    map: true,
  },
  report: {
    label: 'Reports',
    description: 'Where and how the HTML report is written.',
  },
  'report.output_dir': { label: 'Output directory' },
  'report.filename_prefix': { label: 'Filename prefix' },
  'report.timezone': {
    label: 'Timezone',
    description: 'Which clock stamps the report date.',
  },
  sponsor_packets: {
    label: 'Sponsor packets',
    description: 'Sponsor email packet template and recipients.',
  },
  'sponsor_packets.email_template_html_file': { label: 'Email template' },
  'sponsor_packets.instructions_file': { label: 'Instructions PDF' },
  'sponsor_packets.output_dir': { label: 'Output directory' },
  'sponsor_packets.filename_prefix': { label: 'Filename prefix' },
  'sponsor_packets.bcc_emails': {
    label: 'BCC recipients',
    description: 'Addresses copied into every draft.',
  },
  storage: {
    label: 'Storage',
    description: 'Where season data and generated artifacts live.',
  },
  'storage.mode': {
    label: 'Mode',
    description: 'Workbook keeps the legacy path; db uses the SQLite store.',
  },
  'storage.database_url': { label: 'Database URL' },
  'storage.artifact_root': { label: 'Artifact root' },
  api: {
    label: 'API',
    description: 'Web API server settings.',
  },
  'api.host': { label: 'Host' },
  'api.port': { label: 'Port' },
  'api.auth_mode': {
    label: 'Auth mode',
    description: 'dev enables the passwordless accounts; oidc defers to the provider.',
  },
  'api.session_secret_env': { label: 'Session secret variable' },
  'api.session_ttl_hours': { label: 'Session lifetime (hours)' },
  'api.dev_users': {
    label: 'Dev users',
    description: 'Passwordless accounts created in dev mode.',
  },
  worker: {
    label: 'Worker',
    description: 'Background job worker settings.',
  },
  'worker.poll_interval_seconds': { label: 'Poll interval (seconds)' },
  'worker.heartbeat_seconds': { label: 'Heartbeat (seconds)' },
  'worker.lease_seconds': { label: 'Lease (seconds)' },
  'worker.max_attempts': { label: 'Max attempts' },
  'worker.progress_throttle_seconds': { label: 'Progress throttle (seconds)' },
  // New entries need a shape the configuration contract accepts.
  'tables.*.columns': {
    map: true,
    titleFrom: 'rename',
    newItem: { type: 'string', rename: null, default: null, values: null },
  },
}

/** Turn a configuration key into a readable label. */
export function humanize(key: string): string {
  // Dotted keys are template variables, not words: show them verbatim.
  if (key.includes('.')) return key
  const words = key.replace(/_/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * Presentation hints for one node.
 *
 * The most specific pattern wins: an exact path first, then patterns
 * with a single wildcard segment.
 */
export function metaFor(path: Path): NodeMeta {
  const exact = NODE_META[path.join('.')]
  if (exact) return exact
  for (let index = 0; index < path.length; index += 1) {
    const pattern = [
      ...path.slice(0, index),
      '*',
      ...path.slice(index + 1),
    ].join('.')
    const hit = NODE_META[pattern]
    if (hit) return hit
  }
  return {}
}

/** Read the node at one path. */
export function getAt(root: unknown, path: Path): unknown {
  let node: unknown = root
  for (const segment of path) {
    if (node === null || typeof node !== 'object') return undefined
    node = (node as Record<string, unknown>)[segment]
  }
  return node
}

/** Write one node, adding the key when it is new. */
export function setAt(root: object, path: Path, value: unknown): void {
  if (path.length === 0) return
  const parent = getAt(root, path.slice(0, -1))
  if (parent === null || typeof parent !== 'object') return
  ;(parent as Record<string, unknown>)[path[path.length - 1]] = value
}

/** Remove one node (array item or object key). */
export function removeAt(root: object, path: Path): void {
  if (path.length === 0) return
  const parent = getAt(root, path.slice(0, -1))
  const key = path[path.length - 1]
  if (Array.isArray(parent)) {
    const index = Number(key)
    if (Number.isInteger(index)) parent.splice(index, 1)
    return
  }
  if (parent !== null && typeof parent === 'object') {
    delete (parent as Record<string, unknown>)[key]
  }
}

/**
 * Rename an object key, keeping the key order of the parent.
 *
 * @param root Configuration tree.
 * @param path Path of the key to rename.
 * @param nextKey New key name.
 */
export function renameAt(root: object, path: Path, nextKey: string): void {
  if (path.length === 0) return
  const parent = getAt(root, path.slice(0, -1))
  const key = path[path.length - 1]
  if (parent === null || typeof parent !== 'object') return
  if (Array.isArray(parent)) return
  const record = parent as Record<string, unknown>
  if (nextKey === key || nextKey in record) return
  const entries: [string, unknown][] = Object.entries(record).map(
    ([name, value]) => (name === key ? [nextKey, value] : [name, value]),
  )
  for (const name of Object.keys(record)) delete record[name]
  for (const [name, value] of entries) record[name] = value
}

/**
 * Value a newly added list item or map entry starts from.
 *
 * An explicit hint wins; otherwise the shape of the existing entries
 * decides, so a list of strings stays a list of strings.
 */
export function templateFor(path: Path, siblings: unknown[]): unknown {
  const meta = metaFor(path)
  if (meta.newItem !== undefined) {
    return JSON.parse(JSON.stringify(meta.newItem))
  }
  const sample = siblings[0]
  if (sample === undefined) return ''
  if (typeof sample === 'number') return 0
  if (typeof sample === 'boolean') return false
  if (Array.isArray(sample)) return []
  if (sample !== null && typeof sample === 'object') return {}
  return ''
}
