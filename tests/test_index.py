"""Unit tests for meter.index.symbols, meter.index.imports, meter.index.tokens."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meter.index import imports, symbols, tokens


class PythonSymbolsTests(unittest.TestCase):
    def test_function_and_class(self):
        text = (
            "def top_level():\n"
            "    return 1\n"
            "\n"
            "class Foo:\n"
            "    def method_a(self):\n"
            "        return 2\n"
            "\n"
            "    async def method_b(self):\n"
            "        return 3\n"
        )
        syms = symbols.index_file("src/a.py", text)
        names = {s["name"]: s for s in syms}
        self.assertIn("top_level", names)
        self.assertEqual(names["top_level"]["kind"], "function")
        self.assertIn("Foo", names)
        self.assertEqual(names["Foo"]["kind"], "class")
        self.assertIn("Foo.method_a", names)
        self.assertEqual(names["Foo.method_a"]["kind"], "method")
        self.assertIn("Foo.method_b", names)

    def test_start_end_lines_are_correct(self):
        text = "def f():\n    x = 1\n    return x\n\ndef g():\n    pass\n"
        syms = {s["name"]: s for s in symbols.index_file("a.py", text)}
        self.assertEqual(syms["f"]["start"], 1)
        self.assertEqual(syms["f"]["end"], 3)
        self.assertEqual(syms["g"]["start"], 5)

    def test_syntax_error_yields_no_symbols_not_an_exception(self):
        self.assertEqual(symbols.index_file("broken.py", "def f(:\n"), [])


class RegexSymbolsTests(unittest.TestCase):
    def test_js_function_and_class(self):
        text = (
            "export function doThing(x) {\n"
            "  return x + 1;\n"
            "}\n"
            "\n"
            "class Widget {\n"
            "  render() {\n"
            "    return null;\n"
            "  }\n"
            "}\n"
        )
        syms = symbols.index_file("src/a.ts", text)
        names = {s["name"] for s in syms}
        self.assertIn("doThing", names)
        self.assertIn("Widget", names)

    def test_go_function(self):
        text = "func DoThing(x int) int {\n\treturn x + 1\n}\n"
        syms = symbols.index_file("a.go", text)
        self.assertEqual(syms[0]["name"], "DoThing")
        self.assertEqual(syms[0]["kind"], "function")

    def test_ruby_class_and_def(self):
        text = "class Widget\n  def render\n    1\n  end\nend\n"
        syms = symbols.index_file("a.rb", text)
        names = {s["name"]: s for s in syms}
        self.assertIn("Widget", names)
        self.assertIn("render", names)
        self.assertEqual(names["Widget"]["end"], 5)

    def test_unknown_extension_yields_nothing(self):
        self.assertEqual(symbols.index_file("data.yaml", "key: value\n"), [])


class TokensTests(unittest.TestCase):
    def test_monotonic_in_length(self):
        self.assertLess(tokens.estimate_tokens("a" * 4), tokens.estimate_tokens("a" * 400))

    def test_minimum_one(self):
        self.assertEqual(tokens.estimate_tokens(""), 1)


class ImportsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write(self, rel_path: str, content: str) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_python_relative_import_resolves(self):
        self._write("src/services/auth.py", "class AuthService: pass\n")
        self._write("src/services/session.py", "x = 1\n")
        text = "from .auth import AuthService\n"
        neighbours = imports.neighbours_of(self.repo_root, "src/services/session.py", text)
        self.assertIn("src/services/auth.py", neighbours)

    def test_python_absolute_package_import_resolves(self):
        self._write("pkg/util.py", "def helper(): pass\n")
        self._write("pkg/__init__.py", "")
        self._write("main.py", "")
        text = "import pkg.util\n"
        neighbours = imports.neighbours_of(self.repo_root, "main.py", text)
        self.assertIn("pkg/util.py", neighbours)

    def test_external_package_import_does_not_resolve(self):
        self._write("main.py", "")
        text = "import numpy\n"
        neighbours = imports.neighbours_of(self.repo_root, "main.py", text)
        self.assertEqual(neighbours, [])

    def test_js_relative_import_resolves(self):
        self._write("src/a.ts", "")
        self._write("src/b.ts", "export const x = 1;\n")
        text = "import { x } from './b';\n"
        neighbours = imports.neighbours_of(self.repo_root, "src/a.ts", text)
        self.assertIn("src/b.ts", neighbours)

    def test_js_bare_specifier_does_not_resolve(self):
        self._write("src/a.ts", "")
        text = "import React from 'react';\n"
        neighbours = imports.neighbours_of(self.repo_root, "src/a.ts", text)
        self.assertEqual(neighbours, [])

    def test_unknown_extension_yields_nothing(self):
        self.assertEqual(imports.neighbours_of(self.repo_root, "a.rs", "use foo;"), [])


if __name__ == "__main__":
    unittest.main()
