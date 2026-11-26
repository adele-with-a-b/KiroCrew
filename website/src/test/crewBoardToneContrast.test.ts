/**
 * The Board's status words and numbers (`.crew-tone-warn` / `-ok` / `-danger`
 * inside `.crew-board`) clear the 4.5:1 text floor on the Board's own card and
 * page in every built-in palette.
 *
 * The Board tints its surfaces (`--crew-board-page`, `--crew-board-card`) and
 * sets its own hue share (`--crew-tone-mix`). At 78% the words fell to 4.16:1 in
 * the default light palette and to 2.6 to 3.2:1 in several others. Every number
 * here is read from crew-mode.css, so a change to the rule is measured rather
 * than a copy of it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { contrastRatio } from '../lib/iconContrast'
import { THEMES, resolveVar } from './themePalette'
import { MIN_TEXT, lum, mix, tokenRgb, type Rgb } from './statusTextTint'

const CREW_CSS = readFileSync(resolve(__dirname, '../styles/crew-mode.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')

function share(re: RegExp, what: string): number {
  const m = re.exec(CREW_CSS)
  if (!m) throw new Error(`crew-mode.css lost ${what}`)
  return Number(m[1]) / 100
}

const TEXT_SHARE = share(/\.crew-board\s*\{[^{}]*--crew-tone-mix:\s*(\d+)%/, 'the Board\'s --crew-tone-mix')
const PAGE_AIM = share(/--crew-board-page:\s*color-mix\(in srgb,\s*var\(--aim\)\s*(\d+)%,\s*var\(--bg\)\)/, '--crew-board-page')
const CARD_AIM = share(/--crew-board-card:\s*color-mix\(in srgb,\s*var\(--aim\)\s*(\d+)%,\s*var\(--bg-elevated\)\)/, '--crew-board-card')

const strongest = (t: string): Rgb => tokenRgb(t, resolveVar(t, '--text-strong') ? '--text-strong' : '--text')

describe('Board status words in every built-in palette', () => {
  it('the tone rule is the one measured: the hue at --crew-tone-mix toward the strongest text', () => {
    for (const hue of ['warn', 'ok', 'danger']) {
      expect(CREW_CSS).toMatch(new RegExp(`\\.crew-tone-${hue}[^{]*\\{\\s*color:\\s*color-mix\\(in srgb,\\s*var\\(--${hue}\\)\\s*var\\(--crew-tone-mix,\\s*45%\\),\\s*var\\(--text-strong,\\s*var\\(--text\\)\\)\\);`))
    }
    expect(THEMES.length).toBeGreaterThan(30)
  })

  it('reach 4.5:1 on the Board card and page', () => {
    const under: string[] = []
    for (const t of THEMES) {
      const aim = tokenRgb(t, '--aim')
      const surfaces = { card: mix(aim, tokenRgb(t, '--bg-elevated'), CARD_AIM), page: mix(aim, tokenRgb(t, '--bg'), PAGE_AIM) }
      for (const hue of ['warn', 'ok', 'danger']) {
        const word = mix(tokenRgb(t, `--${hue}`), strongest(t), TEXT_SHARE)
        for (const [name, bg] of Object.entries(surfaces)) {
          const r = contrastRatio(lum(word), lum(bg))
          if (r < MIN_TEXT) under.push(`${t} ${hue} on ${name}: ${r.toFixed(2)}`)
        }
      }
    }
    expect(under).toEqual([])
  })
})
