"""Evaluate a `${{ fromJSON(...) }}` matrix value from ci.yml for a given context.

A matrix value chosen by an expression is a chain of guarded string literals
joined by ``&&`` and ``||``. GitHub's operators return an operand, not a bool,
exactly as Python's ``and`` / ``or`` do, so after every context reference is
replaced by its value the chain is evaluated as a Python AST and the chosen
literal parsed as JSON. Tests then pin what each context gets, rather than the
expression's spelling.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_CONTEXT_REF = re.compile(
    r"\b(?:needs\.[\w-]+\.(?:result|outputs\.\w+)|github(?:\.\w+)+|vars\.\w+)"
)


def evaluate_matrix_value(value: str, context: dict[str, str]) -> Any:
    """Return the JSON value ``value`` selects under ``context``.

    ``context`` maps a reference such as ``github.repository`` to its value; a
    reference it does not name is ``''``, as on GitHub.
    """
    expr = value.strip()
    assert expr.startswith("${{") and expr.endswith("}}"), value
    expr = expr[3:-2].strip()
    assert expr.startswith("fromJSON(") and expr.endswith(")"), value
    expr = expr[len("fromJSON(") : -1]
    expr = _CONTEXT_REF.sub(lambda m: repr(context.get(m.group(0), "")), expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")

    def walk(node: ast.AST) -> object:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            result: object = None
            for operand in node.values:
                result = walk(operand)
                if isinstance(node.op, ast.And) and not result:
                    return result
                if isinstance(node.op, ast.Or) and result:
                    return result
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = walk(node.left), walk(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f"unsupported expression in {value!r}: {ast.dump(node)}")

    chosen = walk(ast.parse(f"({expr})", mode="eval"))
    assert isinstance(chosen, str), f"{value!r} chose a non-literal: {chosen!r}"
    return json.loads(chosen)
