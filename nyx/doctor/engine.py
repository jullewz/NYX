"""Runs the full diagnosis: syntax repair → analysis with auto-fixes (in rounds,
re-checking after every round) → style fixes → verification.

Every change creates a new revision of the text. Each issue remembers the
revision it was found in, and at the end its line number is mapped back to the
original file, so the report always matches the code the user gave us."""

import re

from . import nyxchecks, pychecks
from .core import Issue, apply_issues, health, map_to_original, style_pass


class Result:
    def __init__(self, lang, original, fixed, issues, broken):
        self.lang = lang
        self.original = original
        self.fixed = fixed
        self.issues = sorted(issues, key=lambda i: (i.line, i.col))
        self.broken = broken          # the code still has a syntax error we couldn't fix
        remaining = [i for i in issues if not i.fixed]
        self.score_before = health(issues)
        self.score_after = health(remaining)

    @property
    def fixed_issues(self):
        return [i for i in self.issues if i.fixed]

    @property
    def remaining(self):
        return [i for i in self.issues if not i.fixed]

    @property
    def changed(self):
        return self.fixed != self.original


def detect_language(source, filename=None):
    if filename:
        low = filename.lower()
        if low.endswith((".py", ".pyw")):
            return "python"
        if low.endswith(".nyx"):
            return "nyx"
    py = nyx = 0
    for line in source.splitlines():
        s = line.strip()
        if re.match(r"(def|class|if|elif|else|for|while|try|except|with)\b.*:\s*(#.*)?$", s):
            py += 2
        if re.match(r"(import\s+\w|from\s+\w+\s+import\b)", s):
            py += 3
        if "self." in s or s.startswith("print(") or s.startswith("@"):
            py += 1
        if re.match(r"(fn\s+\w+|let\s+\w|const\s+\w)", s):
            nyx += 3
        if s.endswith("{") or s == "}":
            nyx += 1
        if " => " in s or "|>" in s:
            nyx += 2
    return "nyx" if nyx > py else "python"


def analyze(text, lang):
    if lang == "python":
        import ast
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
        return pychecks.PyAnalyzer(text, tree).run()
    from ..lexer import Lexer
    from ..parser import Parser
    program = Parser(Lexer(text, "<doctor>").tokenize()).program()
    return nyxchecks.NyxAnalyzer(text, program).run()


def diagnose(source, lang="python", style=True):
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    issues = []
    if text != source:
        crlf = Issue(1, 0, "style", "line-endings", "Windows line endings (CRLF)", fix="converted to \\n")
        crlf.fixed = True
        issues.append(crlf)
    if text and not text.endswith("\n"):
        text += "\n"
    history = [text]
    checks = pychecks if lang == "python" else nyxchecks

    def stamp(found):
        for issue in found:
            issue.rev = len(history) - 1
        return found

    # 1. syntax
    text, syntax_issues, err = checks.repair_syntax(text, history)
    issues += syntax_issues
    if err is not None:
        if lang == "python":
            line, col, msg = err.lineno or 1, max(0, (err.offset or 1) - 1), pychecks.clean_msg(err)
        else:
            tok = err.tok
            line, col, msg = (tok.line if tok else 1), ((tok.col - 1) if tok else 0), err.message
        issues += stamp([Issue(line, col, "error", "syntax", msg + " — couldn't fix this automatically")])
        if style:
            new, style_issues = style_pass(text, lang)
            issues += stamp(style_issues)
            text = new
            history.append(text)
        map_to_original(issues, history)
        return Result(lang, source, text, issues, True)

    # 2. analysis + fixes, repeated until nothing else can be fixed
    remaining = []
    for _ in range(6):
        found = stamp(analyze(text, lang))
        fixable = [i for i in found if i.edits]
        if not fixable:
            remaining = found
            break
        new_text, applied = apply_issues(text, fixable)
        if checks.parse_error(new_text) is not None:  # a combination broke the code: go one by one
            applied, cur = [], text
            for issue in fixable:
                candidate, ok = apply_issues(cur, [issue])
                if ok and checks.parse_error(candidate) is None:
                    cur = candidate
                    applied.append(issue)
            new_text = cur
        if not applied:
            remaining = found
            break
        for issue in applied:
            issue.fixed = True
        issues += applied
        text = new_text
        history.append(text)
    else:
        remaining = stamp(analyze(text, lang))
    for issue in remaining:
        issue.edits = []
    issues += remaining

    # 3. style
    if style:
        if lang == "python":
            semi = pychecks.semicolons(text)
            if semi is not None:
                candidate, ok = apply_issues(text, [semi])
                if ok and pychecks.parse_error(candidate) is None:
                    semi.fixed = True
                    issues += stamp([semi])
                    text = candidate
                    history.append(text)
        new, style_issues = style_pass(text, lang)
        issues += stamp(style_issues)
        if new != text:
            text = new
            history.append(text)

    map_to_original(issues, history)
    return Result(lang, source, text, issues, checks.parse_error(text) is not None)
