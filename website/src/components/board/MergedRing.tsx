/** The small pie before "N of M stories merged": the merged share filled in the
 *  theme's green over a quiet track. A data mark drawn from the two counts, not an
 *  icon, so it is an SVG of its own; the words beside it say the same numbers, so it
 *  is hidden from screen readers. */
const R = 3
const C = 2 * Math.PI * R

export default function MergedRing({ done, total }: { done: number; total: number }) {
  const share = total > 0 ? Math.min(1, Math.max(0, done / total)) : 0
  return (
    <svg viewBox="0 0 12 12" width={11} height={11} aria-hidden="true" className="shrink-0 self-center" data-testid="board-merged-ring" data-share={share.toFixed(2)}>
      <circle cx={6} cy={6} r={R} fill="none" strokeWidth={2 * R} className="crew-board-ring-track" />
      {share > 0 && (
        <circle cx={6} cy={6} r={R} fill="none" strokeWidth={2 * R} className="crew-board-ring-done"
          strokeDasharray={`${share * C} ${C}`} transform="rotate(-90 6 6)" />
      )}
    </svg>
  )
}
