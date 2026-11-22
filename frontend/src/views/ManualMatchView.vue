<script setup lang="ts">
/** /match/manual: place families with sponsors by hand (module 4b). */

import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { ApiError } from '../api/client'
import {
  assign,
  holdFamily,
  listPool,
  listSponsors,
  release,
  releaseHold,
  type AssignmentState,
  type ManualMatchChildCard,
  type ManualMatchFamilyCard,
  type ManualMatchSponsor,
  type ManualMatchAutomaticRun,
  type PoolParams,
} from '../api/manual-match'
import { getMatchReport } from '../api/commands'
import { enqueueJob } from '../api/jobs'
import { getRunSettings } from '../api/runs'
import { useSessionStore } from '../stores/session'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import FamilyCard from '../components/FamilyCard.vue'
import HoldDialog from '../components/HoldDialog.vue'
import SponsorPicker from '../components/SponsorPicker.vue'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const seasonName = computed(() => session.season?.name ?? '')

const ASSIGNMENT_STATES: Array<AssignmentState | 'any'> = [
  'unassigned',
  'partial',
  'split',
  'assigned',
  'any',
]
const HOLD_STATES = ['not_held', 'held', 'any'] as const
const GENDERS = ['Male', 'Female', 'Non-binary']
const CHILD_STATUSES = [
  'audited',
  'in_progress',
  'needs_clarification',
  'new',
]
const PAGE_SIZE = 25

/** Filter state, mirrored into the URL (plan §11.4). */
const q = ref('')
const assignment = ref<AssignmentState | 'any'>('unassigned')
const childGender = ref('any')
const ageMin = ref('')
const ageMax = ref('')
const specialNeeds = ref<'any' | 'yes' | 'no'>('any')
const location = ref<'any' | 'hancock' | 'calumet'>('any')
const familySizeMin = ref('')
const familySizeMax = ref('')
const childStatus = ref('any')
const hold = ref<'any' | 'held' | 'not_held'>('not_held')
const page = ref(1)
const pageSize = ref(PAGE_SIZE)

/** Pool data. */
const items = ref<ManualMatchFamilyCard[]>([])
const total = ref(0)
const sponsors = ref<ManualMatchSponsor[]>([])
const automaticRun = ref<ManualMatchAutomaticRun>({
  active: false,
  job_id: null,
  status: null,
  started_at: null,
})
const loading = ref(false)

/** How often the page re-reads the pool while an automatic run may run. */
const POLL_MS = 10_000
let pollTimer: ReturnType<typeof setInterval> | null = null
const error = ref<string | null>(null)

/** Selection lives in the page, not the URL or a store (§11.4). */
const selection = ref<Set<string>>(new Set())
const wholeFamilies = ref<Set<number>>(new Set())
const sponsorId = ref<number | null>(null)

type ChildInfo = {
  family_id: number | null
  row_version: number
  name: string
  age: number | null
  gender: string | null
  status: string | null
  generated_info_sheet: boolean
  sponsor_id: number | null
}

type FamilyInfo = {
  row_version: number
  child_ids: string[]
  selectable_ids: string[]
  gift_pickup_location: string | null
}

/** Versions of everything the selection touches, so it survives paging. */
const childInfo = ref<Map<string, ChildInfo>>(new Map())
const familyInfo = ref<Map<number, FamilyInfo>>(new Map())

/** Write state. */
const pending = ref(false)
const actionError = ref<string | null>(null)
const stalePrompt = ref(false)
const blockingJobId = ref<number | null>(null)
const notice = ref<string | null>(null)
const resultWarnings = ref<string[]>([])
const lastPlacedSponsorId = ref<number | null>(null)
const generating = ref(false)

/** Dialogs. */
const reassignOpen = ref(false)
const releaseOpen = ref(false)
const holdOpen = ref(false)
const holdTargetId = ref<number | null>(null)

/** The last automatic run's report, for the side panel. */
const reportCount = ref<number | null>(null)

const totalPages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize.value)),
)

const selectedSponsor = computed(
  () => sponsors.value.find((s) => s.sponsor_id === sponsorId.value) ?? null,
)

const sponsorFull = computed(() => {
  const sponsor = selectedSponsor.value
  if (!sponsor || sponsor.num_children === null || sponsor.num_children === undefined) {
    return false
  }
  return (
    sponsor.remaining !== null &&
    sponsor.remaining !== undefined &&
    sponsor.remaining <= 0
  )
})

const capMessage = computed(() => {
  const sponsor = selectedSponsor.value
  if (!sponsorFull.value || !sponsor) return ''
  return `${sponsor.display_name} is full (${sponsor.assigned_count} of ${sponsor.num_children} placed); release an assignment first.`
})

const assignedSelectedIds = computed(() =>
  [...selection.value].filter((id) => {
    const info = childInfo.value.get(id)
    return info?.sponsor_id !== null && info?.sponsor_id !== undefined
  }),
)

const assignBlockedReason = computed(() => {
  if (automaticRun.value.active) return 'An automatic match is running.'
  if (selection.value.size === 0) return 'Select at least one child.'
  if (sponsorId.value === null) return 'Choose a sponsor.'
  if (sponsorFull.value) return capMessage.value
  return ''
})
const canAssign = computed(
  () => assignBlockedReason.value === '' && !pending.value,
)
const canRelease = computed(
  () =>
    assignedSelectedIds.value.length > 0 &&
    !automaticRun.value.active &&
    !pending.value,
)

const holdFamilyId = computed(() => {
  const families = new Set<number>()
  for (const id of selection.value) {
    const familyId = childInfo.value.get(id)?.family_id
    if (familyId !== null && familyId !== undefined) families.add(familyId)
  }
  return families.size === 1 ? [...families][0]! : null
})

const holdLabel = computed(() => {
  const familyId = holdTargetId.value
  if (familyId === null) return 'this family'
  for (const card of items.value) {
    if (card.family_id !== familyId) continue
    const name = [card.first_name, card.last_name].filter(Boolean).join(' ')
    return name || `family ${familyId}`
  }
  return `family ${familyId}`
})

/** D8 advisories computed locally from the cards and the sponsor (§11.5). */
const liveWarnings = computed<string[]>(() => {
  const sponsor = selectedSponsor.value
  if (!sponsor) return []
  const warnings = new Set<string>()
  const seenFamilies = new Set<number>()
  const selectedCounts = new Map<number, number>()

  for (const id of selection.value) {
    const info = childInfo.value.get(id)
    if (!info) continue
    if (info.family_id !== null) {
      selectedCounts.set(
        info.family_id,
        (selectedCounts.get(info.family_id) ?? 0) + 1,
      )
    }

    if (
      info.age !== null &&
      (sponsor.pref_age_min !== null && sponsor.pref_age_min !== undefined) &&
      (sponsor.pref_age_max !== null && sponsor.pref_age_max !== undefined) &&
      (info.age < sponsor.pref_age_min || info.age > sponsor.pref_age_max)
    ) {
      warnings.add(
        `${info.name} is age ${info.age}; ${sponsor.display_name} prefers ${sponsor.pref_age_min}–${sponsor.pref_age_max}.`,
      )
    }

    const preference = (sponsor.gender_preference ?? '').trim().toLowerCase()
    if (
      preference &&
      preference !== 'any' &&
      preference !== 'any gender' &&
      info.gender &&
      preference !== info.gender.trim().toLowerCase()
    ) {
      warnings.add(
        info.gender.trim().toLowerCase() === 'non-binary'
          ? `${info.name} is non-binary; ${sponsor.display_name} prefers ${sponsor.gender_preference}. A manual assignment is allowed.`
          : `${info.name} is ${info.gender}; ${sponsor.display_name} prefers ${sponsor.gender_preference}.`,
      )
    }

    if (info.family_id !== null) {
      const family = familyInfo.value.get(info.family_id)
      const pickup = (family?.gift_pickup_location ?? '').trim().toLowerCase()
      const dropoff = (sponsor.dropoff_location ?? '').trim().toLowerCase()
      if (
        pickup &&
        dropoff &&
        pickup !== 'any' &&
        dropoff !== 'any' &&
        pickup !== dropoff &&
        !seenFamilies.has(info.family_id)
      ) {
        seenFamilies.add(info.family_id)
        warnings.add(
          `Family ${info.family_id} picks up at ${family?.gift_pickup_location}; ${sponsor.display_name} drops off at ${sponsor.dropoff_location}.`,
        )
      }
    }

    if (info.status !== 'audited') {
      warnings.add(`${info.name} is not audited (${info.status ?? 'new'}).`)
    }
    if (info.generated_info_sheet) {
      warnings.add(
        `${info.name} already has a generated info sheet; regenerate the sheets after assigning.`,
      )
    }
  }

  for (const [familyId, count] of selectedCounts) {
    const family = familyInfo.value.get(familyId)
    if (!family) continue
    if (count < family.selectable_ids.length) {
      warnings.add(
        `This selection splits family ${familyId} across sponsors.`,
      )
    }
  }

  return [...warnings]
})

const needsReassignConfirm = computed(() =>
  [...selection.value].some((id) => {
    const sponsorOfChild = childInfo.value.get(id)?.sponsor_id
    return (
      sponsorOfChild !== null &&
      sponsorOfChild !== undefined &&
      sponsorOfChild !== sponsorId.value
    )
  }),
)

/** Read the filters and page back out of the URL. */
function readUrl(): void {
  q.value = String(route.query.q ?? '')
  assignment.value = String(
    route.query.assignment ?? 'unassigned',
  ) as AssignmentState | 'any'
  childGender.value = String(route.query.child_gender ?? 'any')
  ageMin.value = String(route.query.age_min ?? '')
  ageMax.value = String(route.query.age_max ?? '')
  specialNeeds.value = String(route.query.special_needs ?? 'any') as
    | 'any'
    | 'yes'
    | 'no'
  location.value = String(route.query.location ?? 'any') as
    | 'any'
    | 'hancock'
    | 'calumet'
  familySizeMin.value = String(route.query.family_size_min ?? '')
  familySizeMax.value = String(route.query.family_size_max ?? '')
  childStatus.value = String(route.query.child_status ?? 'any')
  page.value = Math.max(1, Number(route.query.page ?? 1) || 1)
  pageSize.value = Math.max(
    1,
    Number(route.query.page_size ?? PAGE_SIZE) || PAGE_SIZE,
  )
}

/** Write the filters and page into the URL. */
function writeUrl(): void {
  const next: Record<string, string> = {}
  if (q.value) next.q = q.value
  if (assignment.value !== 'unassigned') next.assignment = assignment.value
  if (childGender.value !== 'any') next.child_gender = childGender.value
  if (ageMin.value !== '') next.age_min = ageMin.value
  if (ageMax.value !== '') next.age_max = ageMax.value
  if (specialNeeds.value !== 'any') next.special_needs = specialNeeds.value
  if (location.value !== 'any') next.location = location.value
  if (familySizeMin.value !== '') next.family_size_min = familySizeMin.value
  if (familySizeMax.value !== '') next.family_size_max = familySizeMax.value
  if (childStatus.value !== 'any') next.child_status = childStatus.value
  if (page.value > 1) next.page = String(page.value)
  if (pageSize.value !== PAGE_SIZE) next.page_size = String(pageSize.value)
  void router.replace({ path: route.path, query: next })
}

/** Build the pool query from the filter refs. */
function poolParams(): PoolParams {
  const params: PoolParams = {
    page: page.value,
    page_size: pageSize.value,
    assignment: assignment.value,
    hold: hold.value,
  }
  if (q.value) params.q = q.value
  if (childGender.value !== 'any') params.child_gender = childGender.value
  if (ageMin.value !== '') params.age_min = Number(ageMin.value)
  if (ageMax.value !== '') params.age_max = Number(ageMax.value)
  if (specialNeeds.value !== 'any') params.special_needs = specialNeeds.value
  if (location.value !== 'any') params.location = location.value
  if (familySizeMin.value !== '') {
    params.family_size_min = Number(familySizeMin.value)
  }
  if (familySizeMax.value !== '') {
    params.family_size_max = Number(familySizeMax.value)
  }
  if (childStatus.value !== 'any') params.child_status = childStatus.value
  return params
}

/** Remember versions and child facts so the selection survives paging. */
function rememberCards(cards: ManualMatchFamilyCard[]): void {
  const children = new Map(childInfo.value)
  const families = new Map(familyInfo.value)
  for (const card of cards) {
    const cardChildren = card.children ?? []
    families.set(card.family_id, {
      row_version: card.row_version,
      child_ids: cardChildren.map((child) => child.zite_record_id),
      selectable_ids: cardChildren
        .filter((child) => child.selectable)
        .map((child) => child.zite_record_id),
      gift_pickup_location: card.gift_pickup_location ?? null,
    })
    for (const child of cardChildren) {
      children.set(child.zite_record_id, childInfoOf(child, card.family_id))
    }
  }
  childInfo.value = children
  familyInfo.value = families
}

function childInfoOf(
  child: ManualMatchChildCard,
  familyId: number | null,
): ChildInfo {
  return {
    family_id: familyId,
    row_version: child.row_version,
    name: child.first_name ?? 'Unnamed child',
    age: child.age_at_xmas ?? null,
    gender: child.gender ?? null,
    status: child.status ?? null,
    generated_info_sheet: child.generated_info_sheet,
    sponsor_id: child.assigned_sponsor_id ?? null,
  }
}

async function fetchPool(): Promise<void> {
  const pool = await listPool(seasonName.value, poolParams())
  const wasActive = automaticRun.value.active
  items.value = pool.items ?? []
  total.value = pool.total
  automaticRun.value = pool.automatic_run ?? {
    active: false,
    job_id: null,
    status: null,
    started_at: null,
  }
  rememberCards(items.value)
  // The automatic run has just changed the pool: read it once more.
  if (wasActive && !automaticRun.value.active) {
    await fetchPool()
  }
}

async function loadSponsors(): Promise<void> {
  const result = await listSponsors(seasonName.value, { page_size: 200 })
  sponsors.value = result.items ?? []
}

async function loadReport(): Promise<void> {
  if (!seasonName.value) return
  try {
    reportCount.value = (await getMatchReport(seasonName.value)).length
  } catch {
    reportCount.value = null
  }
}

async function load(): Promise<void> {
  if (!seasonName.value) {
    error.value = 'No active season loaded.'
    return
  }
  loading.value = true
  error.value = null
  try {
    await fetchPool()
    await loadSponsors()
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : 'pool request failed'
  } finally {
    loading.value = false
  }
}

function submitSearch(): void {
  page.value = 1
  writeUrl()
}

function applyFilters(): void {
  page.value = 1
  writeUrl()
}

function setAssignment(next: AssignmentState | 'any'): void {
  assignment.value = next
  page.value = 1
  writeUrl()
}

function setHold(next: 'any' | 'held' | 'not_held'): void {
  hold.value = next
  page.value = 1
  writeUrl()
}

function go(next: number): void {
  page.value = Math.min(Math.max(1, next), totalPages.value)
  writeUrl()
}

/** Selection handlers: the set is replaced so the children re-render. */
function toggleChild(id: string): void {
  const next = new Set(selection.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  const whole = new Set(wholeFamilies.value)
  const familyId = childInfo.value.get(id)?.family_id
  if (familyId !== null && familyId !== undefined) whole.delete(familyId)
  wholeFamilies.value = whole
  selection.value = next
}

function selectFamily(familyId: number): void {
  const family = familyInfo.value.get(familyId)
  if (!family || family.selectable_ids.length === 0) return
  const next = new Set(selection.value)
  for (const id of family.selectable_ids) next.add(id)
  const whole = new Set(wholeFamilies.value)
  whole.add(familyId)
  wholeFamilies.value = whole
  selection.value = next
}

function clearSelection(): void {
  selection.value = new Set()
  wholeFamilies.value = new Set()
}

function openHold(): void {
  holdTargetId.value = holdFamilyId.value
  if (holdTargetId.value === null) return
  holdOpen.value = true
}

function resetWriteState(): void {
  actionError.value = null
  stalePrompt.value = false
  blockingJobId.value = null
  notice.value = null
  resultWarnings.value = []
}

/** Surface a write failure; a 412 asks for a reload instead of failing mute. */
function handleWriteError(failure: unknown): void {
  if (failure instanceof ApiError) {
    if (failure.status === 412) {
      stalePrompt.value = true
      actionError.value =
        'The pool changed while you were choosing — reload it and try again.'
      return
    }
    actionError.value = failure.message
    if (failure.status === 409) {
      const match = failure.message.match(/#(\d+)/)
      if (match) blockingJobId.value = Number(match[1])
    }
    return
  }
  actionError.value =
    failure instanceof Error ? failure.message : 'assignment request failed'
}

function onAssign(): void {
  if (!canAssign.value) return
  resetWriteState()
  if (needsReassignConfirm.value) {
    reassignOpen.value = true
    return
  }
  void submitAssign(false)
}

function expectedVersions(): Record<string, number> {
  const versions: Record<string, number> = {}
  for (const id of selection.value) {
    const info = childInfo.value.get(id)
    if (info) versions[`child:${id}`] = info.row_version
  }
  for (const familyId of wholeFamilies.value) {
    const family = familyInfo.value.get(familyId)
    if (family) versions[`family:${familyId}`] = family.row_version
  }
  if (sponsorId.value !== null && selectedSponsor.value) {
    versions[`sponsor:${sponsorId.value}`] =
      selectedSponsor.value.row_version
  }
  return versions
}

async function submitAssign(confirm: boolean): Promise<void> {
  reassignOpen.value = false
  const sponsor = selectedSponsor.value
  if (!sponsor || !seasonName.value) return
  const familyIds = [...wholeFamilies.value]
  const childIds = [...selection.value].filter((id) => {
    const familyId = childInfo.value.get(id)?.family_id
    return familyId === null || familyId === undefined
      ? true
      : !wholeFamilies.value.has(familyId)
  })
  pending.value = true
  try {
    const result = await assign(seasonName.value, {
      sponsor_id: sponsor.sponsor_id,
      family_ids: familyIds,
      child_ids: childIds,
      expected_versions: expectedVersions(),
      confirm,
      note: null,
    })
    lastPlacedSponsorId.value = sponsor.sponsor_id
    resultWarnings.value = result.warnings ?? []
    notice.value = `Assigned ${(result.assigned ?? []).length + (result.reassigned ?? []).length} children to ${sponsor.display_name}.`
    clearSelection()
    await load()
  } catch (failure) {
    handleWriteError(failure)
  } finally {
    pending.value = false
  }
}

async function submitRelease(): Promise<void> {
  releaseOpen.value = false
  if (!seasonName.value) return
  const childIds = assignedSelectedIds.value
  const versions: Record<string, number> = {}
  for (const id of childIds) {
    const info = childInfo.value.get(id)
    if (info) versions[`child:${id}`] = info.row_version
  }
  for (const familyId of wholeFamilies.value) {
    const family = familyInfo.value.get(familyId)
    if (family) versions[`family:${familyId}`] = family.row_version
  }
  pending.value = true
  try {
    const result = await release(seasonName.value, {
      child_ids: childIds,
      family_ids: [],
      expected_versions: versions,
      confirm: true,
      note: null,
    })
    resultWarnings.value = result.warnings ?? []
    notice.value = `Released ${(result.released ?? []).length} assignments.`
    clearSelection()
    await load()
  } catch (failure) {
    handleWriteError(failure)
  } finally {
    pending.value = false
  }
}

async function submitHold(note: string): Promise<void> {
  holdOpen.value = false
  const familyId = holdTargetId.value
  if (familyId === null || !seasonName.value) return
  pending.value = true
  try {
    await holdFamily(seasonName.value, { family_id: familyId, note })
    const next = new Set(selection.value)
    for (const id of [...next]) {
      if (childInfo.value.get(id)?.family_id === familyId) next.delete(id)
    }
    selection.value = next
    const whole = new Set(wholeFamilies.value)
    whole.delete(familyId)
    wholeFamilies.value = whole
    notice.value = `Family ${familyId} is held: ${note}`
    await load()
  } catch (failure) {
    handleWriteError(failure)
  } finally {
    pending.value = false
  }
}

async function onReleaseHold(familyId: number): Promise<void> {
  if (!seasonName.value) return
  resetWriteState()
  try {
    await releaseHold(seasonName.value, { family_id: familyId })
    notice.value = `Released the hold on family ${familyId}.`
    await load()
  } catch (failure) {
    handleWriteError(failure)
  }
}

/** Enqueue sheet generation scoped to the sponsor just placed (§6.12). */
async function regenerate(): Promise<void> {
  if (!seasonName.value || lastPlacedSponsorId.value === null) return
  resetWriteState()
  generating.value = true
  try {
    const settings = await getRunSettings('generate')
    const job = await enqueueJob(seasonName.value, 'generate', {
      ...(settings.run ?? {}),
      sponsor: lastPlacedSponsorId.value,
    })
    notice.value = `Generate job #${job.id} queued for sponsor ${lastPlacedSponsorId.value}.`
  } catch (failure) {
    handleWriteError(failure)
  } finally {
    generating.value = false
  }
}

async function reload(): Promise<void> {
  resetWriteState()
  await load()
}

watch(
  () => route.fullPath,
  () => {
    readUrl()
    void load()
  },
)

/**
 * The automatic run changes the pool while it runs, and the page must
 * notice when it finishes.  A 10s poll reads the pool; hidden documents
 * skip the work, the house's polling pattern.
 */
function startPolling(): void {
  stopPolling()
  pollTimer = setInterval(() => {
    if (document.hidden || !seasonName.value) return
    void fetchPool().catch(() => undefined)
  }, POLL_MS)
}

function stopPolling(): void {
  if (pollTimer !== null) clearInterval(pollTimer)
  pollTimer = null
}

onMounted(() => {
  readUrl()
  void load()
  void loadReport()
  startPolling()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<template>
  <section>
    <div class="status-line" style="justify-content: space-between">
      <h1 style="margin-top: 0">Manual matching</h1>
      <button
        type="button"
        data-testid="regenerate"
        :disabled="generating || lastPlacedSponsorId === null"
        :title="
          lastPlacedSponsorId === null
            ? 'Assign a family first; the run is scoped to the sponsor just placed.'
            : `Regenerate the sheets for sponsor ${lastPlacedSponsorId}`
        "
        @click="regenerate"
      >
        Regenerate this sponsor's sheets
      </button>
    </div>

    <p class="status-line muted" data-testid="last-run">
      <span>
        Last automatic run:
        {{
          reportCount === null
            ? 'no match report yet'
            : `${reportCount} report rows`
        }}
      </span>
      <RouterLink to="/match">Open the match report</RouterLink>
    </p>

    <form class="filters" @submit.prevent="submitSearch">
      <input
        v-model="q"
        type="search"
        name="q"
        placeholder="Search name, email, city, zip, child"
        aria-label="Search families"
      />
      <button type="submit">Search</button>
      <label class="muted">
        Gender
        <select v-model="childGender" aria-label="Child gender" @change="applyFilters">
          <option value="any">any</option>
          <option v-for="gender in GENDERS" :key="gender" :value="gender">
            {{ gender }}
          </option>
        </select>
      </label>
      <label class="muted">
        Age min
        <input v-model="ageMin" type="number" min="0" @change="applyFilters" />
      </label>
      <label class="muted">
        Age max
        <input v-model="ageMax" type="number" min="0" @change="applyFilters" />
      </label>
      <label class="muted">
        Special needs
        <select v-model="specialNeeds" @change="applyFilters">
          <option value="any">any</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
      </label>
      <label class="muted">
        Location
        <select v-model="location" @change="applyFilters">
          <option value="any">any</option>
          <option value="hancock">hancock</option>
          <option value="calumet">calumet</option>
        </select>
      </label>
      <label class="muted">
        Family size min
        <input
          v-model="familySizeMin"
          type="number"
          min="1"
          @change="applyFilters"
        />
      </label>
      <label class="muted">
        Family size max
        <input
          v-model="familySizeMax"
          type="number"
          min="1"
          @change="applyFilters"
        />
      </label>
      <label class="muted">
        Child status
        <select v-model="childStatus" @change="applyFilters">
          <option value="any">any</option>
          <option v-for="status in CHILD_STATUSES" :key="status" :value="status">
            {{ status.replaceAll('_', ' ') }}
          </option>
        </select>
      </label>
    </form>

    <div class="filters" role="group" aria-label="Assignment state">
      <span class="muted">Assignment</span>
      <button
        v-for="state in ASSIGNMENT_STATES"
        :key="state"
        type="button"
        class="chip"
        :aria-pressed="assignment === state"
        @click="setAssignment(state)"
      >
        {{ state }}
      </button>
    </div>

    <div class="filters" role="group" aria-label="Hold filter">
      <span class="muted">Hold</span>
      <button
        v-for="state in HOLD_STATES"
        :key="state"
        type="button"
        class="chip"
        :aria-pressed="hold === state"
        @click="setHold(state)"
      >
        {{ state.replaceAll('_', ' ') }}
      </button>
      <span class="muted" data-testid="pool-total">{{ total }} families</span>
    </div>

    <p v-if="loading" role="status">Loading the pool…</p>
    <p v-else-if="error" class="error-text" role="alert" data-testid="list-error">
      {{ error }} <button type="button" @click="load">Retry</button>
    </p>
    <p v-else-if="items.length === 0" class="muted" data-testid="list-empty">
      No families match these filters.
    </p>

    <div v-else class="grid" role="list">
      <FamilyCard
        v-for="card in items"
        :key="card.family_id"
        role="listitem"
        :card="card"
        :selected="selection"
        @toggle="toggleChild"
        @select-all="selectFamily"
        @release-hold="onReleaseHold"
      />
    </div>

    <div class="pagination">
      <button type="button" :disabled="page <= 1" @click="go(page - 1)">
        Previous
      </button>
      <span data-testid="pool-page">
        Page {{ page }} of {{ totalPages }} ({{ total }} total)
      </span>
      <button type="button" :disabled="page >= totalPages" @click="go(page + 1)">
        Next
      </button>
      <label class="muted">
        Page size
        <select v-model.number="pageSize" @change="applyFilters">
          <option :value="25">25</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
    </div>

    <footer class="selection-tray" data-testid="selection-tray">
      <div class="status-line" style="justify-content: space-between">
        <span role="status" data-testid="selection-count">
          {{ selection.size }} selected
        </span>
        <SponsorPicker
          v-model="sponsorId"
          :sponsors="sponsors"
          :disabled="automaticRun.active"
        />
      </div>

      <p
        v-if="automaticRun.active"
        class="notice"
        role="status"
        data-testid="automatic-running"
      >
        An automatic match is running (job #{{ automaticRun.job_id }}, status
        {{ automaticRun.status ?? 'running' }}); manual assignment is paused.
      </p>

      <p v-if="sponsorFull" class="error-text" data-testid="cap-warning">
        {{ capMessage }}
      </p>

      <p
        v-else-if="assignBlockedReason"
        class="muted"
        data-testid="assign-blocked"
      >
        {{ assignBlockedReason }}
      </p>

      <ul v-if="liveWarnings.length" class="live-warnings" data-testid="live-warnings">
        <li v-for="warning in liveWarnings" :key="warning">⚠ {{ warning }}</li>
      </ul>

      <div class="filters" style="margin-bottom: 0">
        <button
          type="button"
          class="primary"
          data-testid="assign"
          :disabled="!canAssign"
          @click="onAssign"
        >
          Assign {{ selection.size }} children
        </button>
        <button
          type="button"
          data-testid="release"
          :disabled="!canRelease"
          @click="releaseOpen = true"
        >
          Release {{ assignedSelectedIds.length }} assignments
        </button>
        <button
          type="button"
          data-testid="hold-family"
          :disabled="holdFamilyId === null || automaticRun.active"
          @click="openHold"
        >
          Hold family…
        </button>
        <button
          type="button"
          data-testid="clear-selection"
          :disabled="automaticRun.active || selection.size === 0"
          @click="clearSelection"
        >
          Clear selection
        </button>
      </div>

      <p v-if="stalePrompt" class="error-text" role="alert" data-testid="stale-prompt">
        {{ actionError }}
        <button type="button" @click="reload">Reload pool</button>
      </p>
      <p
        v-else-if="actionError"
        class="error-text"
        role="alert"
        data-testid="assign-error"
      >
        {{ actionError }}
        <RouterLink
          v-if="blockingJobId !== null"
          :to="`/jobs/${blockingJobId}`"
        >
          Open job #{{ blockingJobId }}
        </RouterLink>
      </p>
      <p v-if="notice" class="notice" role="status" data-testid="write-notice">
        {{ notice }}
      </p>
      <ul v-if="resultWarnings.length" class="live-warnings">
        <li v-for="warning in resultWarnings" :key="warning">⚠ {{ warning }}</li>
      </ul>
    </footer>

    <ConfirmDialog
      :open="reassignOpen"
      title="Reassign selected children?"
      message="Some selected children already have a different sponsor. Assigning moves them to the chosen sponsor."
      confirm-label="Reassign"
      dangerous
      @confirm="submitAssign(true)"
      @cancel="reassignOpen = false"
    />
    <ConfirmDialog
      :open="releaseOpen"
      title="Release assignments?"
      message="Releasing clears the selected children's sponsors. The family's sheets and packets become stale until regenerated."
      confirm-label="Release"
      dangerous
      @confirm="submitRelease"
      @cancel="releaseOpen = false"
    />
    <HoldDialog
      :open="holdOpen"
      :family-label="holdLabel"
      @confirm="submitHold"
      @cancel="holdOpen = false"
    />
  </section>
</template>

<style scoped>
.selection-tray {
  position: sticky;
  bottom: 0;
  z-index: 10;
  background: #fff;
  border-top: 2px solid #12324f;
  padding: 0.6rem 0.8rem;
  margin-top: 1rem;
  box-shadow: 0 -4px 16px rgba(10, 20, 30, 0.12);
}

.live-warnings {
  margin: 0.3rem 0;
  padding-left: 1.1rem;
  color: #9a5b12;
  max-height: 9rem;
  overflow-y: auto;
}
</style>
