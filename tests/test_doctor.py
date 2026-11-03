"""Tests for `nyx doctor`.  Run:  python -m unittest discover -s tests -v"""

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nyx.doctor import detect_language, diagnose  # noqa: E402
from nyx.doctor.nyxchecks import parse_error as nyx_parse_error  # noqa: E402


def fix(code, lang="python"):
    return diagnose(code, lang)


def codes(result, fixed=None):
    return {i.code for i in result.issues if fixed is None or i.fixed == fixed}


class PythonSyntaxRepair(unittest.TestCase):
    def check(self, broken, expected_fragment):
        r = fix(broken)
        self.assertFalse(r.broken, r.issues)
        ast.parse(r.fixed)
        self.assertIn(expected_fragment, r.fixed)
        return r

    def test_missing_colon(self):
        self.check("def f(x)\n    return x\n", "def f(x):")
        self.check("for i in range(3)\n    print(i)\n", "for i in range(3):")

    def test_assignment_in_condition(self):
        self.check("x = 1\nif x = 1:\n    pass\n", "if x == 1:")

    def test_brackets(self):
        self.check("print((1 + 2)\nx = 3\n", "print((1 + 2))")
        self.check("xs = [1, 2\nprint(xs)\n", "xs = [1, 2]")
        self.check("print(1))\n", "print(1)\n")

    def test_strings(self):
        self.check("name = 'abc\nprint(name)\n", "name = 'abc'")

    def test_missing_comma(self):
        self.check("xs = [1, 2 3]\n", "[1, 2, 3]")

    def test_indentation(self):
        self.check("def f():\nreturn 1\n", "    return 1")
        self.check("x = 1\n    y = 2\n", "\ny = 2")

    def test_python2_and_js_habits(self):
        self.check('print "hi"\n', 'print("hi")')
        self.check("try:\n    pass\nexcept ValueError, e:\n    pass\n", "except ValueError as e:")
        self.check("x = 1\nif x > 0 && x < 5:\n    pass\n", "x > 0 and x < 5")
        r = self.check("function f(a) {\n    if (a > 1) {\n        return a\n    }\n    return 0\n}\n", "def f(a):")
        self.assertIn("    if a > 1:", r.fixed)


class PythonAnalysis(unittest.TestCase):
    def test_undefined_name_typo(self):
        r = fix('def hello():\n    pritn("hi")\n')
        self.assertIn('print("hi")', r.fixed)

    def test_aliases(self):
        r = fix("x = true\ny = null\n")
        self.assertIn("x = True", r.fixed)
        self.assertIn("y = None", r.fixed)

    def test_missing_import(self):
        r = fix("print(math.pi)\n")
        self.assertTrue(r.fixed.startswith("import math\n"))
        r = fix('"""Doc."""\nc = Counter("abc")\n')
        self.assertIn('"""Doc."""\nfrom collections import Counter\n', r.fixed)

    def test_unused_imports(self):
        r = fix("import os, sys\nprint(sys.argv)\n")
        self.assertIn("import sys\n", r.fixed)
        self.assertNotIn("os", r.fixed)

    def test_comparisons(self):
        r = fix("def f(x):\n    if x == None:\n        return x is 'a'\n    if type(x) == int:\n        return 1\n")
        self.assertIn("x is None", r.fixed)
        self.assertIn("x == 'a'", r.fixed)
        self.assertIn("isinstance(x, int)", r.fixed)

    def test_mutable_default(self):
        r = fix("def add(x, xs=[]):\n    xs.append(x)\n    return xs\n")
        self.assertIn("def add(x, xs=None):\n    if xs is None:\n        xs = []\n", r.fixed)

    def test_methods(self):
        r = fix("class A:\n    def m():\n        return 1\n    def n(self):\n        return m()\n")
        self.assertIn("def m(self):", r.fixed)
        self.assertIn("return self.m()", r.fixed)

    def test_input_used_as_number(self):
        r = fix('age = input("age? ")\nif age >= 18:\n    print("ok")\n')
        self.assertIn('age = int(input("age? "))', r.fixed)

    def test_string_plus_number(self):
        r = fix('print("n = " + 5)\n')
        self.assertIn('"n = " + str(5)', r.fixed)

    def test_bare_except_and_fstring(self):
        r = fix('try:\n    pass\nexcept:\n    x = 1\nprint(f"plain {{x}}")\n')
        self.assertIn("except Exception:", r.fixed)
        self.assertIn('print("plain {x}")', r.fixed)

    def test_warnings_without_fixes(self):
        r = fix("def f():\n    unused = 1\n    return 2\n    print('never')\nwhile True:\n    pass\n")
        found = codes(r, fixed=False)
        self.assertTrue({"unused-variable", "unreachable", "infinite-loop"} <= found, found)

    def test_clean_code_is_untouched(self):
        clean = "import sys\n\n\ndef main(argv):\n    for i, arg in enumerate(argv):\n        print(i, arg)\n\n\nmain(sys.argv)\n"
        r = fix(clean)
        self.assertEqual(r.fixed, clean)
        self.assertEqual(r.issues, [])
        self.assertEqual(r.score_after, 100)

    def test_idempotent(self):
        once = fix("import os\ndef f(x)\n    return x == None\n").fixed
        self.assertEqual(fix(once).fixed, once)


class NyxDoctor(unittest.TestCase):
    def test_foreign_syntax_and_names(self):
        src = "function add(a, b) {\n    return a + b\n}\nvar x = add(1, 2)\nif x != null && true {\n    console.log('x')\n}\n"
        r = fix(src, "nyx")
        self.assertIsNone(nyx_parse_error(r.fixed))
        self.assertIn("fn add(a, b)", r.fixed)
        self.assertIn("let x = ", r.fixed)
        self.assertIn("x != nil and true", r.fixed)
        self.assertIn('print("x")', r.fixed)

    def test_typos_consts_and_undeclared(self):
        r = fix("const LIMIT = 3\nlet total = 0\ntotl = 1\nLIMIT = 4\ncount = 2\nprint(total, count, LIMIT)\n", "nyx")
        self.assertIn("total = 1", r.fixed)
        self.assertIn("let LIMIT = 3", r.fixed)
        self.assertIn("let count = 2", r.fixed)

    def test_syntax_repair(self):
        r = fix("let xs = [1, 2\nprint(xs)\nfn f(x) {\n    return x\n", "nyx")
        self.assertIsNone(nyx_parse_error(r.fixed), r.fixed)

    def test_arg_count(self):
        r = fix("fn area(w, h) => w * h\nprint(area(3))\n", "nyx")
        self.assertIn("arg-count", codes(r, fixed=False))


class Detection(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_language("def f():\n    return 1\n"), "python")
        self.assertEqual(detect_language("fn f() => 1\nlet x = f()\n"), "nyx")
        self.assertEqual(detect_language("anything", "a.nyx"), "nyx")


if __name__ == "__main__":
    unittest.main()
