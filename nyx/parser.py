"""Parser: a recursive-descent statement parser plus a Pratt (precedence climbing)
expression parser that turns tokens into an AST."""

from .errors import NyxSyntaxError
from .lexer import KEYWORDS, Lexer
from .nodes import (
    Assign, Binary, Block, Break, Call, ClassDecl, Continue, ExprStmt, FnDecl, For, Get, If,
    Import, Index, Lambda, Let, ListComp, ListExpr, Literal, Logical, MapExpr, Match, Node,
    PBind, PLit, PRange, Program, PWild, RangeExpr, Return, SelfExpr, Spread, StringInterp,
    SuperExpr, Ternary, Throw, Try, Unary, Var, While,
)

# binding power of infix operators (higher binds tighter)
BINARY = {
    "|>": 1,
    "??": 2,
    "or": 3,
    "and": 4,
    "==": 6, "!=": 6, "<": 6, "<=": 6, ">": 6, ">=": 6, "in": 6, "not in": 6,
    "..": 7, "..=": 7,
    "+": 8, "-": 8,
    "*": 9, "/": 9, "//": 9, "%": 9,
}
NOT_PREC = 5
ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=")
DECLARES = ()  # filled after class definitions below


def describe(tok):
    t = tok.type
    if t == "eof":
        return "end of file"
    if t == "str":
        return "a string"
    if t == "num":
        return f"number {tok.value}"
    if t == "ident":
        return f"'{tok.value}'"
    if t in KEYWORDS:
        return f"keyword '{t}'"
    return f"'{t}'"


class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0
        self.loop_depth = 0
        self.fn_depth = 0
        self.in_arm_block = False

    # -- token helpers ------------------------------------------------------ #
    def peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, *types):
        return self.toks[self.i].type in types

    def advance(self):
        tok = self.toks[self.i]
        if tok.type != "eof":
            self.i += 1
        return tok

    def match(self, *types):
        if self.toks[self.i].type in types:
            return self.advance()
        return None

    def error(self, msg, tok=None, hint=None):
        tok = tok or self.peek()
        err = NyxSyntaxError(msg, tok, hint)
        err.at_eof = tok.type == "eof"
        return err

    def expect(self, type, what=None, hint=None):
        if self.toks[self.i].type == type:
            return self.advance()
        tok = self.peek()
        raise self.error(f"expected {what or repr(type)} but found {describe(tok)}", tok, hint)

    def name(self, what="a name"):
        tok = self.advance()
        if tok.type == "ident" or tok.type in KEYWORDS:
            return tok.value
        raise self.error(f"expected {what} but found {describe(tok)}", tok)

    # -- program & statements ---------------------------------------------- #
    def program(self):
        stmts = []
        while not self.at("eof"):
            stmts.append(self.statement())
        return Program(stmts, tok=self.toks[0])

    def block(self):
        start = self.expect("{", "'{'")
        stmts = []
        while not self.at("}"):
            if self.at("eof"):
                raise self.error("missing '}' to close the block", hint=f"the block opened on line {start.line}")
            stmts.append(self.statement())
        self.advance()
        scoped = any(isinstance(s, DECLARES) for s in stmts)
        return Block(stmts, scoped, tok=start)

    def statement(self):
        tok = self.peek()
        t = tok.type
        if t in ("let", "const"):
            stmt = self.let_stmt()
        elif t == "fn" and self.peek(1).type == "ident":
            stmt = self.fn_decl()
        elif t == "class":
            stmt = self.class_decl()
        elif t == "if":
            stmt = self.if_stmt()
        elif t == "while":
            stmt = self.while_stmt()
        elif t == "for":
            stmt = self.for_stmt()
        elif t == "return":
            stmt = self.return_stmt()
        elif t in ("break", "continue"):
            self.advance()
            if self.loop_depth == 0 or self.in_arm_block:
                raise self.error(f"'{t}' can only be used inside a loop", tok)
            stmt = Break(tok=tok) if t == "break" else Continue(tok=tok)
        elif t == "try":
            stmt = self.try_stmt()
        elif t == "throw":
            self.advance()
            stmt = Throw(self.expression(), tok=tok)
        elif t == "import":
            stmt = self.import_stmt()
        elif t == "{" and not self.looks_like_map():
            stmt = self.block()
        else:
            stmt = self.expr_or_assign()
        self.match(";")
        return stmt

    def looks_like_map(self):
        """At the start of a statement '{' is a block — unless it is clearly a map
        literal like {a: 1} or {}.keys()."""
        a, b = self.peek(1), self.peek(2)
        if a.type == "}":
            return b.type in (".", "?.", "[") and not b.nl
        if a.type == "[":
            return True
        return (a.type in ("ident", "str", "num") or a.type in KEYWORDS) and b.type == ":"

    def let_stmt(self):
        kw = self.advance()
        const = kw.type == "const"
        if self.match("["):
            names, rest = [], None
            while not self.at("]"):
                if self.match("..."):
                    rest = self.expect("ident", "a name after '...'").value
                    break
                names.append(self.expect("ident", "a variable name").value)
                if not self.match(","):
                    break
            self.expect("]", "']'")
            target = ("list", names, rest)
        elif self.match("{"):
            names = []
            while not self.at("}"):
                names.append(self.expect("ident", "a key name").value)
                if not self.match(","):
                    break
            self.expect("}", "'}'")
            target = ("map", names)
        else:
            target = self.expect("ident", "a variable name").value
        if self.match("="):
            value = self.expression()
        elif const:
            raise self.error("a const needs a value", hint="const NAME = value")
        elif not isinstance(target, str):
            raise self.error("destructuring needs a value after '='")
        else:
            value = None
        return Let(target, value, const, tok=kw)

    def params(self):
        self.expect("(", "'(' to start the parameter list")
        params, defaults, rest = [], [], None
        while not self.at(")"):
            if self.match("..."):
                rest = self.expect("ident", "a name after '...'").value
                break
            ptok = self.expect("ident", "a parameter name")
            if ptok.value in params:
                raise self.error(f"duplicate parameter '{ptok.value}'", ptok)
            params.append(ptok.value)
            defaults.append(self.expression() if self.match("=") else None)
            if not self.match(","):
                break
        self.expect(")", "')' to close the parameter list")
        return params, defaults, rest

    def function_body(self):
        saved = (self.loop_depth, self.in_arm_block)
        self.loop_depth, self.in_arm_block = 0, False
        self.fn_depth += 1
        try:
            return self.block()
        finally:
            self.fn_depth -= 1
            self.loop_depth, self.in_arm_block = saved

    def fn_decl(self):
        self.advance()  # fn
        name_tok = self.expect("ident", "a function name")
        params, defaults, rest = self.params()
        if self.at("=>"):  # fn square(x) => x * x
            arrow = self.advance()
            saved = (self.loop_depth, self.in_arm_block)
            self.loop_depth, self.in_arm_block = 0, False
            self.fn_depth += 1
            try:
                expr = self.expression()
            finally:
                self.fn_depth -= 1
                self.loop_depth, self.in_arm_block = saved
            body = Block([Return(expr, tok=arrow)], False, tok=arrow)
        else:
            body = self.function_body()
        return FnDecl(name_tok.value, params, defaults, rest, body, tok=name_tok)

    def class_decl(self):
        self.advance()
        name_tok = self.expect("ident", "a class name")
        superclass = None
        if self.match("extends"):
            st = self.expect("ident", "a parent class name")
            superclass = Var(st.value, tok=st)
        self.expect("{", "'{' to start the class body")
        methods, statics = [], []
        while not self.at("}"):
            if self.at("eof"):
                raise self.error("missing '}' to close the class")
            static = bool(self.match("static"))
            if not self.at("fn"):
                raise self.error(f"expected a method ('fn name()') in class body but found {describe(self.peek())}")
            method = self.fn_decl()
            (statics if static else methods).append(method)
            self.match(";")
        self.advance()
        return ClassDecl(name_tok.value, superclass, methods, statics, tok=name_tok)

    def if_stmt(self):
        kw = self.advance()
        branches = [(self.expression(), self.block())]
        other = None
        while True:
            if self.match("elif"):
                branches.append((self.expression(), self.block()))
            elif self.at("else") and self.peek(1).type == "if":
                self.advance()
                self.advance()
                branches.append((self.expression(), self.block()))
            else:
                break
        if self.match("else"):
            other = self.block()
        return If(branches, other, tok=kw)

    def loop_body(self):
        self.loop_depth += 1
        try:
            return self.block()
        finally:
            self.loop_depth -= 1

    def while_stmt(self):
        kw = self.advance()
        cond = self.expression()
        return While(cond, self.loop_body(), tok=kw)

    def for_stmt(self):
        kw = self.advance()
        var = self.expect("ident", "a loop variable").value
        var2 = self.expect("ident", "a second loop variable").value if self.match(",") else None
        self.expect("in", "'in'", hint="for item in list { ... }")
        iterable = self.expression()
        return For(var, var2, iterable, self.loop_body(), tok=kw)

    def return_stmt(self):
        kw = self.advance()
        if self.fn_depth == 0:
            raise self.error("'return' outside of a function", kw)
        if self.in_arm_block:
            raise self.error("'return' can't be used inside a match arm block", kw)
        value = None
        if not self.at("}", ";", "eof") and not self.peek().nl:
            value = self.expression()
        return Return(value, tok=kw)

    def try_stmt(self):
        kw = self.advance()
        body = self.block()
        name = handler = final = None
        if self.match("catch"):
            if self.at("ident"):
                name = self.advance().value
            handler = self.block()
        if self.match("finally"):
            final = self.block()
        if handler is None and final is None:
            raise self.error("'try' needs a 'catch' or 'finally' block", kw)
        return Try(body, name, handler, final, tok=kw)

    def import_stmt(self):
        kw = self.advance()
        ptok = self.expect("str", 'a file path like "lib/math.nyx"')
        if len(ptok.value) != 1 or not isinstance(ptok.value[0], str):
            raise self.error("import paths can't contain {interpolation}", ptok)
        alias = self.expect("ident", "a module name").value if self.match("as") else None
        return Import(ptok.value[0], alias, tok=kw)

    def expr_or_assign(self):
        start = self.peek()
        expr = self.expression()
        if self.at(*ASSIGN_OPS):
            op = self.advance()
            if not isinstance(expr, (Var, Get, Index)):
                raise self.error("can't assign to this expression", op,
                                 hint="you can assign to variables, properties (a.b) and items (a[i])")
            if isinstance(expr, Get) and expr.optional:
                raise self.error("can't assign through '?.'", op)
            return Assign(expr, op.type, self.expression(), tok=op)
        return ExprStmt(expr, tok=start)

    # -- expressions --------------------------------------------------------- #
    def expression(self):
        cond = self.binary(0)
        if self.at("?"):
            q = self.advance()
            then = self.expression()
            self.expect(":", "':' in the ternary (cond ? a : b)")
            other = self.expression()
            return Ternary(cond, then, other, tok=q)
        return cond

    def binary(self, min_prec):
        left = self.unary()
        while True:
            tok = self.peek()
            op = tok.type
            if op == "not" and self.peek(1).type == "in":
                op = "not in"
            prec = BINARY.get(op)
            if prec is None or prec <= min_prec:
                return left
            if tok.nl and op in ("+", "-"):
                return left  # a new line starting with +/- is a new statement
            self.advance()
            if op == "not in":
                self.advance()
            right = self.binary(prec)
            if op in ("and", "or", "??"):
                left = Logical(op, left, right, tok=tok)
            elif op == "|>":
                left = self.pipe(left, right, tok)
            elif op in ("..", "..="):
                left = RangeExpr(left, right, op == "..=", tok=tok)
            else:
                left = Binary(op, left, right, tok=tok)

    def pipe(self, value, target, tok):
        """x |> f(a, b)  ==>  f(x, a, b)      x |> f  ==>  f(x)"""
        if isinstance(target, Call):
            return Call(target.callee, [value] + target.args, tok=target.tok)
        return Call(target, [value], tok=tok)

    def unary(self):
        tok = self.peek()
        if tok.type == "-":
            self.advance()
            operand = self.unary()
            if isinstance(operand, Literal) and type(operand.value) in (int, float):
                return Literal(-operand.value, tok=tok)  # constant folding
            return Unary("-", operand, tok=tok)
        if tok.type == "not":
            self.advance()
            return Unary("not", self.binary(NOT_PREC), tok=tok)
        return self.power()

    def power(self):
        base = self.postfix()
        if self.at("**"):
            tok = self.advance()
            return Binary("**", base, self.unary(), tok=tok)  # right associative
        return base

    def postfix(self):
        expr = self.primary()
        while True:
            tok = self.peek()
            t = tok.type
            if t == "(" and not tok.nl:
                expr = Call(expr, self.arguments(), tok=tok)
            elif t == "[" and not tok.nl:
                self.advance()
                index = self.expression()
                self.expect("]", "']'")
                expr = Index(expr, index, tok=tok)
            elif t in (".", "?."):
                self.advance()
                name_tok = self.peek()
                expr = Get(expr, self.name("a property name"), t == "?.", tok=name_tok)
            else:
                return expr

    def arguments(self):
        self.advance()  # (
        args = []
        while not self.at(")"):
            if self.at("..."):
                st = self.advance()
                args.append(Spread(self.expression(), tok=st))
            else:
                args.append(self.expression())
            if not self.match(","):
                break
        self.expect(")", "')' to close the call", hint="did you forget a comma between arguments?")
        return args

    def primary(self):
        tok = self.advance()
        t = tok.type
        if t == "num":
            return Literal(tok.value, tok=tok)
        if t == "str":
            return self.string_node(tok)
        if t == "true":
            return Literal(True, tok=tok)
        if t == "false":
            return Literal(False, tok=tok)
        if t == "nil":
            return Literal(None, tok=tok)
        if t == "ident":
            return Var(tok.value, tok=tok)
        if t == "self":
            return SelfExpr(tok=tok)
        if t == "super":
            self.expect(".", "'.' after super", hint="super.method(...)")
            return SuperExpr(self.name("a method name"), tok=tok)
        if t == "(":
            expr = self.expression()
            self.expect(")", "')'")
            return expr
        if t == "[":
            return self.list_literal(tok)
        if t == "{":
            return self.map_literal(tok)
        if t == "fn":
            return self.lambda_expr(tok)
        if t == "match":
            return self.match_expr(tok)
        if t == "eof":
            raise self.error("unexpected end of file", tok)
        hint = None
        if t in ("=", "+=", "-="):
            hint = "use '==' to compare values"
        elif t == "}":
            hint = "there might be an extra '}' or a missing value"
        raise self.error(f"unexpected {describe(tok)}", tok, hint)

    def string_node(self, tok):
        parts = tok.value
        if len(parts) == 1 and isinstance(parts[0], str):
            return Literal(parts[0], tok=tok)
        nodes = []
        for part in parts:
            if isinstance(part, str):
                nodes.append(part)
                continue
            code, line, col = part
            sub = Parser(Lexer(code, tok.file, line, col).tokenize())
            expr = sub.expression()
            if not sub.at("eof"):
                raise sub.error(f"unexpected {describe(sub.peek())} in string interpolation")
            nodes.append(expr)
        return StringInterp(nodes, tok=tok)

    def list_literal(self, tok):
        if self.match("]"):
            return ListExpr([], tok=tok)
        first = self.list_item()
        if self.at("for"):
            self.advance()
            var = self.expect("ident", "a loop variable").value
            var2 = self.expect("ident", "a second loop variable").value if self.match(",") else None
            self.expect("in", "'in'")
            iterable = self.expression()
            cond = self.expression() if self.match("if") else None
            self.expect("]", "']' to close the list comprehension")
            return ListComp(first, var, var2, iterable, cond, tok=tok)
        items = [first]
        while self.match(","):
            if self.at("]"):
                break
            items.append(self.list_item())
        self.expect("]", "']' to close the list", hint="did you forget a comma between items?")
        return ListExpr(items, tok=tok)

    def list_item(self):
        if self.at("..."):
            st = self.advance()
            return Spread(self.expression(), tok=st)
        return self.expression()

    def map_literal(self, tok):
        entries = []
        while not self.at("}"):
            kt = self.peek()
            if kt.type == "ident" and self.peek(1).type in (",", "}"):
                self.advance()
                entries.append((kt.value, Var(kt.value, tok=kt)))  # {name} shorthand
            else:
                if kt.type == "ident" or kt.type in KEYWORDS:
                    key = self.advance().value
                elif kt.type == "str":
                    node = self.string_node(self.advance())
                    key = node.value if isinstance(node, Literal) else node
                elif kt.type == "num":
                    key = self.advance().value
                elif kt.type == "[":
                    self.advance()
                    key = self.expression()
                    self.expect("]", "']' after computed key")
                else:
                    raise self.error(f"expected a map key but found {describe(kt)}", kt)
                self.expect(":", "':' after the map key")
                entries.append((key, self.expression()))
            if not self.match(","):
                break
        self.expect("}", "'}' to close the map", hint="did you forget a comma between entries?")
        return MapExpr(entries, tok=tok)

    def lambda_expr(self, tok):
        params, defaults, rest = self.params()
        if self.match("=>"):
            saved = (self.loop_depth, self.in_arm_block)
            self.loop_depth, self.in_arm_block = 0, False
            self.fn_depth += 1
            try:
                body = self.expression()
            finally:
                self.fn_depth -= 1
                self.loop_depth, self.in_arm_block = saved
            return Lambda(params, defaults, rest, body, True, tok=tok)
        return Lambda(params, defaults, rest, self.function_body(), False, tok=tok)

    def match_expr(self, tok):
        subject = self.expression()
        self.expect("{", "'{' after the match value")
        arms = []
        while not self.at("}"):
            if self.at("eof"):
                raise self.error("missing '}' to close the match")
            patterns = [self.pattern()]
            while self.match("|"):
                patterns.append(self.pattern())
            guard = self.expression() if self.match("if") else None
            self.expect("=>", "'=>' after the pattern")
            if self.at("{"):
                saved = self.in_arm_block
                self.in_arm_block = True
                try:
                    body = self.block()
                finally:
                    self.in_arm_block = saved
                arms.append((patterns, guard, body, True))
            else:
                arms.append((patterns, guard, self.expression(), False))
            self.match(",")
        self.advance()
        if not arms:
            raise self.error("a match needs at least one arm", tok)
        return Match(subject, arms, tok=tok)

    def pattern(self):
        tok = self.peek()
        if tok.type == "ident":
            self.advance()
            return PWild(tok=tok) if tok.value == "_" else PBind(tok.value, tok=tok)
        lo = self.literal_value()
        if self.at("..", "..="):
            inclusive = self.advance().type == "..="
            return PRange(lo, self.literal_value(), inclusive, tok=tok)
        return PLit(lo, tok=tok)

    def literal_value(self):
        tok = self.advance()
        if tok.type == "-":
            num = self.advance()
            if num.type != "num":
                raise self.error("expected a number after '-' in pattern", num)
            return -num.value
        if tok.type == "num":
            return tok.value
        if tok.type == "str" and len(tok.value) == 1 and isinstance(tok.value[0], str):
            return tok.value[0]
        if tok.type in ("true", "false", "nil"):
            return {"true": True, "false": False, "nil": None}[tok.type]
        raise self.error(f"patterns can be literals, ranges, '_' or a name — found {describe(tok)}", tok)


DECLARES = (Let, FnDecl, ClassDecl, Import)


def parse(source, file="<input>"):
    return Parser(Lexer(source, file).tokenize()).program()


__all__ = ["Parser", "parse", "Node"]
