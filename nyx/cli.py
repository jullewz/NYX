"""The `nyx` command: run files, the REPL, and developer tools (--tokens, --ast, --show)."""

import argparse
import os
import sys
import threading
import time

from . import __author__, __version__
from .errors import Interrupted, NyxError, NyxSyntaxError, SOURCES, format_error
from .interpreter import Interpreter
from .lexer import Lexer
from .nodes import Node
from .parser import Parser
from .term import P, USE_COLOR, banner, est, highlight, st


# --------------------------------------------------------------------------- #
#  Running code on a big stack (deep recursion) with clean Ctrl+C handling
# --------------------------------------------------------------------------- #
def run_guarded(interp, fn):
    """Run fn() on a worker thread with a large stack. Ctrl+C asks the program to
    stop at the next loop iteration or call; a second Ctrl+C exits immediately."""
    result = {"rc": 0}

    def target():
        try:
            result["rc"] = fn() or 0
        except SystemExit as e:
            result["rc"] = e.code if isinstance(e.code, int) else 0
        except Interrupted:
            print(est("\n  ⏹ interrupted", P.WARN), file=sys.stderr)
            result["rc"] = 130
        except NyxError as e:
            print(format_error(e), file=sys.stderr)
            result["rc"] = 1
        except BaseException as e:  # never crash with a Python traceback
            print(est(f"✖ internal error: {type(e).__name__}: {e}", P.ERR, True), file=sys.stderr)
            result["rc"] = 70

    for size in (512, 256, 64, 16):
        try:
            threading.stack_size(size * 1024 * 1024)
            break
        except (ValueError, RuntimeError):
            continue
    sys.setrecursionlimit(200_000)
    worker = threading.Thread(target=target, name="nyx", daemon=True)
    worker.start()
    interrupts = 0
    while worker.is_alive():
        try:
            worker.join(0.1)
        except KeyboardInterrupt:
            interrupts += 1
            interp.interrupted = True
            if interrupts >= 2:
                print(est("\n  ⏹ force quit", P.WARN), file=sys.stderr)
                os._exit(130)
    return result["rc"]


# --------------------------------------------------------------------------- #
#  Developer tools
# --------------------------------------------------------------------------- #
def dump_tokens(source, file):
    SOURCES[file] = source
    for tok in Lexer(source, file).tokenize():
        where = st(f"{tok.line}:{tok.col}".rjust(7), P.MUTED)
        kind = st(tok.type.ljust(8), P.VIOLET, True)
        if tok.type == "str":
            value = st(repr("".join(p if isinstance(p, str) else "{" + p[0] + "}" for p in tok.value)), P.STRING)
        elif tok.type == "num":
            value = st(tok.value, P.NUMBER)
        elif tok.type == "ident":
            value = st(tok.value, P.TEXT)
        else:
            value = ""
        print(f"{where}  {kind}{value}")


def dump_ast(source, file):
    SOURCES[file] = source
    program = Parser(Lexer(source, file).tokenize()).program()
    print(st("Program", P.VIOLET, True))
    for i, stmt in enumerate(program.stmts):
        _dump(stmt, "", i == len(program.stmts) - 1)


def _label(node):
    name = type(node).__name__
    bits = []
    for f in node.fields:
        v = getattr(node, f)
        if isinstance(v, (str, int, float, bool)) and not isinstance(v, Node) and f not in ("scoped",):
            bits.append(f"{f}={v!r}")
        elif v is None and f in ("value", "alias"):
            continue
    return st(name, P.LAVENDER, True) + (" " + st(" ".join(bits), P.MUTED) if bits else "")


def _kids(node):
    out = []
    for f in node.fields:
        v = getattr(node, f)
        if isinstance(v, Node):
            out.append((f, v))
        elif isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, Node):
                    out.append((f, item))
                elif isinstance(item, tuple):
                    for sub in item:
                        if isinstance(sub, Node):
                            out.append((f, sub))
                        elif isinstance(sub, list):
                            out.extend((f, s) for s in sub if isinstance(s, Node))
    return out


def _dump(node, prefix, last):
    branch = "└─ " if last else "├─ "
    print(prefix + st(branch, P.FAINT) + _label(node))
    kids = _kids(node)
    for i, (_, child) in enumerate(kids):
        _dump(child, prefix + ("   " if last else st("│  ", P.FAINT)), i == len(kids) - 1)


def show_source(source, file):
    lines = source.splitlines()
    width = len(str(len(lines)))
    print(st(f"  {file}", P.LAVENDER, True) + st(f"  ·  {len(lines)} lines", P.MUTED))
    for n, line in enumerate(lines, 1):
        print(f"  {st(str(n).rjust(width), P.FAINT)} {st('│', P.FAINT)} {highlight(line)}")


# --------------------------------------------------------------------------- #
#  REPL
# --------------------------------------------------------------------------- #
HELP = """\
  .help          show this help
  .vars          list the variables you defined
  .load FILE     run a .nyx file into this session
  .time          toggle timing of each command
  .clear         clear the screen
  .reset         forget all variables
  .exit          leave (or press Ctrl+D)"""


def needs_more(source):
    """True while the input is an unfinished statement (open braces, strings...)."""
    try:
        Parser(Lexer(source, "<repl>").tokenize()).program()
        return False
    except NyxSyntaxError as e:
        return e.at_eof


def pretty(interp, v):
    text = interp.to_str(v, True)
    t = v.__class__
    if t is str:
        return st(text, P.STRING)
    if t in (int, float):
        return st(text, P.NUMBER)
    if v is None or t is bool:
        return st(text, P.VIOLET, True)
    return st(text, P.TEXT)


def repl(interp):
    banner(__version__, __author__)
    print(f"  {st('type', P.MUTED)} {st('.help', P.LAVENDER)} {st('for commands', P.MUTED)}  "
          f"{st('·', P.FAINT)}  {st('Ctrl+D to exit', P.MUTED)}\n")
    readline = None
    history = os.path.join(os.path.expanduser("~"), ".nyx_history")
    try:
        import readline  # noqa: F811
        try:
            readline.read_history_file(history)
        except OSError:
            pass
    except ImportError:
        pass

    def prompt(text, color):
        if not USE_COLOR:
            return text
        styled = st(text, color, True)
        if readline is not None:  # tell readline which characters are invisible
            styled = "".join(f"\001{p}\002" if p.startswith("\033") else p
                             for p in _split_ansi(styled))
        return styled

    main_prompt = prompt("nyx ❯ ", P.VIOLET)
    more_prompt = prompt("    · ", P.FAINT)
    timing = False
    counter = 0
    buf = []
    while True:
        try:
            line = input(more_prompt if buf else main_prompt)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            buf = []
            continue
        if not buf and line.strip().startswith("."):
            cmd, _, arg = line.strip().partition(" ")
            if cmd in (".exit", ".quit", ".q"):
                break
            if cmd == ".help":
                print(st(HELP, P.MUTED))
            elif cmd == ".clear":
                print("\033[2J\033[H" if USE_COLOR else "", end="")
            elif cmd == ".time":
                timing = not timing
                print(st(f"  timing {'on' if timing else 'off'}", P.MUTED))
            elif cmd == ".reset":
                interp.globals.vars.clear()
                print(st("  all variables cleared", P.MUTED))
            elif cmd == ".vars":
                names = sorted(interp.globals.vars)
                if not names:
                    print(st("  (no variables yet)", P.MUTED))
                for name in names:
                    print(f"  {st(name, P.LAVENDER)} {st('=', P.FAINT)} {pretty(interp, interp.globals.vars[name])}")
            elif cmd == ".load":
                path = arg.strip()
                try:
                    with open(path, encoding="utf-8") as f:
                        src = f.read()
                    run_guarded(interp, lambda: interp.run(src, path) and 0)
                except OSError as e:
                    print(est(f"✖ can't open {path}: {e.strerror}", P.ERR), file=sys.stderr)
            else:
                print(est(f"  unknown command {cmd} — try .help", P.WARN))
            continue
        buf.append(line)
        source = "\n".join(buf)
        if not source.strip():
            buf = []
            continue
        if needs_more(source):
            continue
        buf = []
        counter += 1
        name = f"<repl:{counter}>"
        box = {}

        def execute():
            box["value"] = interp.run(source, name)
            return 0

        t0 = time.perf_counter()
        rc = run_guarded(interp, execute)
        if rc == 0 and box.get("value") is not None:
            print(pretty(interp, box["value"]))
        if timing:
            print(st(f"  ◷ {(time.perf_counter() - t0) * 1000:.2f} ms", P.MUTED))
    if readline is not None:
        try:
            readline.write_history_file(history)
        except OSError:
            pass
    print(st("  bye ·", P.MUTED), st("NYX", P.VIOLET, True), st(f"by {__author__}", P.MUTED))
    return 0


def _split_ansi(s):
    import re
    return [p for p in re.split(r"(\x1b\[[0-9;]*m)", s) if p]


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "doctor":
        from .doctor.cli import main as doctor_main
        return doctor_main(argv[1:])
    ap = argparse.ArgumentParser(
        prog="nyx",
        description=f"NYX {__version__} — a small, fast programming language by {__author__}.",
        epilog="Run without arguments to start the interactive REPL. "
               "Run `nyx doctor` to check and auto-fix Python or NYX code.",
    )
    ap.add_argument("file", nargs="?", help="a .nyx file to run")
    ap.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the program (as `args`)")
    ap.add_argument("-e", "--eval", metavar="CODE", help="run a line of code")
    ap.add_argument("--tokens", action="store_true", help="print the tokens of the file")
    ap.add_argument("--ast", action="store_true", help="print the syntax tree of the file")
    ap.add_argument("--show", action="store_true", help="print the file with syntax highlighting")
    ap.add_argument("--check", action="store_true", help="only check the file for syntax errors")
    ap.add_argument("--time", action="store_true", help="print how long the program took")
    ap.add_argument("-v", "--version", action="version", version=f"nyx {__version__} by {__author__}")
    opts = ap.parse_args(argv)

    interp = Interpreter(argv=opts.args)

    if opts.eval is not None:
        source, file = opts.eval, "<eval>"
    elif opts.file:
        try:
            with open(opts.file, encoding="utf-8") as f:
                source = f.read()
        except OSError as e:
            print(est(f"✖ can't open {opts.file}: {e.strerror}", P.ERR, True), file=sys.stderr)
            return 1
        file = opts.file
    else:
        return repl(interp)

    if opts.tokens or opts.ast or opts.show or opts.check:
        try:
            if opts.tokens:
                dump_tokens(source, file)
            if opts.ast:
                dump_ast(source, file)
            if opts.show:
                show_source(source, file)
            if opts.check:
                SOURCES[file] = source
                Parser(Lexer(source, file).tokenize()).program()
                print(f"{st('✔', P.STRING, True)} {st(file, P.TEXT)} {st('has no syntax errors', P.MUTED)}")
        except NyxError as e:
            print(format_error(e), file=sys.stderr)
            return 1
        return 0

    t0 = time.perf_counter()
    rc = run_guarded(interp, lambda: interp.run(source, file) and 0)
    if opts.time:
        print(est(f"◷ {file} ran in {(time.perf_counter() - t0) * 1000:.1f} ms", P.MUTED), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
