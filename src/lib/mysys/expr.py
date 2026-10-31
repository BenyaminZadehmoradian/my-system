"""Small, safe expression language for flow conditions, filters and assertions.

    {{wns}} >= 0 and {{lut}} < 50000
    "{{mode}}" in ("a", "b")
    not ({{seed}} % 2 == 0)

Only literals, comparisons, arithmetic, and/or/not, `in`, tuples/lists and the functions
abs/min/max/len/float/int/str are allowed; anything else is rejected.
"""
import ast
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
        ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_FUN = {"abs": abs, "min": min, "max": max, "len": len, "float": float, "int": int, "str": str, "round": round}


class ExprError(Exception):
    pass


def _ev(n, names):
    if isinstance(n, ast.Expression):
        return _ev(n.body, names)
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Name):
        if n.id in ("true", "True"):
            return True
        if n.id in ("false", "False"):
            return False
        if n.id in names:
            return names[n.id]
        raise ExprError(f"unknown name {n.id}")
    if isinstance(n, (ast.Tuple, ast.List)):
        return tuple(_ev(e, names) for e in n.elts)
    if isinstance(n, ast.UnaryOp):
        v = _ev(n.operand, names)
        if isinstance(n.op, ast.Not):
            return not v
        if isinstance(n.op, ast.USub):
            return -v
        if isinstance(n.op, ast.UAdd):
            return +v
    if isinstance(n, ast.BoolOp):
        vals = [_ev(v, names) for v in n.values]
        return all(vals) if isinstance(n.op, ast.And) else any(vals)
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        return _BIN[type(n.op)](_ev(n.left, names), _ev(n.right, names))
    if isinstance(n, ast.Compare):
        left = _ev(n.left, names)
        for op, right in zip(n.ops, n.comparators):
            r = _ev(right, names)
            if type(op) not in _CMP or not _CMP[type(op)](left, r):
                return False
            left = r
        return True
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUN and not n.keywords:
        return _FUN[n.func.id](*[_ev(a, names) for a in n.args])
    if isinstance(n, ast.IfExp):
        return _ev(n.body, names) if _ev(n.test, names) else _ev(n.orelse, names)
    raise ExprError(f"not allowed in expressions: {type(n).__name__}")


def evaluate(text, names=None):
    try:
        tree = ast.parse(str(text).strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error in {text!r}: {e.msg}")
    try:
        return _ev(tree, names or {})
    except ExprError:
        raise
    except Exception as e:
        raise ExprError(f"{text!r}: {type(e).__name__}: {e}")


def check(text):
    """Syntax/allowed-node check without values (placeholders must already be filled with sample values)."""
    evaluate(text)
