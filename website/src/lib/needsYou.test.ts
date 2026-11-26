import { describe, it, expect } from 'vitest'
import { needsYouAge, needsYouOf, oldestWaitFirst, NEEDS_YOU_FIRST_WORDS_MAX, type NeedsYouRow } from './needsYou'

const row = (over: Partial<NeedsYouRow> = {}): NeedsYouRow => ({ key: 'k', running: false, ...over })

describe('needsYouOf — the gateway summary', () => {
  it('trusts a null summary over every flag: the chat waits on nobody', () => {
    expect(needsYouOf(row({ needs_you: null, pending_approval: true, needs_input: true }), 3)).toBeNull()
  })

  it('returns the summary, checked', () => {
    const words = 'x'.repeat(NEEDS_YOU_FIRST_WORDS_MAX + 20)
    const got = needsYouOf(row({
      needs_you: { count: 2.7, oldest_ts: 1_700_000_000, first_words: `  ${words}  `, row_id: 'm-1', kinds: ['labelled', 'bogus' as never, 'options'] },
    }))
    expect(got).toEqual({ count: 2, oldest_ts: 1_700_000_000, first_words: 'x'.repeat(NEEDS_YOU_FIRST_WORDS_MAX), row_id: 'm-1', kinds: ['labelled', 'options'] })
  })

  it('reads a summary with no open ask as not waiting', () => {
    expect(needsYouOf(row({ needs_you: { count: 0, oldest_ts: 1, first_words: 'a', row_id: null, kinds: ['question'] } }))).toBeNull()
    expect(needsYouOf(row({ needs_you: { count: Number.NaN, oldest_ts: 1, first_words: 'a', row_id: null, kinds: [] } }))).toBeNull()
  })

  it('drops an empty row id', () => {
    expect(needsYouOf(row({ needs_you: { count: 1, oldest_ts: 5, first_words: '', row_id: '', kinds: ['approval'] } }))?.row_id).toBeNull()
  })
})

describe('needsYouOf — the fallback, with no summary field', () => {
  it('waits on nothing for a plain idle row', () => {
    expect(needsYouOf(row({ last_ts: '2026-11-26T10:00:00Z' }))).toBeNull()
  })

  it('names one ask per flag, count 1, no words, no row', () => {
    const got = needsYouOf(row({ pending_approval: true, needs_input: true, last_turn_ts: '2026-11-26T10:00:00Z' }))
    expect(got).toEqual({ count: 1, oldest_ts: Date.parse('2026-11-26T10:00:00Z') / 1000, first_words: '', row_id: null, kinds: ['approval', 'question'] })
  })

  it('counts an [OPTIONS:] ending only while the chat is idle, dated by the ask', () => {
    const asks = row({ has_options: true, options: ['Ship it', 'Wait'], options_ts: '2026-11-26T11:40:02Z', last_ts: '2026-11-26T11:41:00Z' })
    expect(needsYouOf(asks)).toEqual({ count: 1, oldest_ts: Date.parse('2026-11-26T11:40:02Z') / 1000, first_words: '', row_id: null, kinds: ['options'] })
    expect(needsYouOf({ ...asks, running: true })).toBeNull()
    expect(needsYouOf({ ...asks, interrupted: true })).toBeNull()
    expect(needsYouOf({ ...asks, queue_depth: 1 })).toBeNull()
    expect(needsYouOf({ ...asks, options: [] })).toBeNull()
  })

  it('rolls crew approvals onto a Crew Mode root only', () => {
    expect(needsYouOf(row({ crew_mode_root: true }), 2)?.kinds).toEqual(['approval'])
    expect(needsYouOf(row(), 2)).toBeNull()
    expect(needsYouOf(row({ crew_mode_root: true }), 0)).toBeNull()
  })
})

describe('oldestWaitFirst', () => {
  it('orders by the oldest ask, unknown starts last in list order', () => {
    const rows = [
      row({ key: 'new', needs_you: { count: 1, oldest_ts: 300, first_words: '', row_id: null, kinds: [] } }),
      row({ key: 'unknown-a', needs_you: { count: 1, oldest_ts: 0, first_words: '', row_id: null, kinds: [] } }),
      row({ key: 'old', needs_you: { count: 1, oldest_ts: 100, first_words: '', row_id: null, kinds: [] } }),
      row({ key: 'unknown-b', needs_you: { count: 1, oldest_ts: 0, first_words: '', row_id: null, kinds: [] } }),
    ]
    expect(oldestWaitFirst(rows, r => needsYouOf(r)).map(r => r.key)).toEqual(['old', 'new', 'unknown-a', 'unknown-b'])
  })
})

describe('needsYouAge', () => {
  const now = Date.parse('2026-11-26T12:00:00Z')
  it('reads minutes, then hours and minutes, never under a minute', () => {
    expect(needsYouAge(now / 1000 - 10, now)).toBe('1m')
    expect(needsYouAge(now / 1000 - 34 * 60, now)).toBe('34m')
    expect(needsYouAge(now / 1000 - 125 * 60, now)).toBe('2h 5m')
  })
  it('says nothing when the start is not known', () => {
    expect(needsYouAge(0, now)).toBe('')
  })
})
