"""Fork-only tooling: every change merged into a ``local/<release>`` branch gets a verdict.

A verdict says whether the change is worth sending to the original project:
``UPSTREAM``, ``SPLIT``, ``FORK-ONLY`` or ``ALREADY-UPSTREAM``. The record is an
append-only file outside the repository (``ledger.state_dir``), so "which merge has
no verdict" is a query (``python3 scripts/fork/kc_upstream pending``), never a habit.

This package only reads the original project. Nothing in it writes there.
"""

UPSTREAM = "kirodotdev/KiroCrew"
