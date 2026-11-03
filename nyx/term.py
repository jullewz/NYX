"""Terminal colors and the NYX theme: black, violet and blue (24-bit color)."""

import os
import re
import sys


def _enable_terminal():
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            for std in (-11, -12):
                handle = k32.GetStdHandle(std)
                mode = ctypes.c_uint32()
                if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                    k32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            os.system("")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_enable_terminal()

USE_COLOR = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")) or bool(os.environ.get("FORCE_COLOR"))
ERR_COLOR = (sys.stderr.isatty() and not os.environ.get("NO_COLOR")) or bool(os.environ.get("FORCE_COLOR"))

ESC = "\033["
BOLD = ESC + "1m"
RESET = ESC + "22;39m"


class P:
    """Palette."""
    DEEP = (76, 29, 149)
    VIOLET = (139, 92, 246)
    LAVENDER = (196, 181, 253)
    MIST = (237, 233, 254)
    BLUE = (96, 165, 250)
    CYAN = (34, 211, 238)
    TEXT = (228, 228, 231)
    MUTED = (113, 113, 122)
    FAINT = (63, 63, 70)
    ERR = (248, 113, 113)
    WARN = (251, 191, 36)
    STRING = (134, 239, 172)
    NUMBER = (251, 191, 36)
    COMMENT = (100, 100, 110)


GRADIENT = [(76, 29, 149), (109, 40, 217), (139, 92, 246), (99, 102, 241), (56, 189, 248)]


def rgb(c):
    return f"{ESC}38;2;{c[0]};{c[1]};{c[2]}m"


def st(text, color=None, bold=False, enabled=None):
    on = USE_COLOR if enabled is None else enabled
    if not on:
        return str(text)
    return (BOLD if bold else "") + rgb(color or P.TEXT) + str(text) + RESET


def est(text, color=None, bold=False):
    """Style for text written to stderr."""
    return st(text, color, bold, ERR_COLOR)


def grad(t):
    t = max(0.0, min(1.0, t))
    seg = t * (len(GRADIENT) - 1)
    i = min(int(seg), len(GRADIENT) - 2)
    f = seg - i
    a, b = GRADIENT[i], GRADIENT[i + 1]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def grad_rule(n, ch="━"):
    if not USE_COLOR:
        return "-" * n
    return "".join(rgb(grad(i / max(1, n - 1))) + ch for i in range(n)) + RESET


BANNER = r"""
███╗   ██╗██╗   ██╗██╗  ██╗
████╗  ██║╚██╗ ██╔╝╚██╗██╔╝
██╔██╗ ██║ ╚████╔╝  ╚███╔╝
██║╚██╗██║  ╚██╔╝   ██╔██╗
██║ ╚████║   ██║   ██╔╝ ██╗
╚═╝  ╚═══╝   ╚═╝   ╚═╝  ╚═╝
"""


def banner(version, author):
    lines = BANNER.strip("\n").splitlines()
    try:
        BANNER.encode(sys.stdout.encoding or "ascii")
        fancy = True
    except (UnicodeEncodeError, LookupError):
        lines, fancy = ["  N Y X"], False
    width = max(len(l) for l in lines)
    print()
    for line in lines:
        if not USE_COLOR:
            print("  " + line)
            continue
        out = []
        for x, ch in enumerate(line):
            if ch == " ":
                out.append(ch)
                continue
            col = grad(x / max(1, width - 1))
            if fancy and ch != "█":
                col = tuple(int(c * 0.42) for c in col)
            out.append(rgb(col) + ch)
        print("  " + "".join(out) + RESET)
    print()
    tag = (f"{ESC}48;2;109;40;217m{rgb((255, 255, 255))}{BOLD} v{version} {RESET}{ESC}49m"
           if USE_COLOR else f"[v{version}]")
    print(f"  {tag}  {st('a programming language', P.TEXT)}  {st('·', P.FAINT)}  "
          f"{st('by', P.MUTED)} {st(author, P.LAVENDER, True)}")
    print("  " + grad_rule(max(width, 44)))


# --------------------------------------------------------------------------- #
#  Syntax highlighting (used for error snippets and `nyx --show`)
# --------------------------------------------------------------------------- #
KEYWORDS = {
    "let", "const", "fn", "return", "if", "elif", "else", "while", "for", "in", "break",
    "continue", "class", "extends", "self", "super", "true", "false", "nil", "and", "or",
    "not", "try", "catch", "finally", "throw", "import", "as", "match", "static",
}

_HL = re.compile(
    r'(?P<comment>#.*)'
    r'|(?P<string>"(?:\\.|[^"\\])*"?)'
    r'|(?P<number>\b(?:0x[0-9a-fA-F_]+|0b[01_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)'
    r'|(?P<word>\b[A-Za-z_][A-Za-z0-9_]*\b)'
)


def highlight(code, enabled=None):
    on = USE_COLOR if enabled is None else enabled
    if not on:
        return code
    out, pos = [], 0
    for m in _HL.finditer(code):
        out.append(st(code[pos:m.start()], P.TEXT, enabled=on))
        kind, text = m.lastgroup, m.group(0)
        if kind == "comment":
            out.append(st(text, P.COMMENT, enabled=on))
        elif kind == "string":
            out.append(st(text, P.STRING, enabled=on))
        elif kind == "number":
            out.append(st(text, P.NUMBER, enabled=on))
        elif text in KEYWORDS:
            out.append(st(text, P.VIOLET, True, enabled=on))
        elif text[0].isupper():
            out.append(st(text, P.CYAN, enabled=on))
        else:
            follows = code[m.end():m.end() + 1]
            out.append(st(text, P.BLUE if follows == "(" else P.TEXT, enabled=on))
        pos = m.end()
    out.append(st(code[pos:], P.TEXT, enabled=on))
    return "".join(out)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    return ANSI_RE.sub("", s)
