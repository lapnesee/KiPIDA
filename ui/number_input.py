"""Numbers typed in the dialogs, as a French or an English user types them."""


def parse_number(text):
    """The value of a numeric field: a decimal comma reads as a point, and
    the spaces that separate thousands are ignored. ValueError otherwise.

    The fields were read with float(), which refuses "0,1": 71 of them in
    ui/, while labels such as "Rendement (0,0-1,0)" invited the comma.
    """
    cleaned = "".join(str(text or "").split()).replace(" ", "").replace(" ", "")
    if cleaned.count(",") == 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    return float(cleaned)
