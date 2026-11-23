/** Display helpers and the field allow-list per entity (§7.2). */

/** Data older than this is flagged stale on the overview. */
export const STALE_AFTER_HOURS = 6

/** Format an ISO timestamp for display; "—" when absent. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

/** Whether the data revision is old/absent enough to flag as stale. */
export function isStale(
  iso: string | null | undefined,
  now: Date = new Date(),
): boolean {
  if (!iso) return true
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return true
  return now.getTime() - date.getTime() > STALE_AFTER_HOURS * 3_600_000
}

/** Human label for one stored status. */
export function statusLabel(status: string | null | undefined): string {
  return (status ?? 'new').replaceAll('_', ' ')
}

/** Field display names. */
export type FieldSpec = {
  field: string
  label: string
  kind?: 'text' | 'number' | 'textarea' | 'date' | 'boolean'
  /** Control field whose diff is informational, not status-relevant. */
  neutral?: boolean
  /**
   * Hidden from the entity editor. The field still exists and still
   * round-trips; the editor simply does not offer it.
   */
  hidden?: boolean
}

export const ENTITY_FIELDS: Record<'sponsor' | 'family' | 'child', FieldSpec[]> = {
  sponsor: [
    { field: 'first_name', label: 'First name' },
    { field: 'last_name', label: 'Last name' },
    { field: 'email', label: 'Email' },
    { field: 'phone', label: 'Phone' },
    { field: 'organization_name', label: 'Organization', kind: 'textarea' },
    { field: 'sponsor_type', label: 'Sponsor type' },
    { field: 'num_children', label: 'Children requested', kind: 'number' },
    { field: 'age_preference', label: 'Age preference' },
    { field: 'gender_preference', label: 'Gender preference' },
    { field: 'dropoff_location', label: 'Drop-off location' },
    { field: 'elevated_budget', label: 'Elevated budget', kind: 'boolean' },
    { field: 'physical_packet', label: 'Physical packet', kind: 'boolean' },
    {
      field: 'prior_assigned_angel_ids',
      label: 'Prior angel ids',
      kind: 'textarea',
      // Manual matching places families by hand; the free-text field
      // cannot express a partial placement, so the editor hides it.
      hidden: true,
    },
  ],
  family: [
    { field: 'first_name', label: 'First name' },
    { field: 'last_name', label: 'Last name' },
    { field: 'primary_email', label: 'Primary email' },
    { field: 'phone', label: 'Phone' },
    { field: 'street_address', label: 'Street address' },
    { field: 'apt_floor', label: 'Apt / floor' },
    { field: 'city', label: 'City' },
    { field: 'state', label: 'State' },
    { field: 'zip_code', label: 'ZIP' },
    { field: 'secondary_first_name', label: 'Secondary first name' },
    { field: 'secondary_last_name', label: 'Secondary last name' },
    { field: 'secondary_email', label: 'Secondary email' },
    { field: 'secondary_phone', label: 'Secondary phone' },
    { field: 'gift_pickup_location', label: 'Gift pickup location' },
  ],
  child: [
    { field: 'first_name', label: 'First name' },
    { field: 'primary_email', label: 'Primary email' },
    { field: 'dob', label: 'Date of birth', kind: 'date' },
    { field: 'age_reported', label: 'Age reported', kind: 'number' },
    { field: 'gender', label: 'Gender' },
    { field: 'special_needs', label: 'Special needs', neutral: true },
    { field: 'need_1', label: 'Need 1', kind: 'textarea' },
    { field: 'need_2', label: 'Need 2', kind: 'textarea' },
    { field: 'desire_1', label: 'Desire 1', kind: 'textarea' },
    { field: 'desire_2', label: 'Desire 2', kind: 'textarea' },
    { field: 'shoe_size', label: 'Shoe size' },
    { field: 'shoe_size_type', label: 'Shoe size type' },
    { field: 'pant_waist_size', label: 'Pant waist' },
    { field: 'pant_inseam', label: 'Pant inseam' },
    { field: 'pant_size_letter', label: 'Pant letter size' },
    { field: 'pant_size_type', label: 'Pant size type' },
    { field: 'jean_size', label: 'Jean size' },
    { field: 'shirt_size_letter', label: 'Shirt letter size' },
    { field: 'shirt_size_type', label: 'Shirt size type' },
    { field: 'favorite_colors', label: 'Favorite colors' },
    { field: 'interests', label: 'Interests', kind: 'textarea' },
    { field: 'other_notes', label: 'Other notes', kind: 'textarea' },
    {
      field: 'ai_screening_notes',
      label: 'AI screening notes',
      kind: 'textarea',
    },
  ],
}
