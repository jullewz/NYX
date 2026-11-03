"""NYX checks for `nyx doctor`: syntax repair using NYX's own parser, then a
scope analysis that finds undefined names, constant reassignment, wrong
argument counts, unreachable code and more."""

import difflib
import re

from .. import stdlib
from ..errors import NyxSyntaxError
from ..lexer import Lexer
from ..nodes import (
    Assign, Block, Break, Call, ClassDecl, Continue, ExprStmt, FnDecl, For, Get, If, Import, Lambda,
    Let, ListComp, Literal, Match, Node, PBind, Return, SelfExpr, Spread, SuperExpr, Throw, Try, Var,
    While, children,
)
from ..parser import Parser
from .core import (
    PAIRS, Edit, Issue, brackets, code_end, indent_of, insert, line_starts, offset_to_pos, sub_code,
)

LANG = "nyx"
BUILTINS = set(stdlib.GLOBALS) | {"PI", "TAU", "E", "INF", "args"}
ALIASES = {
    "null": "nil", "None": "nil", "undefined": "nil", "none": "nil", "True": "true", "False": "false",
    "this": "self", "println": "print", "echo": "print", "puts": "print", "printf": "print",
    "length": "len", "lenght": "len",
}


def parse_error(text):
    try:
        Parser(Lexer(text, "<doctor>").tokenize()).program()
        return None
    except NyxSyntaxError as e:
        return e


def err_key(e):
    tok = e.tok
    return (tok.line, tok.col) if tok is not None else (0, 0)


def with_line(lines, idx, new):
    out = list(lines)
    out[idx] = new
    return "\n".join(out)


# =========================================================================== #
#  Pre-pass: syntax from other languages that is never valid NYX
# =========================================================================== #
FOREIGN = [
    (r"^(\s*)(?:def|function|func)\s+(?=\w+\s*\()", r"\1fn ", "'def/function' → 'fn'"),
    (r"^(\s*)(?:var|let\s+mut|auto)\s+(?=\w)", r"\1let ", "'var' → 'let'"),
    (r"\s*&&\s*", " and ", "'&&' → 'and'"),
    (r"\s*\|\|\s*", " or ", "'||' → 'or'"),
    (r"!==", "!=", "'!==' → '!='"),
    (r"===", "==", "'===' → '=='"),
    (r"!(?=[\w(])", "not ", "'!' → 'not'"),
    (r"^(\s*)//\s?", r"\1# ", "'//' comment → '#'"),
    (r"^(\s*)([\w.\[\]]+)\s*\+\+\s*;?\s*$", r"\1\2 += 1", "'x++' → 'x += 1'"),
    (r"^(\s*)([\w.\[\]]+)\s*--\s*;?\s*$", r"\1\2 -= 1", "'x--' → 'x -= 1'"),
    (r"\belse\s+if\b", "elif", None),
]
SINGLE_QUOTED = re.compile(r"'((?:[^'\\\n]|\\.)*)'")


def foreign_pass(text):
    lines = text.split("\n")
    issues = []
    for i, line in enumerate(lines):
        new = line
        notes = []
        for pattern, repl, note in FOREIGN:
            changed = sub_code(new, LANG, pattern, repl)
            if changed != new and note:
                notes.append(note)
            new = changed
        if "'" in new:
            changed = sub_code(new, LANG, SINGLE_QUOTED.pattern,
                               lambda m: '"' + m.group(1).replace("\\'", "'").replace('"', '\\"') + '"')
            if changed != new:
                notes.append("'single quotes' → \"double quotes\"")
                new = changed
        if new != line:
            lines[i] = new
            issue = Issue(i + 1, 0, "error", "foreign-syntax", "syntax from another language", fix=", ".join(notes))
            issue.fixed = True
            issues.append(issue)
    return "\n".join(lines), issues


# =========================================================================== #
#  Syntax repair
# =========================================================================== #
def repair_syntax(text, history, max_rounds=60):
    new_text, fixed = foreign_pass(text)
    for issue in fixed:
        issue.rev = len(history) - 1
    if new_text != text:
        text = new_text
        history.append(text)
    for _ in range(max_rounds):
        err = parse_error(text)
        if err is None:
            return text, fixed, None
        for desc, new in candidates(text, err):
            if not new or new == text:
                continue
            after = parse_error(new)
            if after is None or err_key(after) > err_key(err):
                tok = err.tok
                issue = Issue(tok.line if tok else 1, (tok.col - 1) if tok else 0, "error", "syntax", err.message, fix=desc)
                issue.fixed = True
                issue.rev = len(history) - 1
                fixed.append(issue)
                text = new
                history.append(text)
                break
        else:
            return text, fixed, err
    return text, fixed, parse_error(text)


def prev_token_end(text, line, col):
    """(line, col0) just after the token that comes before the one at (line, col)."""
    try:
        toks = Lexer(text, "<doctor>").tokenize()
    except NyxSyntaxError:
        return None
    prev = None
    for t in toks:
        if (t.line, t.col) >= (line, col):
            break
        prev = t
    if prev is None:
        return None
    return prev.line, prev.col - 1 + prev.length


def candidates(text, err):
    lines = text.split("\n")
    tok = err.tok
    msg = err.message
    L = min(max(1, tok.line if tok else len(lines)), len(lines))
    starts = line_starts(text)

    def at(line, col0, new_text, length=0):
        o = min(starts[line - 1] + col0, len(text)) if line <= len(starts) else len(text)
        return text[:o] + new_text + text[o + length:]

    if tok is not None and tok.type != "eof":
        col0 = tok.col - 1
        if "unexpected character '!'" in msg:
            yield "replaced '!' with 'not'", at(tok.line, col0, "not ", 1)
        if "unexpected '='" in msg:
            yield "replaced '=' with '=='", at(tok.line, col0, "==", 1)
        m = re.search(r"expected '?([)\]}])'? ", msg)
        if m:
            closer = m.group(1)
            prev = prev_token_end(text, tok.line, tok.col)
            if tok.nl and prev:
                yield f"added the missing '{closer}'", at(prev[0], prev[1], closer)
            elif "comma" in (err.hint or ""):
                yield "added a missing comma", at(tok.line, col0, ", ")
            elif prev:
                yield f"added the missing '{closer}'", at(prev[0], prev[1], closer)
    if "unterminated string" in msg:
        line = lines[L - 1]
        yield 'closed the string with "', with_line(lines, L - 1, line.rstrip() + '"')
    if "expected '{'" in msg and L - 1 < len(lines):
        line = lines[L - 1]
        m = re.match(r"^(\s*)(if|elif|while|for|else)\b(.*?):\s*$", line)
        if m:  # Python-style `if x:` header
            yield "replaced ':' with '{'", with_line(lines, L - 1, f"{m.group(1)}{m.group(2)}{m.group(3)} {{")
    yield from bracket_candidates(text, lines)


def bracket_candidates(text, lines):
    rep = brackets(text, LANG)
    starts = line_starts(text)
    for o, c in rep.mismatched[:1]:
        yield f"replaced '{text[c]}' with '{PAIRS[text[o]]}'", text[:c] + PAIRS[text[o]] + text[c + 1:]
    for c in rep.unmatched[:1]:
        yield f"removed an extra '{text[c]}'", text[:c] + text[c + 1:]
    for o in reversed(rep.unclosed[-3:]):
        ch = text[o]
        line_no, _ = offset_to_pos(starts, o)
        idx = line_no - 1
        base = indent_of(lines[idx])
        if ch == "{":
            j = idx + 1
            while j < len(lines) and (not lines[j].strip() or indent_of(lines[j]) > base):
                j += 1
            new = lines[:j] + [" " * base + "}"] + lines[j:]
            yield "added the missing '}'", "\n".join(new)
        else:
            closer = PAIRS[ch]
            line = lines[idx]
            end = code_end(line, LANG)
            yield f"added the missing '{closer}'", with_line(lines, idx, line[:end] + closer + line[end:])


# =========================================================================== #
#  Analysis
# =========================================================================== #
class Decl:
    __slots__ = ("name", "kind", "tok", "used", "info")

    def __init__(self, name, kind, tok, info=None):
        self.name, self.kind, self.tok, self.info = name, kind, tok, info
        self.used = False


class Scope:
    def __init__(self, parent=None, kind="block"):
        self.parent = parent
        self.kind = kind
        self.names = {}

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return s.names[name]
            s = s.parent
        return None

    def visible(self):
        out, s = set(), self
        while s is not None:
            out.update(s.names)
            s = s.parent
        return out


def tok_edit(tok, new):
    return Edit(tok.line, tok.col - 1, tok.line, tok.col - 1 + tok.length, new)


class NyxAnalyzer:
    def __init__(self, text, program):
        self.text = text
        self.program = program
        self.issues = []
        self.decls = []
        self.undefined = {}   # name -> [(tok, visible names)]
        self.handled = set()

    def add(self, tok, severity, code, message, edits=None, fix=None):
        line = tok.line if tok is not None else 1
        col = tok.col - 1 if tok is not None else 0
        self.issues.append(Issue(line, col, severity, code, message, edits, fix))

    def run(self):
        root = Scope(None, "builtins")
        for name in BUILTINS:
            root.names[name] = Decl(name, "builtin", None)
        top = Scope(root, "module")
        self.block(self.program.stmts, top)
        self.report_undefined()
        for d in self.decls:
            if d.kind in ("let", "const") and not d.used and not d.name.startswith("_") and d.tok is not None:
                self.add(d.tok, "warning", "unused-variable", f"'{d.name}' is declared but never used")
        return self.issues

    # -- declarations --------------------------------------------------------- #
    def declare(self, scope, name, kind, tok, info=None):
        d = Decl(name, kind, tok, info)
        scope.names[name] = d
        if kind in ("let", "const", "fn", "class"):
            self.decls.append(d)
            if name in BUILTINS and name not in ("args",):
                self.add(tok, "warning", "shadow-builtin", f"'{name}' hides the built-in {name}()")
        return d

    def hoist(self, stmts, scope):
        for s in stmts:
            if isinstance(s, Let):
                t = s.target
                names = [t] if isinstance(t, str) else (t[1] + ([t[2]] if len(t) > 2 and t[2] else []))
                for n in names:
                    self.declare(scope, n, "const" if s.const else "let", s.tok)
            elif isinstance(s, FnDecl):
                self.declare(scope, s.name, "fn", s.tok, s)
            elif isinstance(s, ClassDecl):
                self.declare(scope, s.name, "class", s.tok, s)
            elif isinstance(s, Import):
                alias = s.alias or re.sub(r"\W", "_", re.sub(r"\.nyx$", "", s.path.split("/")[-1]))
                d = self.declare(scope, alias, "import", s.tok)
                d.used = True

    # -- statements ----------------------------------------------------------- #
    def block(self, stmts, scope):
        self.hoist(stmts, scope)
        for i, s in enumerate(stmts):
            if isinstance(s, (Return, Break, Continue, Throw)) and i + 1 < len(stmts):
                nxt = stmts[i + 1]
                self.add(nxt.tok, "warning", "unreachable", f"this code never runs (it comes after '{type(s).__name__.lower()}')")
            self.stmt(s, scope)

    def function(self, params, defaults, rest, body, scope, kind="fn", is_expr=False):
        fs = Scope(scope, kind)
        if kind == "method":
            fs.names["self"] = Decl("self", "param", None)
            fs.names["__class__"] = Decl("__class__", "param", None)
        for p, d in zip(params, defaults):
            fs.names[p] = Decl(p, "param", None)
            if d is not None:
                self.expr(d, fs)
        if rest:
            fs.names[rest] = Decl(rest, "param", None)
        if is_expr:
            self.expr(body, fs)
        else:
            self.block(body.stmts, fs)

    def stmt(self, s, scope):
        t = type(s)
        if t is Let:
            if s.value is not None:
                self.expr(s.value, scope)
        elif t is FnDecl:
            self.function(s.params, s.defaults, s.rest, s.body, scope)
        elif t is ClassDecl:
            if s.superclass is not None:
                self.expr(s.superclass, scope)
            for m in s.methods:
                self.function(m.params, m.defaults, m.rest, m.body, scope, "method")
            for m in s.statics:
                self.function(m.params, m.defaults, m.rest, m.body, scope, "fn")
        elif t is If:
            for cond, blk in s.branches:
                self.expr(cond, scope)
                self.block(blk.stmts, Scope(scope))
            if s.other is not None:
                self.block(s.other.stmts, Scope(scope))
        elif t is While:
            self.expr(s.cond, scope)
            self.block(s.body.stmts, Scope(scope, "loop"))
            if isinstance(s.cond, Literal) and s.cond.value is True and not self.escapes(s.body):
                self.add(s.tok, "warning", "infinite-loop", "'while true' has no break, return or throw — it never ends")
        elif t is For:
            self.expr(s.iterable, scope)
            fs = Scope(scope, "loop")
            for v in (s.var, s.var2):
                if v:
                    fs.names[v] = Decl(v, "loop", None)
            self.block(s.body.stmts, fs)
        elif t in (Return, Throw):
            if s.value is not None:
                self.expr(s.value, scope)
        elif t is Try:
            self.block(s.body.stmts, Scope(scope))
            if s.handler is not None:
                cs = Scope(scope)
                if s.name:
                    cs.names[s.name] = Decl(s.name, "catch", None)
                if not s.handler.stmts:
                    self.add(s.tok, "warning", "silent-catch", "the error is silently ignored (empty catch block)")
                self.block(s.handler.stmts, cs)
            if s.final is not None:
                self.block(s.final.stmts, Scope(scope))
        elif t is Block:
            self.block(s.stmts, Scope(scope))
        elif t is ExprStmt:
            self.expr(s.expr, scope)
        elif t is Assign:
            self.assign(s, scope)

    def escapes(self, body):
        stack = list(body.stmts)
        while stack:
            n = stack.pop()
            if isinstance(n, (Return, Throw)):
                return True
            if isinstance(n, Break):
                return True
            if isinstance(n, (While, For, FnDecl, Lambda)):
                continue
            if isinstance(n, Call) and isinstance(n.callee, Var) and n.callee.name == "exit":
                return True
            stack.extend(children(n))
        return False

    def assign(self, s, scope):
        target = s.target
        self.expr(s.value, scope)
        if isinstance(target, Var):
            d = scope.lookup(target.name)
            if d is None:
                visible = scope.visible() | BUILTINS
                close = difflib.get_close_matches(target.name, sorted(visible - BUILTINS), 1, 0.75)
                if close:
                    self.add(target.tok, "error", "undefined-name",
                             f"assignment to undeclared '{target.name}' — did you mean '{close[0]}'?",
                             [tok_edit(target.tok, close[0])], f"renamed to '{close[0]}'")
                elif s.op == "=":
                    self.add(target.tok, "error", "undeclared-assign", f"'{target.name}' is assigned but never declared",
                             [insert(target.tok.line, target.tok.col - 1, "let ")], f"declared it with 'let {target.name} = ...'")
                else:
                    self.add(target.tok, "error", "undefined-name", f"undefined variable '{target.name}'")
                return
            if s.op != "=":
                d.used = True
            if d.kind == "const" and d.tok is not None:
                self.add(target.tok, "error", "const-reassign", f"'{target.name}' is a constant and can't be reassigned",
                         [Edit(d.tok.line, d.tok.col - 1, d.tok.line, d.tok.col - 1 + 5, "let")],
                         f"changed 'const {target.name}' to 'let {target.name}'")
            elif d.kind in ("fn", "class", "builtin"):
                self.add(target.tok, "warning", "overwrite", f"assigning to '{target.name}' replaces a {d.kind}")
        else:
            self.expr(target, scope)

    # -- expressions ------------------------------------------------------- #
    def expr(self, e, scope):
        if e is None or not isinstance(e, Node):
            return
        t = type(e)
        if t is Var:
            self.resolve(e, scope)
            return
        if t is SelfExpr or t is SuperExpr:
            if scope.lookup("self") is None:
                word = "self" if t is SelfExpr else "super"
                self.add(e.tok, "error", "self-outside", f"'{word}' can only be used inside a class method")
            return
        if t is Get and isinstance(e.obj, Var) and e.obj.name == "console" and e.name in ("log", "info", "error", "warn") \
                and scope.lookup("console") is None:
            v = e.obj.tok
            end = e.tok.col - 1 + e.tok.length
            self.add(v, "error", "foreign-syntax", f"console.{e.name} is JavaScript — NYX uses print()",
                     [Edit(v.line, v.col - 1, e.tok.line, end, "print")], "replaced with print")
            self.handled.add(id(e.obj))
            return
        if t is Lambda:
            self.function(e.params, e.defaults, e.rest, e.body, scope, "fn", e.is_expr)
            return
        if t is ListComp:
            self.expr(e.iterable, scope)
            cs = Scope(scope)
            for v in (e.var, e.var2):
                if v:
                    cs.names[v] = Decl(v, "loop", None)
            self.expr(e.expr, cs)
            self.expr(e.cond, cs)
            return
        if t is Match:
            self.expr(e.subject, scope)
            for patterns, guard, body, is_block in e.arms:
                ms = Scope(scope)
                for p in patterns:
                    if isinstance(p, PBind):
                        ms.names[p.name] = Decl(p.name, "match", None)
                self.expr(guard, ms)
                if is_block:
                    self.block(body.stmts, ms)
                else:
                    self.expr(body, ms)
            return
        if t is Call:
            self.check_call(e, scope)
        for child in children(e):
            if isinstance(child, Node):
                self.expr(child, scope)

    def check_call(self, call, scope):
        if not isinstance(call.callee, Var) or any(isinstance(a, Spread) for a in call.args):
            return
        d = scope.lookup(call.callee.name)
        if d is None or d.kind != "fn" or d.info is None:
            return
        fn = d.info
        n = len(call.args)
        required = sum(1 for x in fn.defaults if x is None)
        if n < required:
            missing = fn.params[n] if n < len(fn.params) else "?"
            self.add(call.tok, "error", "arg-count", f"{fn.name}() needs {required} argument(s) but gets {n} — '{missing}' is missing")
        elif fn.rest is None and n > len(fn.params):
            self.add(call.tok, "error", "arg-count", f"{fn.name}() takes {len(fn.params)} argument(s) but gets {n}")

    def resolve(self, var, scope):
        if id(var) in self.handled:
            return
        d = scope.lookup(var.name)
        if d is not None:
            d.used = True
            return
        self.undefined.setdefault(var.name, []).append((var.tok, scope.visible()))

    def report_undefined(self):
        for name, uses in self.undefined.items():
            tok, visible = uses[0]
            count = f" (used {len(uses)} times)" if len(uses) > 1 else ""
            target = ALIASES.get(name)
            if target == "self" and not any("self" in v for _, v in uses):
                target = None
            if target is None:
                pool = sorted(visible - BUILTINS)
                close = difflib.get_close_matches(name, pool, 1, 0.75) or \
                    difflib.get_close_matches(name, sorted(BUILTINS), 1, 0.78)
                target = close[0] if close else None
                msg = f"undefined variable '{name}'{count}" + (f" — did you mean '{target}'?" if target else "")
            else:
                msg = f"'{name}' isn't NYX — use '{target}'{count}"
            if target:
                self.add(tok, "error", "undefined-name", msg, [tok_edit(t, target) for t, _ in uses], f"replaced with '{target}'")
            else:
                self.add(tok, "error", "undefined-name", msg)
