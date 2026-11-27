// Pure relay logic for the dashboard channel (no deps, so tests/channel_relay.test.mjs can drive it).
//
// ORDER MATTERS: claim first, then notify. The inbox hook polls the same outbox; a row is relayed by
// whoever WINS the server's conditional claim (POST …/delivered → 200), so the channel and the hook
// never both deliver it. If the notification then fails, the claim is undone (…/undelivered) so the row
// is offered again on the next poll.

// Relayed text can never open or close a tag (</channel>, <system-reminder>, …). Idempotent: & is untouched.
export const esc = (s) => String(s ?? '').replace(/</g, '&lt;').replace(/>/g, '&gt;')

export function metaFor(it) {
  const kind = String(it.kind || 'task_update')
  const meta = { kind: /^[a-z][a-z0-9_]{0,39}$/.test(kind) ? kind : 'other' }
  if (Number.isInteger(it.task_id)) meta.task_id = String(it.task_id)
  return meta
}

// claim(id) -> true (won) | false (someone else has it: 409/404); throws on transport errors.
// notify(params) -> sends the channel notification; throws on failure.
// unclaim(id) -> best effort.
export async function relayItems(items, { claim, notify, unclaim }) {
  const relayed = []
  for (const it of items) {
    if (!(await claim(it.id))) continue
    try {
      await notify({ content: esc(it.message), meta: metaFor(it) })
      relayed.push(it.id)
    } catch {
      try { await unclaim(it.id) } catch { /* unclaim failed too: the row stays claimed (still visible on the task) */ }
    }
  }
  return relayed
}
