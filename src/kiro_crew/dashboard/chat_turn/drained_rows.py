"""The rows a drained queue turn writes: each consumed entry's ``queue_pop`` frame and
the transcript row the turn opens on (``_write_drained_row``). A drain writes one row for
the entries it consumed; an agent run (``_dequeue_agent_run``) writes one per entry, so
each note keeps its own sender card and drop-notice address."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from kiro_crew.dashboard.channel_prompt import PROMPT_ON_CHANNEL_META_KEY
from kiro_crew.dashboard.chat_delivery import (
    START_BACKGROUND_META_KEY,
    attachment_meta,
    banner_meta,
    queue_entry_is_user_origin,
    queued_text_for_display,
    quote_meta,
)
from kiro_crew.dashboard.chat_title import RESTORED_TURN_META_KEY
from kiro_crew.dashboard.chat_utils import (
    COMPOSER_SEND_META,
    MCP_APP_MESSAGE_KIND,
    STEER_POSSIBLY_DELIVERED_META,
    _remove_queued_by_id,
    app_inject_row,
)
from kiro_crew.dashboard.chat_utils_injected_rows import (
    AUTOMATION_STEER_META,
    REQUEUED_STEER_ROW_MID_META,
)
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY
from kiro_crew.dashboard.state import CRON_NOTIFY_RE
from kiro_crew.history import HUMAN_TURN_META_KEY

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        COMMANDS_OFF_META_KEY,
        CRON_NOTIFICATION_KIND,
        SUBAGENT_COMPLETION_KIND,
        DashboardState,
        _ChatSlot,
        append_and_surface,
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.dashboard.chat_turn.recipient import MIRROR_ECHO_OWED_META_KEY
    from kiro_crew.dashboard.chat_turn.superseded_steer import (
        _carry_superseded_steer_meta,
        _requeued_steer_row,
        _retire_superseded_steer_row,
    )


def _automation_nudge(items: list[dict]) -> dict | None:
    """The ``meta.nudge`` of the first requeued automation steer in *items*, or None.

    A requeued automation steer (a work-ledger wake the turn never took): its entry
    names the ``meta.nudge`` its row carries, so it drains as the same nudge row it
    was steered as, never as the person's ``user`` row. A merge never takes it
    (``_dequeue_next_message`` stops at it); an agent run writes its row alone.
    """
    return next(
        (
            (item.get("meta") or {}).get(AUTOMATION_STEER_META)
            for item in items
            if isinstance((item.get("meta") or {}).get(AUTOMATION_STEER_META), dict)
        ),
        None,
    )


def _write_drained_row(
    state: DashboardState,
    slot: _ChatSlot,
    consumed: list[dict],
    next_msg: str,
    *,
    deliver_as_typed: bool,
    directive_user_origin: bool,
    synthetic_payload: bool,
    is_cron: bool,
    is_subagent: bool,
    is_recovery: bool,
    is_app_message: bool,
    is_automation_notice: bool,
    gateway_inject_kind: str | None,
) -> tuple[dict, bool, bool]:
    """Pop *consumed* off the clients' queue and write the row *next_msg* opens on.

    The flags are the drain's classification of the turn. An agent run calls this
    once per entry with the run's flags, and every one of them is False there: no
    kinded entry, recovery or app message joins a run (``_is_agent_row``). Returns
    the written (or reused) row, whether a consumed steer may already have been
    delivered, and whether any consumed entry keeps its text a message rather than
    a command.
    """
    # Owner code runs on ``chat_runner``'s globals, so a module it reads is imported here.
    from kiro_crew.dashboard import responsive_chat

    is_gateway_prompt = gateway_inject_kind is not None
    automation_nudge = _automation_nudge(consumed)
    is_automation_steer = automation_nudge is not None
    # A steer the RPC accepted already has its transcript row; the turn that was
    # meant to take it ended first, so the requeue marked that row `requeued` and
    # named it on the entry. This turn still writes its own row below, AFTER the
    # stop card, so turn grouping and the interrupted/stopped tail walks see the
    # same shape as any other queued send. The old row is retired once that row
    # lands, so the text shows once. Only when the row stands for this ONE entry
    # (drained alone, or as its own row in an agent run) and the old
    # row still holds exactly the text this turn runs: an edited card, a merge
    # with later sends, or a redaction that changed the text keep both rows,
    # because then the old row holds different text from what runs. And only when
    # this drain writes a row of the old row's own role: a `user` row for the
    # person's or a peer's steer, a `nudge` row for an automation steer (a
    # work-ledger wake). Typed text that opens with a cron or sub-agent prefix
    # drains as an event row, and hiding the old row would leave no copy drawn as
    # the person's own words.
    _drains_as_user = not (
        is_cron
        or is_subagent
        or is_recovery
        or is_app_message
        or is_automation_notice
        or is_automation_steer
        or is_gateway_prompt
    )
    superseded_steer_row = (
        _requeued_steer_row(slot, consumed, next_msg, "nudge" if is_automation_steer else "user")
        if _drains_as_user or is_automation_steer
        else None
    )

    for item in consumed:
        content, _ = redact_exfiltration_urls(item["content"])
        content, _ = redact_credentials(content)
        # The client rebuilds this entry as a user row from the frame alone (no
        # `chat_message` echo follows for a user row), so the attachment lists
        # the entry carries travel with it -- without them the rebuilt row
        # resolves `[attached_file N]` markers by whitespace and a spaced path
        # is truncated until the next reload. The text follows the row this
        # drain writes from `next_msg`: as typed when this is the session's own
        # human's send (`deliver_as_typed`, matching the pending card and the
        # row/input the turn receives), redacted otherwise.
        _pop: dict = {
            "slot": slot.key,
            "content": queued_text_for_display(
                item["content"] if deliver_as_typed else content,
                user_origin=deliver_as_typed,
            ),
            "queue_id": item["id"],
            # The DRAIN's own verdict rides the pop: True when this drain
            # writes its own row for the turn these entries become (inject /
            # subagent), so the reducer must NOT rebuild the popped entry as
            # a user row — doing so shows the text twice, once attributed to
            # the human. Computed here from the same classification the row
            # write uses, so the client cannot drift from the server (an
            # EDITED cron card whose text lacks the cron prefix drains as
            # a real user row, and this flag correctly says "rebuild").
            "drain_writes_row": is_cron
            or is_subagent
            or is_recovery
            or is_app_message
            or is_automation_notice
            or is_automation_steer
            or is_gateway_prompt
            or responsive_chat.reusable_row(slot, consumed, next_msg) is not None,
        }
        # The client rebuilds the drained entry as a user row from this frame
        # alone (no `chat_message` echo follows for a user row), so the lists
        # AND the quote ride it: without the quote the rebuilt row shows the
        # blockquote as text until a reload.
        _pop_meta = {
            **attachment_meta(item.get("meta")),
            **quote_meta(item.get("meta"), user_origin=queue_entry_is_user_origin(item)),
            **banner_meta(item.get("meta")),
        }
        from kiro_crew.dashboard.chat_delivery import normalize_send_id

        _pop_send_id = normalize_send_id((item.get("meta") or {}).get("sendId"))
        if _pop_send_id:
            _pop_meta["sendId"] = _pop_send_id
        # A peer's queued ``session_send`` names its sender (``meta.relay``, set
        # only by the gateway's delivery path; an edit drops it). The rebuilt row
        # is that one entry's text, so its own sender rides its own pop, and the
        # live row draws the same incoming card a reload does. Rebuilt, never
        # copied: an entry restored from disk is data (``relay_spans.relay_for``).
        _pop_relay = (item.get("meta") or {}).get("relay")
        if _pop_relay is not None:
            from kiro_crew.dashboard import relay_spans
            from kiro_crew.dashboard.session_control import _SEND_PROVENANCE

            _pop_relay_record = relay_spans.relay_for(
                str(_pop.get("content") or ""), [_pop_relay], _SEND_PROVENANCE
            )
            if _pop_relay_record is not None:
                _pop_meta["relay"] = _pop_relay_record
        if superseded_steer_row is not None:
            # The rebuilt row must carry these before the drain hides the old row.
            _carry_superseded_steer_meta(superseded_steer_row, _pop_meta)
        if _pop_meta:
            _pop["meta"] = _pop_meta
        state.broadcast_ws("queue_pop", _pop)
        _remove_queued_by_id(slot.messages, item["id"])

    match = CRON_NOTIFY_RE.match(next_msg) if is_cron else None
    cron_label = match.group(1) if match else "cron"
    cron_label, _ = redact_exfiltration_urls(cron_label)
    cron_label, _ = redact_credentials(cron_label)
    # The app label for a drained MCP-App message: read from the meta the
    # producer stamped on the entry — the banner is written in one place and
    # parsed in none (display only; classification stayed structural above).
    app_label = ""
    if is_app_message:
        for item in consumed:
            if item.get("kind") == MCP_APP_MESSAGE_KIND:
                _lbl = (item.get("meta") or {}).get("appLabel")
                if isinstance(_lbl, str):
                    app_label = _lbl
                break
        app_label, _ = redact_exfiltration_urls(app_label)
        app_label, _ = redact_credentials(app_label)
    if is_automation_steer:
        row_role = "nudge"
    elif is_subagent:
        row_role = "subagent"
    elif is_cron or is_recovery or is_app_message or is_automation_notice or is_gateway_prompt:
        row_role = "inject"
    else:
        row_role = "user"
    if is_automation_steer:
        row_cls = "msg msg-nudge"
    elif is_cron:
        # A cron row's `cls` slot carries a JSON payload, not a CSS class name:
        # `cronLabel` is structured data the frontend reads off the row.
        row_cls = json.dumps({"cronLabel": cron_label})
    elif is_app_message:
        # The ONE row shape both delivery paths share (`app_inject_row`):
        # plain CSS cls, identity in meta — a JSON cls would make
        # `_prepare_messages` replace the stored meta on HTTP rebuild,
        # dropping `injectKind` and the row's collapse/fold exemptions.
        row_cls = app_inject_row(app_label or "app")[1]
    elif is_recovery or is_automation_notice or is_gateway_prompt:
        row_cls = "msg msg-inject"
    else:
        row_cls = "msg msg-u"
    # Provenance a producer attached to the queue entry belongs on the row the
    # drain writes, not only on the entry that is about to disappear.
    _drained_meta: dict = {}
    # Delivery ids ACCUMULATE; everything else is last-writer-wins. A merge folds
    # several queued messages into one row, and each may carry its own steer's id —
    # a plain `update` would keep only the last, and every other caller would see
    # no row for its delivery and append a duplicate. The row stands for all of
    # them, so it has to name all of them.
    #
    # Accumulating generally does not undo the narrow rule above: per-entry
    # subagent facts would be meaningless on a merged row, but a subagent
    # completion never merges (it drains alone and breaks any user-message
    # merge), so a merged row cannot carry them in the first place.
    _drained_ids: list[str] = []
    # Client send-correlation ids accumulate for the same reason: a merged row
    # stands for every queued send folded into it, and a sender proving its own
    # delivery by identity must be able to find its id on that row even when it
    # was not the last writer. The single-entry case (the overwhelmingly common
    # one) keeps the plain-send shape -- `sendId` alone -- so a client reading
    # only that key sees exactly what a dispatched send's row carries.
    _drained_send_ids: list[str] = []
    # Spec decision delivery ids accumulate for the same reason: a merged row
    # stands for every queued decision answer folded into it, and the spec
    # builder's outbox proves an answer relayed by finding a row carrying its id.
    # A plain union would keep only the last, so every earlier answer's outbox
    # record would see no row and redispatch it. The single-entry case keeps the
    # plain scalar so a reader of only that key is unchanged.
    _drained_decision_ids: list[str] = []
    # Owner code runs on ``chat_runner``'s globals, so a module it reads is imported here.
    from kiro_crew.dashboard import queue_age

    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        CHANNEL_RECIPIENT_META_KEY,
        QUEUED_CONTAINMENT_META_KEY,
        SEND_ORIGIN_META_KEY,
        SESSION_SEND_QUEUED_META,
        audit_queued_allow,
    )

    # The ALLOW side of the drain's permission decision: these entries
    # passed re-validation and are now becoming a turn. Audited at consumption —
    # not per sweep pass — so an entry that waits across several drains yields
    # one row when it actually executes. Exempt kinds were never subject to the
    # decision, so they are not counted as one.
    _revalidated_ids = [
        item["id"]
        for item in consumed
        if item.get("kind") not in (CRON_NOTIFICATION_KIND, SUBAGENT_COMPLETION_KIND)
    ]
    if _revalidated_ids:
        audit_queued_allow(slot, _revalidated_ids)

    for item in consumed:
        _item_meta = item.get("meta")
        if isinstance(_item_meta, dict):
            _one = _item_meta.get("steer_delivery_id")
            if isinstance(_one, str) and _one:
                _drained_ids.append(_one)
            _many = _item_meta.get("steer_delivery_ids")
            if isinstance(_many, list):
                _drained_ids.extend(x for x in _many if isinstance(x, str) and x)
            _sid = _item_meta.get("sendId")
            if isinstance(_sid, str) and _sid and _sid not in _drained_send_ids:
                _drained_send_ids.append(_sid)
            _did = _item_meta.get("spec_decision_delivery_id")
            if isinstance(_did, str) and _did and _did not in _drained_decision_ids:
                _drained_decision_ids.append(_did)
            _dids = _item_meta.get("spec_decision_delivery_ids")
            if isinstance(_dids, list):
                _drained_decision_ids.extend(
                    x for x in _dids if isinstance(x, str) and x and x not in _drained_decision_ids
                )
            # The admission-time containment snapshot is queue plumbing,
            # consumed by _drop_stale_admissions above; it says nothing about the
            # ROW, so it must not ride into the persisted transcript meta.
            _drained_meta.update(
                (k, v) for k, v in _item_meta.items() if k != QUEUED_CONTAINMENT_META_KEY
            )

    # A drop-notice address names the one sender whose text the notice quotes.
    # A merged row's text is every entry's, so the row keeps an address only
    # when every consumed entry carries that same address; otherwise the union's
    # last writer would be quoted another sender's words. A code channel's
    # Slack sender (``slack_sender``) names the person whose words the row
    # holds, so it follows the same rule.
    # ``relay`` (the cross-session sender for the incoming card) is a per-sender
    # address like the ones beside it: a merged row of different senders' queued
    # sends must not wear one sender's card, so it is dropped unless every entry
    # agrees. It is compared by its SENDER alone: one session's ``session_send``
    # and its ``session_broadcast`` differ only in the verb and are still that
    # sender's.
    def _address_of(item: dict, key: str) -> object:
        meta = item.get("meta")
        value = meta.get(key) if isinstance(meta, dict) else None
        if key == "relay":
            return value.get("senderKey") if isinstance(value, dict) else None
        return value

    for _address_key in (
        CHANNEL_RECIPIENT_META_KEY,
        SEND_ORIGIN_META_KEY,
        "author",
        "slack_sender",
        "relay",
    ):
        _addresses = [_address_of(item, _address_key) for item in consumed]
        if any(_address != _addresses[0] for _address in _addresses):
            _drained_meta.pop(_address_key, None)
    if "relay" in _drained_meta:
        # Every consumed entry is the same sender's send (above), so the row is
        # their texts: rebuild its relay with one envelope span per send, so the
        # bubble drops each provenance line by position (``relay_spans``).
        from kiro_crew.dashboard import relay_spans
        from kiro_crew.dashboard.session_control import _SEND_PROVENANCE

        _row_relay = relay_spans.relay_for(
            next_msg,
            [(item.get("meta") or {}).get("relay") for item in consumed],
            _SEND_PROVENANCE,
        )
        if _row_relay is None:
            _drained_meta.pop("relay", None)
        else:
            _drained_meta["relay"] = _row_relay
    # Model input only: the row keeps the user's text as typed.
    _possibly_delivered_steer = bool(_drained_meta.pop(STEER_POSSIBLY_DELIVERED_META, False))
    # Queue plumbing for the sub-agent hold only; says nothing about the row.
    _drained_meta.pop(COMPOSER_SEND_META, None)
    # Queue plumbing too: the row says it as `meta.nudge`, the key the chip reads.
    _drained_meta.pop(AUTOMATION_STEER_META, None)
    if automation_nudge is not None:
        _drained_meta["nudge"] = dict(automation_nudge)
    # Read off the consumed entries (``_batch_recovery_echo_owed``); the merged
    # row's union says nothing about each entry, so it does not ride along.
    _drained_meta.pop(MIRROR_ECHO_OWED_META_KEY, None)
    # Read off the consumed entries too (``batch_prompt_on_channel``).
    _drained_meta.pop(PROMPT_ON_CHANNEL_META_KEY, None)
    # Queue plumbing: read above to pick the row, never part of a row's meta.
    _drained_meta.pop(REQUEUED_STEER_ROW_MID_META, None)
    # Queue plumbing for ``session_queue``'s ownership read; never row meta.
    _drained_meta.pop(SESSION_SEND_QUEUED_META, None)
    # Queue plumbing for the unread wake (``queue_age``); once drained it was read.
    _drained_meta.pop(queue_age.STEER_WANTED_META, None)
    # Queue plumbing like the steer mark above, read the way the channel origin is:
    # ANY consumed entry a channel hand-off stamped keeps the merged text content,
    # so a batch cannot smuggle a channel's "/clear" into a command by sitting
    # behind a dashboard entry. Popped so it does not ride into the row's meta.
    _commands_off = bool(_drained_meta.pop(COMMANDS_OFF_META_KEY, False)) or any(
        (item.get("meta") or {}).get(COMMANDS_OFF_META_KEY) is True for item in consumed
    )
    # Start plumbing too, read per entry below; it says nothing about the row.
    _drained_meta.pop(START_BACKGROUND_META_KEY, None)
    if "quote" in _drained_meta:
        # The row's quote record follows the row's text: as typed only when
        # every consumed entry is the human's own (`deliver_as_typed`), else
        # redacted -- a restored entry carries no origin stamp and the user-row
        # emit path keeps `meta.quote` as stored, so this is where its form
        # is decided. A record the bound refuses is dropped whole.
        _drained_meta.update(
            quote_meta({"quote": _drained_meta.pop("quote")}, user_origin=deliver_as_typed)
        )
    if superseded_steer_row is not None:
        # The old row is about to be hidden, so the row that stays drawn takes its
        # carried meta. The quote is already in its stored form for this text.
        _carry_superseded_steer_meta(superseded_steer_row, _drained_meta)
    if _drained_ids:
        _drained_meta.pop("steer_delivery_id", None)
        _drained_meta["steer_delivery_ids"] = _drained_ids
    if len(_drained_send_ids) > 1:
        # A merged row: `sendId` stays as the union left it (last writer) so the
        # key is never absent when any entry had one, and `sendIds` names every
        # send the row stands for. Membership in `sendIds` is the proof a client
        # should read on a merged row; on a plain row `sendId` is the whole story.
        _drained_meta["sendIds"] = _drained_send_ids
    if len(_drained_decision_ids) > 1:
        # A merged row: `spec_decision_delivery_id` stays as the union left it
        # (last writer) and `spec_decision_delivery_ids` names every decision
        # answer the row stands for, so the outbox can find a row for each id.
        # A single answer keeps the plain scalar (no list), unchanged.
        _drained_meta["spec_decision_delivery_ids"] = _drained_decision_ids
    # Durable provenance for every `inject` row. `cls` is NOT persisted for this
    # role (chat_persistence only keeps it for `role == "system"`), and the
    # frontend's `meta.cronLabel` exists on the wire only because parse_cls_meta
    # synthesizes it at emit time — so anything keyed on it silently disappears
    # after a flush + rehydrate. `meta` IS persisted and restored, so the render
    # side can ask what a row IS instead of guessing from what its text is not.
    #
    # The recovery split matters: build_recovery_requeue replays the USER'S OWN
    # message verbatim when the turn emitted nothing, and that row must keep
    # rendering as speech. `synthetic_payload` is the existing answer to exactly
    # that question, so reuse it rather than inventing a second signal.
    #
    # Folded into the drained meta rather than a separate `_row_meta`: this drain
    # now unions the meta of EVERY consumed entry (a merged row names all of its
    # steer delivery ids), so the drained mapping is the one the row write reads.
    # An `inject` row and a `subagent` row are mutually exclusive by `row_role`,
    # so these two provenance blocks can never both fire on one row.
    if row_role == "inject":
        if is_cron:
            _inject_kind = "cron"
        elif is_app_message:
            # Before the synthetic_payload arm: an app entry IS a synthetic
            # payload (that is what suppresses the channel mirror), but its
            # inject identity is its own, matching the direct-dispatch twin.
            _inject_kind = "mcp_app"
        elif is_automation_notice:
            _inject_kind = "automation_notice"
        elif gateway_inject_kind is not None:
            _inject_kind = gateway_inject_kind
        elif synthetic_payload:
            _inject_kind = "recovery"
        else:
            _inject_kind = "user_replay"
        _inject_meta: dict = {"injectKind": _inject_kind}
        if is_cron:
            _inject_meta["cronLabel"] = cron_label
        elif is_app_message:
            _inject_meta["appLabel"] = app_label or "app"
        _drained_meta.update(_inject_meta)
    elif row_role == "user" and directive_user_origin:
        # The direct-send path stamps HUMAN_TURN_META_KEY on a user row whenever
        # the send is human-authored (chat_handlers: ``if not request_app``). A
        # message that arrives while the slot is busy is QUEUED and later drained
        # here instead, so without this it would write an UNMARKED user row and
        # the last-human-turn ranking gate (chat_persistence, keyed on this
        # marker) would skip it, staling ``last_user_at`` for a genuine human
        # turn. ``directive_user_origin`` is that same ``not request_app`` signal
        # carried on the queue entry (see ``_directive_user_origin``), so stamp
        # it here on exactly the human-origin drain. It fails closed: a drain
        # with any non-human (app) entry, or the ``inject`` branch above, never
        # reaches here and stays unmarked.
        _drained_meta[HUMAN_TURN_META_KEY] = True
    if row_role == "user" and any(item.get(RESTORED_QUEUE_KEY) for item in consumed):
        # This is a loss-of-provenance marker, never authority recovered from disk.
        # Persist it on the row so auto-titling and refresh rebase both exclude it.
        _drained_meta[RESTORED_TURN_META_KEY] = True
    current_row = responsive_chat.reusable_row(slot, consumed, next_msg)
    if current_row is None:
        if is_automation_steer:
            current_row = append_and_surface(
                state, slot, row_role, next_msg, row_cls, meta=_drained_meta or None
            )
        else:
            current_row = slot.append(
                row_role,
                next_msg,
                row_cls,
                meta=_drained_meta or None,
            )
    if superseded_steer_row is not None and superseded_steer_row is not current_row:
        _retire_superseded_steer_row(state, slot, superseded_steer_row, current_row)
    return current_row, _possibly_delivered_steer, _commands_off
