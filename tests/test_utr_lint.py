import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "utr_lint.py"
spec = importlib.util.spec_from_file_location("utr_lint", SCRIPT)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)
CASES = json.loads((ROOT / "tests/meaning_cases.json").read_text(encoding="utf-8"))["cases"]
TEMPLATE = json.loads((ROOT / "assets/glossary.template.json").read_text(encoding="utf-8"))


def codes(findings):
    return {item["code"] for item in findings}


class MeaningTests(unittest.TestCase):
    def test_acceptable_rewrites(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                self.assertEqual(lint.compare_findings(case["before"], case["accepted"]), [])

    def test_observable_regressions(self):
        for case in CASES:
            if case["review"] == "automatic_signal":
                with self.subTest(case=case["id"]):
                    actual = codes(lint.compare_findings(case["before"], case["rejected"]))
                    self.assertTrue(set(case["expected_codes"]) <= actual, actual)

    def test_known_blind_spots_never_claim_equivalence(self):
        for case in CASES:
            if case["review"] == "semantic_review":
                with self.subTest(case=case["id"]):
                    result = lint.report(case["rejected"], before=case["before"])
                    # An empty report on these wrong rewrites documents a limitation.
                    self.assertEqual(result["findings"], [])
                    self.assertEqual(result["semantic_equivalence"], "not_assessed")
                    self.assertTrue(case["criterion"])

    def test_duplicate_literal_is_not_lost(self):
        self.assertIn("LITERAL_CHANGED", codes(lint.compare_findings("`x` и `x`", "`x`")))

    def test_renumbered_markdown_is_not_numeric_change(self):
        self.assertEqual(lint.compare_findings("1. Проверьте сервис.\n2. Откройте журнал.", "3. Проверьте сервис.\n4. Откройте журнал."), [])

    def test_code_block_is_protected_but_compared(self):
        before = "```sh\ncommand --count 5\n```\n"
        self.assertEqual(lint.style_findings(before), [])
        self.assertEqual(codes(lint.compare_findings(before, before.replace("5", "10"))), {"LITERAL_CHANGED"})

    def test_identical_input_never_creates_compare_advice(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                self.assertEqual(lint.compare_findings(case["before"], case["before"]), [])


class StyleTests(unittest.TestCase):
    def test_word_limit_is_advice_with_correct_line(self):
        text = "# Заголовок\n\n" + "слово " * 21 + "."
        result = lint.style_findings(text)
        self.assertEqual(codes(result), {"SENTENCE_LENGTH"})
        self.assertEqual(result[0]["line"], 3)
        self.assertEqual(result[0]["level"], "advice")
        self.assertEqual(lint.style_findings("слово " * 20 + "."), [])

    def test_paragraphs_and_lists(self):
        self.assertIn("PARAGRAPH_LENGTH", codes(lint.style_findings("Факт. " * 7)))
        self.assertNotIn("PARAGRAPH_LENGTH", codes(lint.style_findings("\n".join("- Факт." for _ in range(7)))))
        self.assertEqual(lint.style_findings("Факт. " * 6), [])

    def test_heading_does_not_hide_following_paragraph(self):
        result = lint.style_findings("# Заголовок\n" + "слово " * 21 + ".")
        self.assertEqual(codes(result), {"SENTENCE_LENGTH"})
        self.assertEqual(result[0]["line"], 2)

    def test_parentheses_count_separately(self):
        self.assertEqual(lint.style_findings("слово " * 12 + "(" + "пояснение " * 12 + ")."), [])
        self.assertIn("SENTENCE_LENGTH", codes(lint.style_findings("Проверьте (" + "пояснение " * 21 + ").")))

    def test_decimal_not_sentence_boundary(self):
        self.assertIn("SENTENCE_LENGTH", codes(lint.style_findings("слово " * 19 + "3.5 секунды.")))

    def test_physical_line_wrap_does_not_split_sentence(self):
        self.assertIn("SENTENCE_LENGTH", codes(lint.style_findings("слово " * 11 + "\n" + "слово " * 10 + ".")))

    def test_quotes_and_code_do_not_trigger_style(self):
        for text in ('«' + 'слово ' * 30 + '»', '`произвести проверку`', '~~~\n' + 'слово ' * 30 + '\n~~~', '```\n' + 'слово ' * 30):
            with self.subTest(text=text[:20]):
                self.assertEqual(lint.style_findings(text), [])

    def test_english_is_not_linted(self):
        self.assertEqual(lint.style_findings("word " * 30 + "."), [])

    def test_plain_copula_is_not_banned(self):
        self.assertEqual(lint.style_findings("Этот файл является конфигурацией сервиса."), [])


class GlossaryTests(unittest.TestCase):
    def test_template_and_proposed_status(self):
        entries = lint.validate_glossary(TEMPLATE)
        result = lint.style_findings("Проверьте бэкап.", entries)
        self.assertEqual(codes(result), {"GLOSSARY_VARIANT"})
        self.assertIn("рабочее предложение", result[0]["message"])

    def test_word_boundaries_and_literals(self):
        entries = lint.validate_glossary(TEMPLATE)
        self.assertEqual(lint.style_findings("бэкапирование `бэкап` «бэкап»", entries), [])
        self.assertEqual(len(lint.style_findings("БЭКАП бэкапа", entries)), 2)

    def test_yo_and_case_matching(self):
        entry = dict(term="учётная запись", definition="Пример.", variants=["учётка"], status="proposed", source="Пример.")
        self.assertIn("GLOSSARY_VARIANT", codes(lint.style_findings("УЧЕТКА", [entry])))

    def test_duplicate_and_invalid_schema_rejected(self):
        invalid = [[], {}, {"schema_version": True, "entries": []}]
        duplicate = copy.deepcopy(TEMPLATE)
        duplicate["entries"][0]["variants"].append("БЭКАП")
        invalid.append(duplicate)
        for field, value in (("status", "confirmed"), ("variants", "бэкап"), ("source", "")):
            bad = copy.deepcopy(TEMPLATE)
            bad["entries"][0][field] = value
            invalid.append(bad)
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    lint.validate_glossary(data)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, text="Проверьте журнал."):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=text, text=True, encoding="utf-8", capture_output=True)

    def test_stdin_json_and_default_success(self):
        result = self.run_cli("-", "--format", "json", text="произвести проверку")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["semantic_equivalence"], "not_assessed")
        self.assertFalse(report["comparison_performed"])
        self.assertIn("VERBOSE_PHRASE", codes(report["findings"]))

    def test_opt_in_exit_code(self):
        self.assertEqual(self.run_cli("-", "--fail-on", "advice", text="произвести проверку").returncode, 1)
        self.assertEqual(self.run_cli("-", "--fail-on", "advice").returncode, 0)

    def test_file_before_glossary_and_read_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            before, after = Path(folder) / "before.md", Path(folder) / "after.md"
            before.write_text("Не удаляйте бэкап.", encoding="utf-8")
            after.write_text("Удалите бэкап.", encoding="utf-8")
            result = self.run_cli(str(after), "--before", str(before), "--glossary", str(ROOT / "assets/glossary.template.json"), "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["comparison_performed"])
            self.assertTrue({"GLOSSARY_VARIANT", "NEGATION_CHANGED"} <= codes(report["findings"]))
            self.assertEqual(after.read_text(encoding="utf-8"), "Удалите бэкап.")

    def test_bad_input_exits_two_without_traceback(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            bad = Path(folder) / "bad.json"
            bad.write_text("{", encoding="utf-8")
            for args in ((str(Path(folder) / "missing"),), ("-", "--glossary", str(bad)), ("-", "--format", "xml")):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
