"""Lightweight S-expression parser for KiCad file formats.

All KiCad file formats (.kicad_sch, .kicad_pcb, .kicad_mbs) are S-expressions.
This module provides a minimal, dependency-free parser.
"""

from __future__ import annotations


def parse(text: str) -> list:
    """Parse S-expression text into nested Python lists.

    Atoms are strings. Lists are Python lists whose first element is the tag
    string. Quoted strings are unquoted. Returns the list of top-level
    expressions found in *text*.
    """
    tokens = _tokenize(text)
    results = []
    pos = 0
    while pos < len(tokens):
        node, pos = _read(tokens, pos)
        if node is not None:
            results.append(node)
    return results


def find(node: list, tag: str) -> list | None:
    """Return the first direct child list whose tag equals *tag*, or None."""
    if not isinstance(node, list):
        return None
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == tag:
            return child
    return None


def find_all(node: list, tag: str) -> list:
    """Return all direct child lists whose tag equals *tag*."""
    if not isinstance(node, list):
        return []
    return [
        child for child in node[1:]
        if isinstance(child, list) and child and child[0] == tag
    ]


def get_str(node: list, tag: str, default: str = "") -> str:
    """Return the first atom value of a direct child with *tag*, or *default*."""
    child = find(node, tag)
    if child is None or len(child) < 2:
        return default
    val = child[1]
    if isinstance(val, str):
        return val
    return default


# ---------------------------------------------------------------------------
# Internal tokenizer
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # Skip line comments
        if c == ";":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Skip whitespace
        if c in " \t\r\n":
            i += 1
            continue
        if c == "(":
            tokens.append("(")
            i += 1
        elif c == ")":
            tokens.append(")")
            i += 1
        elif c == '"':
            # Quoted string — collect until closing unescaped quote
            i += 1
            buf: list[str] = []
            while i < n:
                ch = text[i]
                if ch == "\\":
                    i += 1
                    if i < n:
                        next_ch = text[i]
                        if next_ch == "n":
                            buf.append("\n")
                        elif next_ch == "t":
                            buf.append("\t")
                        elif next_ch == "\\":
                            buf.append("\\")
                        elif next_ch == '"':
                            buf.append('"')
                        else:
                            buf.append("\\")
                            buf.append(next_ch)
                        i += 1
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            tokens.append("".join(buf))
        else:
            # Unquoted atom — read until whitespace or paren
            start = i
            while i < n and text[i] not in " \t\r\n()\"":
                i += 1
            tokens.append(text[start:i])
    return tokens


def _read(tokens: list[str], pos: int):
    """Recursively read one S-expression starting at *pos*.
    Returns (node, new_pos). node is a str atom or a list."""
    if pos >= len(tokens):
        return None, pos
    tok = tokens[pos]
    if tok == "(":
        pos += 1
        node: list = []
        while pos < len(tokens) and tokens[pos] != ")":
            child, pos = _read(tokens, pos)
            if child is not None:
                node.append(child)
        pos += 1  # consume ")"
        return node, pos
    if tok == ")":
        return None, pos + 1
    return tok, pos + 1
