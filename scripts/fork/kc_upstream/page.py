"""The batch page: one self-contained HTML file per batch, drawn again when its state changes.

Waiting: one line per fix, the detail one click down, a tick on each line to keep a
fix back, and the exact reply to type in chat (the ticks rewrite it). After an answer
the same page says what was decided. The page sends nothing: the answer is typed in chat.

Once the send door has run, the page is drawn from its journal: what is on the original
(with each link), what was kept back, where a run stopped, or why a yes stopped counting.
"""

from __future__ import annotations

import time
from html import escape
from pathlib import Path
from typing import Optional

from . import batch, journal

_HERE = Path(__file__).parent
_TICK = (
    '<svg class="ic" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_INFO = (
    '<svg class="ic" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" '
    'stroke="currentColor" stroke-width="1.6"/><path d="M8 7.2v4M8 4.8v.2" fill="none" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
)
_CHEV = (
    '<svg class="chev" viewBox="0 0 16 16"><path d="M6 3.5l4.5 4.5L6 12.5" fill="none" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _count(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _fold(label: str, title: str, text: str) -> str:
    return (
        f'<details class="fold"><summary>{_CHEV}{escape(label)}</summary><div class="doc">'
        f'<div class="doctitle">{escape(title)}</div><pre>{escape(text)}</pre></div></details>'
    )


def _checks(entry: dict) -> str:
    rows = []
    for check in entry["checks"]:
        kind, icon = ("note", _INFO) if check.get("note") else ("ok", _TICK)
        rows.append(
            f'<li class="{kind}">{icon}<span><b>{escape(check["label"])}</b>'
            f'<em>{escape(check["detail"])}</em></span></li>'
        )
    return f'<ul class="ckl">{"".join(rows)}</ul>'


def _meta(entry: dict, state: str, send: list[int], again: set[int]) -> tuple[str, str, str]:
    """(row class, number class, the right-hand cell) for one fix in this state."""
    n = entry["n"]
    files = f'<span class="dim">{_count(entry["files"], "file")}</span>'
    if state == "waiting":
        tick = (
            f'<label class="inc"><input type="checkbox" checked data-inc="{n}">'
            f'<span class="box">{_TICK}</span><span class="inc-t">Send this one</span></label>'
        )
        return "", "", tick + files
    if n in send:
        return "", " ok", f'<span class="lv ok">{_TICK}Yes recorded</span>' + files
    label = "Asked again later" if n in again else "Kept in your fork"
    return " off", "", f'<span class="lv off">{label}</span>' + files


def _author(entry: dict) -> str:
    """The name and address on the commit: public the moment the branch is sent."""
    if not entry.get("author"):
        return ""
    return f'<p class="who"><b>The commit is signed:</b> {escape(entry["author"])}</p>'


def _row(number: int, entry: dict, state: str, send: list[int], again: set[int]) -> str:
    issue_title, issue_body, pr_title, pr_body = batch.frozen_texts(number, entry["n"])
    row_class, num_class, meta = _meta(entry, state, send, again)
    return (
        f'<details class="arow{row_class}"><summary><span class="num{num_class}">{entry["n"]}</span>'
        f'<span class="a-main"><b>{escape(entry["title"])}</b>'
        f'<span class="a-sub">{escape(entry["wrong"])}</span></span>'
        f'<span class="a-meta">{meta}</span>{_CHEV}</summary><div class="a-open">'
        f'<p class="who"><b>Who it helps:</b> {escape(entry["who"])}</p>{_author(entry)}{_checks(entry)}'
        f'{_fold("Read the issue, word for word", issue_title, issue_body)}'
        f'{_fold("Read the pull request, word for word", pr_title, pr_body)}</div></details>'
    )


def _say(code: str, note: str, live: str = "") -> str:
    primary = " primary" if live else ""
    return (
        f'<div class="say{primary}"{live}><code data-reply>{escape(code)}</code>'
        f"<small data-reply-note>{escape(note)}</small>"
        '<button class="copy" type="button">Copy</button></div>'
    )


def _waiting(made: dict) -> tuple[str, str, str, str, str]:
    count = len(made["items"])
    base = batch.reply(made["batch"], made["code"], list(range(1, count + 1)), count)
    asks = "this fix" if count == 1 else f"these {count} fixes"
    box = (
        '<div class="answer"><h4>Your answer, in chat</h4><div class="says">'
        + _say(base, "", f' data-live-reply data-base="{escape(base)}"')
        + '</div><p class="note">Untick a fix above to keep it back: the reply changes with it. '
        f'A yes puts {_count(count, "issue")} and {_count(count, "pull request")} on '
        f'{escape(made["upstream"])} under <b>{escape(made["account"])}</b>. Public. '
        "They can be closed later, not erased.</p></div>"
    )
    return (
        "amber",
        "Waiting for your yes · nothing sent",
        f"Send {asks} to the original Kiro Crew?",
        "Each one fixes a bug the original still has. Nothing in them is personal to you.",
        box,
    )


def _answered(made: dict, answer: dict, again: set[int]) -> tuple[str, str, str, str, str]:
    count, send = len(made["items"]), answer["send"]
    kept = [e["n"] for e in made["items"] if e["n"] not in send and e["n"] not in again]
    says = "".join(
        _say(f"ask again about fix {n}", "It goes into the next batch.", " data-again")
        for n in kept
    )
    said = f'<p class="note">You said: <b>{escape(answer["words"])}</b></p>'
    box = ""
    if kept:
        box = (
            '<div class="answer"><h4>Changed your mind? Say it in chat</h4>'
            f'<div class="says">{says}</div>{said}</div>'
        )
    if not send:
        fixes = "This fix stays" if count == 1 else f"These {count} fixes stay"
        return (
            "grey",
            "Kept in your fork · nothing sent",
            f"{fixes} in your fork",
            "Nothing went to the original. I will not ask about them again unless you say so.",
            box or said,
        )
    return (
        "green",
        "Your yes is recorded · nothing sent yet",
        f"{len(send)} of {_count(count, 'fix', 'fixes')} will go to the original Kiro Crew",
        "Sending is its own step. This page changes when they are out.",
        box or said,
    )


def _fixes(positions: list[int]) -> str:
    listed = [str(n) for n in positions]
    if len(listed) == 1:
        return f"Fix {listed[0]}"
    return f"Fixes {', '.join(listed[:-1])} and {listed[-1]}"


def _when(at: float) -> str:
    local = time.localtime(at)
    return f"{local.tm_mday} {time.strftime('%b', local)} at {time.strftime('%H:%M', local)}"


def _link(row: Optional[dict], word: str) -> str:
    if row is None:
        return ""
    return f'<a class="ghlink" href="{escape(row["url"], quote=True)}">{word} #{row["number"]}</a>'


def _sent_row(entry: dict, done: list[dict]) -> str:
    n = entry["n"]
    issue, pull = journal.step_of(done, n, "issue"), journal.step_of(done, n, "pr")
    if pull:
        num, chip = " ok", f'<span class="lv ok">{_TICK}Sent</span>'
    elif issue:
        num, chip = " warn", '<span class="lv warn">Issue filed · pull request not yet</span>'
    else:
        num, chip = "", '<span class="lv">Not sent yet</span>'
    links = _link(issue, "Issue") + _link(pull, "Pull request")
    return (
        f'<div class="sent"><span class="num{num}">{n}</span><div class="sent-main">'
        f'<b>{escape(entry["title"])}</b><div class="links">{links}</div></div>'
        f'<div class="live">{chip}</div></div>'
    )


def _held_row(entry: dict, again: set[int]) -> str:
    n = entry["n"]
    if n in again:
        tail, chip = "It goes into the next batch.", "Asked again later"
    else:
        tail = "It stays in your fork and I will not ask about it again."
        chip = "Kept back by you"
    undo = (
        ""
        if n in again
        else f'<p class="undo">Changed your mind? Say <code>ask again about fix {n}</code>.</p>'
    )
    return (
        f'<div class="held"><span class="num off">{n}</span><div><b>{escape(entry["title"])}</b>'
        f"<p>Not sent. {tail}</p>{undo}</div>"
        f'<span class="lv off">{chip}</span></div>'
    )


def _sent(
    made: dict, answer: dict, again: set[int], done: list[dict], replaced: bool
) -> tuple[str, ...]:
    """The page once the send door has filed something: (tone, pill, title, lead, body)."""
    approved = [e for e in made["items"] if e["n"] in answer["send"]]
    kept = [e for e in made["items"] if e["n"] not in answer["send"]]
    out = sorted(journal.sent_positions(done))
    last = max(row["at"] for row in done)
    body = f'<div class="sentlist">{"".join(_sent_row(e, done) for e in approved)}</div>'
    if kept:
        body += '<div class="divider">Not sent</div>' + "".join(_held_row(e, again) for e in kept)
    filed = (
        f'{_count(len(out), "issue")} and {_count(len(out), "pull request")}, filed as '
        f'{made["account"]} on {_when(last)}.'
    )
    body += (
        '<p class="fine">Each link opens it on GitHub. They can be closed later, not erased.</p>'
    )
    if len(out) < len(approved):
        title = f"{len(out)} of {_count(len(approved), 'fix', 'fixes')} on the original so far"
        if replaced:
            lead = "A newer batch asks about the rest. An issue already filed here is reused there."
            return "grey", "Replaced by a newer question", title, lead, body
        lead = (
            "Sending stopped partway. Sending again carries on from here; nothing is filed twice."
        )
        return "amber", "Sending stopped partway", title, lead, body
    if kept:
        bar = "".join(
            f'<span class="{"s-on" if e["n"] in out else "s-off"}"></span>' for e in made["items"]
        )
        title = f"{_fixes(out)} {'is' if len(out) == 1 else 'are'} on the original"
        band = f'<div class="splitbar">{bar}</div>'
        return "green", f"{len(out)} sent · {len(kept)} kept back", title, filed, band + body
    count = len(out)
    title = "It is on the original" if count == 1 else f"All {count} are on the original"
    if count == 2:
        title = "Both are on the original"
    return "green", "Sent", title, filed, body


def _changed(made: dict, stop: dict, done: list[dict], replaced: bool) -> tuple[str, ...]:
    """The page when the batch stopped being what was shown: the yes no longer counts."""
    by_fix: dict[int, list[dict]] = {}
    for row in stop["fixes"]:
        by_fix.setdefault(int(row["n"]), []).append(row)
    body = ""
    for entry in made["items"]:
        n = entry["n"]
        issue = journal.step_of(done, n, "issue")
        if n in by_fix:
            whys = "".join(f"<p>{escape(row['why'])}</p>" for row in by_fix[n])
            if issue:
                whys += f'<div class="links">{_link(issue, "Issue")}</div>'
            diffs = "".join(
                f'<div class="diff"><pre>{escape(row["diff"])}</pre></div>'
                for row in by_fix[n]
                if row.get("diff")
            )
            body += (
                f'<div class="chg"><div class="chg-top"><span class="num warn">{n}</span>'
                f'<b>{escape(entry["title"])}</b><span class="lv warn">Changed · needs a new yes'
                f"</span></div>{whys}{diffs}</div>"
            )
        elif issue:
            body += f'<div class="sentlist chg-sent">{_sent_row(entry, done)}</div>'
        else:
            body += (
                f'<div class="same"><span class="num">{n}</span><span><b>{escape(entry["title"])}'
                "</b> · not changed, not sent</span></div>"
            )
    whole = "".join(f"<p>{escape(row['why'])}</p>" for row in by_fix.get(0, []))
    named = sorted(n for n in by_fix if n)
    what = _fixes(named) if named else "This batch"
    title = f"{what} changed after you said yes, so I stopped"
    lead = "Your yes was for the exact words and code you read. Nothing more goes out on it."
    after = "A newer batch asks again." if replaced else "I will ask again with a new code."
    codes = (
        f'<div class="codes"><span class="code old">{escape(made["code"])}</span>'
        f'<span class="why">no longer counts. {after}</span></div>'
    )
    return "orange", "Your yes no longer counts", title, lead, codes + whole + body


def render(number: int) -> str:
    """The page for batch ``number`` in its current state."""
    made = batch.manifest(number)
    answer: Optional[dict] = batch.answer_of(number)
    again = batch.asked_again(number)
    done = journal.rows(number)
    stop = journal.stopped(done)
    replaced = batch.voided(number)
    if stop is not None:
        state = "changed"
        tone, pill, title, lead, body = _changed(made, stop, done, replaced)
    elif answer is not None and done:
        state = "sent"
        tone, pill, title, lead, body = _sent(made, answer, again, done, replaced)
    else:
        if answer is None:
            state, send = "waiting", []
            tone, pill, title, lead, box = _waiting(made)
        else:
            state, send = "answered", answer["send"]
            tone, pill, title, lead, box = _answered(made, answer, again)
        if replaced:
            again = again | set(send or [entry["n"] for entry in made["items"]])
            state, send, tone, pill, box = (
                "replaced",
                [],
                "grey",
                "Replaced by a newer question",
                "",
            )
            title, lead = (
                "This question was replaced",
                "A newer batch asks again. Nothing was sent.",
            )
        listed = "".join(_row(number, entry, state, send, again) for entry in made["items"])
        body = f'<div class="alist">{listed}</div>{box}'
    css = (_HERE / "page.css").read_text(encoding="utf-8")
    script = (_HERE / "page.js").read_text(encoding="utf-8")
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Batch {number}: fixes for the original Kiro Crew</title><style>{css}</style></head>"
        f'<body><div class="page" data-state="{state}" style="--tone:var(--{tone})"><div class="page-in">'
        f'<div class="band"><span class="pill">{escape(pill)}</span><h1>{escape(title)}</h1>'
        f"<p>{escape(lead)}</p></div>{body}</div></div>"
        f"<script>{script}</script></body></html>\n"
    )


def write(number: int) -> Path:
    path = batch.batch_dir(number) / "look.html"
    path.write_text(render(number), encoding="utf-8")
    return path
