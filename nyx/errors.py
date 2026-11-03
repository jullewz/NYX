"""Error types and the pretty error printer."""

import difflib

from .term import P, est, highlight, ERR_COLOR

# file name -> source text, so errors can show the offending line
SOURCES = {}


class NyxError(Exception):
    """A runtime error inside a NYX program. Catchable with try/catch."""

    kind = "RuntimeError"

    def __init__(self, message, tok=None, hint=None):
        super().__init__(message)
        self.message = message
        self.tok = tok
        self.hint = hint
        self.trace = []  # (function name, call-site token), innermost first
        self.at_eof = False


class NyxSyntaxError(NyxError):
    kind = "SyntaxError"


class NyxThrow(NyxError):
    """A value thrown with `throw`."""

    kind = "Uncaught"

    def __init__(self, value, tok, text):
        super().__init__(text, tok)
        self.value = value


class Interrupted(Exception):
    """Ctrl+C while a program runs (deliberately not a NyxError, so try/catch can't swallow it)."""


def suggest(name, candidates):
    """'did you mean ...?' for a misspelled name."""
    names = [c for c in set(candidates) if not c.startswith("__")]
    match = difflib.get_close_matches(name, names, n=1, cutoff=0.72)
    return f"did you mean '{match[0]}'?" if match else None


def format_error(err):
    """Render an error like a modern compiler: location, source line, caret, hint, traceback."""
    lines = [f"{est('✖', P.ERR, True)} {est(err.kind + ':', P.ERR, True)} {est(err.message, P.TEXT, True)}"]
    tok = err.tok
    if tok is not None and getattr(tok, "line", None):
        src = SOURCES.get(tok.file)
        lineno, col = tok.line, max(1, tok.col)
        width = len(str(lineno)) + 1
        pad = " " * width
        lines.append(f"{pad}{est('╭─', P.FAINT)} {est(f'{tok.file}:{lineno}:{col}', P.LAVENDER)}")
        if src is not None:
            text_lines = src.splitlines()
            if 0 < lineno <= len(text_lines):
                code = text_lines[lineno - 1].replace("\t", "    ")
                lines.append(f"{est(str(lineno).rjust(width - 1), P.MUTED)} {est('│', P.FAINT)} {highlight(code, ERR_COLOR)}")
                length = max(1, min(getattr(tok, "length", 1) or 1, max(1, len(code) - col + 1)))
                lines.append(f"{pad}{est('│', P.FAINT)} {' ' * (col - 1)}{est('^' * length, P.ERR, True)}")
        if err.hint:
            lines.append(f"{pad}{est('╰─', P.FAINT)} {est('hint:', P.VIOLET, True)} {est(err.hint, P.TEXT)}")
        else:
            lines.append(f"{pad}{est('╰─', P.FAINT)}")
    elif err.hint:
        lines.append(f"   {est('hint:', P.VIOLET, True)} {est(err.hint, P.TEXT)}")

    if err.trace:
        lines.append(est("  traceback (innermost call first):", P.MUTED))
        shown = err.trace[:12]
        for name, call_tok in shown:
            where = f"{call_tok.file}:{call_tok.line}" if call_tok is not None else "?"
            lines.append(f"    {est('at', P.FAINT)} {est(name, P.BLUE)} {est('(' + where + ')', P.MUTED)}")
        if len(err.trace) > len(shown):
            lines.append(est(f"    … {len(err.trace) - len(shown)} more call(s)", P.MUTED))
    return "\n".join(lines)
