"""Lexer: turns NYX source text into a list of tokens."""

from .errors import NyxSyntaxError

KEYWORDS = {
    "let", "const", "fn", "return", "if", "elif", "else", "while", "for", "in", "break",
    "continue", "class", "extends", "self", "super", "true", "false", "nil", "and", "or",
    "not", "try", "catch", "finally", "throw", "import", "as", "match", "static",
}

# longest first, so "..=" wins over ".." and "."
SYMBOLS = sorted([
    "...", "..=", "**", "//", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "=>", "|>",
    "..", "??", "?.", "(", ")", "{", "}", "[", "]", ",", ".", ":", ";", "?", "+", "-",
    "*", "/", "%", "=", "<", ">", "|",
], key=len, reverse=True)

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "{": "{", "}": "}", "0": "\0", "e": "\x1b"}


class Token:
    __slots__ = ("type", "value", "line", "col", "nl", "file", "length")

    def __init__(self, type, value, line, col, nl, file, length=1):
        self.type = type      # "num", "str", "ident", "eof", a keyword or a symbol
        self.value = value
        self.line = line
        self.col = col
        self.nl = nl          # True if a line break came before this token
        self.file = file
        self.length = length

    def __repr__(self):
        return f"Token({self.type!r}, {self.value!r}, {self.line}:{self.col})"


class Lexer:
    def __init__(self, source, file="<input>", line=1, col=1):
        self.src = source
        self.file = file
        self.i = 0
        self.line = line
        self.col = col
        self.tokens = []
        self.nl = False

    # -- helpers ------------------------------------------------------------ #
    def advance(self, k=1):
        for _ in range(k):
            if self.i >= len(self.src):
                return
            if self.src[self.i] == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
            self.i += 1

    def peek(self, k=0):
        j = self.i + k
        return self.src[j] if j < len(self.src) else ""

    def add(self, type, value, line, col, length):
        self.tokens.append(Token(type, value, line, col, self.nl, self.file, length))
        self.nl = False

    def error(self, msg, line, col, length=1, at_eof=False, hint=None):
        err = NyxSyntaxError(msg, Token("error", None, line, col, False, self.file, length), hint)
        err.at_eof = at_eof
        raise err

    # -- main loop ---------------------------------------------------------- #
    def tokenize(self):
        src = self.src
        n = len(src)
        while self.i < n:
            c = src[self.i]
            if c == "\n":
                self.advance()
                self.nl = True
                continue
            if c in " \t\r\f﻿":
                self.advance()
                continue
            if c == "#":
                while self.i < n and src[self.i] != "\n":
                    self.advance()
                continue
            line, col = self.line, self.col
            if c.isdigit():
                self.number(line, col)
                continue
            if c.isalpha() or c == "_":
                start = self.i
                while self.i < n and (src[self.i].isalnum() or src[self.i] == "_"):
                    self.advance()
                word = src[start:self.i]
                if word in KEYWORDS:
                    self.add(word, word, line, col, len(word))
                else:
                    self.add("ident", word, line, col, len(word))
                continue
            if c == '"':
                self.string(line, col)
                continue
            for sym in SYMBOLS:
                if src.startswith(sym, self.i):
                    self.advance(len(sym))
                    self.add(sym, sym, line, col, len(sym))
                    break
            else:
                hint = "use double quotes for strings" if c == "'" else None
                self.error(f"unexpected character {c!r}", line, col, hint=hint)
        self.add("eof", None, self.line, self.col, 1)
        return self.tokens

    def number(self, line, col):
        src = self.src
        start = self.i
        nxt = self.peek(1).lower()
        if src[self.i] == "0" and nxt in ("x", "b"):
            base = 16 if nxt == "x" else 2
            digits = "0123456789abcdefABCDEF_" if base == 16 else "01_"
            self.advance(2)
            while self.i < len(src) and src[self.i] in digits:
                self.advance()
            text = src[start + 2:self.i].replace("_", "")
            if not text:
                self.error("number is missing its digits", line, col, self.i - start)
            self.add("num", int(text, base), line, col, self.i - start)
            return
        is_float = False
        while self.i < len(src) and (src[self.i].isdigit() or src[self.i] == "_"):
            self.advance()
        if self.peek() == "." and self.peek(1).isdigit():  # "1..5" stays a range
            is_float = True
            self.advance()
            while self.i < len(src) and (src[self.i].isdigit() or src[self.i] == "_"):
                self.advance()
        if self.peek() in ("e", "E") and (self.peek(1).isdigit() or (self.peek(1) in "+-" and self.peek(2).isdigit())):
            is_float = True
            self.advance(2)
            while self.i < len(src) and src[self.i].isdigit():
                self.advance()
        text = src[start:self.i].replace("_", "")
        self.add("num", float(text) if is_float else int(text), line, col, self.i - start)

    def string(self, line, col):
        """Strings support escapes and {expression} interpolation. Parts are plain
        text or (code, line, col) tuples that the parser turns into expressions."""
        src = self.src
        n = len(src)
        start = self.i
        self.advance()  # opening quote
        parts, buf = [], []
        while True:
            if self.i >= n:
                self.error("unterminated string", line, col, at_eof=True, hint='add a closing "')
            c = src[self.i]
            if c == '"':
                self.advance()
                break
            if c == "\\":
                esc = ESCAPES.get(self.peek(1))
                if esc is None:
                    self.error(f"unknown escape sequence '\\{self.peek(1)}'", self.line, self.col, 2)
                buf.append(esc)
                self.advance(2)
                continue
            if c == "{":
                if self.peek(1) == "{":
                    buf.append("{")
                    self.advance(2)
                    continue
                if buf:
                    parts.append("".join(buf))
                    buf = []
                self.advance()
                eline, ecol = self.line, self.col
                estart = self.i
                depth = 1
                while True:
                    if self.i >= n:
                        self.error("unterminated {interpolation} in string", eline, ecol, at_eof=True)
                    ch = src[self.i]
                    if ch == '"':  # skip a nested string literal
                        self.advance()
                        while self.i < n and src[self.i] != '"':
                            if src[self.i] == "\\":
                                self.advance()
                            self.advance()
                        self.advance()
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    self.advance()
                code = src[estart:self.i]
                self.advance()  # closing }
                if not code.strip():
                    self.error("empty {} in string", eline, ecol - 1, 2, hint="write {{ for a literal brace")
                parts.append((code, eline, ecol))
                continue
            if c == "}" and self.peek(1) == "}":
                buf.append("}")
                self.advance(2)
                continue
            buf.append(c)
            self.advance()
        if buf or not parts:
            parts.append("".join(buf))
        length = self.i - start if self.line == line else 1
        self.add("str", parts, line, col, length)
