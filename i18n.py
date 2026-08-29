"""Runtime internationalisation for Ki-PIDA.

English source strings are the canonical fallback.  Translation selection is
kept independent from the process locale so numeric parsing and project file
formats always retain their stable dot-decimal representation.
"""

import ctypes
import gettext
from functools import lru_cache
import locale
import os
from pathlib import Path
import threading
import re


DOMAIN = "kipida"
SYSTEM_LANGUAGE = "SYSTEM"
SUPPORTED_LANGUAGES = {"en": "English", "fr": "Français"}
_lock = threading.RLock()
_translation = gettext.NullTranslations()
_active_language = "en"
_requested_language = SYSTEM_LANGUAGE
_wx_hooks_installed = False
_template_translations = []
_BRACE_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def normalize_language(value):
    """Return a supported two-letter language code, or an empty string."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split(":", 1)[0].split(".", 1)[0].replace("-", "_")
    code = text.split("_", 1)[0].lower()
    return code if code in SUPPORTED_LANGUAGES else ""


def detect_system_language():
    """Detect the UI language without calling locale.setlocale()."""
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                code = normalize_language(buffer.value)
                if code:
                    return code
        except (AttributeError, OSError):
            pass
    for key in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        code = normalize_language(os.environ.get(key))
        if code:
            return code
    try:
        code = normalize_language(locale.getlocale()[0])
        if code:
            return code
    except (ValueError, TypeError):
        pass
    return "en"


def locale_directory(root=None):
    return Path(root) if root else Path(__file__).resolve().parent / "locales"


def configure(language=SYSTEM_LANGUAGE, root=None):
    """Activate a catalog and return the effective language code."""
    global _translation, _active_language, _requested_language, _template_translations
    requested = str(language or SYSTEM_LANGUAGE).strip()
    code = detect_system_language() if requested.upper() == SYSTEM_LANGUAGE else normalize_language(requested)
    code = code or "en"
    with _lock:
        _requested_language = SYSTEM_LANGUAGE if requested.upper() == SYSTEM_LANGUAGE else code
        if code == "en":
            _translation = gettext.NullTranslations()
        else:
            _translation = gettext.translation(
                DOMAIN, localedir=str(locale_directory(root)), languages=[code], fallback=True,
            )
        _active_language = code
        _template_translations = _build_template_translations(_translation)
        gettext_text.cache_clear()
    return code


def _build_template_translations(translation):
    patterns = []
    catalog = getattr(translation, "_catalog", {}) or {}
    for message, translated in catalog.items():
        if not isinstance(message, str) or not isinstance(translated, str) or message == translated:
            continue
        fields = _BRACE_FIELD.findall(message)
        if not fields or len(fields) != len(set(fields)):
            continue
        cursor = 0
        parts = []
        for match in _BRACE_FIELD.finditer(message):
            parts.append(re.escape(message[cursor:match.start()]))
            parts.append(f"(?P<{match.group(1)}>.+?)")
            cursor = match.end()
        parts.append(re.escape(message[cursor:]))
        patterns.append((re.compile("^" + "".join(parts) + "$", re.DOTALL), translated))
    return patterns


@lru_cache(maxsize=4096)
def gettext_text(message):
    message = str(message)
    # GNU gettext reserves the empty msgid for catalog metadata.  wxPython
    # commonly creates placeholder controls with label=""; asking gettext to
    # translate those labels would display the PO header throughout the UI.
    if not message:
        return ""
    with _lock:
        translated = _translation.gettext(message)
        if translated != message:
            return translated
        for pattern, template in _template_translations:
            match = pattern.match(message)
            if match:
                try:
                    return template.format(**match.groupdict())
                except (KeyError, ValueError):
                    continue
        return message


def ngettext(singular, plural, count):
    with _lock:
        return _translation.ngettext(singular, plural, count)


def current_language():
    return _active_language


def requested_language():
    return _requested_language


def available_languages():
    return dict(SUPPORTED_LANGUAGES)


_ = gettext_text


def install_wx_translation_hooks(wx):
    """Translate common wx display boundaries while preserving canonical data.

    Choices are intentionally not rewritten because many panels use their
    selected strings as stable solver enum values.  Those controls use an
    explicit label/value mapping when localisation is required.
    """
    global _wx_hooks_installed
    if _wx_hooks_installed:
        return

    def localized_label_class(base, title_keyword="label"):
        class Localized(base):
            def __init__(self, *args, **kwargs):
                values = list(args)
                if title_keyword in kwargs and isinstance(kwargs[title_keyword], str):
                    kwargs[title_keyword] = _(kwargs[title_keyword])
                positional_index = 2
                if len(values) > positional_index and isinstance(values[positional_index], str):
                    values[positional_index] = _(values[positional_index])
                super().__init__(*values, **kwargs)

            def SetLabel(self, label):
                return super().SetLabel(_(label) if isinstance(label, str) else label)

        Localized.__name__ = f"Localized{base.__name__}"
        return Localized

    wx.StaticText = localized_label_class(wx.StaticText)
    wx.Button = localized_label_class(wx.Button)
    wx.CheckBox = localized_label_class(wx.CheckBox)
    wx.StaticBox = localized_label_class(wx.StaticBox)
    wx.Dialog = localized_label_class(wx.Dialog, title_keyword="title")

    original_message_dialog = wx.MessageDialog

    class LocalizedMessageDialog(original_message_dialog):
        def __init__(self, *args, **kwargs):
            values = list(args)
            if len(values) > 1 and isinstance(values[1], str):
                values[1] = _(values[1])
            if len(values) > 2 and isinstance(values[2], str):
                values[2] = _(values[2])
            if "message" in kwargs and isinstance(kwargs["message"], str):
                kwargs["message"] = _(kwargs["message"])
            if "caption" in kwargs and isinstance(kwargs["caption"], str):
                kwargs["caption"] = _(kwargs["caption"])
            super().__init__(*values, **kwargs)

    wx.MessageDialog = LocalizedMessageDialog

    original_static_box_sizer = wx.StaticBoxSizer

    class LocalizedStaticBoxSizer(original_static_box_sizer):
        def __init__(self, *args, **kwargs):
            values = list(args)
            if len(values) >= 3 and isinstance(values[2], str):
                values[2] = _(values[2])
            if "label" in kwargs and isinstance(kwargs["label"], str):
                kwargs["label"] = _(kwargs["label"])
            super().__init__(*values, **kwargs)

    wx.StaticBoxSizer = LocalizedStaticBoxSizer

    original_message_box = wx.MessageBox

    def localized_message_box(message, caption="Message", *args, **kwargs):
        return original_message_box(_(message), _(caption), *args, **kwargs)

    wx.MessageBox = localized_message_box

    original_list_ctrl = wx.ListCtrl

    class LocalizedListCtrl(original_list_ctrl):
        def InsertColumn(self, col, heading, *args, **kwargs):
            return super().InsertColumn(col, _(heading), *args, **kwargs)

    wx.ListCtrl = LocalizedListCtrl

    original_notebook = wx.Notebook

    class LocalizedNotebook(original_notebook):
        def AddPage(self, page, text, *args, **kwargs):
            return super().AddPage(page, _(text), *args, **kwargs)

        def InsertPage(self, index, page, text, *args, **kwargs):
            return super().InsertPage(index, page, _(text), *args, **kwargs)

        def SetPageText(self, index, text):
            return super().SetPageText(index, _(text))

    wx.Notebook = LocalizedNotebook
    _wx_hooks_installed = True
