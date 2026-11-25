# Remote dispatch (preview)

Read this only while `work_ledger.remote_dispatch` is on: the `crew` and
`work_item` arguments of the session tools exist only then and point here.
With it off, none of this applies and the gateway refuses `crew=`
(`remote_dispatch_disabled`).

With it on, you can put a leaf item's worker on a connected crew (another
machine). The session lives there, shows in that crew's sidebar group, and runs
under that crew's own approvals and credentials. The crew must have the switch
on too and run the same major.minor version; `agent.session_control` must be on
here, as for every session tool.

**Remote workers are visible sessions.** Dispatch a worker on another machine as
its own session there, never as hidden subagents of a session on that machine.
Only a visible session can be read, steered, stopped and patrolled. A hidden
subagent has no item to report on, so its work never reaches the ledger.

**Conductor mode owner turns only.** Every `crew=` call, and every placed create,
is admitted only in a turn the owner typed in their own persistent conductor mode
chat. Anything else is refused (`not_user_turn`, `owner_only`, `conductor_mode_only`,
`crew_owner_unverifiable`, `crew_caller_is_worker` and others): a plain chat on
any other agent, a patrol wake, a `session_send` turn, a cron, and a conductor
another session created. `work_ledger_read` and
`action=decide` need no owner turn.

**So on a patrol wake** you may read, verify, `accept`, `close` and `decide` a
remote item, but not `session_send`, `session_read_message`, `session_stop` or
`session_close` it. A `decide` reaches the worker's crew, but an idle worker
reads it only at its next `work_brief`; nothing wakes it. When a remote worker
needs a message, record the `decide`, tell the user, and send it in the next
owner turn. Skip a remote item in the patrol drift check for the same reason.

**Unless the owner turned on `work_ledger.remote_dispatch_patrol_wake`.** Then a
wake of the default patrol (the one the gateway armed when you dispatched) may
use `crew=` like an owner turn: create, send, read, stop and close. Try the
call; a refusal `crew_owner_unverifiable` means one of: the setting is off;
this wake is not that patrol; the owner has not yet dispatched from this chat
since the patrol was armed; or another message (a worker's report, a note)
joined this wake. In the last case the next wake can try again. A loop you
armed yourself with your own text does not count, so keep the default patrol.
A cron, a `session_send` turn and a worker are refused either way.

**Dispatch a remote item** (`work_dispatch` is local only):

1. `work_ledger_record` `action=create`, as for a local item.
2. `session_create` with `work_item=<item_id>`, `title` and
   `agent="kirocrew-worker"`, `agent_kind="template"` (optionally `model`). A
   crew create refuses `folder`. The session is created already bound, so **skip
   `bind`**. Name a crew with `crew=`, or omit it and the hub picks the
   connected crew with the most headroom after its own reservations; the reply
   then says which crew, and you pass that crew's id as `crew=` on every later
   call.
3. `session_send` the seed with `crew=` and the returned key as `target`. The
   worker is on another machine and cannot see your checkout: the seed names
   the repo URL and the branch to push.

The crew create reply does not show the `patrol=` outcome; check with
`monitor_inspect` and arm the loop as Patrol says if none runs.

**No room, or no answer: run it here.** A named crew short on memory, CPU or
disk answers `crew_busy` and creates nothing. With no `crew=`, placement skips
crews that are busy, unreachable, on another version or have the switch off,
and answers `crew_none_available` when none admits the item. A crew that is not
connected answers `proxy_peer_not_connected`, and one whose version cannot be
read (its Crew is stopped, or it refused the read), `crew_version_unknown`. In all four the item stays
open and unbound, and the reply says so: run it on this machine in the same
turn (`session_create` without `crew` or `work_item`, `work_ledger_record`
`action=bind`, then `session_send` the seed, since `work_dispatch` would make a
second item), and tell the user where it ran. Do not wait for the crew. Any other refusal may have reached the crew, so do not dispatch a
second worker for it; a create refused `crew_peer_lacks_work_items`, or one
that keeps failing, goes to the user.

**Driving it.** `session_send`, `session_read_message`, `session_stop` and
`session_close` take the same `crew=` and reach only a crew session you created
or one an open item of yours is bound to. After a hub restart only the open
item counts, so close the worker's session before you `close` its item. A
`decide` whose forward fails says it "did NOT reach the worker's crew"; record
it again to retry. A remote row adds `worker_instance_id`, `crew_state`,
`crew_down_since`, `pending_approval`, `remote_error` and
`remote_reports_skipped`. While remote dispatch is on, every bound row carries
`runs_on`: the crew's name for a remote worker, `local` for one on this machine.

**Reports reach you by a pull.** The hub pulls every report the worker filed
since the last pull from each open remote item's crew, in order, and files each
as an ordinary report, so
`accept_eval.py`, `accept` and `close` work as for a local item. A background
puller does this every 30 seconds with no model turn, so a remote `done`,
`blocked`, `question` or `reply` wakes your work-ledger patrol like a local
one; a `work_ledger_read` pulls too. Each pull is best effort (a read takes at
most 32 items in about 6 seconds), and a row whose pull failed keeps its last
state until the next pull files what it missed. `remote_reports_skipped` counts
places in the worker's reports the hub could not file (a report it can never
file, or a worker log that rolled past the last pull); above 0, read the
worker's session for what you missed.

**Follow-ups use `reply`.** To ask a finished worker something, keep its item
open, `session_send` the question in an owner turn, and tell it to answer with
`work_report status=reply`. A `reply` wakes you once pulled and claims nothing,
so it needs no acceptance: relay its summary to the worker that asked. A
`reply` replaces the item's status like any report, so verify a `done` before
you send its worker a follow-up.

**A lost crew.** A crew counts as down when its tunnel is, and also when the
tunnel is up but nothing answers behind it (`crew_down_since` set, with
`remote_error` `crew_version_unknown`; the same error with no `crew_down_since`
means the crew answered and refused the read, which is not an outage). When an item's crew has been seen down longer than
`work_ledger.remote_dispatch_retire_minutes` (default 30), the next pull
retires that binding: the item is open and unbound again, the row shows
`redispatch_due` and `retired_crews`, and any later report from the retired
worker is ignored, `done` included. The clock counts only while this hub is
running and sees the crew down, so a restart starts it again. Redispatch in an
owner turn with `session_create(work_item=)` and no `crew=`: placement skips
retired crews, and its reply names the old worker's `previous_branch` when that
worker reported one. Seed the new worker to start from that branch, if any, and
push a NEW one. An explicit `crew=` create may still name a retired crew and
returns no branch. The retired session can only be closed. Once its crew is
back, a pull tries once to close it; if that fails, `session_close(crew=)` it
in an owner turn. An item keeps at most 8 retired sessions not yet closed; a
retire past that is refused `retired_history_full` and the binding stays.
