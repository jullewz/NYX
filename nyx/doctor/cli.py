"""`nyx doctor` — check code, fix what can be fixed, show exactly what changed."""

import argparse
import os
import shutil
import sys
import time

from .. import __author__, __version__
from ..term import ESC, P, USE_COLOR, grad, grad_rule, highlight, rgb, st
from .engine import detect_language, diagnose

ICON = {"error": ("✖", P.ERR), "warning": ("▲", P.WARN), "style": ("·", P.MUTED)}
OK_COLOR = P.STRING


def term_width():
    return max(50, shutil.get_terminal_size((90, 24)).columns)


def meter(value, width=24):
    filled = round(width * value / 100)
    if not USE_COLOR:
        return "#" * filled + "-" * (width - filled)
    return "".join(rgb(grad(i / max(1, width - 1))) + "━" for i in range(filled)) + rgb(P.FAINT) + "━" * (width - filled) + f"{ESC}22;39m"


def score_color(v):
    return P.STRING if v >= 85 else P.WARN if v >= 55 else P.ERR


def header(title, meta):
    print()
    print(f"  {st('◆', P.VIOLET)} {st(title.upper(), P.TEXT, True)}   {st(meta, P.MUTED)}")
    print("  " + st("─" * min(70, term_width() - 4), P.FAINT))


def report(result, name, show_diff=True, quiet=False):
    lines = result.original.count("\n") + (0 if result.original.endswith("\n") else 1)
    header("nyx doctor", f"{name} · {result.lang} · {lines} lines")
    width = term_width()
    if not result.issues:
        print(f"  {st('✔', OK_COLOR, True)} {st('no problems found — clean code', P.TEXT, True)}")
    shown = [i for i in result.issues if not (quiet and i.severity == "style")]
    for issue in shown:
        where = f"{issue.line}:{issue.col + 1}"
        if issue.fixed:
            icon, color = "✔", OK_COLOR
        else:
            icon, color = ICON[issue.severity]
        sev = st(issue.severity.ljust(7), ICON[issue.severity][1])
        msg = issue.message
        room = width - 26
        if len(msg) > room:
            msg = msg[:room - 1] + "…"
        print(f"  {st(icon, color, True)} {st(where.rjust(7), P.MUTED)}  {sev}  {st(msg, P.TEXT)}")
        if issue.fixed and issue.fix:
            print(f"  {' ' * 10}{st('↳ fixed:', OK_COLOR)} {st(issue.fix, P.MUTED)}")
        elif not issue.fixed:
            print(f"  {' ' * 10}{st('↳ needs your attention', P.MUTED)}")

    fixed = len(result.fixed_issues)
    remaining = result.remaining
    errors = sum(1 for i in remaining if i.severity == "error")
    print()
    print(f"  {st('health', P.MUTED)}  {meter(result.score_before)}  "
          f"{st(result.score_before, score_color(result.score_before), True)} {st('→', P.FAINT)} "
          f"{st(result.score_after, score_color(result.score_after), True)}  {meter(result.score_after)}")
    parts = [f"{st(fixed, OK_COLOR, True)} fixed automatically"]
    if remaining:
        parts.append(f"{st(len(remaining), P.WARN, True)} need your attention")
    if errors:
        parts.append(f"{st(errors, P.ERR, True)} error(s) left")
    print(f"  {st('●', P.VIOLET)} " + f"  {st('·', P.FAINT)}  ".join(parts))
    if result.broken:
        print(f"  {st('✖', P.ERR, True)} {st('the code still has a syntax error that needs a human', P.ERR)}")

    if show_diff and result.changed:
        from .core import unified_diff
        diff = unified_diff(result.original.replace("\r\n", "\n"), result.fixed, name)
        if diff:
            header("changes", f"{sum(1 for d in diff if d.startswith('+') and not d.startswith('+++'))} line(s) added or changed")
            for d in diff:
                if d.startswith(("+++", "---")):
                    continue
                if d.startswith("@@"):
                    print("  " + st(d, P.VIOLET))
                elif d.startswith("+"):
                    print("  " + st("+ ", OK_COLOR, True) + highlight(d[1:]))
                elif d.startswith("-"):
                    print("  " + st("- " + d[1:], P.ERR))
                else:
                    print("  " + st("  " + d[1:], P.MUTED))


def read_pasted():
    interactive = sys.stdin.isatty()
    if interactive:
        eof = "Ctrl+Z then Enter" if os.name == "nt" else "Ctrl+D"
        print()
        print(f"  {st('◆', P.VIOLET)} {st('NYX DOCTOR', P.TEXT, True)}  {st(f'v{__version__} · by {__author__}', P.MUTED)}")
        print("  " + grad_rule(44))
        print(f"  {st('paste your code below, then press', P.MUTED)} {st(eof, P.LAVENDER, True)} "
              f"{st('(or type', P.MUTED)} {st('::done', P.LAVENDER, True)}{st(')', P.MUTED)}")
        print()
    lines = []
    try:
        while True:
            line = sys.stdin.readline() if not interactive else input()
            if not interactive and line == "":
                break
            if interactive and line.strip() == "::done":
                break
            lines.append(line.rstrip("\n") if not interactive else line)
    except (EOFError, KeyboardInterrupt):
        pass
    return "\n".join(lines) + "\n"


def main(argv=None):
    try:
        return run(argv)
    except BrokenPipeError:  # output piped into head/more and closed early
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        print()
        return 130


def run(argv=None):
    ap = argparse.ArgumentParser(
        prog="nyx doctor",
        description="Find problems in Python or NYX code and fix everything that can be fixed safely.",
        epilog="With no files, paste code straight into the terminal.",
    )
    ap.add_argument("files", nargs="*", help="files to check (.py or .nyx); '-' reads from stdin")
    ap.add_argument("-w", "--write", action="store_true", help="fix the files in place (keeps a .bak backup)")
    ap.add_argument("-o", "--output", help="write the fixed code to this file")
    ap.add_argument("--check", action="store_true", help="only report, never write anything (exit 1 if problems)")
    ap.add_argument("--print", dest="print_code", action="store_true", help="print only the fixed code (for piping)")
    ap.add_argument("--lang", choices=["python", "nyx"], help="force the language")
    ap.add_argument("--no-style", action="store_true", help="skip whitespace/style fixes")
    ap.add_argument("--no-diff", action="store_true", help="don't show the diff")
    ap.add_argument("-q", "--quiet", action="store_true", help="hide style-only notes")
    opts = ap.parse_args(argv)

    sources = []
    if not opts.files or opts.files == ["-"]:
        code = read_pasted() if not opts.files else sys.stdin.read()
        if not code.strip():
            print(st("  nothing to check", P.MUTED))
            return 0
        sources.append(("pasted code" if not opts.files else "<stdin>", None, code))
    else:
        for path in opts.files:
            try:
                with open(path, encoding="utf-8") as f:
                    sources.append((path, path, f.read()))
            except (OSError, UnicodeDecodeError) as e:
                print(st(f"  ✖ can't read {path}: {getattr(e, 'strerror', None) or e}", P.ERR), file=sys.stderr)
                return 2

    exit_code = 0
    for name, path, code in sources:
        lang = opts.lang or detect_language(code, path)
        t0 = time.perf_counter()
        result = diagnose(code, lang, style=not opts.no_style)
        took = time.perf_counter() - t0

        if opts.print_code:
            sys.stdout.write(result.fixed)
            exit_code = max(exit_code, 1 if result.remaining else 0)
            continue

        report(result, name, show_diff=not opts.no_diff, quiet=opts.quiet)
        if any(i.severity == "error" for i in result.remaining) or result.broken:
            exit_code = 1
        elif opts.check and result.issues:
            exit_code = 1

        ext = ".py" if lang == "python" else ".nyx"
        if opts.check or not result.changed:
            pass
        elif opts.output:
            write(opts.output, result.fixed)
            saved(f"fixed code saved to {opts.output}")
        elif path is None:
            out = "doctor-fixed" + ext
            write(out, result.fixed)
            header("fixed code", f"also saved to {out}")
            for line in result.fixed.rstrip("\n").split("\n"):
                print(line)
        elif opts.write:
            write(path + ".bak", code)
            write(path, result.fixed)
            saved(f"fixed {path} in place (backup: {path}.bak)")
        else:
            root, e = os.path.splitext(path)
            out = f"{root}.fixed{e or ext}"
            write(out, result.fixed)
            saved(f"fixed copy saved to {out}  ·  use --write to fix the original")
        print(st(f"  ◷ checked in {took * 1000:.0f} ms", P.MUTED))
    print()
    return exit_code


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def saved(msg):
    print()
    print(f"  {st('✔', OK_COLOR, True)} {st(msg, P.TEXT)}")


if __name__ == "__main__":
    sys.exit(main())
