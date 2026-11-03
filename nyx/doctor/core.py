"""Shared machinery for `nyx doctor`: issues, text edits, a string/comment-aware
scanner, bracket matching, style fixes, the health score and diffs."""

import difflib
import re

SEVERITIES = ("error", "warning", "style")
PAIRS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {v: k for k, v in PAIRS.items()}

CODE, STRING, COMMENT = 0, 1, 2


class Edit:
    """Replace text between (line, col) and (end_line, end_col).
    Lines are 1-based, columns are 0-based character offsets, the end is exclusive."""

    __slots__ = ("line", "col", "end_line", "end_col", "text")

    def __init__(self, line, col, end_line=None, end_col=None, text=""):
        self.line = line
        self.col = col
        self.end_line = line if end_line is None else end_line
        self.end_col = col if end_col is None else end_col
        self.text = text


def insert(line, col, text):
    return Edit(line, col, line, col, text)


class Issue:
    def __init__(self, line, col, severity, code, message, edits=None, fix=None):
        self.line = line
        self.col = col
        self.severity = severity      # error | warning | style
        self.code = code              # short machine name, e.g. "undefined-name"
        self.message = message
        self.edits = edits or []
        self.fix = fix                # human description of the fix
        self.fixed = False
        self.rev = 0                  # which revision of the text the position refers to

    def __repr__(self):
        state = "fixed" if self.fixed else ("fixable" if self.edits else "manual")
        return f"<{self.severity} {self.code} {self.line}:{self.col} {state}: {self.message}>"


# --------------------------------------------------------------------------- #
#  Applying edits
# --------------------------------------------------------------------------- #
def line_starts(text):
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def to_offset(starts, text, line, col):
    if line < 1:
        return 0
    if line > len(starts):
        return len(text)
    start = starts[line - 1]
    end = starts[line] - 1 if line < len(starts) else len(text)
    return min(start + max(0, col), end)


def _overlaps(a, b):
    s1, e1 = a
    s2, e2 = b
    if s1 == e1 and s2 == e2:
        return False
    if s1 == e1:
        return s2 < s1 < e2
    if s2 == e2:
        return s1 < s2 < e1
    return s1 < e2 and s2 < e1


def apply_issues(text, issues):
    """Apply the edits of as many issues as possible without any overlapping.
    Each issue is all-or-nothing. Returns (new_text, applied_issues)."""
    starts = line_starts(text)
    taken, spans, applied = [], [], []
    for issue in issues:
        mine = []
        for e in issue.edits:
            s = to_offset(starts, text, e.line, e.col)
            end = to_offset(starts, text, e.end_line, e.end_col)
            if end < s:
                s, end = end, s
            mine.append((s, end, e.text))
        ranges = [(s, e) for s, e, _ in mine]
        clash = any(_overlaps(r, t) for r in ranges for t in taken)
        clash = clash or any(_overlaps(a, b) for i, a in enumerate(ranges) for b in ranges[i + 1:])
        if clash or not mine:
            continue
        taken.extend(ranges)
        spans.extend(mine)
        applied.append(issue)
    for s, e, t in sorted(spans, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s] + t + text[e:]
    return text, applied


# --------------------------------------------------------------------------- #
#  Scanning: which characters are code, string or comment
# --------------------------------------------------------------------------- #
PY_PREFIX = re.compile(r"(?i)(?:r|u|b|f|br|rb|fr|rf)?$")


def scan(text, lang):
    """Return a bytearray with CODE / STRING / COMMENT for every character."""
    mask = bytearray(len(text))
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            mask[i:j] = bytes([COMMENT]) * (j - i)
            i = j
            continue
        if (lang == "python" and c in "\"'") or (lang == "nyx" and c == '"'):
            start = i
            if lang == "python":
                k = i
                while k > 0 and text[k - 1].isalpha() and i - k < 2:
                    k -= 1
                if k < i and PY_PREFIX.match(text[k:i]) and (k == 0 or not (text[k - 1].isalnum() or text[k - 1] == "_")):
                    start = k
            q = text[i:i + 3] if lang == "python" and text[i:i + 3] in ('"""', "'''") else c
            j = i + len(q)
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text.startswith(q, j):
                    j += len(q)
                    break
                if lang == "python" and len(q) == 1 and text[j] == "\n":
                    break
                j += 1
            j = min(j, n)
            mask[start:j] = bytes([STRING]) * (j - start)
            i = j
            continue
        i += 1
    return mask


def code_end(line, lang):
    """Index just after the last code character of a single line (before a comment)."""
    mask = scan(line, lang)
    end = 0
    for i, ch in enumerate(line):
        if mask[i] == COMMENT:
            break
        if not ch.isspace():
            end = i + 1
    return end


def sub_code(line, lang, pattern, repl, count=0):
    """re.sub, but only for matches that are entirely code (not in strings/comments)."""
    mask = scan(line, lang)
    out, pos, done = [], 0, 0
    for m in re.finditer(pattern, line):
        if count and done >= count:
            break
        if any(mask[k] != CODE for k in range(m.start(), m.end())):
            continue
        out.append(line[pos:m.start()])
        out.append(m.expand(repl) if isinstance(repl, str) else repl(m))
        pos = m.end()
        done += 1
    out.append(line[pos:])
    return "".join(out)


class BracketReport:
    def __init__(self):
        self.unclosed = []      # offsets of openers never closed
        self.unmatched = []     # offsets of closers with no opener
        self.mismatched = []    # (opener offset, closer offset)


def brackets(text, lang, mask=None):
    mask = mask if mask is not None else scan(text, lang)
    rep = BracketReport()
    stack = []
    for i, ch in enumerate(text):
        if mask[i] != CODE:
            continue
        if ch in PAIRS:
            stack.append(i)
        elif ch in CLOSERS:
            if stack and text[stack[-1]] == CLOSERS[ch]:
                stack.pop()
            elif stack:
                rep.mismatched.append((stack.pop(), i))
            else:
                rep.unmatched.append(i)
    rep.unclosed = stack
    return rep


def offset_to_pos(starts, offset):
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, offset - starts[lo]


def indent_of(line):
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


# --------------------------------------------------------------------------- #
#  Style fixes shared by every language
# --------------------------------------------------------------------------- #
def style_pass(text, lang):
    issues = []
    mask = scan(text, lang)
    starts = line_starts(text)
    lines = text.split("\n")
    trailing = tabs = 0
    first_trailing = first_tab = None
    in_string_line = set()
    for idx, line in enumerate(lines):
        nl = starts[idx] + len(line)
        if nl < len(text) and mask[nl] == STRING:
            in_string_line.add(idx + 1)
    out = []
    for idx, line in enumerate(lines):
        start = starts[idx]
        inside = idx in in_string_line  # the line starts inside a multi-line string
        stripped = line.rstrip(" \t")
        if len(stripped) < len(line) and (idx + 1) not in in_string_line:
            if all(mask[start + k] != STRING for k in range(len(stripped), len(line))):
                trailing += 1
                first_trailing = first_trailing or idx + 1
                line = stripped
        lead = line[:len(line) - len(line.lstrip(" \t"))]
        if "\t" in lead and not inside:
            tabs += 1
            first_tab = first_tab or idx + 1
            line = lead.replace("\t", "    ") + line[len(lead):]
        out.append(line)
    # collapse runs of 3+ blank lines to 2
    collapsed, blank_run, removed_blank = [], 0, 0
    for idx, line in enumerate(out):
        if line.strip() == "" and idx not in in_string_line:
            blank_run += 1
            if blank_run > 2:
                removed_blank += 1
                continue
        else:
            blank_run = 0
        collapsed.append(line)
    new = "\n".join(collapsed)
    stripped_end = new.rstrip("\n")
    final_fix = new != stripped_end + "\n"
    new = stripped_end + "\n" if stripped_end else ""
    if trailing:
        issues.append(_style(first_trailing, f"trailing whitespace on {trailing} line(s)", "removed trailing whitespace"))
    if tabs:
        issues.append(_style(first_tab, f"tab indentation on {tabs} line(s)", "converted tabs to 4 spaces"))
    if removed_blank:
        issues.append(_style(1, f"{removed_blank} extra blank line(s)", "collapsed to at most 2 blank lines"))
    if final_fix and text.strip():
        issues.append(_style(len(collapsed), "file should end with exactly one newline", "fixed the end of the file"))
    return new, issues


def _style(line, message, fix):
    issue = Issue(line, 0, "style", "whitespace", message, fix=fix)
    issue.fixed = True
    return issue


# --------------------------------------------------------------------------- #
#  Mapping positions back to the original text
# --------------------------------------------------------------------------- #
def _back(old, new, line):
    """Line number in `new` -> the matching line number in `old`."""
    old_lines, new_lines = old.split("\n"), new.split("\n")
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if j1 <= line - 1 < j2:
            if tag == "equal":
                return i1 + (line - 1 - j1) + 1
            return min(i1 + (line - 1 - j1), max(i1, i2 - 1)) + 1
    return min(line, len(old_lines))


def map_to_original(issues, history):
    """Every issue was found in some revision of the text; report its line in the original."""
    cache = {}
    for issue in issues:
        line = issue.line
        for k in range(min(issue.rev, len(history) - 1), 0, -1):
            key = (k, line)
            if key not in cache:
                cache[key] = _back(history[k - 1], history[k], line)
            line = cache[key]
        issue.line = max(1, line)


# --------------------------------------------------------------------------- #
#  Score & diff
# --------------------------------------------------------------------------- #
WEIGHTS = {"error": 18, "warning": 6, "style": 1}


def health(issues):
    penalty = sum(WEIGHTS[i.severity] for i in issues)
    return max(0, 100 - penalty)


def unified_diff(old, new, name):
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     f"{name} (before)", f"{name} (after)", lineterm="", n=1))
