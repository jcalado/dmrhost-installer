"""Every catalog in locales/ translates exactly the text the wizard shows.

    python3 tests/test_i18n.py --missing pt-PT   prints what a catalog lacks, as JSON to fill in
"""
import ast
import json
import re
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hotspot  # noqa: E402

SECTION_TEXT = ("title", "label", "help", "warn", "find", "find_link_text")


def source_strings() -> set[str]:
    """The English text of every t()/tx() call in the Python and the page, and of the questions."""
    found = set()
    for name in ("hotspot.py", "wizard.py"):
        for node in ast.walk(ast.parse((ROOT / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) \
                    and (getattr(node.func, "id", None) == "t" or getattr(node.func, "attr", None) == "t"):
                found.add(node.args[0].value)
    for s in hotspot.SECTIONS:
        found.add(s["title"])
        for f in s["fields"]:
            found.update(f[k] for k in SECTION_TEXT if f.get(k))
            for _, name, desc in f.get("choices", []):
                found.update(x for x in (name, desc) if x)
    page = (ROOT / "ui.html").read_text(encoding="utf-8")
    found.update(json.loads(f'"{m}"') for m in re.findall(r'\btx?\("((?:[^"\\]|\\.)*)"', page))
    return found


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


class Catalogs(unittest.TestCase):
    def setUp(self):
        self.source = source_strings()

    def test_every_language_has_a_complete_catalog(self):
        for code in hotspot.LANGS:
            if code == "en":
                continue
            with self.subTest(code):
                cat = json.loads((hotspot.LOCALES / f"{code}.json").read_text(encoding="utf-8"))
                self.assertEqual(sorted(self.source - cat.keys()), [], "missing")
                self.assertEqual(sorted(cat.keys() - self.source), [], "no longer in the source")
                for en, tr in cat.items():
                    self.assertTrue(tr.strip(), en)
                    self.assertEqual(placeholders(tr), placeholders(en), en)

    def test_no_catalog_for_a_language_we_dont_offer(self):
        self.assertEqual({p.stem for p in hotspot.LOCALES.glob("*.json")}, set(hotspot.LANGS) - {"en"})


class Languages(unittest.TestCase):
    def test_pick_lang(self):
        self.assertEqual(hotspot.pick_lang("pt-PT,pt;q=0.9,en;q=0.8"), "pt-PT")
        self.assertEqual(hotspot.pick_lang("pt-BR"), "pt-PT")
        self.assertEqual(hotspot.pick_lang("nl-NL,de;q=0.7"), "de-DE")
        self.assertEqual(hotspot.pick_lang("en-GB,fr;q=0.5"), "en")
        self.assertEqual(hotspot.pick_lang("ja"), "en")
        self.assertEqual(hotspot.pick_lang(""), "en")

    def test_each_thread_speaks_its_own_language(self):
        seen = {}

        def run(code):
            hotspot.set_lang(code)
            seen[code] = hotspot.validate({"callsign": ""})[1]["callsign"]
        for code in ("pt-PT", "de-DE"):
            th = threading.Thread(target=run, args=(code,))
            th.start()
            th.join()
        self.assertEqual(hotspot.lang(), "en")
        self.assertEqual(seen["pt-PT"], hotspot.catalog("pt-PT")["Required."])
        self.assertEqual(seen["de-DE"], hotspot.catalog("de-DE")["Required."])

    def test_sections_are_translated_and_keep_their_values(self):
        def run():
            hotspot.set_lang("fr-FR")
            seen.extend(hotspot.sections())
        seen = []
        th = threading.Thread(target=run)
        th.start()
        th.join()
        hat = next(f for s in seen for f in s["fields"] if f["key"] == "hat")
        self.assertEqual([c[0] for c in hat["choices"]], ["duplex", "simplex"])
        self.assertEqual(hat["label"], hotspot.catalog("fr-FR")["Hat"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--missing"]:
        cat = json.loads((hotspot.LOCALES / f"{sys.argv[2]}.json").read_text(encoding="utf-8")) \
            if (hotspot.LOCALES / f"{sys.argv[2]}.json").is_file() else {}
        print(json.dumps({s: "" for s in sorted(source_strings() - cat.keys())}, ensure_ascii=False, indent=2))
    else:
        unittest.main()
