import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import sys

import i18n
from tools.i18n_catalog import compile_mo, parse_po, validate_po


class I18NTests(unittest.TestCase):
    def tearDown(self):
        i18n.configure("en")

    def test_language_codes_are_normalized_without_changing_numeric_locale(self):
        self.assertEqual(i18n.normalize_language("fr_FR.UTF-8"), "fr")
        self.assertEqual(i18n.normalize_language("fr-FR"), "fr")
        self.assertEqual(i18n.normalize_language("en_US"), "en")
        self.assertEqual(i18n.normalize_language("de_DE"), "")

    def test_posix_system_detection_uses_locale_environment(self):
        environment = {"LANGUAGE": "fr_FR:en", "LANG": "en_US.UTF-8"}
        with patch.dict(os.environ, environment, clear=True), patch.object(i18n.os, "name", "posix"):
            self.assertEqual(i18n.detect_system_language(), "fr")

    def test_unknown_requested_language_falls_back_to_english(self):
        self.assertEqual(i18n.configure("de_DE"), "en")
        self.assertEqual(i18n.gettext_text("Results"), "Results")

    def test_compiled_catalog_is_loaded_by_gettext(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            po = root / "fr.po"
            po.write_text(
                'msgid ""\nmsgstr ""\n'
                '"Language: fr\\n"\n'
                '"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
                'msgid "Results"\nmsgstr "Résultats"\n\n'
                'msgid "Risk score {score}/100"\nmsgstr "Score de risque {score}/100"\n',
                encoding="utf-8",
            )
            mo = root / "fr" / "LC_MESSAGES" / "kipida.mo"
            compile_mo(po, mo)
            self.assertEqual(i18n.configure("fr", root=root), "fr")
            self.assertEqual(i18n.gettext_text("Results"), "Résultats")
            self.assertEqual(i18n.gettext_text("Risk score 42/100"), "Score de risque 42/100")

    def test_empty_labels_never_render_catalog_metadata(self):
        self.assertEqual(i18n.configure("fr"), "fr")
        self.assertEqual(i18n.gettext_text(""), "")

    def test_thermal_probe_readout_is_translated(self):
        self.assertEqual(i18n.configure("fr"), "fr")
        template = (
            "Thermal probe  {temperature:.2f} C  |  X {x:.2f} mm, Y {y:.2f} mm, "
            "Z {z:.3f} mm  |  {layer}"
        )
        rendered = i18n.gettext_text(template).format(
            temperature=42.5, x=10.0, y=20.0, z=1.6, layer="F.Cu",
        )
        self.assertTrue(rendered.startswith("Sonde thermique"))

    def test_committed_french_catalog_is_complete_and_placeholder_safe(self):
        root = Path(__file__).resolve().parent.parent
        po = root / "locales" / "fr" / "LC_MESSAGES" / "kipida.po"
        if not po.exists():
            self.fail("French catalog is missing")
        errors = validate_po(po, root / "locales" / "kipida.pot", require_complete=True)
        self.assertEqual(errors, [])
        self.assertTrue((po.parent / "kipida.mo").is_file())

        untranslated = [
            entry["msgid"] for entry in parse_po(po)
            if entry.get("msgid") and entry.get("msgstr") == entry["msgid"]
        ]
        self.assertLessEqual(len(untranslated), 60)

    def test_wx_display_boundaries_use_the_selected_catalog(self):
        root = Path(__file__).resolve().parent.parent
        script = (
            "import wx; "
            "from i18n import configure,install_wx_translation_hooks; "
            "configure('fr'); install_wx_translation_hooks(wx); "
            "app=wx.App(False); d=wx.Dialog(None,title='Startup Error'); p=wx.Panel(d); "
            "t=wx.StaticText(p,label='Results'); b=wx.Button(p,label='Close'); e=wx.StaticText(p,label=''); "
            "assert t.GetLabel() != 'Results'; assert b.GetLabel() != 'Close'; "
            "assert e.GetLabel() == ''; "
            "d.Destroy()"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
