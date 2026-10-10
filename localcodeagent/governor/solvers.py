"""Formal solvers — exact answers where a model should not approximate.

The Reasoning Strategy Router maps ``formal_math`` onto the
``run_formal_solver`` op; this module is its first instrument: a
whitelist AST evaluator for arithmetic. No ``eval`` — only literal
numbers, +−*/^, parentheses, unary minus, percent-of. Anything outside
the grammar returns ``None`` so the caller falls back to the model.
"""
from __future__ import annotations

import ast
import operator
import re
from typing import Any

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_MAX_TERMS = 40          # runaway-expression guard
_MAX_ABS = 10 ** 15      # result magnitude cap — keeps pow() bounded


def _eval(node: ast.AST, depth: int = 0) -> float:
    if depth > _MAX_TERMS:
        raise ValueError("expression too deep")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(
                node.value, bool):
            return float(node.value)
        raise ValueError("non-numeric constant")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported operator")
        lhs, rhs = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        result = op(lhs, rhs)
        if abs(result) > _MAX_ABS:
            raise ValueError("result out of range")
        return result
    if isinstance(node, ast.UnaryOp):
        op = _UNOPS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported unary")
        return op(_eval(node.operand, depth + 1))
    raise ValueError("unsupported syntax")


def eval_arithmetic(expr: str) -> float | int | None:
    """Evaluate a pure arithmetic string, or None if it isn't one."""
    text = str(expr or "").strip().replace("^", "**")
    if not text or len(text) > 300:
        return None
    try:
        value = _eval(ast.parse(text, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError,
            MemoryError, RecursionError):
        return None
    if value == int(value):
        return int(value)
    return round(value, 10)


# -- expression extraction ---------------------------------------------------

# Words → operators. Order matters: longest/multi-word first so
# "divided by" wins over "by".
_WORD_OPS = [
    (re.compile(r"\bpercent of\b", re.I), "*(1/100)*"),
    (re.compile(r"\bto the power of\b|\braised to\b", re.I), "**"),
    (re.compile(r"\bdivided by\b|\bover\b", re.I), "/"),
    (re.compile(r"\btimes\b|\bmultiplied by\b", re.I), "*"),
    (re.compile(r"\bplus\b|\badded to\b", re.I), "+"),
    (re.compile(r"\bminus\b|\bless\b|\bsubtracted from\b", re.I), "-"),
    (re.compile(r"\bmodulo\b|\bmod\b", re.I), "%"),
]

_QUESTION_STRIP = re.compile(
    r"^\s*(?:please\s+)?(?:what(?:'s| is| are)|whats|calculate|compute|"
    r"solve|evaluate|how much is|tell me|work out|figure out)\s+",
    re.I)

# The cleaned text must be nothing but arithmetic.
_PURE_MATH_RE = re.compile(r"^[\d\s.()+\-*/%^*]+$")


def extract_expression(text: str) -> str | None:
    """Pull a pure-arithmetic expression out of a math ask.

    Returns the expression only when the *whole* ask reduces to
    arithmetic — numbers inside prose are not computed ('the server has
    14 cores, why does it crash' must never answer 14).
    """
    t = str(text or "").strip()
    if len(t) > 300:
        return None
    t = _QUESTION_STRIP.sub("", t)
    t = t.rstrip("?.").strip()
    for rx, op in _WORD_OPS:
        t = rx.sub(f" {op} ", t)
    t = t.replace("×", "*").replace("÷", "/").replace("−", "-")
    t = t.replace("^", "**").replace("x", "*")
    t = re.sub(r"\s+", " ", t).strip()
    if not _PURE_MATH_RE.match(t):
        return None
    if not re.search(r"\d", t) or not re.search(r"[+\-*/%]", t):
        return None
    return t


def math_answer(text: str) -> str | None:
    """Exact answer string for a pure math ask, else None."""
    expr = extract_expression(text)
    if expr is None:
        return None
    value = eval_arithmetic(expr)
    if value is None:
        return None
    pretty = expr.replace("**", "^")
    return f"{pretty} = {value}"
