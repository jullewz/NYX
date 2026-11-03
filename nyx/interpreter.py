"""The NYX engine.

Instead of walking the AST every time a node runs, the interpreter first
*compiles* every node into a Python closure (a function that takes a scope and
returns a value). The closures call each other directly, so running a program is
a chain of fast function calls with no dispatching or isinstance checks on the
hot path. Statements return `Flow` signals for break/continue/return instead of
raising exceptions, which keeps loops and calls cheap.
"""

import operator
import os
import re
import sys

from . import stdlib
from .errors import Interrupted, NyxError, NyxThrow, SOURCES, suggest
from .lexer import Lexer
from .nodes import ExprStmt, Get, Literal, Node, PBind, PLit, PRange, PWild, Spread, creates_closures
from .parser import Parser
from .runtime import (
    BREAK, CONTINUE, IDENT_RE, Builtin, Env, Flow, Module, NativeMethod, NyxClass, NyxFunction,
    NyxInstance, NyxRange, equal, fmt_float, quote, truthy, type_name,
)

MAX_DEPTH = 2500
_MISSING = object()

FAST = {
    "+": operator.add, "-": operator.sub, "*": operator.mul,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}


def lookup(env, name):
    e = env
    while e is not None:
        v = e.vars
        if name in v:
            return v[name]
        e = e.parent
    return _MISSING


class Interpreter:
    def __init__(self, out=None, argv=None, read_line=None):
        self.out = out or sys.stdout
        self.read_line = read_line
        self.builtins = Env()
        self.globals = Env(self.builtins)
        self.modules = {}
        self.call_stack = []
        self.interrupted = False
        stdlib.install(self, argv or [])

    # ===================================================================== #
    #  Entry points
    # ===================================================================== #
    def compile_program(self, source, file):
        SOURCES[file] = source
        program = Parser(Lexer(source, file).tokenize()).program()
        compiled = []
        for stmt in program.stmts:
            if isinstance(stmt, ExprStmt):
                compiled.append((True, self.compile(stmt.expr)))
            else:
                compiled.append((False, self.compile(stmt)))
        return compiled

    def run(self, source, file="<input>", env=None):
        """Compile and run source code. Returns the value of the last expression statement."""
        env = env or self.globals
        compiled = self.compile_program(source, file)
        last = None
        try:
            for is_expr, fn in compiled:
                r = fn(env)
                last = r if is_expr else None
        except RecursionError:
            tok = self.call_stack[-1][1] if self.call_stack else None
            raise NyxError("stack overflow — too much recursion", tok, "check for a function that never stops calling itself")
        finally:
            self.call_stack.clear()
        return last

    def write(self, text):
        self.out.write(text)
        if getattr(self.out, "isatty", lambda: False)():
            self.out.flush()

    def check_interrupt(self):
        self.interrupted = False
        raise Interrupted()

    # ===================================================================== #
    #  Compiler: AST node -> closure
    # ===================================================================== #
    def compile(self, node):
        return getattr(self, "c_" + node.__class__.__name__)(node)

    def compile_block(self, block, new_scope=None):
        """A block runner always returns None or a Flow signal."""
        fns = [self.compile(s) for s in block.stmts]
        scoped = block.scoped if new_scope is None else new_scope
        if not fns:
            return lambda env: None
        if len(fns) == 1:
            f0 = fns[0]
            if scoped:
                return lambda env: f0(Env(env))
            return f0
        if scoped:
            def run_scoped(env):
                env = Env(env)
                for f in fns:
                    r = f(env)
                    if r is not None:
                        return r
                return None
            return run_scoped

        def run(env):
            for f in fns:
                r = f(env)
                if r is not None:
                    return r
            return None
        return run

    def compile_block_value(self, block):
        """For match arms: run a block and return the value of its last expression."""
        stmts = block.stmts
        if not stmts:
            return lambda env: None
        head = [self.compile(s) for s in stmts[:-1]]
        last = stmts[-1]
        tail = self.compile(last.expr) if isinstance(last, ExprStmt) else self.compile(last)
        tail_is_expr = isinstance(last, ExprStmt)

        def run(env):
            env = Env(env)
            for f in head:
                f(env)
            r = tail(env)
            return r if tail_is_expr else None
        return run

    def compile_defaults(self, defaults):
        return [self.compile(d) if d is not None else None for d in defaults]

    # -- literals & names --------------------------------------------------- #
    def c_Literal(self, node):
        value = node.value
        return lambda env: value

    def c_StringInterp(self, node):
        parts = [p if isinstance(p, str) else self.compile(p) for p in node.parts]
        to_str = self.to_str

        def f(env):
            return "".join([p if p.__class__ is str else to_str(p(env)) for p in parts])
        return f

    def c_Var(self, node):
        name, tok = node.name, node.tok

        def f(env):
            e = env
            while e is not None:
                v = e.vars
                if name in v:
                    return v[name]
                e = e.parent
            raise self.undefined(name, env, tok)
        return f

    def undefined(self, name, env, tok):
        hint = suggest(name, env.all_names())
        if hint is None and name in ("print_line", "println", "echo", "console"):
            hint = "use print(...)"
        elif hint is None and name in ("null", "None", "undefined"):
            hint = "NYX uses 'nil'"
        elif hint is None and name in ("True", "False"):
            hint = f"NYX uses '{name.lower()}'"
        elif hint is None and name in ("def", "function", "func"):
            hint = "functions are declared with 'fn'"
        return NyxError(f"undefined variable '{name}'", tok, hint)

    def c_SelfExpr(self, node):
        tok = node.tok

        def f(env):
            v = lookup(env, "self")
            if v is _MISSING:
                raise NyxError("'self' can only be used inside a method", tok)
            return v
        return f

    def c_SuperExpr(self, node):
        name, tok = node.name, node.tok

        def f(env):
            klass = lookup(env, "__class__")
            if klass is _MISSING or klass is None:
                raise NyxError("'super' can only be used inside a method", tok)
            sup = klass.superclass
            if sup is None:
                raise NyxError(f"class {klass.name} has no parent class", tok)
            m = sup.find_method(name)
            if m is None:
                raise NyxError(f"parent class {sup.name} has no method '{name}'", tok, suggest(name, sup.method_names()))
            return m.bind(lookup(env, "self"))
        return f

    # -- collections -------------------------------------------------------- #
    def c_ListExpr(self, node):
        tok = node.tok
        if not any(isinstance(i, Spread) for i in node.items):
            items = [self.compile(i) for i in node.items]
            return lambda env: [f(env) for f in items]
        specs = [(True, self.compile(i.expr)) if isinstance(i, Spread) else (False, self.compile(i)) for i in node.items]

        def f(env):
            out = []
            for spread, fn in specs:
                if spread:
                    out.extend(self.iter_for(fn(env), False, tok))
                else:
                    out.append(fn(env))
            return out
        return f

    def c_ListComp(self, node):
        E, I = self.compile(node.expr), self.compile(node.iterable)
        C = self.compile(node.cond) if node.cond is not None else None
        v1, v2, tok = node.var, node.var2, node.tok
        two = v2 is not None

        def f(env):
            out = []
            le = Env(env)
            vs = le.vars
            for item in self.iter_for(I(env), two, tok):
                if two:
                    vs[v1], vs[v2] = item
                else:
                    vs[v1] = item
                if C is None or truthy(C(le)):
                    out.append(E(le))
            return out
        return f

    def c_MapExpr(self, node):
        tok = node.tok
        entries = []
        for key, value in node.entries:
            dynamic = isinstance(key, Node)
            entries.append((self.compile(key) if dynamic else key, dynamic, self.compile(value)))

        def f(env):
            d = {}
            for key, dynamic, value in entries:
                if dynamic:
                    key = key(env)
                    if key.__class__ in (list, dict):
                        raise NyxError(f"a {type_name(key)} can't be a map key", tok,
                                       "map keys can be strings, numbers, booleans or nil")
                d[key] = value(env)
            return d
        return f

    # -- operators ---------------------------------------------------------- #
    def c_Unary(self, node):
        O, tok = self.compile(node.operand), node.tok
        if node.op == "-":
            def neg(env):
                v = O(env)
                if v.__class__ is int or v.__class__ is float:
                    return -v
                raise NyxError(f"can't negate a {type_name(v)}", tok)
            return neg
        return lambda env: not truthy(O(env))

    def c_Binary(self, node):
        op, tok = node.op, node.tok
        L, R = self.compile(node.left), self.compile(node.right)
        binop = self.binop
        if op in FAST:
            fast = FAST[op]
            right = node.right
            if isinstance(right, Literal) and right.value.__class__ in (int, float):
                const = right.value  # e.g. `n - 1`, `i < 10`: skip evaluating the constant

                def fc(env):
                    a = L(env)
                    if a.__class__ is int or a.__class__ is float:
                        return fast(a, const)
                    return binop(op, a, const, tok)
                return fc

            def f(env):
                a = L(env)
                b = R(env)
                ta, tb = a.__class__, b.__class__
                if (ta is int or ta is float) and (tb is int or tb is float):
                    return fast(a, b)
                return binop(op, a, b, tok)
            return f
        if op == "==":
            def eq(env):
                a, b = L(env), R(env)
                if a.__class__ is bool or b.__class__ is bool:
                    return a is b
                return a == b
            return eq
        if op == "!=":
            def ne(env):
                a, b = L(env), R(env)
                if a.__class__ is bool or b.__class__ is bool:
                    return a is not b
                return a != b
            return ne
        return lambda env: binop(op, L(env), R(env), tok)

    def binop(self, op, a, b, tok):
        ta, tb = a.__class__, b.__class__
        an = ta is int or ta is float
        bn = tb is int or tb is float
        if op == "+":
            if an and bn:
                return a + b
            if ta is str and tb is str:
                return a + b
            if ta is str or tb is str:
                return self.to_str(a) + self.to_str(b)
            if ta is list and tb is list:
                return a + b
            if ta is dict and tb is dict:
                merged = dict(a)
                merged.update(b)
                return merged
        elif op == "-":
            if an and bn:
                return a - b
        elif op == "*":
            if an and bn:
                return a * b
            if (ta is str or ta is list) and tb is int:
                return a * b
            if ta is int and (tb is str or tb is list):
                return a * b
        elif op in ("/", "//", "%"):
            if an and bn:
                if b == 0:
                    raise NyxError("division by zero", tok)
                if op == "/":
                    return a / b
                if op == "//":
                    return a // b
                return a % b
        elif op == "**":
            if an and bn:
                try:
                    r = a ** b
                except OverflowError:
                    raise NyxError("number too large", tok)
                except ZeroDivisionError:
                    raise NyxError("division by zero (0 to a negative power)", tok)
                if isinstance(r, complex):
                    raise NyxError("the result is not a real number", tok)
                return r
        elif op in ("<", "<=", ">", ">="):
            if (an and bn) or (ta is str and tb is str):
                return FAST[op](a, b)
            raise NyxError(f"can't compare {type_name(a)} with {type_name(b)}", tok)
        elif op == "==":
            return equal(a, b)
        elif op == "!=":
            return not equal(a, b)
        elif op == "in":
            return self.contains(b, a, tok)
        elif op == "not in":
            return not self.contains(b, a, tok)
        raise NyxError(f"can't use '{op}' with {type_name(a)} and {type_name(b)}", tok)

    def contains(self, container, item, tok):
        t = container.__class__
        if t is list:
            if item.__class__ is bool:
                return any(x is item for x in container)
            return item in container
        if t is str:
            if item.__class__ is not str:
                raise NyxError(f"can't search for a {type_name(item)} in a string", tok)
            return item in container
        if t is dict:
            try:
                return item in container
            except TypeError:
                return False
        if t is NyxRange:
            if item.__class__ in (int, float) and float(item).is_integer():
                return int(item) in container.py()
            return False
        if t is NyxInstance:
            return item in container.fields
        raise NyxError(f"can't use 'in' with a {type_name(container)}", tok)

    def c_Logical(self, node):
        L, R = self.compile(node.left), self.compile(node.right)
        if node.op == "and":
            def f_and(env):
                a = L(env)
                if a is True:
                    return R(env)
                if a is False or a is None:
                    return a
                return R(env) if truthy(a) else a
            return f_and
        if node.op == "or":
            def f_or(env):
                a = L(env)
                if a is True:
                    return a
                if a is False or a is None:
                    return R(env)
                return a if truthy(a) else R(env)
            return f_or

        def f_nil(env):
            a = L(env)
            return a if a is not None else R(env)
        return f_nil

    def c_Ternary(self, node):
        C, T, O = self.compile(node.cond), self.compile(node.then), self.compile(node.other)

        def f(env):
            c = C(env)
            if c is True or (c is not False and c is not None and truthy(c)):
                return T(env)
            return O(env)
        return f

    def c_RangeExpr(self, node):
        S, E, inclusive, tok = self.compile(node.start), self.compile(node.end), node.inclusive, node.tok
        whole = stdlib.whole

        def f(env):
            return NyxRange(whole(S(env), tok, "range start"), whole(E(env), tok, "range end"), inclusive)
        return f

    # -- calls -------------------------------------------------------------- #
    def compile_args(self, args, tok):
        if any(isinstance(a, Spread) for a in args):
            specs = [(True, self.compile(a.expr)) if isinstance(a, Spread) else (False, self.compile(a)) for a in args]

            def spread_args(env):
                out = []
                for spread, fn in specs:
                    if spread:
                        out.extend(self.iter_for(fn(env), False, tok))
                    else:
                        out.append(fn(env))
                return out
            return spread_args
        A = [self.compile(a) for a in args]
        if not A:
            return lambda env: []
        if len(A) == 1:
            a0 = A[0]
            return lambda env: [a0(env)]
        if len(A) == 2:
            a0, a1 = A
            return lambda env: [a0(env), a1(env)]
        return lambda env: [a(env) for a in A]

    def c_Call(self, node):
        tok = node.tok
        args = self.compile_args(node.args, tok)
        call, call_function = self.call, self.call_function
        callee = node.callee
        if isinstance(callee, Get) and not callee.optional:
            O, name, gtok = self.compile(callee.obj), callee.name, callee.tok
            get_attr = self.get_attr

            def method_call(env):
                obj = O(env)
                if obj.__class__ is NyxInstance and name not in obj.fields:
                    m = obj.klass.find_method(name)
                    if m is not None:
                        return call_function(m, args(env), tok, obj)  # no bound-method allocation
                return call(get_attr(obj, name, gtok), args(env), tok)
            return method_call
        F = self.compile(callee)

        def f(env):
            fn = F(env)
            if fn.__class__ is NyxFunction:
                return call_function(fn, args(env), tok)
            return call(fn, args(env), tok)
        return f

    def call(self, fn, args, tok):
        t = fn.__class__
        if t is NyxFunction:
            return self.call_function(fn, args, tok)
        if t is NativeMethod or t is Builtin:
            n = len(args)
            if n < fn.lo or (fn.hi is not None and n > fn.hi):
                label = f"{type_name(fn.obj)}.{fn.name}()" if t is NativeMethod else f"{fn.name}()"
                raise NyxError(f"{label} {expects(fn.lo, fn.hi)} but got {n}", tok)
            if t is NativeMethod:
                return fn.fn(self, fn.obj, args, tok)
            return fn.fn(self, args, tok)
        if t is NyxClass:
            return self.instantiate(fn, args, tok)
        hint = "this value is nil — check the name, or that the function returns something" if fn is None else None
        raise NyxError(f"a {type_name(fn)} is not callable", tok, hint)

    def call_function(self, fn, args, tok, self_obj=_MISSING):
        if self.interrupted:
            self.check_interrupt()
        env = Env(fn.closure)
        v = env.vars
        if self_obj is not _MISSING:
            v["self"] = self_obj
            v["__class__"] = fn.klass
        if len(args) == fn.arity and fn.rest is None:
            params = fn.params
            for i in range(fn.arity):
                v[params[i]] = args[i]
        else:
            self.bind_args(fn, args, env, tok)
        stack = self.call_stack
        stack.append((fn.name, tok))
        try:
            if len(stack) > MAX_DEPTH:
                raise NyxError(f"stack overflow — more than {MAX_DEPTH} nested calls", tok,
                               "check for a function that never stops calling itself")
            r = fn.body(env)
            if r.__class__ is Flow:
                return r.value
            return r
        except NyxError as e:
            if len(e.trace) < 64:
                e.trace.append((fn.name, tok))
            raise
        finally:
            stack.pop()

    def bind_args(self, fn, args, env, tok):
        params, defaults = fn.params, fn.defaults
        n, np = len(args), len(params)
        if n > np and fn.rest is None:
            required = sum(1 for d in defaults if d is None)
            raise NyxError(f"{fn.name}() {expects(required, np)} but got {n}", tok)
        v = env.vars
        for i, p in enumerate(params):
            if i < n:
                v[p] = args[i]
            elif defaults[i] is not None:
                v[p] = defaults[i](env)  # defaults can use earlier parameters
            else:
                raise NyxError(f"{fn.name}() is missing the argument '{p}'", tok)
        if fn.rest is not None:
            v[fn.rest] = list(args[np:])

    def instantiate(self, klass, args, tok):
        inst = NyxInstance(klass)
        init = klass.find_method("init")
        if init is not None:
            self.call_function(init, args, tok, inst)
        elif args:
            raise NyxError(f"{klass.name}() takes no arguments", tok, "add an init(...) method to the class")
        return inst

    # -- properties & indexing ---------------------------------------------- #
    def c_Get(self, node):
        O, name, tok, optional = self.compile(node.obj), node.name, node.tok, node.optional
        get_attr = self.get_attr

        def f(env):
            obj = O(env)
            if obj.__class__ is NyxInstance:
                fields = obj.fields
                if name in fields:
                    return fields[name]
            elif optional and obj is None:
                return None
            return get_attr(obj, name, tok)
        return f

    def get_attr(self, obj, name, tok):
        t = obj.__class__
        if t is NyxInstance:
            if name in obj.fields:
                return obj.fields[name]
            m = obj.klass.find_method(name)
            if m is not None:
                return m.bind(obj)
            raise NyxError(f"{obj.klass.name} has no property '{name}'", tok,
                           suggest(name, list(obj.fields) + list(obj.klass.method_names())))
        if t is dict:
            if name in obj:
                return obj[name]
            spec = stdlib.METHODS[dict].get(name)
            if spec is not None:
                return NativeMethod(name, obj, spec)
            keys = [k for k in obj if isinstance(k, str)]
            raise NyxError(f"map has no key '{name}'", tok, suggest(name, keys + list(stdlib.METHODS[dict])))
        if t is NyxClass:
            if name == "name":
                return obj.name
            m = obj.find_static(name)
            if m is not None:
                return m
            raise NyxError(f"class {obj.name} has no static method '{name}'", tok)
        if t is Module:
            exports = obj.env.vars
            if name in exports and not name.startswith("_"):
                return exports[name]
            raise NyxError(f"module '{obj.name}' has no '{name}'", tok, suggest(name, obj.exports))
        table = stdlib.METHODS.get(t)
        if table is not None:
            spec = table.get(name)
            if spec is not None:
                return NativeMethod(name, obj, spec)
            raise NyxError(f"{type_name(obj)} has no method '{name}'", tok, suggest(name, table))
        if obj is None:
            raise NyxError(f"can't read '{name}' of nil", tok, "use ?. to read properties of something that may be nil")
        raise NyxError(f"{type_name(obj)} has no property '{name}'", tok)

    def set_attr(self, obj, name, value, tok):
        t = obj.__class__
        if t is NyxInstance:
            obj.fields[name] = value
        elif t is dict:
            obj[name] = value
        else:
            raise NyxError(f"can't set properties on a {type_name(obj)}", tok)

    def c_Index(self, node):
        O, I, tok = self.compile(node.obj), self.compile(node.index), node.tok
        get_index = self.get_index

        def f(env):
            obj, idx = O(env), I(env)
            if obj.__class__ is list and idx.__class__ is int and 0 <= idx < len(obj):
                return obj[idx]
            return get_index(obj, idx, tok)
        return f

    def get_index(self, obj, idx, tok):
        t = obj.__class__
        if t is list or t is str:
            ti = idx.__class__
            if ti is NyxRange:
                start, stop = idx.start, idx.stop
                if idx.inclusive:
                    stop = None if stop == -1 else stop + 1
                return obj[start:stop]
            if ti is int:
                i = idx
            elif ti is float and idx.is_integer():
                i = int(idx)
            else:
                raise NyxError(f"{type_name(obj)} index must be a whole number, not {type_name(idx)}", tok)
            n = len(obj)
            if i < 0:
                i += n
            if 0 <= i < n:
                return obj[i]
            what = "item(s)" if t is list else "character(s)"
            raise NyxError(f"index {idx} is out of range — the {type_name(obj)} has {n} {what}", tok)
        if t is dict:
            try:
                return obj[idx]
            except KeyError:
                keys = [k for k in obj if isinstance(k, str)]
                hint = suggest(idx, keys) if isinstance(idx, str) else None
                raise NyxError(f"key {self.to_str(idx, True)} not found in map", tok,
                               hint or "use map.get(key, default) when a key may be missing")
            except TypeError:
                raise NyxError(f"a {type_name(idx)} can't be a map key", tok)
        if t is NyxInstance and isinstance(idx, str):
            if idx in obj.fields:
                return obj.fields[idx]
            raise NyxError(f"{obj.klass.name} has no field {quote(idx)}", tok)
        raise NyxError(f"can't index a {type_name(obj)}", tok)

    def set_index(self, obj, idx, value, tok):
        t = obj.__class__
        if t is list:
            i = stdlib.whole(idx, tok, "list index")
            n = len(obj)
            if i < 0:
                i += n
            if not 0 <= i < n:
                raise NyxError(f"index {idx} is out of range — the list has {n} item(s)", tok, "use .push(x) to add items")
            obj[i] = value
        elif t is dict:
            if idx.__class__ in (list, dict):
                raise NyxError(f"a {type_name(idx)} can't be a map key", tok)
            obj[idx] = value
        elif t is NyxInstance and isinstance(idx, str):
            obj.fields[idx] = value
        elif t is str:
            raise NyxError("strings can't be changed in place", tok, "build a new string instead")
        else:
            raise NyxError(f"can't set items on a {type_name(obj)}", tok)

    def iter_for(self, value, two, tok):
        t = value.__class__
        if t is list:
            return enumerate(value) if two else value
        if t is NyxRange:
            r = value.py()
            return enumerate(r) if two else r
        if t is str:
            return enumerate(value) if two else value
        if t is dict:
            return list(value.items()) if two else list(value.keys())
        if t is NyxInstance:
            m = value.klass.find_method("iter")
            if m is not None:
                return self.iter_for(self.call_function(m, [], tok, value), two, tok)
        hint = "loop over a range: for i in 0..n" if t in (int, float) else None
        raise NyxError(f"can't loop over a {type_name(value)}", tok, hint)

    # -- functions, matching ------------------------------------------------ #
    def c_Lambda(self, node):
        body = self.compile(node.body) if node.is_expr else self.compile_block(node.body, False)
        params, rest = node.params, node.rest
        defaults = self.compile_defaults(node.defaults)
        return lambda env: NyxFunction("<lambda>", params, defaults, rest, body, env)

    def c_Match(self, node):
        S, tok = self.compile(node.subject), node.tok
        arms = []
        for patterns, guard, body, is_block in node.arms:
            G = self.compile(guard) if guard is not None else None
            B = self.compile_block_value(body) if is_block else self.compile(body)
            arms.append((patterns, G, B))
        match_pattern = self.match_pattern

        def f(env):
            v = S(env)
            for patterns, G, B in arms:
                for p in patterns:
                    bound = match_pattern(p, v)
                    if bound is None:
                        continue
                    scope = env
                    if bound:
                        scope = Env(env)
                        scope.vars.update(bound)
                    if G is not None and not truthy(G(scope)):
                        continue
                    return B(scope)
            raise NyxError(f"no match arm matched {self.to_str(v, True)}", tok,
                           "add a '_ => ...' arm to handle everything else")
        return f

    @staticmethod
    def match_pattern(p, v):
        t = p.__class__
        if t is PWild:
            return {}
        if t is PBind:
            return {p.name: v}
        if t is PLit:
            return {} if equal(v, p.value) else None
        if t is PRange:
            if v.__class__ in (int, float):
                ok = p.lo <= v <= p.hi if p.inclusive else p.lo <= v < p.hi
                return {} if ok else None
            return None
        return None

    # ===================================================================== #
    #  Statements (each returns None or a Flow signal)
    # ===================================================================== #
    def c_ExprStmt(self, node):
        E = self.compile(node.expr)

        def f(env):
            E(env)
        return f

    def c_Block(self, node):
        return self.compile_block(node)

    def c_Let(self, node):
        V = self.compile(node.value) if node.value is not None else None
        target, const, tok = node.target, node.const, node.tok
        if isinstance(target, str):
            name = target
            if const:
                def f_const(env):
                    env.define(name, V(env), True)
                return f_const

            def f(env):
                env.vars[name] = V(env) if V is not None else None
                if env.consts is not None:
                    env.consts.discard(name)
            return f
        if target[0] == "list":
            _, names, rest = target

            def f_list(env):
                seq = list(self.iter_for(V(env), False, tok))
                if len(seq) < len(names):
                    raise NyxError(f"can't unpack {len(seq)} value(s) into {len(names)} names", tok)
                if rest is None and len(seq) > len(names):
                    raise NyxError(f"too many values to unpack ({len(seq)} values, {len(names)} names)", tok,
                                   "collect the rest with ...name")
                for n, val in zip(names, seq):
                    env.define(n, val, const)
                if rest is not None:
                    env.define(rest, seq[len(names):], const)
            return f_list
        names = target[1]

        def f_map(env):
            src = V(env)
            if src.__class__ is dict:
                fields = src
            elif src.__class__ is NyxInstance:
                fields = src.fields
            else:
                raise NyxError(f"can only destructure a map or an object with {{...}}, not a {type_name(src)}", tok)
            for n in names:
                if n not in fields:
                    raise NyxError(f"no key '{n}' to destructure", tok, suggest(n, [k for k in fields if isinstance(k, str)]))
                env.define(n, fields[n], const)
        return f_map

    def c_Assign(self, node):
        target, op, tok = node.target, node.op, node.tok
        V = self.compile(node.value)
        bop = op[:-1] if op != "=" else None
        binop = self.binop
        if target.__class__.__name__ == "Var":
            name = target.name

            def f_var(env):
                val = V(env)
                e = env
                while e is not None:
                    vs = e.vars
                    if name in vs:
                        if e.consts is not None and name in e.consts:
                            raise NyxError(f"can't reassign the constant '{name}'", tok, "declare it with let if it needs to change")
                        if bop is not None:
                            cur = vs[name]
                            if (cur.__class__ is int or cur.__class__ is float) and (val.__class__ is int or val.__class__ is float) and bop != "/":
                                val = FAST[bop](cur, val)
                            else:
                                val = binop(bop, cur, val, tok)
                        vs[name] = val
                        return None
                    e = e.parent
                hint = suggest(name, env.all_names()) or f"declare it first: let {name} = ..."
                raise NyxError(f"undefined variable '{name}'", target.tok, hint)
            return f_var
        if target.__class__.__name__ == "Get":
            O, name = self.compile(target.obj), target.name

            def f_get(env):
                obj = O(env)
                val = V(env)
                if bop is not None:
                    val = binop(bop, self.get_attr(obj, name, target.tok), val, tok)
                self.set_attr(obj, name, val, target.tok)
            return f_get
        O, I = self.compile(target.obj), self.compile(target.index)

        def f_index(env):
            obj, idx = O(env), I(env)
            val = V(env)
            if bop is not None:
                val = binop(bop, self.get_index(obj, idx, target.tok), val, tok)
            self.set_index(obj, idx, val, target.tok)
        return f_index

    def c_FnDecl(self, node):
        name, params, rest = node.name, node.params, node.rest
        body = self.compile_block(node.body, False)
        defaults = self.compile_defaults(node.defaults)

        def f(env):
            env.vars[name] = NyxFunction(name, params, defaults, rest, body, env)
        return f

    def c_ClassDecl(self, node):
        name, tok = node.name, node.tok
        S = self.compile(node.superclass) if node.superclass is not None else None
        specs = [(m.name, m.params, self.compile_defaults(m.defaults), m.rest, self.compile_block(m.body, False))
                 for m in node.methods]
        static_specs = [(m.name, m.params, self.compile_defaults(m.defaults), m.rest, self.compile_block(m.body, False))
                        for m in node.statics]

        def f(env):
            sup = None
            if S is not None:
                sup = S(env)
                if sup.__class__ is not NyxClass:
                    raise NyxError(f"'{node.superclass.name}' is not a class", node.superclass.tok)
            klass = NyxClass(name, sup, {}, {})
            for mname, params, defaults, rest, body in specs:
                klass.methods[mname] = NyxFunction(f"{name}.{mname}", params, defaults, rest, body, env, klass)
            for mname, params, defaults, rest, body in static_specs:
                klass.statics[mname] = NyxFunction(f"{name}.{mname}", params, defaults, rest, body, env, klass)
            env.vars[name] = klass
        return f

    def c_If(self, node):
        branches = [(self.compile(c), self.compile_block(b)) for c, b in node.branches]
        O = self.compile_block(node.other) if node.other is not None else None
        if len(branches) == 1:
            C, B = branches[0]

            def f1(env):
                c = C(env)
                if c is True or (c is not False and c is not None and truthy(c)):
                    return B(env)
                if O is not None:
                    return O(env)
                return None
            return f1

        def f(env):
            for C, B in branches:
                c = C(env)
                if c is True or (c is not False and c is not None and truthy(c)):
                    return B(env)
            if O is not None:
                return O(env)
            return None
        return f

    def c_While(self, node):
        C, B = self.compile(node.cond), self.compile_block(node.body)
        interp = self

        def f(env):
            while True:
                c = C(env)
                if not (c is True or (c is not False and c is not None and truthy(c))):
                    return None
                r = B(env)
                if r is not None:
                    if r is BREAK:
                        return None
                    if r is not CONTINUE:
                        return r
                if interp.interrupted:
                    interp.check_interrupt()
        return f

    def c_For(self, node):
        I, B = self.compile(node.iterable), self.compile_block(node.body, False)
        v1, v2, tok = node.var, node.var2, node.tok
        two = v2 is not None
        fresh = creates_closures(node.body)  # closures need their own copy of the loop variable
        interp = self

        def f(env):
            it = interp.iter_for(I(env), two, tok)
            le = Env(env)
            vs = le.vars
            for item in it:
                if fresh:
                    le = Env(env)
                    vs = le.vars
                if two:
                    vs[v1], vs[v2] = item
                else:
                    vs[v1] = item
                r = B(le)
                if r is not None:
                    if r is BREAK:
                        return None
                    if r is not CONTINUE:
                        return r
                if interp.interrupted:
                    interp.check_interrupt()
            return None
        return f

    def c_Return(self, node):
        if node.value is None:
            return lambda env: Flow("return", None)
        V = self.compile(node.value)
        return lambda env: Flow("return", V(env))

    def c_Break(self, node):
        return lambda env: BREAK

    def c_Continue(self, node):
        return lambda env: CONTINUE

    def c_Try(self, node):
        B = self.compile_block(node.body)
        H = self.compile_block(node.handler, False) if node.handler is not None else None
        F = self.compile_block(node.final) if node.final is not None else None
        name = node.name

        def f(env):
            depth = len(self.call_stack)
            try:
                try:
                    return B(env)
                except NyxError as e:
                    if H is None:
                        raise
                    del self.call_stack[depth:]
                    henv = Env(env)
                    if name is not None:
                        henv.vars[name] = e.value if isinstance(e, NyxThrow) else self.error_object(e)
                    return H(henv)
                except RecursionError:
                    if H is None:
                        raise
                    del self.call_stack[depth:]
                    henv = Env(env)
                    if name is not None:
                        henv.vars[name] = {"message": "stack overflow", "type": "RuntimeError", "line": None}
                    return H(henv)
            finally:
                if F is not None:
                    F(env)
        return f

    def error_object(self, e):
        return {"message": e.message, "type": e.kind, "line": e.tok.line if e.tok is not None else None}

    def c_Throw(self, node):
        V, tok = self.compile(node.value), node.tok

        def f(env):
            v = V(env)
            if v.__class__ is dict and isinstance(v.get("message"), str):
                text = v["message"]
            else:
                text = self.to_str(v)
            raise NyxThrow(v, tok, text)
        return f

    def c_Import(self, node):
        path, alias, tok = node.path, node.alias, node.tok

        def f(env):
            mod = self.load_module(path, tok)
            env.vars[alias or mod.name] = mod
        return f

    def load_module(self, path, tok):
        if tok.file and not tok.file.startswith("<"):
            base = os.path.dirname(os.path.abspath(tok.file))
        else:
            base = os.getcwd()
        full = os.path.normpath(os.path.join(base, path))
        if not os.path.exists(full) and os.path.exists(full + ".nyx"):
            full += ".nyx"
        if full in self.modules:
            return self.modules[full]
        try:
            with open(full, encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            raise NyxError(f"can't import '{path}': file not found", tok, f"looked for {full}")
        try:
            display = os.path.relpath(full)
        except ValueError:
            display = full
        name = re.sub(r"\W", "_", os.path.splitext(os.path.basename(full))[0])
        menv = Env(self.builtins)
        mod = Module(name, full, menv)
        self.modules[full] = mod  # registered first, so circular imports don't loop forever
        for _, fn in self.compile_program(source, display):
            fn(menv)
        return mod

    # ===================================================================== #
    #  Converting values to text
    # ===================================================================== #
    def to_str(self, v, quote_str=False, seen=None):
        t = v.__class__
        if t is str:
            return quote(v) if quote_str else v
        if v is None:
            return "nil"
        if t is bool:
            return "true" if v else "false"
        if t is int:
            return str(v)
        if t is float:
            return fmt_float(v)
        if t is list or t is dict:
            seen = seen if seen is not None else set()
            if id(v) in seen:
                return "[...]" if t is list else "{...}"
            seen.add(id(v))
            try:
                if t is list:
                    return "[" + ", ".join(self.to_str(x, True, seen) for x in v) + "]"
                parts = []
                for k, x in v.items():
                    key = k if (k.__class__ is str and IDENT_RE.match(k)) else self.to_str(k, True, seen)
                    parts.append(f"{key}: {self.to_str(x, True, seen)}")
                return "{" + ", ".join(parts) + "}"
            finally:
                seen.discard(id(v))
        if t is NyxInstance:
            m = v.klass.find_method("to_str")
            if m is not None:
                r = self.call_function(m, [], None, v)
                return r if r.__class__ is str else self.to_str(r)
            return f"{v.klass.name} {self.to_str(v.fields, True, seen)}"
        if t is NyxFunction:
            return f"<fn {v.name}>"
        if t is Builtin:
            return f"<builtin {v.name}>"
        if t is NativeMethod:
            return f"<method {type_name(v.obj)}.{v.name}>"
        if t is NyxClass:
            return f"<class {v.name}>"
        if t is NyxRange:
            return f"{v.start}..{'=' if v.inclusive else ''}{v.stop}"
        if t is Module:
            return f"<module {v.name}>"
        return str(v)


def expects(lo, hi):
    if hi is None:
        return f"expects at least {lo} argument{'s' if lo != 1 else ''}"
    if lo == hi:
        return f"expects {lo} argument{'s' if lo != 1 else ''}"
    return f"expects {lo} to {hi} arguments"
