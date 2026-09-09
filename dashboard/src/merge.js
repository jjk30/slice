// Merging a fresh /dashboard/teams payload over the one on screen.
//
// A live event or a Settings save refetches the payload. Should a refetch ever answer
// with fewer fields than the first load (a partial patch, a server that trimmed a
// field), keeping the previous value for anything the update leaves out means a panel
// line that depends on that field (the budget note reads budget_source and spend_usd)
// does not vanish until the next reload. A field the update carries always wins, so a
// real change is never hidden; only an absent key falls back.
export function mergeTeams(previous, update) {
  if (!previous) return update ?? null
  if (!update) return previous
  const merged = { ...previous, ...update }
  if (previous.budget && update.budget) merged.budget = { ...previous.budget, ...update.budget }
  else if (previous.budget && !('budget' in update)) merged.budget = previous.budget
  return merged
}
