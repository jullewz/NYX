"""
NYX - a small, fast, dynamically typed programming language written in pure Python.
by jullewz
"""

__version__ = "1.0.0"
__author__ = "jullewz"

from .errors import NyxError, NyxSyntaxError  # noqa: E402,F401
from .interpreter import Interpreter  # noqa: E402,F401


def run(source, file="<input>"):
    """Run NYX source code and return the value of the last expression."""
    return Interpreter().run(source, file)
