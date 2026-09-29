"""How a report names a copper layer."""


def canonical_copper_name(ipc_layer_id) -> str:
    """KiCad's own name for an IPC copper layer id ("BL_In1_Cu" -> "In1.Cu"), or ""."""
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer
        name = BoardLayer.Name(int(ipc_layer_id))
    except Exception:
        return ""
    if not (name.startswith("BL_") and name.endswith("_Cu")):
        return ""
    return name[3:-3] + ".Cu"


def copper_layer_label(entry, layer_id) -> str:
    """A stackup copper layer as every report names it.

    The live stackup carries the designer's name for a layer, which KiCad
    reports through IPC ("F.Signal" for P02 Rev E's F.Cu), while the other
    analyses name the same layer by KiCad's own ("F.Cu"): the DC hotspots
    and the campaign disagreed on which layer they meant (COH-12). The
    designer's name is kept, KiCad's own after it in parentheses when they
    differ, as the HTML report already writes a remediation's layer.
    """
    entry = entry or {}
    name = str(entry.get("name", "") or "")
    canonical = str(entry.get("canonical_name", "") or "")
    if not name:
        return canonical or str(layer_id)
    if not canonical or canonical == name:
        return name
    return f"{name} ({canonical})"
