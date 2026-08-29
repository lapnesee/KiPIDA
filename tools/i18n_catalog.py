"""Extract, validate and compile Ki-PIDA gettext catalogs without runtime deps."""

import argparse
import ast
from collections import defaultdict
from datetime import datetime, timezone
import re
from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parent.parent
POT = ROOT / "locales" / "kipida.pot"
PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)(?:![^}:]+)?(?::[^}]+)?\}(?!\})")
SOURCE_FILES = (
    list((ROOT / "ui").glob("*.py")) +
    [path for path in ROOT.glob("*.py") if path.name != "i18n.py"]
)


def po_quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    return '"' + escaped.replace("\n", "\\n") + '"'


def human_literal(value, force=False):
    if not isinstance(value, str) or not value.strip() or not any(char.isalpha() for char in value):
        return False
    text = value.strip()
    if len(text) > 500:
        return False
    if re.fullmatch(r"[A-Z0-9_+./-]+", text) and " " not in text:
        return False
    if not force and re.fullmatch(r"[a-z_]+", text):
        return False
    if text.startswith(("KiPIDA-", "__", "http://", "https://")):
        return False
    if text.startswith("%") or (text.startswith(".") and " " not in text):
        return False
    if text.startswith("^") or ("|" in text and " " not in text):
        return False
    if "(?P<" in text or "[^" in text or "[_/]" in text:
        return False
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", text, re.I):
        return False
    literal_text = re.sub(r"\{[A-Za-z_][^}]*\}", "", text)
    if "{" in text and sum(char.isalpha() for char in literal_text) < 4:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.-]+\.(py|json|png|toml|txt|kicad_[a-z]+)", text):
        return False
    return True


class MessageVisitor(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.messages = defaultdict(set)

    def add(self, value, node, force=False):
        if human_literal(value, force=force):
            reference = f"{self.path.relative_to(ROOT).as_posix()}:{getattr(node, 'lineno', 1)}"
            self.messages[value].add(reference)

    def visit_Call(self, node):
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in {"_", "gettext_text"} and node.args:
            value = self._literal(node.args[0])
            if value is not None:
                self.add(value, node, force=True)
        elif name in {"log", "_log", "emit", "output", "ValueError", "RuntimeError"} and node.args:
            value = self._literal(node.args[0])
            if value is not None:
                self.add(value, node)
        elif name in {"SetLabel", "SetTitle", "AppendText", "set_title", "set_xlabel", "set_ylabel", "set_zlabel"} and node.args:
            value = self._literal(node.args[0])
            if value is not None:
                self.add(value, node)
        self.generic_visit(node)

    def visit_Expr(self, node):
        # Module/class/function docstrings are not user-interface messages.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        # Joined strings are extracted only through a known display/log/error
        # call in visit_Call. Walking every f-string also captures paths,
        # regular expressions and intermediate fragments.
        return

    @staticmethod
    def _joined_template(node):
        parts = []
        placeholder_index = 0
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{p" + str(placeholder_index) + "}")
                placeholder_index += 1
        template = "".join(parts)
        return template if placeholder_index else None

    def visit_Constant(self, node):
        # Existing wx panels contain many label tuples that flow through a
        # local ``label`` variable. Include human-facing literals from UI and
        # EMC rule files so the automatic display hooks have catalog entries.
        if self.path.parent.name == "ui" or self.path.name in {"emc_analyzer.py", "em_field_solver.py"}:
            if isinstance(node.value, str):
                self.add(node.value, node)

    @staticmethod
    def _literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return MessageVisitor._joined_template(node)
        return None


def extract_messages():
    combined = defaultdict(set)
    for path in SOURCE_FILES:
        if not path.is_file():
            continue
        visitor = MessageVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)))
        for message, references in visitor.messages.items():
            combined[message].update(references)
    return dict(combined)


def write_pot(messages, target=POT):
    target.parent.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M%z")
    lines = [
        "# Ki-PIDA translation template.",
        "#, fuzzy",
        "msgid \"\"",
        "msgstr \"\"",
        po_quote("Project-Id-Version: Ki-PIDA\n"),
        po_quote(f"POT-Creation-Date: {created}\n"),
        po_quote("MIME-Version: 1.0\n"),
        po_quote("Content-Type: text/plain; charset=UTF-8\n"),
        po_quote("Content-Transfer-Encoding: 8bit\n"),
        "",
    ]
    for message in sorted(messages, key=lambda item: item.casefold()):
        references = sorted(messages[message])
        lines.append("#: " + " ".join(references))
        if "{" in message and "}" in message:
            lines.append("#, python-brace-format")
        lines.append("msgid " + po_quote(message))
        lines.append('msgstr ""')
        lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def parse_po(path):
    entries = []
    current = None
    active = None
    fuzzy = False

    def finish():
        nonlocal current, active, fuzzy
        if current is not None and "msgid" in current:
            current["fuzzy"] = fuzzy
            entries.append(current)
        current, active, fuzzy = None, None, False

    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines() + [""]:
        line = raw_line.strip()
        if line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
            continue
        if line.startswith("msgid "):
            if current is not None and "msgid" in current:
                finish()
            current = {}
            active = "msgid"
            current[active] = ast.literal_eval(line[6:].strip())
        elif line.startswith("msgstr "):
            current = current or {}
            active = "msgstr"
            current[active] = ast.literal_eval(line[7:].strip())
        elif line.startswith('"') and current is not None and active:
            current[active] += ast.literal_eval(line)
        elif not line:
            finish()
    return entries


def validate_po(path, template=POT, require_complete=False):
    source = {entry["msgid"] for entry in parse_po(template) if entry.get("msgid")}
    translated = {entry["msgid"]: entry for entry in parse_po(path) if entry.get("msgid")}
    errors = []
    missing = sorted(source - set(translated))
    if missing:
        errors.append(f"{len(missing)} source message(s) missing from {path}")
    for message in sorted(source & set(translated)):
        entry = translated[message]
        value = entry.get("msgstr", "")
        if require_complete and (not value or entry.get("fuzzy")):
            errors.append(f"untranslated or fuzzy: {message!r}")
        if value and set(PLACEHOLDER.findall(message)) != set(PLACEHOLDER.findall(value)):
            errors.append(f"placeholder mismatch: {message!r}")
    return errors


def compile_mo(po_path, mo_path):
    entries = {}
    for entry in parse_po(po_path):
        message = entry.get("msgid", "")
        translation = entry.get("msgstr", "")
        if entry.get("fuzzy") or (message and not translation):
            continue
        entries[message] = translation
    keys = sorted(entries)
    original_data = b""
    translated_data = b""
    original_table = []
    translated_table = []
    count = len(keys)
    original_offset = 28 + 16 * count
    translated_offset = original_offset + sum(len(key.encode("utf-8")) + 1 for key in keys)
    for key in keys:
        encoded = key.encode("utf-8")
        original_table.append((len(encoded), original_offset + len(original_data)))
        original_data += encoded + b"\0"
    for key in keys:
        encoded = entries[key].encode("utf-8")
        translated_table.append((len(encoded), translated_offset + len(translated_data)))
        translated_data += encoded + b"\0"
    output = [struct.pack("<7I", 0x950412DE, 0, count, 28, 28 + 8 * count, 0, 0)]
    output.extend(struct.pack("<2I", *item) for item in original_table)
    output.extend(struct.pack("<2I", *item) for item in translated_table)
    output.extend((original_data, translated_data))
    target = Path(mo_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"".join(output))
    return target


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("extract")
    validate = subparsers.add_parser("validate")
    validate.add_argument("po")
    validate.add_argument("--complete", action="store_true")
    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("po")
    compile_parser.add_argument("mo")
    args = parser.parse_args()
    if args.command == "extract":
        messages = extract_messages()
        path = write_pot(messages)
        print(f"Wrote {len(messages)} messages to {path}")
    elif args.command == "validate":
        errors = validate_po(args.po, require_complete=args.complete)
        if errors:
            raise SystemExit("\n".join(errors))
        print(f"Catalog is valid: {args.po}")
    else:
        print(compile_mo(args.po, args.mo))


if __name__ == "__main__":
    main()
