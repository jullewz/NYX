"""NYX test suite.  Run:  python -m unittest discover -s tests -v"""

import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nyx import Interpreter  # noqa: E402
from nyx.errors import NyxError, NyxSyntaxError  # noqa: E402


def run(code):
    out = io.StringIO()
    Interpreter(out=out).run(code, "<test>")
    return out.getvalue().rstrip("\n")


def value(code):
    return Interpreter(out=io.StringIO()).run(code, "<test>")


class Basics(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(value("1 + 2 * 3"), 7)
        self.assertEqual(value("(1 + 2) * 3"), 9)
        self.assertEqual(value("2 ** 3 ** 2"), 512)
        self.assertEqual(value("-2 ** 2"), -4)
        self.assertEqual(value("7 // 2"), 3)
        self.assertEqual(value("7 % 3"), 1)
        self.assertEqual(value("10 / 4"), 2.5)

    def test_strings(self):
        self.assertEqual(run('print("a" + "b", "x" * 3)'), "ab xxx")
        self.assertEqual(run('let n = 5\nprint("n is {n}, doubled {n * 2}")'), "n is 5, doubled 10")
        self.assertEqual(run('print("braces {{ok}}")'), "braces {ok}")
        self.assertEqual(run('print("nested {"inner {1 + 1}"}")'), "nested inner 2")
        self.assertEqual(value('"hello"[1]'), "e")
        self.assertEqual(value('"hello"[1..3]'), "el")
        self.assertEqual(value('"hello"[-1]'), "o")

    def test_comparisons_and_logic(self):
        self.assertIs(value("1 < 2 and 2 < 3"), True)
        self.assertIs(value("not 1 == 2"), True)
        self.assertEqual(value("nil or 5"), 5)
        self.assertEqual(value("0 or \"x\""), "x")
        self.assertEqual(value("nil ?? 3"), 3)
        self.assertEqual(value("0 ?? 3"), 0)
        self.assertIs(value("2 in [1, 2, 3]"), True)
        self.assertIs(value('"z" not in "abc"'), True)
        self.assertIs(value("true == 1"), False)

    def test_ternary(self):
        self.assertEqual(value('5 > 3 ? "yes" : "no"'), "yes")

    def test_variables_and_const(self):
        self.assertEqual(run("let x = 1\nx += 4\nx *= 2\nprint(x)"), "10")
        with self.assertRaises(NyxError) as cm:
            run("const y = 1\ny = 2")
        self.assertIn("constant", cm.exception.message)

    def test_newline_rules(self):
        self.assertEqual(run("let a = 1\n-1\nprint(a)"), "1")
        self.assertEqual(value("let xs = [3, 1, 2]\nxs\n  .sort()\n  .reverse()"), [3, 2, 1])


class Collections(unittest.TestCase):
    def test_lists(self):
        self.assertEqual(value("[1, 2, 3].map(fn(x) => x * 2)"), [2, 4, 6])
        self.assertEqual(value("[1, 2, 3, 4].filter(fn(x) => x % 2 == 0)"), [2, 4])
        self.assertEqual(value("[1, 2, 3].reduce(fn(a, b) => a + b)"), 6)
        self.assertEqual(value("[3, 1, 2].sort()"), [1, 2, 3])
        self.assertEqual(value('["bb", "a", "ccc"].sort(fn(s) => s.len())'), ["a", "bb", "ccc"])
        self.assertEqual(value("[1, [2, 3], 4].flat()"), [1, 2, 3, 4])
        self.assertEqual(value("let xs = [1]\nxs.push(2, 3)\nxs"), [1, 2, 3])
        self.assertEqual(value("[1, 2, 3, 4, 5][1..3]"), [2, 3])
        self.assertEqual(value("[1, 2, 3, 4, 5][1..=3]"), [2, 3, 4])
        self.assertEqual(value("[...[1, 2], 3, ...[4]]"), [1, 2, 3, 4])

    def test_comprehensions(self):
        self.assertEqual(value("[x * x for x in 1..=5 if x % 2 == 1]"), [1, 9, 25])
        self.assertEqual(value('[i for i, c in "abc" if c != "b"]'), [0, 2])

    def test_maps(self):
        self.assertEqual(value('let m = {a: 1, "b": 2}\nm.a + m["b"]'), 3)
        self.assertEqual(value("let m = {a: 1}\nm.c = 5\nm.keys()"), ["a", "c"])
        self.assertEqual(value('{a: 1}.get("zz", 9)'), 9)
        self.assertEqual(value("let k = \"x\"\n{[k]: 1}"), {"x": 1})
        self.assertEqual(value("let name = \"n\"\nlet m = {name}\nm"), {"name": "n"})
        self.assertEqual(run("for k, v in {a: 1, b: 2} { print(k, v) }"), "a 1\nb 2")

    def test_ranges(self):
        self.assertEqual(value("list(0..4)"), [0, 1, 2, 3])
        self.assertEqual(value("list(1..=3)"), [1, 2, 3])
        self.assertEqual(value("(1..=10).sum()"), 55)
        self.assertEqual(value("list(3..0)"), [3, 2, 1])

    def test_destructuring(self):
        self.assertEqual(run("let [a, b, ...r] = [1, 2, 3, 4]\nprint(a, b, r)"), "1 2 [3, 4]")
        self.assertEqual(run("let {x, y} = {x: 1, y: 2, z: 3}\nprint(x + y)"), "3")


class Functions(unittest.TestCase):
    def test_recursion(self):
        self.assertEqual(value("fn f(n) { if n < 2 { return n }\nreturn f(n-1) + f(n-2) }\nf(15)"), 610)

    def test_defaults_and_rest(self):
        self.assertEqual(value("fn f(a, b = a * 2) => a + b\nf(3)"), 9)
        self.assertEqual(value("fn f(first, ...rest) => rest\nf(1, 2, 3)"), [2, 3])
        self.assertEqual(value("fn f(...xs) => xs.len()\nf(...[1, 2, 3])"), 3)

    def test_closures(self):
        code = "fn counter() { let n = 0\nreturn fn() { n += 1\nreturn n } }\nlet c = counter()\nc()\nc()\nc()"
        self.assertEqual(value(code), 3)

    def test_closures_in_loops_capture_each_value(self):
        self.assertEqual(value("let fs = []\nfor i in 0..3 { fs.push(fn() => i) }\nfs.map(fn(f) => f())"), [0, 1, 2])

    def test_pipes(self):
        self.assertEqual(value("fn double(x) => x * 2\n5 |> double"), 10)
        self.assertEqual(value("[1, 2, 3] |> len()"), 3)
        self.assertEqual(value("fn add(a, b) => a + b\n1 |> add(10) |> add(100)"), 111)

    def test_arity_errors(self):
        with self.assertRaises(NyxError) as cm:
            run("fn f(a, b) => a\nf(1)")
        self.assertIn("missing the argument 'b'", cm.exception.message)
        with self.assertRaises(NyxError):
            run("fn f(a) => a\nf(1, 2)")


class Classes(unittest.TestCase):
    def test_class_and_inheritance(self):
        code = """
class A {
    fn init(x) { self.x = x }
    fn hello() => "A{self.x}"
}
class B extends A {
    fn hello() => super.hello() + "B"
    static fn make() => B(7)
}
print(B.make().hello())
print(is_instance(B(1), A))
"""
        self.assertEqual(run(code), "A7B\ntrue")

    def test_to_str(self):
        self.assertEqual(run("class P { fn init(n) { self.n = n }\nfn to_str() => \"P<{self.n}>\" }\nprint(P(3))"), "P<3>")

    def test_iter_protocol(self):
        code = "class Bag { fn init() { self.items = [1, 2] }\nfn iter() => self.items }\nlet t = 0\nfor x in Bag() { t += x }\nprint(t)"
        self.assertEqual(run(code), "3")


class ControlFlow(unittest.TestCase):
    def test_loops(self):
        self.assertEqual(run("let i = 0\nwhile true { i += 1\nif i == 3 { break } }\nprint(i)"), "3")
        self.assertEqual(run("for i in 0..5 { if i % 2 == 0 { continue }\nprint(i) }"), "1\n3")

    def test_if_elif_else(self):
        code = 'fn g(n) { if n < 0 { return "neg" } elif n == 0 { return "zero" } else { return "pos" } }\nprint(g(-1), g(0), g(1))'
        self.assertEqual(run(code), "neg zero pos")
        self.assertEqual(run('if false { print(1) } else if true { print(2) }'), "2")

    def test_match(self):
        code = 'fn d(n) => match n { 0 => "zero", 1 | 2 => "small", 3..10 => "mid", x if x < 0 => "neg", _ => "big" }'
        self.assertEqual(run(code + "\nprint(d(0), d(2), d(5), d(-4), d(50))"), "zero small mid neg big")
        self.assertEqual(value('match "b" { "a" => 1, "b" => { let z = 40\nz + 2 }, _ => 0 }'), 42)
        with self.assertRaises(NyxError):
            run("match 5 { 1 => 1 }")

    def test_try_catch_finally(self):
        code = 'try { throw "bad" } catch e { print("got", e) } finally { print("done") }'
        self.assertEqual(run(code), "got bad\ndone")
        self.assertEqual(run('try { [1][5] } catch e { print(e.type) }'), "RuntimeError")

    def test_return_inside_loop(self):
        self.assertEqual(value("fn f() { for i in 0..10 { if i == 4 { return i } } }\nf()"), 4)


class Errors(unittest.TestCase):
    def test_did_you_mean(self):
        with self.assertRaises(NyxError) as cm:
            run("let counter = 1\nprint(countr)")
        self.assertEqual(cm.exception.hint, "did you mean 'counter'?")
        self.assertEqual(cm.exception.tok.line, 2)

    def test_syntax_errors(self):
        for bad in ["let = 5", "print(1", "fn f( {}", "break", "return 1", "x = = 2", "{a: }", "let x = [1, 2"]:
            with self.assertRaises(NyxSyntaxError, msg=bad):
                run(bad)

    def test_type_errors(self):
        for bad in ['1 - "a"', "nil.x", "5()", '{}["missing"]', "[1, 2] < 3", "1 / 0"]:
            with self.assertRaises(NyxError, msg=bad):
                run(bad)

    def test_traceback(self):
        with self.assertRaises(NyxError) as cm:
            run("fn a() => b()\nfn b() => 1 / 0\na()")
        self.assertEqual([name for name, _ in cm.exception.trace], ["b", "a"])

    def test_stack_overflow_is_reported(self):
        import threading
        result = {}

        def go():
            try:
                run("fn f(n) => f(n + 1)\nf(0)")
            except NyxError as e:
                result["msg"] = e.message
        threading.stack_size(256 * 1024 * 1024)
        sys.setrecursionlimit(200_000)
        t = threading.Thread(target=go)
        t.start()
        t.join()
        self.assertIn("stack overflow", result.get("msg", ""))


class Examples(unittest.TestCase):
    """Every example program must run without errors."""

    def test_examples_run(self):
        import threading
        folder = os.path.join(ROOT, "examples")
        cwd = os.getcwd()
        os.chdir(folder)
        try:
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".nyx"):
                    continue
                with self.subTest(example=name):
                    with open(name, encoding="utf-8") as f:
                        source = f.read()
                    errors = []

                    def go():
                        try:
                            Interpreter(out=io.StringIO(), argv=["2"]).run(source, name)
                        except Exception as e:  # noqa: BLE001
                            errors.append(e)
                    threading.stack_size(256 * 1024 * 1024)
                    t = threading.Thread(target=go)
                    t.start()
                    t.join()
                    self.assertEqual(errors, [])
        finally:
            if os.path.exists(os.path.join(folder, "demo-config.json")):
                os.remove(os.path.join(folder, "demo-config.json"))
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
