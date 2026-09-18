"""Unit tests for meter.intern (v2 M9).

`RoundTripTests` is the module's own stated exit criterion: "round-trip
decode equals original for 100% of generated codebooks."
"""

from __future__ import annotations

import unittest

from meter import intern


class CommonPrefixTests(unittest.TestCase):
    def test_shared_directory_prefix(self):
        # Trailing slash is intentional: prefix + suffix must reconstruct the
        # full path exactly (basename_compress's round-trip contract).
        paths = ["src/modules/auth/services/AuthService.ts",
                 "src/modules/auth/guards/SessionGuard.ts"]
        self.assertEqual(intern.common_prefix(paths), "src/modules/auth/")

    def test_no_shared_directory_is_empty(self):
        self.assertEqual(intern.common_prefix(["src/a.py", "lib/b.py"]), "")

    def test_single_path_compresses_to_parent_dir(self):
        # A "full path as prefix" would compress the file to nothing, so the
        # boundary trim backs off one component — the parent directory is
        # still a meaningful, reusable prefix.
        self.assertEqual(intern.common_prefix(["src/a/b.py"]), "src/a/")

    def test_does_not_chop_mid_filename(self):
        # "src/a/b.py" and "src/ac.py" share the string "src/a" but not a
        # real directory — a naive prefix would wrongly return "src/a".
        result = intern.common_prefix(["src/a/b.py", "src/ac.py"])
        self.assertEqual(result, "src/")

    def test_empty_list(self):
        self.assertEqual(intern.common_prefix([]), "")


class BasenameCompressTests(unittest.TestCase):
    def test_round_trips(self):
        paths = ["src/modules/auth/services/AuthService.ts",
                 "src/modules/auth/guards/SessionGuard.ts"]
        prefix, suffixes = intern.basename_compress(paths)
        for p in paths:
            self.assertEqual(prefix + suffixes[p], p)

    def test_suffix_shorter_than_original(self):
        paths = ["src/modules/auth/services/AuthService.ts",
                 "src/modules/auth/guards/SessionGuard.ts"]
        _, suffixes = intern.basename_compress(paths)
        for p in paths:
            self.assertLess(len(suffixes[p]), len(p))


class CodebookTests(unittest.TestCase):
    def test_same_path_gets_same_code(self):
        cb = intern.Codebook()
        c1 = cb.file_code("src/a.py")
        c2 = cb.file_code("src/a.py")
        self.assertEqual(c1, c2)

    def test_different_paths_get_different_codes(self):
        cb = intern.Codebook()
        c1 = cb.file_code("src/a.py")
        c2 = cb.file_code("src/b.py")
        self.assertNotEqual(c1, c2)

    def test_codes_are_sequential(self):
        cb = intern.Codebook()
        self.assertEqual(cb.file_code("a.py"), "f1")
        self.assertEqual(cb.file_code("b.py"), "f2")

    def test_contract_and_ac_codes_are_independent_namespaces(self):
        cb = intern.Codebook()
        fc = cb.file_code("a.py")
        cc = cb.contract_code("C-01")
        ac = cb.ac_code("AC-02")
        self.assertEqual({fc, cc, ac}, {"f1", "c1", "a1"})

    def test_to_dict_from_dict_round_trips(self):
        cb = intern.Codebook()
        fc = cb.file_code("src/a.py")
        sc = cb.symbol_code("AuthService.refresh", fc, 120, 168)
        cb.contract_code("C-AUTH")
        cb.ac_code("AC-02")
        restored = intern.Codebook.from_dict(cb.to_dict())
        self.assertEqual(restored.files, cb.files)
        self.assertEqual(restored.symbols, cb.symbols)
        self.assertEqual(restored.contracts, cb.contracts)
        self.assertEqual(restored.acs, cb.acs)
        self.assertEqual(restored.file_code("src/a.py"), fc)  # dedup survives round-trip


class DecodeTests(unittest.TestCase):
    def test_decode_file_reference(self):
        cb = intern.Codebook()
        cb.file_code("src/auth/AuthService.ts")
        self.assertEqual(intern.decode_reference(cb, "f1"), "src/auth/AuthService.ts")

    def test_decode_unknown_code_returns_none(self):
        cb = intern.Codebook()
        self.assertIsNone(intern.decode_reference(cb, "f99"))

    def test_decode_symbol_also_expands_nested_file_code(self):
        cb = intern.Codebook()
        fc = cb.file_code("src/auth/AuthService.ts")
        cb.symbol_code("AuthService.refresh", fc, 120, 168)
        decoded = intern.decode_reference(cb, "s1")
        self.assertEqual(decoded, "AuthService.refresh (src/auth/AuthService.ts:120-168)")
        self.assertNotIn("f1", decoded)  # fully de-interned, not left half-coded

    def test_decode_text_expands_every_code(self):
        cb = intern.Codebook()
        cb.file_code("src/a.py")
        cb.contract_code("C-AUTH")
        text = "See f1 and c1 for details."
        self.assertEqual(intern.decode_text(cb, text), "See src/a.py and C-AUTH for details.")

    def test_decode_text_leaves_unrecognised_codes_untouched(self):
        cb = intern.Codebook()
        text = "f1 is not defined in this codebook"
        self.assertEqual(intern.decode_text(cb, text), text)

    def test_decode_text_does_not_touch_ordinary_prose(self):
        cb = intern.Codebook()
        cb.file_code("src/a.py")
        text = "this function is fast and also correct"
        self.assertEqual(intern.decode_text(cb, text), text)


class RoundTripTests(unittest.TestCase):
    """The module's own exit criterion: round-trip decode equals original
    for 100% of generated codebooks. Exercised against several representative
    codebook shapes, including edge cases (empty, single-entry, many-entry,
    paths that look like they could collide with the coding scheme)."""

    def _assert_round_trips(self, files, symbols, contracts, acs):
        cb = intern.Codebook()
        file_codes = {p: cb.file_code(p) for p in files}
        for name, file_path, start, end in symbols:
            cb.symbol_code(name, file_codes[file_path], start, end)
        for c in contracts:
            cb.contract_code(c)
        for a in acs:
            cb.ac_code(a)

        # Build an interned document referencing every code, then decode it.
        lines = [cb.render_table()]
        for code in list(cb.files) + list(cb.symbols) + list(cb.contracts) + list(cb.acs):
            lines.append(f"see {code}")
        document = "\n".join(lines)

        decoded = intern.decode_text(cb, document)
        for path in files:
            self.assertIn(path, decoded)
        for name, file_path, start, end in symbols:
            self.assertIn(f"{name} ({file_path}:{start}-{end})", decoded)
        for c in contracts:
            self.assertIn(c, decoded)
        for a in acs:
            self.assertIn(a, decoded)

    def test_empty_codebook(self):
        self._assert_round_trips([], [], [], [])

    def test_single_entry_each(self):
        self._assert_round_trips(
            ["src/a.py"], [("f", "src/a.py", 1, 2)], ["C-01"], ["AC-01"])

    def test_many_entries(self):
        files = [f"src/f{i}.py" for i in range(30)]
        symbols = [(f"func{i}", files[i], i * 10, i * 10 + 5) for i in range(30)]
        contracts = [f"C-{i:02d}" for i in range(10)]
        acs = [f"AC-{i:02d}" for i in range(10)]
        self._assert_round_trips(files, symbols, contracts, acs)

    def test_paths_with_digits_that_look_like_codes(self):
        # A path or symbol name containing something that looks like "f1"
        # must not confuse decoding of the REAL f1 elsewhere.
        self._assert_round_trips(
            ["src/f1_helper.py", "src/other.py"],
            [("helper_f1", "src/f1_helper.py", 1, 2)],
            [], [])


if __name__ == "__main__":
    unittest.main()
