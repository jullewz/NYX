"""AST node definitions.

Every node keeps the token it came from (`tok`) so runtime errors can point at
the exact line and column in the source.
"""


class Node:
    __slots__ = ("tok",)
    fields = ()

    def __init__(self, *values, tok=None):
        for name, value in zip(self.fields, values):
            setattr(self, name, value)
        self.tok = tok

    def __repr__(self):
        inner = ", ".join(f"{f}={getattr(self, f)!r}" for f in self.fields)
        return f"{type(self).__name__}({inner})"


# --------------------------------------------------------------------------- #
#  Expressions
# --------------------------------------------------------------------------- #
class Literal(Node):
    __slots__ = fields = ("value",)


class StringInterp(Node):
    __slots__ = fields = ("parts",)          # str | Node


class Var(Node):
    __slots__ = fields = ("name",)


class SelfExpr(Node):
    __slots__ = fields = ()


class SuperExpr(Node):
    __slots__ = fields = ("name",)


class ListExpr(Node):
    __slots__ = fields = ("items",)


class ListComp(Node):
    __slots__ = fields = ("expr", "var", "var2", "iterable", "cond")


class MapExpr(Node):
    __slots__ = fields = ("entries",)        # [(str | number | Node, Node)]


class Unary(Node):
    __slots__ = fields = ("op", "operand")


class Binary(Node):
    __slots__ = fields = ("op", "left", "right")


class Logical(Node):
    __slots__ = fields = ("op", "left", "right")   # and / or / ??


class Ternary(Node):
    __slots__ = fields = ("cond", "then", "other")


class RangeExpr(Node):
    __slots__ = fields = ("start", "end", "inclusive")


class Call(Node):
    __slots__ = fields = ("callee", "args")


class Spread(Node):
    __slots__ = fields = ("expr",)


class Get(Node):
    __slots__ = fields = ("obj", "name", "optional")


class Index(Node):
    __slots__ = fields = ("obj", "index")


class Lambda(Node):
    __slots__ = fields = ("params", "defaults", "rest", "body", "is_expr")


class Match(Node):
    __slots__ = fields = ("subject", "arms")     # [(patterns, guard, body, is_block)]


# patterns used by `match`
class PWild(Node):
    __slots__ = fields = ()


class PBind(Node):
    __slots__ = fields = ("name",)


class PLit(Node):
    __slots__ = fields = ("value",)


class PRange(Node):
    __slots__ = fields = ("lo", "hi", "inclusive")


# --------------------------------------------------------------------------- #
#  Statements
# --------------------------------------------------------------------------- #
class Program(Node):
    __slots__ = fields = ("stmts",)


class Block(Node):
    __slots__ = fields = ("stmts", "scoped")   # scoped: declares names, needs its own scope


class ExprStmt(Node):
    __slots__ = fields = ("expr",)


class Let(Node):
    __slots__ = fields = ("target", "value", "const")   # target: name | ("list", names, rest) | ("map", names)


class Assign(Node):
    __slots__ = fields = ("target", "op", "value")


class FnDecl(Node):
    __slots__ = fields = ("name", "params", "defaults", "rest", "body")


class ClassDecl(Node):
    __slots__ = fields = ("name", "superclass", "methods", "statics")


class If(Node):
    __slots__ = fields = ("branches", "other")


class While(Node):
    __slots__ = fields = ("cond", "body")


class For(Node):
    __slots__ = fields = ("var", "var2", "iterable", "body")


class Return(Node):
    __slots__ = fields = ("value",)


class Break(Node):
    __slots__ = fields = ()


class Continue(Node):
    __slots__ = fields = ()


class Try(Node):
    __slots__ = fields = ("body", "name", "handler", "final")


class Throw(Node):
    __slots__ = fields = ("value",)


class Import(Node):
    __slots__ = fields = ("path", "alias")


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def children(node):
    """Yield every direct child node (used by analysis passes and the AST dump)."""
    for f in node.fields:
        v = getattr(node, f)
        yield from _flatten(v)


def _flatten(v):
    if isinstance(v, Node):
        yield v
    elif isinstance(v, (list, tuple)):
        for item in v:
            yield from _flatten(item)


def walk(node):
    yield node
    for c in children(node):
        yield from walk(c)


def creates_closures(node):
    """True if running this node can create a function that captures the scope."""
    return any(isinstance(n, (Lambda, FnDecl, ClassDecl, ListComp)) for n in walk(node))
