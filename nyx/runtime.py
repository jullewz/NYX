"""Runtime values: scopes, functions, classes, instances, ranges and modules."""

import math
import re


class Env:
    """A scope. Variables live in a plain dict; scopes chain to their parent."""

    __slots__ = ("vars", "parent", "consts")

    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent
        self.consts = None

    def define(self, name, value, const=False):
        self.vars[name] = value
        if const:
            if self.consts is None:
                self.consts = set()
            self.consts.add(name)
        elif self.consts and name in self.consts:
            self.consts.discard(name)

    def all_names(self):
        names, e = set(), self
        while e is not None:
            names.update(e.vars)
            e = e.parent
        return names


class Flow:
    """Control-flow signal returned (not raised) by statements: break, continue, return."""

    __slots__ = ("kind", "value")

    def __init__(self, kind, value=None):
        self.kind = kind
        self.value = value


BREAK = Flow("break")
CONTINUE = Flow("continue")


class NyxFunction:
    __slots__ = ("name", "params", "defaults", "rest", "body", "closure", "klass", "arity")

    def __init__(self, name, params, defaults, rest, body, closure, klass=None):
        self.name = name
        self.params = params
        self.defaults = defaults      # compiled default-value closures or None
        self.rest = rest
        self.body = body              # compiled closure: env -> Flow | value
        self.closure = closure
        self.klass = klass            # class that defines this method (for super)
        self.arity = len(params)

    def bind(self, instance):
        env = Env(self.closure)
        env.vars["self"] = instance
        env.vars["__class__"] = self.klass
        return NyxFunction(self.name, self.params, self.defaults, self.rest, self.body, env, self.klass)


class Builtin:
    __slots__ = ("name", "fn", "lo", "hi")

    def __init__(self, name, fn, lo, hi):
        self.name = name
        self.fn = fn          # fn(interp, args, tok)
        self.lo = lo
        self.hi = hi          # None = any number of arguments


class NativeMethod:
    """A method of a built-in type bound to its receiver, e.g. [1, 2].map."""

    __slots__ = ("name", "obj", "fn", "lo", "hi")

    def __init__(self, name, obj, spec):
        self.name = name
        self.obj = obj
        self.fn, self.lo, self.hi = spec   # fn(interp, obj, args, tok)


class NyxClass:
    __slots__ = ("name", "superclass", "methods", "statics")

    def __init__(self, name, superclass, methods, statics):
        self.name = name
        self.superclass = superclass
        self.methods = methods
        self.statics = statics

    def find_method(self, name):
        k = self
        while k is not None:
            m = k.methods.get(name)
            if m is not None:
                return m
            k = k.superclass
        return None

    def find_static(self, name):
        k = self
        while k is not None:
            m = k.statics.get(name)
            if m is not None:
                return m
            k = k.superclass
        return None

    def method_names(self):
        names, k = set(), self
        while k is not None:
            names.update(k.methods)
            k = k.superclass
        return names

    def is_subclass_of(self, other):
        k = self
        while k is not None:
            if k is other:
                return True
            k = k.superclass
        return False


class NyxInstance:
    __slots__ = ("klass", "fields")

    def __init__(self, klass):
        self.klass = klass
        self.fields = {}


class NyxRange:
    __slots__ = ("start", "stop", "inclusive")

    def __init__(self, start, stop, inclusive):
        self.start = start
        self.stop = stop
        self.inclusive = inclusive

    def py(self):
        step = 1 if self.stop >= self.start else -1
        end = self.stop + step if self.inclusive else self.stop
        return range(self.start, end, step)

    def __eq__(self, other):
        return isinstance(other, NyxRange) and self.py() == other.py()

    def __hash__(self):
        return hash((self.start, self.stop, self.inclusive))


class Module:
    __slots__ = ("name", "path", "env")

    def __init__(self, name, path, env):
        self.name = name
        self.path = path
        self.env = env

    @property
    def exports(self):
        return {k: v for k, v in self.env.vars.items() if not k.startswith("_")}


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def type_name(v):
    if v is None:
        return "nil"
    t = type(v)
    if t is bool:
        return "bool"
    if t is int:
        return "int"
    if t is float:
        return "float"
    if t is str:
        return "string"
    if t is list:
        return "list"
    if t is dict:
        return "map"
    if t in (NyxFunction, Builtin, NativeMethod):
        return "function"
    if t is NyxClass:
        return "class"
    if t is NyxInstance:
        return v.klass.name
    if t is NyxRange:
        return "range"
    if t is Module:
        return "module"
    return t.__name__


def truthy(v):
    if v is None or v is False:
        return False
    if v is True:
        return True
    t = type(v)
    if t is int or t is float:
        return v != 0
    if t is str or t is list or t is dict:
        return len(v) > 0
    return True


def equal(a, b):
    if type(a) is bool or type(b) is bool:
        return a is b
    try:
        return a == b
    except RecursionError:
        return a is b


def fmt_float(f):
    if math.isnan(f):
        return "nan"
    if math.isinf(f):
        return "inf" if f > 0 else "-inf"
    if f.is_integer() and abs(f) < 1e16:
        return str(int(f))
    return repr(f)


def quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'
