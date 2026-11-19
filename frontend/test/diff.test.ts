import { describe, expect, it } from 'vitest'

import {
  buildPatch,
  changedFields,
  differs,
  normalizeValue,
  toAge,
} from '../src/utils/diff'
import { isStale, statusLabel } from '../src/utils/format'

describe('diff helpers', () => {
  it('treats blank and null as equal', () => {
    expect(differs(null, '')).toBe(false)
    expect(differs(' Ann ', 'Ann')).toBe(false)
    expect(differs('Ann', 'Anna')).toBe(true)
    expect(normalizeValue(null)).toBe('')
  })

  it('lists only changed fields against the baseline', () => {
    expect(
      changedFields(
        { first_name: 'Ann', dob: '2020-01-01', gender: 'F' },
        { first_name: 'Ann', dob: '2020-02-02', gender: 'F' },
        ['first_name', 'dob', 'gender'],
      ),
    ).toEqual(['dob'])
  })

  it('builds the tri-state patch from touched keys only', () => {
    const draft = { first_name: 'Ann', dob: '2020-01-01', gender: 'F' }
    const touched = new Set(['dob', 'gender'])
    expect(buildPatch(draft, touched)).toEqual({ dob: '2020-01-01', gender: 'F' })
    expect(buildPatch(draft, new Set())).toEqual({})
  })

  it('emits null for a cleared touched field', () => {
    expect(buildPatch({ email: '' }, new Set(['email']))).toEqual({
      email: null,
    })
  })

  it('converts age values to int or null', () => {
    expect(toAge('7')).toBe(7)
    expect(toAge('7.9')).toBe(7)
    expect(toAge('')).toBeNull()
    expect(toAge(null)).toBeNull()
    expect(toAge('n/a')).toBeNull()
  })

  it('flags stale revisions and labels statuses', () => {
    const now = new Date('2026-11-19T12:00:00Z')
    expect(isStale('2026-11-19T11:00:00Z', now)).toBe(false)
    expect(isStale('2026-11-01T11:00:00Z', now)).toBe(true)
    expect(isStale(null, now)).toBe(true)
    expect(statusLabel('needs_clarification')).toBe('needs clarification')
  })
})
