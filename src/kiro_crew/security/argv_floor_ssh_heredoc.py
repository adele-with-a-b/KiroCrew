"""Which here-document bodies the ssh self-target floor reads as data, not argv.

``_is_ssh_to_self`` walks every word of a line's frames, here-document bodies
included. A body that no program on the line can run as a shell command line is
data, so reading it as argv refuses lines that run no ssh: a Python edit script's
``n = a * localhost`` reads as a glob verb (``*``) naming ``ssh`` and its target.
:func:`_heredoc_bodies_as_data` cuts such bodies out before the walk, and keeps
the text whole for every line where a body could run or the shared reader could
misread where a body ends.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch
through it reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads
the shared shell reader, the shell tokenizer and the rm floor's body cutter, and
nothing from the argv floor.
"""

from __future__ import annotations

import re
from itertools import takewhile

from . import rm_floor as _rm
from . import shell_commands as _shell_commands
from . import shell_normalizer as _shell_normalizer

#: Programs that read a here-document body as data, never as a shell command line.
_HEREDOC_DATA_PROGRAMS = frozenset(("cat", "tee", "cd"))
_HEREDOC_PYTHON_RE = re.compile(r"python(?:3(?:\.\d+)?)?\Z")
#: Options that leave Python reading its program from stdin or a file, never ``-c``/``-m``.
_HEREDOC_PYTHON_OPTIONS = frozenset(("-", "-u", "-b", "-i", "-e", "-s", "-q", "-o"))
#: Text outside the bodies that marks arithmetic or compound syntax, where the shared
#: reader can take a ``<<`` shift (``((a<<b))``, ``$((a<<b))``) for a heredoc. A paren
#: covers ``((``, ``$((``, ``$(`` and a subshell; a brace covers ``{ …; }`` and ``${``.
_HEREDOC_COMPOUND_MARKS = ("(", ")", "{", "}", "`", "[[", "$[")
#: First words of a command that open or continue compound syntax, or ``let``.
_HEREDOC_COMPOUND_WORDS = frozenset(
    "if then elif else fi while until do done for select case esac function let".split()
)
#: Characters that end a heredoc tag word.
_HEREDOC_TAG_ENDS = frozenset(" \t\n;&|<>()")


def _is_option(arg: str) -> bool:
    return arg.startswith("-")


def _is_word_char(ch: str) -> bool:
    """True for one letter, digit or underscore; False for anything else, ``""`` included."""
    return len(ch) == 1 and (ch.isalnum() or ch == "_")


def _heredoc_tag(text: str, at: int) -> "tuple[str, int]":
    """The tag after the ``<<`` at *at*, with one pair of enclosing quotes removed, and
    the offset just past the tag word as written."""
    rest = text[at + 2 :].removeprefix("-").lstrip(" \t")
    raw = "".join(takewhile(lambda ch: ch not in _HEREDOC_TAG_ENDS, rest))
    end = len(text) - len(rest) + len(raw)
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        return raw[1:-1], end
    return raw, end


def _heredoc_operators(text: str) -> int:
    """How many ``<<`` operators *text* holds, a ``<<<`` here-string not counted."""
    return sum(
        1
        for i in range(len(text) - 1)
        if text.startswith("<<", i)
        and not text.startswith("<<<", i)
        and (i == 0 or text[i - 1] != "<")
    )


def _whole_word_count(text: str, word: str) -> int:
    """How many times *word* appears in *text* with no word character on either side."""
    return sum(
        1
        for i in range(len(text))
        if text.startswith(word, i)
        and (i == 0 or not _is_word_char(text[i - 1]))
        and not _is_word_char(text[i + len(word) : i + len(word) + 1])
    )


def _runs_no_body(cmd: "_shell_commands.ShellCommand") -> bool:
    """True when *cmd* is a direct ``cat``/``tee``/``cd``, or Python reading its program
    from stdin (``python3 -``) or from a file, with no ``-c``/``-m``, and is not piped."""
    words = _shell_normalizer._shell_tokens(" ".join(word.raw for word in cmd.argv))
    if not words:
        return False
    program, args = words[0], words[1:]
    if _HEREDOC_PYTHON_RE.match(program):
        # With a body on stdin, the body is the program (``python3 -``) and every word
        # is an option; without one, the options end at the script's name.
        end = len(args) if cmd.heredocs else len(list(takewhile(_is_option, args)))
        if not set(args[:end]) <= _HEREDOC_PYTHON_OPTIONS:
            return False
        if cmd.heredocs and "-" not in args:
            return False
    elif program not in _HEREDOC_DATA_PROGRAMS:
        return False
    return not (cmd.heredocs and cmd.separator == "|")


def _heredoc_bodies_as_data(
    text_lower: str, raw_text: "str | None" = None
) -> "tuple[str, str | None]":
    """*text_lower* with its here-document bodies cut out when no program can run one,
    and *raw_text*, which is ``None`` once the bodies are cut (its offsets then differ
    from the cut text's), so the floor reads the lowered words.

    Fails closed: the text is returned whole unless every program on the line passes
    :func:`_runs_no_body`, the text outside the bodies holds no arithmetic or compound
    syntax, the text holds no backslash-newline (it can change a delimiter), the line
    holds exactly one ``<<`` (not ``<<<``) whose tag is a plain word (letters, digits,
    underscore, bare or wholly in one pair of quotes) that appears as a whole word only
    as that operand and its closing line in any letter case (the floor reads lowered
    text, while bash matches the delimiter case-sensitively), and no unquoted body
    holds a ``$`` or backtick, which bash expands even for ``cat``. Python running a
    program it reads is the OS network fence's residual, as ``python3 -c`` is here."""
    if "<<" not in text_lower:
        return text_lower, raw_text
    commands = _shell_commands.read_commands(text_lower, heredocs=True)
    outside = _rm._without_bodies(text_lower)
    # (tag, offset of its ``<<``, offset past the tag word) per heredoc operator.
    tags = [
        (*_heredoc_tag(text_lower, step.start), step.start)
        for cmd in commands
        for step in cmd.steps
        if step.redirect
        and text_lower.startswith("<<", step.start)
        and not text_lower.startswith("<<<", step.start)
    ]
    docs = [doc for cmd in commands for doc in cmd.heredocs]
    if (
        "\\\n" in text_lower
        or "\\\r\n" in text_lower
        or any(mark in outside for mark in _HEREDOC_COMPOUND_MARKS)
        or any(cmd.words and cmd.words[0].raw in _HEREDOC_COMPOUND_WORDS for cmd in commands)
        or len(tags) != 1
        or _heredoc_operators(text_lower) != 1
        or not tags[0][0].isascii()
        or not tags[0][0].replace("_", "a").isalnum()
        or _whole_word_count(text_lower.casefold(), tags[0][0].casefold()) != 2
        or not docs
        or any(not doc.quoted and ("$" in doc.body or "`" in doc.body) for doc in docs)
        or not all(_runs_no_body(cmd) for cmd in commands if cmd.argv or cmd.heredocs)
    ):
        return text_lower, raw_text
    # The ``<<`` and its tag go too, so the lines after the closing tag are not read
    # as a body that has lost its comments. Both sit before the body, at the same
    # offsets in *outside*.
    _tag, end, start = tags[0]
    return outside[:start] + outside[end:], None
