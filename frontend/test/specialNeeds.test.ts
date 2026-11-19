import { describe, expect, it } from 'vitest'

import {
  SPECIAL_NEEDS_OPTIONS,
  parseSpecialNeeds,
  serializeSpecialNeeds,
} from '../src/utils/specialNeeds'

describe('special needs serialization', () => {
  it('exposes the legacy fixed vocabulary', () => {
    expect(SPECIAL_NEEDS_OPTIONS).toEqual([
      'ADHD',
      'Autism',
      'Down Syndrome',
      'other',
    ])
  })

  it('parses fixed pieces and the other text', () => {
    const parsed = parseSpecialNeeds('ADHD; other: speech delay')
    expect([...parsed.fixed]).toEqual(['ADHD'])
    expect(parsed.other).toBe('speech delay')
    expect(parseSpecialNeeds(null)).toEqual({ fixed: new Set(), other: '' })
    expect(parseSpecialNeeds('')).toEqual({ fixed: new Set(), other: '' })
  })

  it('sorts fixed pieces and always puts other last', () => {
    expect(
      serializeSpecialNeeds({
        fixed: new Set(['Down Syndrome', 'ADHD']),
        other: '',
      }),
    ).toBe('ADHD; Down Syndrome')
    expect(
      serializeSpecialNeeds({
        fixed: new Set(['ADHD', 'other']),
        other: 'delay',
      }),
    ).toBe('ADHD; other: delay')
    expect(
      serializeSpecialNeeds({ fixed: new Set(['Autism', 'other']), other: '' }),
    ).toBe('Autism; other')
    expect(
      serializeSpecialNeeds({ fixed: new Set(['Zebra', 'other']), other: 'x' }),
    ).toBe('Zebra; other: x')
    expect(
      serializeSpecialNeeds({ fixed: new Set(), other: '' }),
    ).toBeNull()
  })

  it('keeps legacy semantics: other text alone does not select other', () => {
    expect(
      serializeSpecialNeeds(
        parseSpecialNeeds('ADHD; other: speech delay'),
      ),
    ).toBe('ADHD')
  })

  it('round-trips fixed-only values', () => {
    for (const text of ['ADHD', 'Autism; Down Syndrome']) {
      expect(
        serializeSpecialNeeds(parseSpecialNeeds(text)),
      ).toBe(text)
    }
  })
})
