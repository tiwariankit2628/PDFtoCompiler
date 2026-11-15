"""Map Symbol-font private-use characters in PDF text back to real Unicode.

Equation editors (MathType, Word's equation tools) set Greek letters, operators
and tall brackets in the *Symbol* font. Many PDFs carry no Unicode mapping for
it, so text extraction yields private-use code points U+F020–U+F0FF: ``×``,
``π``, ``θ``, ``μ``, ``≤`` and brackets silently vanish or show as empty boxes.

The low byte of such a code point is the glyph's position in Adobe's Symbol
encoding, which is a fixed, published table. This module only translates that
table; it never guesses. Callers must pass the set of characters that were
actually set in a Symbol font on the page, because other fonts (Wingdings,
icon fonts) reuse the same private-use range for unrelated glyphs.
"""

# Adobe Symbol encoding (low byte -> Unicode). Tall bracket/brace/parenthesis
# *pieces* collapse to one ordinary delimiter: the top piece becomes the
# delimiter and the extension/bottom pieces are dropped.
_SYMBOL = {
    0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅",
    0x41: "Α", 0x42: "Β", 0x43: "Χ", 0x44: "Δ", 0x45: "Ε", 0x46: "Φ", 0x47: "Γ",
    0x48: "Η", 0x49: "Ι", 0x4A: "ϑ", 0x4B: "Κ", 0x4C: "Λ", 0x4D: "Μ", 0x4E: "Ν",
    0x4F: "Ο", 0x50: "Π", 0x51: "Θ", 0x52: "Ρ", 0x53: "Σ", 0x54: "Τ", 0x55: "Υ",
    0x56: "ς", 0x57: "Ω", 0x58: "Ξ", 0x59: "Ψ", 0x5A: "Ζ", 0x5C: "∴", 0x5E: "⊥",
    0x61: "α", 0x62: "β", 0x63: "χ", 0x64: "δ", 0x65: "ε", 0x66: "φ", 0x67: "γ",
    0x68: "η", 0x69: "ι", 0x6A: "ϕ", 0x6B: "κ", 0x6C: "λ", 0x6D: "μ", 0x6E: "ν",
    0x6F: "ο", 0x70: "π", 0x71: "θ", 0x72: "ρ", 0x73: "σ", 0x74: "τ", 0x75: "υ",
    0x76: "ϖ", 0x77: "ω", 0x78: "ξ", 0x79: "ψ", 0x7A: "ζ", 0x7E: "∼",
    0xA1: "ϒ", 0xA2: "′", 0xA3: "≤", 0xA4: "⁄", 0xA5: "∞", 0xA6: "ƒ",
    0xAB: "↔", 0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓",
    0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂",
    0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xBC: "…",
    0xC0: "ℵ", 0xC1: "ℑ", 0xC2: "ℜ", 0xC3: "℘", 0xC4: "⊗", 0xC5: "⊕", 0xC6: "∅",
    0xC7: "∩", 0xC8: "∪", 0xC9: "⊃", 0xCA: "⊇", 0xCB: "⊄", 0xCC: "⊂", 0xCD: "⊆",
    0xCE: "∈", 0xCF: "∉", 0xD0: "∠", 0xD1: "∇", 0xD5: "∏", 0xD6: "√", 0xD7: "⋅",
    0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔", 0xDC: "⇐", 0xDD: "⇑", 0xDE: "⇒",
    0xDF: "⇓", 0xE0: "◊", 0xE1: "〈", 0xE5: "∑", 0xF1: "〉", 0xF2: "∫",
    # Tall delimiters built from stacked pieces.
    0xE6: "(", 0xE7: "", 0xE8: "", 0xF6: ")", 0xF7: "", 0xF8: "",
    0xE9: "[", 0xEA: "", 0xEB: "", 0xF9: "]", 0xFA: "", 0xFB: "",
    0xEC: "{", 0xED: "", 0xEE: "", 0xEF: "", 0xFC: "}", 0xFD: "", 0xFE: "",
    0xBD: "", 0xBE: "", 0xF3: "∫", 0xF4: "", 0xF5: "",
}


def is_symbol_font(name):
    return "symbol" in (name or "").lower()


def is_private_use(char):
    return 0xF020 <= ord(char) <= 0xF0FF


def symbol_translation(chars):
    """Build a ``str.translate`` table for the given Symbol-font characters."""
    table = {}
    for char in chars:
        code = ord(char)
        if not 0xF020 <= code <= 0xF0FF:
            continue
        low = code & 0xFF
        if low in _SYMBOL:
            table[code] = _SYMBOL[low]
        elif 0x20 <= low <= 0x7E:
            # Positions shared with ASCII (digits, brackets, punctuation).
            table[code] = chr(low)
    return table
