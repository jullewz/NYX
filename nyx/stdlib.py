"""The NYX standard library: global functions and methods on built-in types."""

import json
import math
import os
import random
import time

from .errors import NyxError
from .runtime import (
    Builtin, NativeMethod, NyxClass, NyxFunction, NyxInstance, NyxRange, equal, truthy, type_name,
)
from . import term

GLOBALS = {}
METHODS = {str: {}, list: {}, dict: {}, int: {}, float: {}, NyxRange: {}}
CALLABLE = (NyxFunction, Builtin, NativeMethod, NyxClass)


def builtin(name, lo=0, hi=-1):
    """Register a global function. hi=-1 means 'same as lo', None means unlimited."""
    def deco(fn):
        GLOBALS[name] = (fn, lo, lo if hi == -1 else hi)
        return fn
    return deco


def method(types, name, lo=0, hi=-1):
    """Register a method on built-in types: fn(interp, receiver, args, tok)."""
    def deco(fn):
        for t in types:
            METHODS[t][name] = (fn, lo, lo if hi == -1 else hi)
        return fn
    return deco


def install(interp, argv):
    for name, (fn, lo, hi) in GLOBALS.items():
        interp.builtins.vars[name] = Builtin(name, fn, lo, hi)
    interp.builtins.vars.update({
        "PI": math.pi,
        "TAU": math.tau,
        "E": math.e,
        "INF": math.inf,
        "args": list(argv),
    })


# --------------------------------------------------------------------------- #
#  Argument helpers
# --------------------------------------------------------------------------- #
def num(v, tok, what="argument"):
    if v.__class__ is int or v.__class__ is float:
        return v
    raise NyxError(f"{what} must be a number, not {type_name(v)}", tok)


def whole(v, tok, what="argument"):
    if v.__class__ is int:
        return v
    if v.__class__ is float and v.is_integer():
        return int(v)
    raise NyxError(f"{what} must be a whole number, not {type_name(v) if v.__class__ is not float else v}", tok)


def text(v, tok, what="argument"):
    if v.__class__ is str:
        return v
    raise NyxError(f"{what} must be a string, not {type_name(v)}", tok)


def func(v, tok, what="argument"):
    if v.__class__ in CALLABLE:
        return v
    raise NyxError(f"{what} must be a function, not {type_name(v)}", tok, "pass a function like fn(x) => x * 2")


def lst(v, tok, what="argument"):
    if v.__class__ is list:
        return v
    raise NyxError(f"{what} must be a list, not {type_name(v)}", tok)


def sort_key(interp, tok):
    def check(values):
        kinds = {("num" if v.__class__ in (int, float) else type_name(v)) for v in values}
        if len(kinds) > 1:
            raise NyxError(f"can't sort a mix of {', '.join(sorted(kinds))}", tok)
        if kinds and next(iter(kinds)) not in ("num", "string", "bool"):
            raise NyxError(f"can't sort {next(iter(kinds))} values without a key function", tok,
                           "pass a key, e.g. list.sort(fn(x) => x.age)")
    return check


def round_half(x, digits=0):
    m = 10 ** digits
    r = math.floor(abs(x) * m + 0.5) / m
    r = r if x >= 0 else -r
    return int(r) if digits == 0 else r


# --------------------------------------------------------------------------- #
#  Global functions
# --------------------------------------------------------------------------- #
@builtin("print", 0, None)
def _print(interp, args, tok):
    interp.write(" ".join(interp.to_str(a) for a in args) + "\n")


@builtin("write", 0, None)
def _write(interp, args, tok):
    interp.write("".join(interp.to_str(a) for a in args))


@builtin("input", 0, 1)
def _input(interp, args, tok):
    prompt = interp.to_str(args[0]) if args else ""
    try:
        if interp.read_line is not None:
            return interp.read_line(prompt)
        return input(prompt)
    except EOFError:
        return None


@builtin("len", 1)
def _len(interp, args, tok):
    v = args[0]
    if v.__class__ in (str, list, dict):
        return len(v)
    if v.__class__ is NyxRange:
        return len(v.py())
    raise NyxError(f"a {type_name(v)} has no length", tok)


@builtin("type", 1)
def _type(interp, args, tok):
    return type_name(args[0])


@builtin("str", 1)
def _str(interp, args, tok):
    return interp.to_str(args[0])


@builtin("repr", 1)
def _repr(interp, args, tok):
    return interp.to_str(args[0], True)


def to_number(v, tok, kind=None):
    if v.__class__ in (int, float):
        return v
    if v.__class__ is bool:
        return int(v)
    if v.__class__ is str:
        s = v.strip().replace("_", "")
        try:
            return int(s, 0) if kind != float else float(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                pass
    raise NyxError(f"can't convert {interp_repr(v)} to a number", tok)


def interp_repr(v):
    return f'"{v}"' if isinstance(v, str) else type_name(v)


@builtin("num", 1)
def _num(interp, args, tok):
    return to_number(args[0], tok)


@builtin("int", 1)
def _int(interp, args, tok):
    v = to_number(args[0], tok)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        raise NyxError("can't convert nan or inf to an int", tok)
    return int(v)


@builtin("float", 1)
def _float(interp, args, tok):
    return float(to_number(args[0], tok, float))


@builtin("bool", 1)
def _bool(interp, args, tok):
    return truthy(args[0])


@builtin("list", 1)
def _list(interp, args, tok):
    return list(interp.iter_for(args[0], False, tok))


@builtin("range", 1, 3)
def _range(interp, args, tok):
    a = [whole(x, tok, "range()") for x in args]
    if len(a) == 1:
        return NyxRange(0, a[0], False)
    if len(a) == 2:
        return NyxRange(a[0], a[1], False)
    if a[2] == 0:
        raise NyxError("range() step can't be zero", tok)
    return list(range(a[0], a[1], a[2]))


def _numbers(interp, args, tok, name):
    values = list(interp.iter_for(args[0], False, tok)) if len(args) == 1 else args
    if not values:
        raise NyxError(f"{name}() of an empty list", tok)
    return [num(v, tok, f"{name}() value") for v in values]


@builtin("min", 1, None)
def _min(interp, args, tok):
    return min(_numbers(interp, args, tok, "min"))


@builtin("max", 1, None)
def _max(interp, args, tok):
    return max(_numbers(interp, args, tok, "max"))


@builtin("sum", 1)
def _sum(interp, args, tok):
    return sum(num(v, tok, "sum() value") for v in interp.iter_for(args[0], False, tok))


@builtin("abs", 1)
def _abs(interp, args, tok):
    return abs(num(args[0], tok))


@builtin("round", 1, 2)
def _round(interp, args, tok):
    digits = whole(args[1], tok, "digits") if len(args) > 1 else 0
    return round_half(num(args[0], tok), digits)


@builtin("floor", 1)
def _floor(interp, args, tok):
    return math.floor(num(args[0], tok))


@builtin("ceil", 1)
def _ceil(interp, args, tok):
    return math.ceil(num(args[0], tok))


@builtin("sqrt", 1)
def _sqrt(interp, args, tok):
    v = num(args[0], tok)
    if v < 0:
        raise NyxError("can't take the square root of a negative number", tok)
    return math.sqrt(v)


@builtin("pow", 2)
def _pow(interp, args, tok):
    return interp.binop("**", num(args[0], tok), num(args[1], tok), tok)


def _math1(name, fn):
    @builtin(name, 1)
    def _f(interp, args, tok):
        try:
            return fn(num(args[0], tok))
        except ValueError:
            raise NyxError(f"{name}() got a value outside its domain", tok)
    return _f


for _name, _fn in (("sin", math.sin), ("cos", math.cos), ("tan", math.tan), ("asin", math.asin),
                   ("acos", math.acos), ("atan", math.atan), ("exp", math.exp)):
    _math1(_name, _fn)


@builtin("atan2", 2)
def _atan2(interp, args, tok):
    return math.atan2(num(args[0], tok), num(args[1], tok))


@builtin("hypot", 2)
def _hypot(interp, args, tok):
    return math.hypot(num(args[0], tok), num(args[1], tok))


@builtin("log", 1, 2)
def _log(interp, args, tok):
    x = num(args[0], tok)
    if x <= 0:
        raise NyxError("log() needs a positive number", tok)
    return math.log(x, num(args[1], tok)) if len(args) > 1 else math.log(x)


@builtin("random", 0)
def _random(interp, args, tok):
    return random.random()


@builtin("rand_int", 2)
def _rand_int(interp, args, tok):
    a, b = whole(args[0], tok), whole(args[1], tok)
    if a > b:
        raise NyxError("rand_int(a, b) needs a <= b", tok)
    return random.randint(a, b)


@builtin("choice", 1)
def _choice(interp, args, tok):
    items = list(interp.iter_for(args[0], False, tok))
    if not items:
        raise NyxError("choice() from an empty list", tok)
    return random.choice(items)


@builtin("shuffle", 1)
def _shuffle(interp, args, tok):
    items = list(lst(args[0], tok))
    random.shuffle(items)
    return items


@builtin("seed", 1)
def _seed(interp, args, tok):
    random.seed(args[0])


@builtin("time", 0)
def _time(interp, args, tok):
    return time.time()


@builtin("clock", 0)
def _clock(interp, args, tok):
    return time.perf_counter()


@builtin("date", 0, 1)
def _date(interp, args, tok):
    fmt = text(args[0], tok) if args else "%Y-%m-%d %H:%M:%S"
    return time.strftime(fmt)


@builtin("sleep", 1)
def _sleep(interp, args, tok):
    end = time.perf_counter() + max(0, num(args[0], tok))
    while True:
        if interp.interrupted:
            interp.check_interrupt()
        left = end - time.perf_counter()
        if left <= 0:
            return None
        time.sleep(min(left, 0.05))


@builtin("read_file", 1)
def _read_file(interp, args, tok):
    try:
        with open(text(args[0], tok), encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise NyxError(f"can't read file: {e.strerror or e}", tok)


@builtin("write_file", 2)
def _write_file(interp, args, tok):
    try:
        with open(text(args[0], tok), "w", encoding="utf-8") as f:
            f.write(interp.to_str(args[1]))
    except OSError as e:
        raise NyxError(f"can't write file: {e.strerror or e}", tok)


@builtin("append_file", 2)
def _append_file(interp, args, tok):
    try:
        with open(text(args[0], tok), "a", encoding="utf-8") as f:
            f.write(interp.to_str(args[1]))
    except OSError as e:
        raise NyxError(f"can't write file: {e.strerror or e}", tok)


@builtin("file_exists", 1)
def _file_exists(interp, args, tok):
    return os.path.exists(text(args[0], tok))


@builtin("list_dir", 0, 1)
def _list_dir(interp, args, tok):
    try:
        return sorted(os.listdir(text(args[0], tok) if args else "."))
    except OSError as e:
        raise NyxError(f"can't list folder: {e.strerror or e}", tok)


@builtin("getenv", 1, 2)
def _getenv(interp, args, tok):
    return os.environ.get(text(args[0], tok), args[1] if len(args) > 1 else None)


@builtin("assert", 1, 2)
def _assert(interp, args, tok):
    if not truthy(args[0]):
        msg = interp.to_str(args[1]) if len(args) > 1 else "condition was false"
        raise NyxError(f"assertion failed: {msg}", tok)


@builtin("exit", 0, 1)
def _exit(interp, args, tok):
    raise SystemExit(whole(args[0], tok) if args else 0)


def to_json_value(interp, v, tok):
    t = v.__class__
    if v is None or t in (bool, int, str):
        return v
    if t is float:
        if math.isnan(v) or math.isinf(v):
            raise NyxError("JSON can't hold nan or inf", tok)
        return v
    if t is list:
        return [to_json_value(interp, x, tok) for x in v]
    if t is NyxRange:
        return list(v.py())
    if t is dict:
        out = {}
        for k, x in v.items():
            if k.__class__ not in (str, int, float, bool) and k is not None:
                raise NyxError("JSON object keys must be strings", tok)
            out[interp.to_str(k)] = to_json_value(interp, x, tok)
        return out
    if t is NyxInstance:
        return to_json_value(interp, v.fields, tok)
    raise NyxError(f"a {type_name(v)} can't be turned into JSON", tok)


@builtin("json_str", 1, 2)
def _json_str(interp, args, tok):
    indent = whole(args[1], tok, "indent") if len(args) > 1 and args[1] is not None else None
    return json.dumps(to_json_value(interp, args[0], tok), indent=indent, ensure_ascii=False)


@builtin("json_parse", 1)
def _json_parse(interp, args, tok):
    try:
        return json.loads(text(args[0], tok))
    except json.JSONDecodeError as e:
        raise NyxError(f"invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}", tok)


@builtin("chr", 1)
def _chr(interp, args, tok):
    try:
        return chr(whole(args[0], tok))
    except (ValueError, OverflowError):
        raise NyxError("chr() code point out of range", tok)


@builtin("ord", 1)
def _ord(interp, args, tok):
    s = text(args[0], tok)
    if len(s) != 1:
        raise NyxError(f"ord() needs a single character, got {len(s)}", tok)
    return ord(s)


@builtin("sorted", 1, 2)
def _sorted(interp, args, tok):
    return _list_sort(interp, list(interp.iter_for(args[0], False, tok)), args[1:], tok)


@builtin("keys", 1)
def _keys(interp, args, tok):
    v = args[0]
    if v.__class__ is dict:
        return list(v.keys())
    if v.__class__ is NyxInstance:
        return list(v.fields.keys())
    raise NyxError(f"keys() needs a map, not {type_name(v)}", tok)


@builtin("values", 1)
def _values(interp, args, tok):
    v = args[0]
    if v.__class__ is dict:
        return list(v.values())
    if v.__class__ is NyxInstance:
        return list(v.fields.values())
    raise NyxError(f"values() needs a map, not {type_name(v)}", tok)


@builtin("is_instance", 2)
def _is_instance(interp, args, tok):
    obj, klass = args
    if klass.__class__ is not NyxClass:
        raise NyxError("is_instance(obj, Class) needs a class as its second argument", tok)
    return obj.__class__ is NyxInstance and obj.klass.is_subclass_of(klass)


@builtin("fixed", 2)
def _fixed(interp, args, tok):
    return f"{num(args[0], tok):.{whole(args[1], tok, 'decimals')}f}"


COLORS = {
    "red": (248, 113, 113), "green": (134, 239, 172), "yellow": (253, 224, 71), "blue": (96, 165, 250),
    "magenta": (232, 121, 249), "cyan": (34, 211, 238), "white": (250, 250, 250), "gray": (140, 140, 150),
    "violet": (167, 139, 250), "orange": (251, 146, 60), "pink": (244, 114, 182),
}


@builtin("color", 2)
def _color(interp, args, tok):
    s, name = interp.to_str(args[0]), text(args[1], tok, "color name")
    bold = name.startswith("bold ")
    name = name[5:] if bold else name
    if name not in COLORS and name != "bold":
        raise NyxError(f"unknown color '{name}'", tok, "colors: " + ", ".join(COLORS))
    if not term.USE_COLOR:
        return s
    if name == "bold":
        return term.BOLD + s + term.RESET
    return (term.BOLD if bold else "") + term.rgb(COLORS[name]) + s + term.RESET


@builtin("rgb", 4)
def _rgb(interp, args, tok):
    s = interp.to_str(args[0])
    c = [max(0, min(255, whole(round_half(num(x, tok)), tok))) for x in args[1:]]
    return term.rgb(c) + s + term.RESET if term.USE_COLOR else s


@builtin("bg_rgb", 4)
def _bg_rgb(interp, args, tok):
    s = interp.to_str(args[0])
    c = [max(0, min(255, whole(round_half(num(x, tok)), tok))) for x in args[1:]]
    return f"\033[48;2;{c[0]};{c[1]};{c[2]}m{s}\033[49m" if term.USE_COLOR else s


@builtin("has_color", 0)
def _has_color(interp, args, tok):
    return term.USE_COLOR


@builtin("clear", 0)
def _clear(interp, args, tok):
    if term.USE_COLOR:
        interp.write("\033[2J\033[H")


# --------------------------------------------------------------------------- #
#  String methods
# --------------------------------------------------------------------------- #
S = (str,)


@method(S, "len")
def _s_len(interp, s, args, tok):
    return len(s)


@method(S, "upper")
def _s_upper(interp, s, args, tok):
    return s.upper()


@method(S, "lower")
def _s_lower(interp, s, args, tok):
    return s.lower()


@method(S, "capitalize")
def _s_capitalize(interp, s, args, tok):
    return s[:1].upper() + s[1:]


@method(S, "title")
def _s_title(interp, s, args, tok):
    return s.title()


@method(S, "trim")
def _s_trim(interp, s, args, tok):
    return s.strip()


@method(S, "trim_start")
def _s_trim_start(interp, s, args, tok):
    return s.lstrip()


@method(S, "trim_end")
def _s_trim_end(interp, s, args, tok):
    return s.rstrip()


@method(S, "split", 0, 1)
def _s_split(interp, s, args, tok):
    if not args or args[0] is None:
        return s.split()
    sep = text(args[0], tok, "separator")
    return list(s) if sep == "" else s.split(sep)


@method(S, "lines")
def _s_lines(interp, s, args, tok):
    return s.splitlines()


@method(S, "chars")
def _s_chars(interp, s, args, tok):
    return list(s)


@method(S, "replace", 2)
def _s_replace(interp, s, args, tok):
    return s.replace(text(args[0], tok), interp.to_str(args[1]))


@method(S, "contains", 1)
def _s_contains(interp, s, args, tok):
    return text(args[0], tok) in s


@method(S, "starts_with", 1)
def _s_starts_with(interp, s, args, tok):
    return s.startswith(text(args[0], tok))


@method(S, "ends_with", 1)
def _s_ends_with(interp, s, args, tok):
    return s.endswith(text(args[0], tok))


@method(S, "find", 1)
def _s_find(interp, s, args, tok):
    return s.find(text(args[0], tok))


@method(S, "count", 1)
def _s_count(interp, s, args, tok):
    return s.count(text(args[0], tok))


@method(S, "repeat", 1)
def _s_repeat(interp, s, args, tok):
    return s * max(0, whole(args[0], tok))


@method(S, "pad_left", 1, 2)
def _s_pad_left(interp, s, args, tok):
    fill = text(args[1], tok) if len(args) > 1 else " "
    return s.rjust(whole(args[0], tok), fill[:1] or " ")


@method(S, "pad_right", 1, 2)
def _s_pad_right(interp, s, args, tok):
    fill = text(args[1], tok) if len(args) > 1 else " "
    return s.ljust(whole(args[0], tok), fill[:1] or " ")


@method(S, "center", 1, 2)
def _s_center(interp, s, args, tok):
    fill = text(args[1], tok) if len(args) > 1 else " "
    return s.center(whole(args[0], tok), fill[:1] or " ")


@method(S, "reverse")
def _s_reverse(interp, s, args, tok):
    return s[::-1]


@method(S, "is_digit")
def _s_is_digit(interp, s, args, tok):
    return s.isdigit()


@method(S, "is_alpha")
def _s_is_alpha(interp, s, args, tok):
    return s.isalpha()


@method(S, "is_space")
def _s_is_space(interp, s, args, tok):
    return s.isspace()


@method(S, "is_upper")
def _s_is_upper(interp, s, args, tok):
    return s.isupper()


@method(S, "is_lower")
def _s_is_lower(interp, s, args, tok):
    return s.islower()


@method(S, "to_num")
def _s_to_num(interp, s, args, tok):
    return to_number(s, tok)


@method(S, "slice", 1, 2)
def _s_slice(interp, s, args, tok):
    a = whole(args[0], tok)
    b = whole(args[1], tok) if len(args) > 1 else None
    return s[a:b]


@method(S, "join", 1)
def _s_join(interp, s, args, tok):
    return s.join(interp.to_str(x) for x in interp.iter_for(args[0], False, tok))


# --------------------------------------------------------------------------- #
#  List methods
# --------------------------------------------------------------------------- #
L = (list,)


@method(L, "len")
def _l_len(interp, xs, args, tok):
    return len(xs)


@method(L, "push", 1, None)
def _l_push(interp, xs, args, tok):
    xs.extend(args)


@method(L, "pop", 0, 1)
def _l_pop(interp, xs, args, tok):
    if not xs:
        raise NyxError("pop() from an empty list", tok)
    if args:
        i = whole(args[0], tok)
        if not -len(xs) <= i < len(xs):
            raise NyxError(f"pop() index {i} out of range", tok)
        return xs.pop(i)
    return xs.pop()


@method(L, "insert", 2)
def _l_insert(interp, xs, args, tok):
    xs.insert(whole(args[0], tok), args[1])


@method(L, "remove_at", 1)
def _l_remove_at(interp, xs, args, tok):
    i = whole(args[0], tok)
    if not -len(xs) <= i < len(xs):
        raise NyxError(f"remove_at() index {i} out of range", tok)
    return xs.pop(i)


@method(L, "remove", 1)
def _l_remove(interp, xs, args, tok):
    for i, x in enumerate(xs):
        if equal(x, args[0]):
            del xs[i]
            return True
    return False


@method(L, "clear")
def _l_clear(interp, xs, args, tok):
    xs.clear()


@method(L, "map", 1)
def _l_map(interp, xs, args, tok):
    f = func(args[0], tok)
    call = interp.call
    return [call(f, [x], tok) for x in xs]


@method(L, "filter", 1)
def _l_filter(interp, xs, args, tok):
    f = func(args[0], tok)
    call = interp.call
    return [x for x in xs if truthy(call(f, [x], tok))]


@method(L, "reduce", 1, 2)
def _l_reduce(interp, xs, args, tok):
    f = func(args[0], tok)
    items = iter(xs)
    if len(args) > 1:
        acc = args[1]
    else:
        try:
            acc = next(items)
        except StopIteration:
            raise NyxError("reduce() of an empty list with no starting value", tok)
    for x in items:
        acc = interp.call(f, [acc, x], tok)
    return acc


@method(L, "each", 1)
def _l_each(interp, xs, args, tok):
    f = func(args[0], tok)
    for x in list(xs):
        interp.call(f, [x], tok)


@method(L, "find", 1)
def _l_find(interp, xs, args, tok):
    f = func(args[0], tok)
    for x in xs:
        if truthy(interp.call(f, [x], tok)):
            return x
    return None


@method(L, "find_index", 1)
def _l_find_index(interp, xs, args, tok):
    f = func(args[0], tok)
    for i, x in enumerate(xs):
        if truthy(interp.call(f, [x], tok)):
            return i
    return -1


@method(L, "any", 0, 1)
def _l_any(interp, xs, args, tok):
    if args:
        f = func(args[0], tok)
        return any(truthy(interp.call(f, [x], tok)) for x in xs)
    return any(truthy(x) for x in xs)


@method(L, "all", 0, 1)
def _l_all(interp, xs, args, tok):
    if args:
        f = func(args[0], tok)
        return all(truthy(interp.call(f, [x], tok)) for x in xs)
    return all(truthy(x) for x in xs)


@method(L, "count", 0, 1)
def _l_count(interp, xs, args, tok):
    if not args:
        return len(xs)
    if args[0].__class__ in CALLABLE:
        return sum(1 for x in xs if truthy(interp.call(args[0], [x], tok)))
    return sum(1 for x in xs if equal(x, args[0]))


def _list_sort(interp, xs, args, tok):
    if args and args[0] is not None:
        f = func(args[0], tok)
        keys = [interp.call(f, [x], tok) for x in xs]
        sort_key(interp, tok)(keys)
        order = sorted(range(len(xs)), key=lambda i: keys[i])
        return [xs[i] for i in order]
    sort_key(interp, tok)(xs)
    return sorted(xs)


@method(L, "sort", 0, 1)
def _l_sort(interp, xs, args, tok):
    return _list_sort(interp, xs, args, tok)


@method(L, "sort_desc", 0, 1)
def _l_sort_desc(interp, xs, args, tok):
    return _list_sort(interp, xs, args, tok)[::-1]


@method(L, "reverse")
def _l_reverse(interp, xs, args, tok):
    return xs[::-1]


@method(L, "join", 0, 1)
def _l_join(interp, xs, args, tok):
    sep = text(args[0], tok, "separator") if args else ""
    return sep.join(interp.to_str(x) for x in xs)


@method(L, "contains", 1)
def _l_contains(interp, xs, args, tok):
    return interp.contains(xs, args[0], tok)


@method(L, "index_of", 1)
def _l_index_of(interp, xs, args, tok):
    for i, x in enumerate(xs):
        if equal(x, args[0]):
            return i
    return -1


@method(L, "slice", 1, 2)
def _l_slice(interp, xs, args, tok):
    a = whole(args[0], tok)
    b = whole(args[1], tok) if len(args) > 1 else None
    return xs[a:b]


@method(L, "sum")
def _l_sum(interp, xs, args, tok):
    return sum(num(x, tok, "sum() value") for x in xs)


@method(L, "min")
def _l_min(interp, xs, args, tok):
    return _min(interp, [xs], tok)


@method(L, "max")
def _l_max(interp, xs, args, tok):
    return _max(interp, [xs], tok)


@method(L, "min_by", 1)
def _l_min_by(interp, xs, args, tok):
    if not xs:
        return None
    s = _list_sort(interp, xs, args, tok)
    return s[0]


@method(L, "max_by", 1)
def _l_max_by(interp, xs, args, tok):
    if not xs:
        return None
    s = _list_sort(interp, xs, args, tok)
    return s[-1]


@method(L, "average")
def _l_average(interp, xs, args, tok):
    if not xs:
        raise NyxError("average() of an empty list", tok)
    return sum(num(x, tok) for x in xs) / len(xs)


@method(L, "first")
def _l_first(interp, xs, args, tok):
    return xs[0] if xs else None


@method(L, "last")
def _l_last(interp, xs, args, tok):
    return xs[-1] if xs else None


@method(L, "unique")
def _l_unique(interp, xs, args, tok):
    out = []
    for x in xs:
        if not any(equal(x, y) for y in out):
            out.append(x)
    return out


@method(L, "take", 1)
def _l_take(interp, xs, args, tok):
    return xs[:max(0, whole(args[0], tok))]


@method(L, "skip", 1)
def _l_skip(interp, xs, args, tok):
    return xs[max(0, whole(args[0], tok)):]


@method(L, "flat")
def _l_flat(interp, xs, args, tok):
    out = []
    for x in xs:
        if x.__class__ is list:
            out.extend(x)
        else:
            out.append(x)
    return out


@method(L, "zip", 1)
def _l_zip(interp, xs, args, tok):
    return [[a, b] for a, b in zip(xs, interp.iter_for(args[0], False, tok))]


@method(L, "enumerate")
def _l_enumerate(interp, xs, args, tok):
    return [[i, x] for i, x in enumerate(xs)]


@method(L, "chunk", 1)
def _l_chunk(interp, xs, args, tok):
    n = whole(args[0], tok)
    if n <= 0:
        raise NyxError("chunk() size must be positive", tok)
    return [xs[i:i + n] for i in range(0, len(xs), n)]


@method(L, "group_by", 1)
def _l_group_by(interp, xs, args, tok):
    f = func(args[0], tok)
    groups = {}
    for x in xs:
        key = interp.call(f, [x], tok)
        if key.__class__ in (list, dict):
            raise NyxError("group_by() key must be a string, number, bool or nil", tok)
        groups.setdefault(key, []).append(x)
    return groups


@method(L, "partition", 1)
def _l_partition(interp, xs, args, tok):
    f = func(args[0], tok)
    yes, no = [], []
    for x in xs:
        (yes if truthy(interp.call(f, [x], tok)) else no).append(x)
    return [yes, no]


@method(L, "copy")
def _l_copy(interp, xs, args, tok):
    return list(xs)


@method(L, "is_empty")
def _l_is_empty(interp, xs, args, tok):
    return not xs


# --------------------------------------------------------------------------- #
#  Map methods
# --------------------------------------------------------------------------- #
M = (dict,)


@method(M, "len")
def _m_len(interp, m, args, tok):
    return len(m)


@method(M, "keys")
def _m_keys(interp, m, args, tok):
    return list(m.keys())


@method(M, "values")
def _m_values(interp, m, args, tok):
    return list(m.values())


@method(M, "items")
def _m_items(interp, m, args, tok):
    return [[k, v] for k, v in m.items()]


@method(M, "has", 1)
def _m_has(interp, m, args, tok):
    try:
        return args[0] in m
    except TypeError:
        return False


@method(M, "get", 1, 2)
def _m_get(interp, m, args, tok):
    try:
        return m.get(args[0], args[1] if len(args) > 1 else None)
    except TypeError:
        return args[1] if len(args) > 1 else None


@method(M, "set", 2)
def _m_set(interp, m, args, tok):
    if args[0].__class__ in (list, dict):
        raise NyxError(f"a {type_name(args[0])} can't be a map key", tok)
    m[args[0]] = args[1]


@method(M, "remove", 1)
def _m_remove(interp, m, args, tok):
    try:
        return m.pop(args[0], None)
    except TypeError:
        return None


@method(M, "merge", 1)
def _m_merge(interp, m, args, tok):
    if args[0].__class__ is not dict:
        raise NyxError("merge() needs a map", tok)
    out = dict(m)
    out.update(args[0])
    return out


@method(M, "copy")
def _m_copy(interp, m, args, tok):
    return dict(m)


@method(M, "clear")
def _m_clear(interp, m, args, tok):
    m.clear()


@method(M, "is_empty")
def _m_is_empty(interp, m, args, tok):
    return not m


@method(M, "each", 1)
def _m_each(interp, m, args, tok):
    f = func(args[0], tok)
    for k, v in list(m.items()):
        interp.call(f, [k, v], tok)


@method(M, "map_values", 1)
def _m_map_values(interp, m, args, tok):
    f = func(args[0], tok)
    return {k: interp.call(f, [v], tok) for k, v in m.items()}


@method(M, "filter", 1)
def _m_filter(interp, m, args, tok):
    f = func(args[0], tok)
    return {k: v for k, v in m.items() if truthy(interp.call(f, [k, v], tok))}


# --------------------------------------------------------------------------- #
#  Number methods
# --------------------------------------------------------------------------- #
N = (int, float)


@method(N, "abs")
def _n_abs(interp, n, args, tok):
    return abs(n)


@method(N, "round", 0, 1)
def _n_round(interp, n, args, tok):
    return round_half(n, whole(args[0], tok) if args else 0)


@method(N, "floor")
def _n_floor(interp, n, args, tok):
    return math.floor(n)


@method(N, "ceil")
def _n_ceil(interp, n, args, tok):
    return math.ceil(n)


@method(N, "sqrt")
def _n_sqrt(interp, n, args, tok):
    return _sqrt(interp, [n], tok)


@method(N, "pow", 1)
def _n_pow(interp, n, args, tok):
    return interp.binop("**", n, num(args[0], tok), tok)


@method(N, "is_int")
def _n_is_int(interp, n, args, tok):
    return n.__class__ is int or n.is_integer()


@method(N, "is_even")
def _n_is_even(interp, n, args, tok):
    return whole(n, tok) % 2 == 0


@method(N, "is_odd")
def _n_is_odd(interp, n, args, tok):
    return whole(n, tok) % 2 == 1


@method(N, "clamp", 2)
def _n_clamp(interp, n, args, tok):
    return max(num(args[0], tok), min(num(args[1], tok), n))


@method(N, "sign")
def _n_sign(interp, n, args, tok):
    return (n > 0) - (n < 0)


@method(N, "times", 1)
def _n_times(interp, n, args, tok):
    f = func(args[0], tok)
    for i in range(whole(n, tok)):
        interp.call(f, [i], tok)


@method(N, "to_hex")
def _n_to_hex(interp, n, args, tok):
    return hex(whole(n, tok))


@method(N, "to_bin")
def _n_to_bin(interp, n, args, tok):
    return bin(whole(n, tok))


@method(N, "to_str")
def _n_to_str(interp, n, args, tok):
    return interp.to_str(n)


@method(N, "fixed", 1)
def _n_fixed(interp, n, args, tok):
    return _fixed(interp, [n, args[0]], tok)


# --------------------------------------------------------------------------- #
#  Range methods
# --------------------------------------------------------------------------- #
R = (NyxRange,)


@method(R, "len")
def _r_len(interp, r, args, tok):
    return len(r.py())


@method(R, "list")
def _r_list(interp, r, args, tok):
    return list(r.py())


@method(R, "contains", 1)
def _r_contains(interp, r, args, tok):
    return interp.contains(r, args[0], tok)


@method(R, "sum")
def _r_sum(interp, r, args, tok):
    return sum(r.py())


@method(R, "reverse")
def _r_reverse(interp, r, args, tok):
    return list(r.py())[::-1]


@method(R, "step", 1)
def _r_step(interp, r, args, tok):
    n = whole(args[0], tok)
    if n <= 0:
        raise NyxError("step() must be positive", tok)
    return list(r.py())[::n]


for _name in ("map", "filter", "each", "reduce", "find", "any", "all", "count", "join"):
    _spec = METHODS[list][_name]

    def _range_method(interp, r, args, tok, _fn=_spec[0]):
        return _fn(interp, list(r.py()), args, tok)
    METHODS[NyxRange][_name] = (_range_method, _spec[1], _spec[2])
