import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'

// The owner's message went into a turn the owner did not start (`steerWithoutOwnerRights`,
// set by the gateway's steer path): the row says it runs there without the owner's rights.

const renderContent = (content: string) => <span data-testid="content">{content}</span>
// Said without naming crew=: on a patrol wake the operator let use crew=, crew= is not refused.
const LINE = "Went into a turn you didn't start, so it runs there without your rights. If a step there needs them, it runs again as your own turn."
const AGAIN = 'Your message again, now as your own turn: a step in the turn it went into needed your rights.'

function renderSteer(meta: Record<string, unknown>) {
  return render(<UserMessage content="Go." meta={meta} renderContent={renderContent} slotRunning />)
}

describe('a steer that went in without the owner\'s rights', () => {
  it.each([
    ['waiting to be read', 'written'],
    ['read by the turn', 'consumed'],
  ])('says so while %s', (_label, steerState) => {
    renderSteer({ steer: true, steerState, steerWithoutOwnerRights: true })

    expect(screen.getByTestId('steer-without-owner-rights').textContent?.trim()).toBe(LINE)
    expect(screen.getByTestId('content').textContent).toBe('Go.')
  })

  it.each([
    ['their own turn', { steer: true, steerState: 'consumed' }],
    ['a turn that ended before reading it', { steer: true, steerState: 'requeued', steerWithoutOwnerRights: true }],
    ['a row the next turn replaced', { steer: true, steerState: 'superseded', steerWithoutOwnerRights: true }],
    ['a plain send', { steerWithoutOwnerRights: true }],
  ])('says nothing for %s', (_label, meta) => {
    renderSteer(meta)

    expect(screen.queryByTestId('steer-without-owner-rights')).toBeNull()
  })
})

describe('the same message run again as the owner\'s own turn', () => {
  it('says it is that message again', () => {
    renderSteer({ ownerSteerAgain: 'mid-of-the-first-row' })

    expect(screen.getByTestId('owner-steer-again').textContent?.trim()).toBe(AGAIN)
    expect(screen.queryByTestId('steer-without-owner-rights')).toBeNull()
  })

  it.each([
    ['a plain send', {}],
    ['the first row, a steer', { steer: true, steerState: 'consumed', ownerSteerAgain: 'mid-1' }],
  ])('says nothing for %s', (_label, meta) => {
    renderSteer(meta)

    expect(screen.queryByTestId('owner-steer-again')).toBeNull()
  })
})
