import argparse
import ast
from pathlib import Path

import polib


TRANSLATION_FUNCTION_NAMES = {"_", "_translate", "_translatef", "N_"}
EXCLUDED_PATH_PARTS = {".git", ".venv", "build", "dist", "__pycache__"}
SOURCE_ROOTS = ("main.py", "core", "ui")


class TranslationExtractor(ast.NodeVisitor):
    def __init__(self):
        self.entries = []

    def visit_Call(self, node):
        function_name = self._get_function_name(node.func)
        if function_name in TRANSLATION_FUNCTION_NAMES and node.args:
            msgid = self._extract_string_literal(node.args[0])
            if msgid:
                self.entries.append((msgid, node.lineno))
        self.generic_visit(node)

    @staticmethod
    def _get_function_name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    @staticmethod
    def _extract_string_literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Maintain gettext catalogs for the project.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract strings to locales/base.pot")
    extract_parser.add_argument("--pot", default="locales/base.pot", help="Path to the POT file.")

    init_parser = subparsers.add_parser("init", help="Initialize a new language catalog from the POT file.")
    init_parser.add_argument("--lang", required=True, help="Language code to initialize, for example es or tr.")
    init_parser.add_argument("--pot", default="locales/base.pot", help="Path to the POT file.")

    update_parser = subparsers.add_parser("update", help="Merge the POT file into an existing language catalog.")
    update_parser.add_argument("--lang", required=True, help="Language code to update, for example fr or es.")
    update_parser.add_argument("--pot", default="locales/base.pot", help="Path to the POT file.")

    return parser.parse_args()


def get_project_root():
    return Path(__file__).resolve().parent.parent


def iter_source_files(project_root):
    for source_root in SOURCE_ROOTS:
        root_path = project_root / source_root
        if root_path.is_file():
            yield root_path
            continue

        if not root_path.exists():
            continue

        for path in sorted(root_path.rglob("*.py")):
            if any(part in EXCLUDED_PATH_PARTS for part in path.parts):
                continue
            yield path


def collect_translatable_strings(project_root):
    collected = {}

    for source_path in iter_source_files(project_root):
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(source_path))
        extractor = TranslationExtractor()
        extractor.visit(tree)

        relative_path = source_path.relative_to(project_root).as_posix()
        for msgid, line_number in extractor.entries:
            references = collected.setdefault(msgid, set())
            references.add(f"{relative_path}:{line_number}")

    return collected


def build_pot(project_root):
    collected_strings = collect_translatable_strings(project_root)
    catalog = polib.POFile()
    catalog.metadata = {
        "Project-Id-Version": "Accessible Media Converter",
        "MIME-Version": "1.0",
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Transfer-Encoding": "8bit",
    }

    for msgid in sorted(collected_strings):
        entry = polib.POEntry(msgid=msgid)
        entry.occurrences = []
        for reference in sorted(collected_strings[msgid]):
            path, line = reference.rsplit(":", 1)
            entry.occurrences.append((path, line))
        catalog.append(entry)

    return catalog


def get_locale_po_path(project_root, language_code):
    return project_root / "locales" / language_code / "LC_MESSAGES" / "base.po"


def ensure_pot_exists(project_root, pot_path):
    resolved_pot_path = (project_root / pot_path).resolve()
    if not resolved_pot_path.exists():
        raise SystemExit(f"POT file not found: {resolved_pot_path}")
    return resolved_pot_path


def save_pot(project_root, pot_path):
    resolved_pot_path = (project_root / pot_path).resolve()
    resolved_pot_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = build_pot(project_root)
    catalog.save(resolved_pot_path)
    print(f"Wrote {resolved_pot_path}")


def init_language(project_root, language_code, pot_path):
    resolved_pot_path = ensure_pot_exists(project_root, pot_path)
    po_path = get_locale_po_path(project_root, language_code)
    if po_path.exists():
        raise SystemExit(f"Catalog already exists: {po_path}")

    pot_catalog = polib.pofile(resolved_pot_path)
    po_catalog = polib.POFile()
    po_catalog.metadata = dict(pot_catalog.metadata)
    po_catalog.merge(pot_catalog)
    po_path.parent.mkdir(parents=True, exist_ok=True)
    po_catalog.save(po_path)
    print(f"Initialized {po_path}")


def update_language(project_root, language_code, pot_path):
    resolved_pot_path = ensure_pot_exists(project_root, pot_path)
    po_path = get_locale_po_path(project_root, language_code)
    if not po_path.exists():
        raise SystemExit(f"Catalog not found: {po_path}")

    pot_catalog = polib.pofile(resolved_pot_path)
    po_catalog = polib.pofile(po_path)
    po_catalog.metadata = dict(pot_catalog.metadata)
    po_catalog.merge(pot_catalog)
    po_catalog.save(po_path)
    print(f"Updated {po_path}")


def main():
    args = parse_args()
    project_root = get_project_root()

    if args.command == "extract":
        save_pot(project_root, args.pot)
        return
    if args.command == "init":
        init_language(project_root, args.lang, args.pot)
        return
    if args.command == "update":
        update_language(project_root, args.lang, args.pot)
        return

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
