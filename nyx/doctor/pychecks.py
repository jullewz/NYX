"""Python checks for `nyx doctor`.

Two stages:
1. Syntax repair — while the code doesn't parse, generate candidate fixes for the
   reported error and keep the first one that makes the parser get further.
2. Analysis — walk the syntax tree looking for bugs, and attach precise text edits
   to every problem that can be fixed safely.
"""

import ast
import builtins
import difflib
import io
import keyword
import re
import tokenize
import warnings
from collections import defaultdict

from .core import (
    CLOSERS, CODE, COMMENT, PAIRS, Edit, Issue, brackets, code_end, indent_of, insert,
    line_starts, offset_to_pos, scan, sub_code,
)

LANG = "python"
HEADER_RE = re.compile(r"^(\s*)(async\s+)?(def|class|if|elif|else|for|while|try|except|finally|with|match|case)\b")

BUILTIN_NAMES = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__builtins__", "__spec__", "__loader__", "__package__",
    "__path__", "__annotations__", "__debug__", "__dict__", "__class__", "__module__", "__qualname__",
}
PUBLIC_BUILTINS = sorted(n for n in dir(builtins) if not n.startswith("_"))

ALIASES = {
    "true": "True", "false": "False", "null": "None", "none": "None", "nil": "None",
    "undefined": "None", "this": "self", "println": "print", "printf": "print", "echo": "print",
    "puts": "print", "lenght": "len", "lengh": "len", "Len": "len", "Print": "print",
    "Input": "input", "string": "str", "integer": "int", "boolean": "bool", "dictionary": "dict",
}

MODULES = {
    "os", "sys", "re", "json", "math", "random", "time", "datetime", "collections", "itertools",
    "functools", "subprocess", "pathlib", "typing", "string", "shutil", "glob", "csv", "hashlib",
    "base64", "socket", "threading", "logging", "argparse", "copy", "statistics", "uuid", "urllib",
    "http", "sqlite3", "pickle", "textwrap", "io", "decimal", "fractions", "operator", "heapq",
    "bisect", "secrets", "platform", "getpass", "tempfile", "zipfile", "struct", "traceback",
    "inspect", "asyncio", "enum", "dataclasses", "contextlib", "unittest", "pprint", "calendar",
    "signal", "queue", "array", "difflib", "html", "xml", "webbrowser", "turtle", "tkinter",
}

FROM_IMPORTS = {
    "defaultdict": "collections", "Counter": "collections", "deque": "collections",
    "namedtuple": "collections", "OrderedDict": "collections", "dataclass": "dataclasses",
    "field": "dataclasses", "Path": "pathlib", "timedelta": "datetime", "date": "datetime",
    "List": "typing", "Dict": "typing", "Optional": "typing", "Any": "typing", "Tuple": "typing",
    "Union": "typing", "Callable": "typing", "Iterable": "typing", "Set": "typing",
    "partial": "functools", "reduce": "functools", "lru_cache": "functools", "wraps": "functools",
    "randint": "random", "choice": "random", "shuffle": "random", "uniform": "random",
    "sqrt": "math", "floor": "math", "ceil": "math", "pi": "math", "sleep": "time",
    "pprint": "pprint", "Enum": "enum", "contextmanager": "contextlib", "copy": "copy",
    "deepcopy": "copy", "dedent": "textwrap", "ABC": "abc", "abstractmethod": "abc",
}

SHADOWABLE = {
    "list", "dict", "str", "int", "float", "set", "tuple", "id", "type", "input", "sum", "max", "min",
    "len", "print", "open", "filter", "map", "range", "sorted", "object", "format", "hash", "iter",
    "next", "vars", "dir", "all", "any", "bytes", "compile", "zip", "round", "abs", "bool",
}
SECRET_NAME = re.compile(r"(?i)(password|passwd|secret|api_?key|token|private_?key)")


def parse_error(text):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(text)
        return None
    except SyntaxError as e:
        return e
    except ValueError as e:  # e.g. null bytes
        err = SyntaxError(str(e))
        err.lineno, err.offset = 1, 1
        return err


def err_key(e):
    return (e.lineno or 0, e.offset or 0)


def clean_msg(e):
    msg = e.msg or "invalid syntax"
    return msg[0].lower() + msg[1:] if msg else msg


# =========================================================================== #
#  Stage 1: syntax repair
# =========================================================================== #
TOKENIZER_LEVEL = ("unmatched", "never closed", "does not match", "unterminated", "eof while",
                   "eol while", "indent", "tabs")


def is_tok_level(e):
    msg = (e.msg or "").lower()
    return any(k in msg for k in TOKENIZER_LEVEL)


def improved(err, after):
    """A fix is kept if the code now parses, the parser gets further, or a
    tokenizer-level problem (brackets, strings, indentation) is gone — those are
    reported before ordinary syntax errors, so the next error may be earlier."""
    if after is None or err_key(after) > err_key(err):
        return True
    if not is_tok_level(err):
        return False
    return not is_tok_level(after) or (after.msg or "").split("(")[0] != (err.msg or "").split("(")[0]


def repair_syntax(text, history, max_rounds=60):
    fixed = []
    for _ in range(max_rounds):
        err = parse_error(text)
        if err is None:
            return text, fixed, None
        for desc, new in candidates(text, err):
            if not new or new == text:
                continue
            after = parse_error(new)
            if improved(err, after):
                lines = text.split("\n")
                line = min(max(1, err.lineno or 1), len(lines))
                issue = Issue(line, max(0, (err.offset or 1) - 1), "error", "syntax", clean_msg(err), fix=desc)
                issue.fixed = True
                issue.rev = len(history) - 1
                fixed.append(issue)
                text = new
                history.append(text)
                break
        else:
            return text, fixed, err
    return text, fixed, parse_error(text)


def with_line(lines, idx, new):
    out = list(lines)
    out[idx] = new
    return "\n".join(out)


def prev_code_line(lines, idx):
    j = idx - 1
    while j >= 0 and (not lines[j].strip() or lines[j].strip().startswith("#")):
        j -= 1
    return j


def line_rewrites(line):
    """Rewrites for syntax from Python 2 and other languages."""
    out = []

    def add(desc, new):
        if new != line:
            out.append((desc, new))

    add("replaced '&&' with 'and'", sub_code(line, LANG, r"\s*&&\s*", " and "))
    add("replaced '||' with 'or'", sub_code(line, LANG, r"\s*\|\|\s*", " or "))
    add("replaced '!==' with '!='", sub_code(line, LANG, r"!==", "!="))
    add("replaced '===' with '=='", sub_code(line, LANG, r"===", "=="))
    add("replaced '<>' with '!='", sub_code(line, LANG, r"<>", "!="))
    add("replaced '!' with 'not'", sub_code(line, LANG, r"!(?=[\w(])", "not "))
    add("replaced '++' with '+= 1'", sub_code(line, LANG, r"^(\s*)([\w.\[\]]+)\s*\+\+\s*;?\s*$", r"\1\2 += 1"))
    add("replaced '--' with '-= 1'", sub_code(line, LANG, r"^(\s*)([\w.\[\]]+)\s*--\s*;?\s*$", r"\1\2 -= 1"))
    add("turned '//' into a # comment", sub_code(line, LANG, r"^(\s*)//\s?", r"\1# ", 1))
    add("replaced 'else if' with 'elif'", sub_code(line, LANG, r"\belse\s+if\b", "elif"))
    add("rewrote a JavaScript-style function", sub_code(
        line, LANG, r"^(\s*)function\s+(\w+)\s*\((.*)\)\s*\{?\s*$", r"\1def \2(\3):"))
    add("replaced '{' with ':'", sub_code(
        line, LANG, r"^(\s*)(if|elif|while|for|with|def|class|try|else)\b(.*?)\s*\{\s*$",
        lambda m: f"{m.group(1)}{m.group(2)}{(' ' + m.group(3).strip(' ()')) if m.group(3).strip() else ''}:"))
    add("replaced '} else {' with 'else:'", sub_code(line, LANG, r"^(\s*)\}\s*else\s*\{?\s*$", r"\1else:"))
    add("added missing '()' to def", sub_code(line, LANG, r"^(\s*(?:async\s+)?def\s+\w+)\s*:", r"\1():"))
    add("converted Python 2 except syntax", sub_code(
        line, LANG, r"\bexcept\s+(\([^)]*\)|[\w.]+)\s*,\s*(\w+)\s*:", r"except \1 as \2:"))
    add("converted Python 2 raise syntax", sub_code(
        line, LANG, r"^(\s*)raise\s+([\w.]+)\s*,\s*(.+?)\s*$", r"\1raise \2(\3)"))
    add("converted Python 2 octal number", sub_code(line, LANG, r"(?<![\w.])0([0-7]+)(?![\w.])", r"0o\1"))
    m = re.match(r"^(\s*)print[ \t]+(?![=(,.\[])(?!>>)(.+?)\s*$", line)
    if m and scan(line, LANG)[len(m.group(1))] == CODE and not re.match(r"^\s*print\s*=", line):
        body = m.group(2)
        if body.endswith(","):
            new = f'{m.group(1)}print({body[:-1].rstrip()}, end=" ")'
        else:
            new = f"{m.group(1)}print({body})"
        add("converted Python 2 print to print()", new)
    hm = re.match(r"^(\s*)(if|elif|while)\b", line)
    if hm:
        add("replaced '=' with '==' in the condition",
            sub_code(line, LANG, r"(?<![=!<>:+\-*/%&|^@])=(?!=)", "==", 1))
    return out


def candidates(text, err):
    lines = text.split("\n")
    L = min(max(1, err.lineno or len(lines)), len(lines))
    msg = (err.msg or "").lower()
    idxs = [i for i in (L - 1, L - 2, L - 3) if 0 <= i < len(lines)]

    # 0. curly-brace blocks (JavaScript / C / Java habits) -> indentation
    if BRACE_HEADER.search(text):
        yield "converted { } blocks to Python indentation", braces_to_python(text)

    # 1. foreign syntax on the error line or the line above
    for i in idxs[:2]:
        for desc, new in line_rewrites(lines[i]):
            yield desc, with_line(lines, i, new)

    # 2. indentation problems
    if isinstance(err, TabError) or "inconsistent use of tabs" in msg:
        yield "converted tabs to spaces", "\n".join(
            re.sub(r"^[ \t]+", lambda m: m.group(0).expandtabs(4), l) for l in lines)
    if "expected an indented block" in msg:
        target = L - 1
        h = prev_code_line(lines, target)
        if h >= 0:
            hind = indent_of(lines[h])
            if target < len(lines) and lines[target].strip() and indent_of(lines[target]) <= hind:
                yield "indented the block body", with_line(lines, target, " " * (hind + 4) + lines[target].lstrip())
            new = lines[:h + 1] + [" " * (hind + 4) + "pass"] + lines[h + 1:]
            yield "added 'pass' to the empty block", "\n".join(new)
    if "unexpected indent" in msg:
        target = L - 1
        p = prev_code_line(lines, target)
        pind = indent_of(lines[p]) if p >= 0 else 0
        yield "removed unexpected indentation", with_line(lines, target, " " * pind + lines[target].lstrip())
    if "unindent does not match" in msg or "dedent" in msg:
        target = L - 1
        cur = indent_of(lines[target])
        levels = sorted({indent_of(lines[j]) for j in range(target) if lines[j].strip()} | {0})
        lower = [lv for lv in levels if lv <= cur]
        for lv in ([lower[-1]] if lower else []) + [lv for lv in levels if lv > cur][:1]:
            yield "fixed the indentation level", with_line(lines, target, " " * lv + lines[target].lstrip())

    # 3. missing colon after a block header
    for i in idxs:
        line = lines[i]
        m = HEADER_RE.match(line)
        if not m:
            continue
        end = code_end(line, LANG)
        code = line[:end]
        if not code.strip() or code.endswith(":"):
            continue
        rep = brackets(code, LANG)
        if rep.unclosed or rep.unmatched or rep.mismatched:
            continue
        yield f"added the missing ':' after '{m.group(3)}'", with_line(lines, i, code + ":" + line[end:])

    # 4. unterminated strings
    if "unterminated" in msg or "eol while scanning" in msg or "eof while scanning" in msg:
        if "triple" in msg:
            start = lines[L - 1][max(0, (err.offset or 1) - 1):]
            q = '"""' if '"""' in start else "'''"
            yield f"closed the triple-quoted string with {q}", text.rstrip("\n") + q + "\n"
        else:
            line = lines[L - 1]
            col = max(0, (err.offset or 1) - 1)
            q = next((ch for ch in line[col:col + 4] if ch in "\"'"), None)
            if q is None:
                q = '"' if line.count('"') % 2 else "'"
            yield f"closed the string with {q}", with_line(lines, L - 1, line.rstrip() + q)

    # 5. brackets
    yield from bracket_candidates(text, lines)

    # 6. missing commas between items
    yield from comma_candidates(text, L)


BRACE_HEADER = re.compile(r"(?m)^\s*(?:\}\s*)?(?:if|elif|else|while|for|def|class|try|with|function)\b[^\n#]*\{\s*$")


def strip_parens(s):
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(s) - 1:
                return s
        return s[1:-1].strip()
    return s


def braces_to_python(text):
    out = []
    for line in text.split("\n"):
        s = line.strip()
        ind = line[:len(line) - len(line.lstrip())]
        if s in ("}", "};"):
            continue
        m = re.match(r"^\}?\s*(else\s+if|elif)\s*(.*?)\s*\{$", s)
        if m:
            out.append(f"{ind}elif {strip_parens(m.group(2))}:")
            continue
        if re.match(r"^\}?\s*else\s*\{$", s):
            out.append(f"{ind}else:")
            continue
        m = re.match(r"^\}?\s*catch\s*\(?\s*(\w*)\s*\)?\s*\{$", s)
        if m:
            out.append(f"{ind}except Exception" + (f" as {m.group(1)}:" if m.group(1) else ":"))
            continue
        m = re.match(r"^function\s+(\w+)\s*\((.*)\)\s*\{$", s)
        if m:
            out.append(f"{ind}def {m.group(1)}({m.group(2)}):")
            continue
        m = re.match(r"^(if|while|for|def|class|try|with)\b\s*(.*?)\s*\{$", s)
        if m:
            rest = strip_parens(m.group(2)) if m.group(1) in ("if", "while", "for") else m.group(2)
            out.append(f"{ind}{m.group(1)}{(' ' + rest) if rest else ''}:")
            continue
        out.append(line[:-1].rstrip() if s.endswith(";") and not s.startswith("#") else line)
    return "\n".join(out)


def statement_end_line(lines, idx):
    base = indent_of(lines[idx])
    last, j = idx, idx + 1
    while j < len(lines):
        s = lines[j]
        if not s.strip():
            j += 1
            continue
        stripped = s.strip()
        if indent_of(s) <= base and stripped[0] not in ")]}" and not stripped.startswith(("and ", "or ", "+", "-", "*", "/")):
            break
        last = j
        j += 1
    return last


def bracket_candidates(text, lines):
    rep = brackets(text, LANG)
    starts = line_starts(text)
    for o, c in rep.mismatched[:1]:
        expected = PAIRS[text[o]]
        yield f"replaced '{text[c]}' with '{expected}'", text[:c] + expected + text[c + 1:]
    for c in rep.unmatched[:1]:
        yield f"removed an extra '{text[c]}'", text[:c] + text[c + 1:]
    for o in reversed(rep.unclosed[-3:]):
        closer = PAIRS[text[o]]
        line_no, _ = offset_to_pos(starts, o)
        end_idx = statement_end_line(lines, line_no - 1)
        if end_idx > line_no - 1:  # multi-line list/call: closer on its own line
            new = lines[:end_idx + 1] + [" " * indent_of(lines[line_no - 1]) + closer] + lines[end_idx + 1:]
            yield f"added the missing '{closer}'", "\n".join(new)
        line = lines[end_idx]
        end = code_end(line, LANG)
        yield f"added the missing '{closer}'", with_line(lines, end_idx, line[:end] + closer + line[end:])


VALUE_END = {tokenize.NAME, tokenize.NUMBER, tokenize.STRING}
FSTRING_END = getattr(tokenize, "FSTRING_END", None)
FSTRING_START = getattr(tokenize, "FSTRING_START", None)


def safe_tokens(text):
    toks = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            toks.append(t)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return toks


def comma_candidates(text, L):
    toks = [t for t in safe_tokens(text) if t.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT,
                                                           tokenize.DEDENT, tokenize.NEWLINE)]
    depth = 0
    lines = text.split("\n")
    for a, b in zip(toks, toks[1:]):
        if a.type == tokenize.OP and a.string in PAIRS:
            depth += 1
        elif a.type == tokenize.OP and a.string in CLOSERS:
            depth = max(0, depth - 1)
        if depth <= 0 or not (L - 2 <= b.start[0] <= L + 1):
            continue
        a_end = (a.type in VALUE_END and not (a.type == tokenize.NAME and keyword.iskeyword(a.string)
                                             and a.string not in ("True", "False", "None"))) \
            or (a.type == tokenize.OP and a.string in CLOSERS) or a.type == FSTRING_END
        b_start = (b.type in VALUE_END and not (b.type == tokenize.NAME and keyword.iskeyword(b.string)
                                               and b.string not in ("True", "False", "None", "lambda", "not"))) \
            or b.type == FSTRING_START
        if a.type in (tokenize.STRING, FSTRING_END) and b.type in (tokenize.STRING, FSTRING_START):
            continue  # implicit string concatenation is valid
        if a_end and b_start:
            line = lines[a.end[0] - 1]
            col = len(line.encode("utf-8")[:a.end[1]].decode("utf-8", "ignore")) if not line.isascii() else a.end[1]
            yield "added a missing comma", with_line(lines, a.end[0] - 1, line[:col] + "," + line[col:])
            return


# =========================================================================== #
#  Stage 2: analysis
# =========================================================================== #
class PyAnalyzer:
    def __init__(self, text, tree):
        self.text = text
        self.lines = text.split("\n")
        self.tree = tree
        self.issues = []
        self.parents = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p):
                self.parents[c] = p

    # -- helpers -------------------------------------------------------------- #
    def ccol(self, lineno, bcol):
        if not 0 < lineno <= len(self.lines):
            return bcol
        line = self.lines[lineno - 1]
        return bcol if line.isascii() else len(line.encode("utf-8")[:bcol].decode("utf-8", "ignore"))

    def replace(self, node, new):
        return Edit(node.lineno, self.ccol(node.lineno, node.col_offset),
                    node.end_lineno, self.ccol(node.end_lineno, node.end_col_offset), new)

    def seg(self, node):
        return ast.get_source_segment(self.text, node) or ""

    def add(self, node, severity, code, message, edits=None, fix=None):
        line = getattr(node, "lineno", 1)
        col = self.ccol(line, getattr(node, "col_offset", 0))
        self.issues.append(Issue(line, col, severity, code, message, edits, fix))

    def enclosing(self, node, types):
        p = self.parents.get(node)
        while p is not None and not isinstance(p, types):
            p = self.parents.get(p)
        return p

    def run(self):
        for check in (self.check_names, self.check_imports, self.check_compare, self.check_except,
                      self.check_fstrings, self.check_defaults, self.check_methods, self.check_asserts,
                      self.check_dict_keys, self.check_unreachable, self.check_shadowing, self.check_concat,
                      self.check_input_numbers, self.check_loops, self.check_security, self.check_unused_locals,
                      self.check_redefinitions):
            check()
        return self.issues

    # -- undefined names ------------------------------------------------------ #
    def check_names(self):
        defined, loads, star = set(), defaultdict(list), False
        for n in ast.walk(self.tree):
            t = type(n)
            if t is ast.Name:
                if isinstance(n.ctx, ast.Load):
                    loads[n.id].append(n)
                else:
                    defined.add(n.id)
            elif t in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef):
                defined.add(n.name)
            elif t is ast.arg:
                defined.add(n.arg)
            elif t is ast.Import:
                for a in n.names:
                    defined.add(a.asname or a.name.split(".")[0])
            elif t is ast.ImportFrom:
                for a in n.names:
                    if a.name == "*":
                        star = True
                    else:
                        defined.add(a.asname or a.name)
            elif t is ast.ExceptHandler and n.name:
                defined.add(n.name)
            elif t in (ast.Global, ast.Nonlocal):
                defined.update(n.names)
            elif getattr(n, "name", None) and t.__name__ in ("MatchAs", "MatchStar"):
                defined.add(n.name)
            elif t.__name__ == "MatchMapping" and getattr(n, "rest", None):
                defined.add(n.rest)
        self.defined = defined
        self.used_names = set(loads)
        module_defined = self.module_names()
        if star:
            self.add(self.tree.body[0] if self.tree.body else self.tree, "warning", "star-import",
                     "'from ... import *' hides where names come from — undefined names can't be checked")
            return
        insert_line = self.import_line()
        for name, all_nodes in loads.items():
            if name in BUILTIN_NAMES:
                continue
            if name in defined and name not in module_defined:
                # a method/attribute of the class used without self.?
                missing = defaultdict(list)
                for n in all_nodes:
                    fn = self.enclosing(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    cls = self.parents.get(fn) if fn is not None else None
                    if isinstance(cls, ast.ClassDef) and fn.args.args and name in self.class_members(cls) \
                            and name not in self.local_names(fn):
                        missing[(fn.args.args[0].arg, id(cls))].append(n)
                for (me, _), nodes in missing.items():
                    self.add(nodes[0], "error", "missing-self", f"'{name}' belongs to the class — it needs '{me}.'",
                             [self.replace(n, f"{me}.{name}") for n in nodes], f"changed to '{me}.{name}'")
                if missing:
                    continue
            if name in defined:
                # defined somewhere — but code at module level can only see module-level names
                nodes = [n for n in all_nodes if name not in module_defined
                         and self.enclosing(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)) is None]
                if not nodes:
                    continue
                self.add(nodes[0], "error", "undefined-name",
                         f"'{name}' is only defined inside a function — it doesn't exist here")
                continue
            nodes = all_nodes
            first = nodes[0]
            count = f" (used {len(nodes)} times)" if len(nodes) > 1 else ""
            target = ALIASES.get(name)
            if target == "self" and "self" not in defined:
                target = None
            if target:
                self.add(first, "error", "undefined-name", f"undefined name '{name}'{count}",
                         [self.replace(n, target) for n in nodes], f"replaced with '{target}'")
                continue
            if name in MODULES:
                self.add(first, "error", "missing-import", f"'{name}' is used but never imported",
                         [insert(insert_line, 0, f"import {name}\n")], f"added 'import {name}'")
                continue
            if name in FROM_IMPORTS:
                mod = FROM_IMPORTS[name]
                self.add(first, "error", "missing-import", f"'{name}' is used but never imported",
                         [insert(insert_line, 0, f"from {mod} import {name}\n")], f"added 'from {mod} import {name}'")
                continue
            fn = self.enclosing(first, (ast.FunctionDef, ast.AsyncFunctionDef))
            cls = self.parents.get(fn) if fn is not None else None
            if isinstance(cls, ast.ClassDef) and fn.args.args and name in self.class_members(cls):
                me = fn.args.args[0].arg
                inside = [n for n in nodes if self.enclosing(n, ast.ClassDef) is cls]
                self.add(first, "error", "missing-self", f"'{name}' belongs to the class — it needs '{me}.'",
                         [self.replace(n, f"{me}.{name}") for n in inside], f"changed to '{me}.{name}'")
                continue
            close = (difflib.get_close_matches(name, sorted(defined), 1, 0.75)
                     or difflib.get_close_matches(name, PUBLIC_BUILTINS, 1, 0.78))
            if close:
                self.add(first, "error", "undefined-name", f"undefined name '{name}'{count} — did you mean '{close[0]}'?",
                         [self.replace(n, close[0]) for n in nodes], f"renamed to '{close[0]}'")
            else:
                self.add(first, "error", "undefined-name", f"undefined name '{name}'{count}")

    def module_names(self):
        """Names bound at module level (plus anything a function declares global)."""
        names = set()
        stack = list(self.tree.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif isinstance(node, ast.Import):
                names.update(a.asname or a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif getattr(node, "name", None) and type(node).__name__ in ("MatchAs", "MatchStar"):
                names.add(node.name)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            stack.extend(ast.iter_child_nodes(node))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Global):
                names.update(node.names)
        return names

    def local_names(self, fn):
        names = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not fn:
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
        return names

    def class_members(self, cls):
        names = set()
        for node in ast.walk(cls):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and self.parents.get(node) is cls:
                names.add(node.name)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "self" and isinstance(node.ctx, ast.Store):
                names.add(node.attr)
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign):
                names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        return names

    def import_line(self):
        body = self.tree.body
        i = 0
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            i = 1
        last = None
        for stmt in body[i:]:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                last = stmt
            else:
                break
        if last is not None:
            return last.end_lineno + 1
        if i == 1:
            return body[0].end_lineno + 1
        ln = 1
        while ln <= len(self.lines) and self.lines[ln - 1].startswith("#"):
            ln += 1
        return ln

    # -- imports -------------------------------------------------------------- #
    def check_imports(self):
        exported = set()
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
                if isinstance(stmt.value, (ast.List, ast.Tuple)):
                    exported.update(e.value for e in stmt.value.elts if isinstance(e, ast.Constant))
        used = getattr(self, "used_names", set())
        for stmt in self.tree.body:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(stmt, ast.ImportFrom) and (stmt.module == "__future__" or any(a.name == "*" for a in stmt.names)):
                continue
            unused, kept = [], []
            for a in stmt.names:
                bound = a.asname or a.name.split(".")[0]
                if bound in used or bound in exported or bound == "_":
                    kept.append(a)
                else:
                    unused.append(bound)
            if not unused:
                continue
            names = ", ".join(f"'{u}'" for u in unused)
            if not kept:
                edit = Edit(stmt.lineno, 0, stmt.end_lineno + 1, 0, "")
                fix = "removed the unused import"
            else:
                parts = [a.name + (f" as {a.asname}" if a.asname else "") for a in kept]
                if isinstance(stmt, ast.Import):
                    new = "import " + ", ".join(parts)
                else:
                    new = f"from {'.' * stmt.level}{stmt.module or ''} import " + ", ".join(parts)
                edit = self.replace(stmt, new)
                fix = f"removed {names} from the import"
            self.add(stmt, "warning", "unused-import", f"{names} imported but never used", [edit], fix)

    # -- comparisons ---------------------------------------------------------- #
    def check_compare(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and isinstance(node.operand, ast.Compare) \
                    and len(node.operand.ops) == 1 and isinstance(node.operand.ops[0], (ast.In, ast.Is)):
                cmp = node.operand
                op = "not in" if isinstance(cmp.ops[0], ast.In) else "is not"
                new = f"{self.seg(cmp.left)} {op} {self.seg(cmp.comparators[0])}"
                bad = "not x in y" if op == "not in" else "not x is y"
                good = "x not in y" if op == "not in" else "x is not y"
                self.add(node, "style", "not-in", f"write '{good}' instead of '{bad}'",
                         [self.replace(node, new)], f"rewrote as '{op}'")
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            op, left, right = node.ops[0], node.left, node.comparators[0]
            is_none = lambda n: isinstance(n, ast.Constant) and n.value is None  # noqa: E731
            if isinstance(op, (ast.Eq, ast.NotEq)) and (is_none(right) or is_none(left)):
                other = left if is_none(right) else right
                word = "is" if isinstance(op, ast.Eq) else "is not"
                self.add(node, "warning", "none-compare", f"compare with None using '{word}', not '{'==' if word == 'is' else '!='}'",
                         [self.replace(node, f"{self.seg(other)} {word} None")], f"changed to '{word} None'")
                continue
            if isinstance(op, (ast.Is, ast.IsNot)):
                lit = right if isinstance(right, ast.Constant) else left if isinstance(left, ast.Constant) else None
                if lit is not None and lit.value is not None and lit.value is not ... and not isinstance(lit.value, bool):
                    word = "==" if isinstance(op, ast.Is) else "!="
                    self.add(node, "error", "is-literal", f"'is' checks identity, not equality — use '{word}' with a value",
                             [self.replace(node, f"{self.seg(left)} {word} {self.seg(right)}")], f"changed to '{word}'")
                continue
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and isinstance(right.value, bool):
                parent = self.parents.get(node)
                in_test = isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and getattr(parent, "test", None) is node
                truthy = (right.value is True) == isinstance(op, ast.Eq)
                if in_test:
                    inner = self.seg(left)
                    if not isinstance(left, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
                        inner = f"({inner})"
                    new = inner if truthy else f"not {inner}"
                    self.add(node, "style", "bool-compare", f"no need to compare with {right.value}",
                             [self.replace(node, new)], f"simplified to '{new}'")
                continue
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(left, ast.Call) and isinstance(left.func, ast.Name) \
                    and left.func.id == "type" and len(left.args) == 1 and not left.keywords \
                    and isinstance(right, (ast.Name, ast.Attribute)):
                new = f"isinstance({self.seg(left.args[0])}, {self.seg(right)})"
                new = new if isinstance(op, ast.Eq) else f"not {new}"
                self.add(node, "warning", "type-compare", "use isinstance() to check types",
                         [self.replace(node, new)], f"changed to '{new}'")
                continue
            if isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.Gt)) and isinstance(left, ast.Name) \
                    and isinstance(right, ast.Name) and left.id == right.id:
                self.add(node, "warning", "self-compare", f"'{left.id}' is compared with itself")

    # -- exceptions ----------------------------------------------------------- #
    def check_except(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                line = self.lines[node.lineno - 1]
                col = self.ccol(node.lineno, node.col_offset)
                m = re.compile(r"except(\s*):").match(line, col)
                if m:
                    self.add(node, "warning", "bare-except",
                             "bare 'except:' also catches Ctrl+C and exit() — catch Exception instead",
                             [Edit(node.lineno, m.start(), node.lineno, m.end(), "except Exception:")],
                             "changed to 'except Exception:'")
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                self.add(node, "warning", "silent-except", "the error is silently ignored (except: pass)")

    # -- f-strings ------------------------------------------------------------ #
    def check_fstrings(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            if any(isinstance(v, ast.FormattedValue) for v in node.values):
                continue
            seg = self.seg(node)
            m = re.fullmatch(r'([rR]?)[fF]([rR]?)("(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\')', seg)
            if not m:
                continue
            body = m.group(3).replace("{{", "{").replace("}}", "}")
            new = m.group(1) + m.group(2) + body
            self.add(node, "style", "fstring-no-placeholder", "f-string has no {placeholders}",
                     [self.replace(node, new)], "removed the f prefix")

    # -- mutable default arguments ------------------------------------------- #
    def check_defaults(self):
        for fn in ast.walk(self.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = fn.args.posonlyargs + fn.args.args
            pairs = list(zip(positional[len(positional) - len(fn.args.defaults):], fn.args.defaults))
            pairs += [(a, d) for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None]
            for arg, default in pairs:
                mutable = isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                    isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                    and default.func.id in ("list", "dict", "set") and not default.args)
                if not mutable:
                    continue
                body = fn.body
                first = body[0]
                if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                        and isinstance(first.value.value, str) and len(body) > 1:
                    first = body[1]
                msg = f"mutable default argument '{arg.arg}={self.seg(default)}' is shared between calls"
                if first.lineno == fn.lineno or first.lineno > len(self.lines):
                    self.add(default, "warning", "mutable-default", msg)
                    continue
                ind = " " * self.ccol(first.lineno, first.col_offset)
                guard = f"{ind}if {arg.arg} is None:\n{ind}    {arg.arg} = {self.seg(default)}\n"
                self.add(default, "warning", "mutable-default", msg,
                         [self.replace(default, "None"), insert(first.lineno, 0, guard)],
                         f"default is now None and created inside the function")

    # -- methods -------------------------------------------------------------- #
    def check_methods(self):
        for cls in ast.walk(self.tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decos = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in fn.decorator_list}
                if decos & {"staticmethod", "classmethod"}:
                    continue
                args = fn.args.posonlyargs + fn.args.args
                if not args and fn.args.vararg is None:
                    line = self.lines[fn.lineno - 1]
                    m = re.compile(r"def\s+" + re.escape(fn.name) + r"\s*\(").search(line)
                    if m:
                        after = line[m.end():].lstrip()
                        text = "self" if after.startswith(")") else "self, "
                        self.add(fn, "error", "missing-self", f"method '{fn.name}' is missing 'self'",
                                 [insert(fn.lineno, m.end(), text)], "added 'self'")
                elif args and args[0].arg not in ("self", "cls", "_self", "mcs", "klass"):
                    self.add(fn, "warning", "first-arg", f"the first argument of method '{fn.name}' is usually 'self', not '{args[0].arg}'")

    # -- asserts, dict keys, unreachable code ---------------------------------- #
    def check_asserts(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) and node.test.elts:
                elts = node.test.elts
                if len(elts) == 2 and node.msg is None:
                    new = f"{self.seg(elts[0])}, {self.seg(elts[1])}"
                    self.add(node, "error", "assert-tuple", "assert on a tuple is always true",
                             [self.replace(node.test, new)], "turned it into 'assert condition, message'")
                else:
                    self.add(node, "error", "assert-tuple", "assert on a tuple is always true")

    def check_dict_keys(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Dict):
                continue
            seen = {}
            for i, k in enumerate(node.keys):
                if not isinstance(k, ast.Constant):
                    continue
                key = (type(k.value).__name__, k.value)
                if key in seen:
                    j = seen[key]
                    earlier_val = node.values[j]
                    msg = f"duplicate key {self.seg(k)} — the first value is silently replaced"
                    nxt = node.keys[j + 1] if node.keys[j + 1] is not None else node.values[j + 1]
                    if isinstance(earlier_val, (ast.Constant, ast.Name, ast.Attribute)):
                        first = node.keys[j]
                        edit = Edit(first.lineno, self.ccol(first.lineno, first.col_offset),
                                    nxt.lineno, self.ccol(nxt.lineno, nxt.col_offset), "")
                        self.add(k, "warning", "duplicate-key", msg, [edit], "removed the first, unused entry")
                    else:
                        self.add(k, "warning", "duplicate-key", msg)
                seen[key] = i

    def check_unreachable(self):
        for node in ast.walk(self.tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        kind = type(stmt).__name__.lower()
                        self.add(block[i + 1], "warning", "unreachable", f"this code never runs (it comes after '{kind}')")
                        break

    # -- shadowing builtins --------------------------------------------------- #
    def check_shadowing(self):
        seen = set()
        for node in ast.walk(self.tree):
            name = None
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, ast.arg):
                name = node.arg
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not isinstance(self.parents.get(node), ast.ClassDef):
                name = node.name  # (methods live on the class, so they don't hide anything)
            if name in SHADOWABLE and name not in seen:
                seen.add(name)
                self.add(node, "warning", "shadow-builtin", f"'{name}' hides the built-in {name}() — pick another name")

    # -- string + number ------------------------------------------------------ #
    def check_concat(self):
        num_calls = {"len", "int", "float", "sum", "round", "abs", "max", "min", "ord"}

        def is_text(n):
            return (isinstance(n, ast.Constant) and isinstance(n.value, str)) or isinstance(n, ast.JoinedStr)

        def is_number(n):
            return (isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)) \
                or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in num_calls)

        for node in ast.walk(self.tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                for text_side, num_side in ((node.left, node.right), (node.right, node.left)):
                    if is_text(text_side) and is_number(num_side):
                        self.add(num_side, "error", "str-plus-number", "can't add a number to a string (TypeError)",
                                 [self.replace(num_side, f"str({self.seg(num_side)})")], "wrapped the number in str()")
                        break

    # -- input() used as a number --------------------------------------------- #
    def check_input_numbers(self):
        scopes = defaultdict(dict)
        converted = defaultdict(set)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                scope = self.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                v = node.value
                name = node.targets[0].id
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "input":
                    scopes[scope][name] = v
                elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in ("int", "float") \
                        and v.args and isinstance(v.args[0], ast.Name) and v.args[0].id == name:
                    converted[scope].add(name)
        for scope, calls in scopes.items():
            for name, call in calls.items():
                if name in converted[scope]:
                    continue
                numeric, use = None, None
                for node in ast.walk(scope or self.tree):
                    if self.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef)) is not scope:
                        continue
                    pairs = []
                    if isinstance(node, ast.Compare) and len(node.ops) == 1 and \
                            isinstance(node.ops[0], (ast.Lt, ast.Gt, ast.LtE, ast.GtE, ast.Eq, ast.NotEq)):
                        pairs.append((node.left, node.comparators[0]))
                    elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.Add)):
                        pairs.append((node.left, node.right))
                    for a, b in pairs:
                        for x, y in ((a, b), (b, a)):
                            if isinstance(x, ast.Name) and x.id == name and isinstance(y, ast.Constant) \
                                    and isinstance(y.value, (int, float)) and not isinstance(y.value, bool):
                                numeric = "float" if isinstance(y.value, float) or numeric == "float" else "int"
                                use = use or node
                if numeric:
                    self.add(use, "error", "input-not-number",
                             f"input() gives text, but '{name}' is used as a number",
                             [self.replace(call, f"{numeric}({self.seg(call)})")],
                             f"converted the input with {numeric}()")

    # -- loops ---------------------------------------------------------------- #
    def check_loops(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
                escapes = False
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Return, ast.Raise)):
                        escapes = True
                    elif isinstance(inner, ast.Break) and self.enclosing(inner, (ast.While, ast.For, ast.AsyncFor)) is node:
                        escapes = True
                    elif isinstance(inner, ast.Call) and isinstance(inner.func, (ast.Name, ast.Attribute)) and \
                            (getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)) in ("exit", "_exit", "quit"):
                        escapes = True
                if not escapes:
                    self.add(node, "warning", "infinite-loop", "'while True' loop has no break, return or exit — it never ends")
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name) \
                    and node.iter.func.id == "range" and len(node.iter.args) == 1:
                arg = node.iter.args[0]
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "len":
                    self.add(node, "style", "range-len", "consider 'for i, item in enumerate(...)' instead of range(len(...))")

    # -- security ------------------------------------------------------------- #
    def check_security(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
                kw = {k.arg: k.value for k in node.keywords}
                if name in ("eval", "exec") and isinstance(f, ast.Name):
                    self.add(node, "warning", "security", f"{name}() runs any code it is given — dangerous with user input")
                elif base == "subprocess" and isinstance(kw.get("shell"), ast.Constant) and kw["shell"].value is True:
                    self.add(node, "warning", "security", "subprocess with shell=True can run injected commands")
                elif base == "os" and name == "system":
                    self.add(node, "style", "security", "prefer subprocess.run([...]) over os.system()")
                elif base == "pickle" and name in ("load", "loads"):
                    self.add(node, "warning", "security", "unpickling untrusted data can run arbitrary code")
                elif base == "yaml" and name == "load" and "Loader" not in kw:
                    self.add(node, "warning", "security", "yaml.load() without a Loader is unsafe",
                             [self.replace(f, "yaml.safe_load")], "changed to yaml.safe_load()")
                elif isinstance(kw.get("verify"), ast.Constant) and kw["verify"].value is False:
                    self.add(node, "warning", "security", "verify=False turns off HTTPS certificate checks")
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) \
                    and len(node.value.value) >= 6:
                for t in node.targets:
                    if isinstance(t, ast.Name) and SECRET_NAME.search(t.id):
                        self.add(node, "warning", "hardcoded-secret",
                                 f"'{t.id}' looks like a hardcoded secret — load it from an environment variable")

    # -- unused local variables ------------------------------------------------ #
    def check_unused_locals(self):
        for fn in ast.walk(self.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stores, loads, declared = {}, set(), set()
            uses_locals = False
            for node in ast.walk(fn):
                if isinstance(node, ast.Name):
                    if isinstance(node.ctx, ast.Load):
                        loads.add(node.id)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    declared.update(node.names)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("locals", "vars"):
                    uses_locals = True
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    loads.add(node.target.id)
            if uses_locals:
                continue
            for stmt in ast.walk(fn):
                if self.enclosing(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)) is not fn:
                    continue
                targets = []
                if isinstance(stmt, ast.Assign):
                    targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None and isinstance(stmt.target, ast.Name):
                    targets = [stmt.target]
                for t in targets:
                    stores.setdefault(t.id, t)
            for name, node in stores.items():
                if name not in loads and name not in declared and not name.startswith("_"):
                    self.add(node, "warning", "unused-variable", f"local variable '{name}' is assigned but never used")

    def check_redefinitions(self):
        for scope in [self.tree] + [n for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)]:
            seen = {}
            for stmt in scope.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not stmt.decorator_list:
                    if stmt.name in seen:
                        self.add(stmt, "warning", "redefined",
                                 f"'{stmt.name}' is defined again — the version on line {seen[stmt.name]} is replaced")
                    seen[stmt.name] = stmt.lineno


# =========================================================================== #
#  Python-only style: semicolons
# =========================================================================== #
def semicolons(text):
    toks = safe_tokens(text)
    edits = []
    for a, b in zip(toks, toks[1:]):
        if a.type == tokenize.OP and a.string == ";" and b.type in (tokenize.NEWLINE, tokenize.COMMENT, tokenize.NL):
            line = text.split("\n")[a.start[0] - 1]
            if line.isascii():
                edits.append(Edit(a.start[0], a.start[1], a.end[0], a.end[1], ""))
    if not edits:
        return None
    issue = Issue(edits[0].line, edits[0].col, "style", "semicolon",
                  f"unneeded ';' at the end of {len(edits)} line(s)", edits, "removed the semicolons")
    return issue


__all__ = ["repair_syntax", "PyAnalyzer", "parse_error", "semicolons", "clean_msg", "COMMENT", "insert", "scan"]
