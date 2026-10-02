import logging
import math
import re
import time
from pathlib import Path
try:
    from shapely.geometry import LineString, Polygon, MultiPolygon, GeometryCollection, Point, box
    from shapely.ops import unary_union
    from shapely import affinity
except ImportError:
    LineString = Polygon = MultiPolygon = GeometryCollection = Point = box = unary_union = affinity = None

try:
    import kipy
    import kipy.board
    import os
    import tempfile
    import sys
    HAS_KIPY = True
except ImportError:
    HAS_KIPY = False

try:
    from .physical_constants import (
        COPPER_RESISTIVITY_OHM_MM, DEFAULT_BOARD_THICKNESS_MM, DEFAULT_COPPER_THICKNESS_MM,
    )
    from .ingest.board_reader import arc_polyline
    from .layer_labels import canonical_copper_name
except (ImportError, ValueError):
    from physical_constants import (
        COPPER_RESISTIVITY_OHM_MM, DEFAULT_BOARD_THICKNESS_MM, DEFAULT_COPPER_THICKNESS_MM,
    )
    from ingest.board_reader import arc_polyline
    from layer_labels import canonical_copper_name

# Constants mapping (approximate)
# Needs to be verified against kipy enums
# For now defining local helpers
def to_mm(val_nm):
    return val_nm / 1e6


def live_capture_has_no_copper(track_count, via_count, zone_count, pad_count):
    """True when a live geometry capture holds no copper and no net pads.

    KiCad's IPC answers every collection empty while the PCB editor is still
    synchronising (``thermal_model.ThermalModelBuilder.build`` refuses the
    same condition, added in 1bf2be7). A capture with zero tracks, zero vias,
    zero filled zones and zero net-connected pads is not "an empty board" --
    it is "no board was captured" -- and scoring or diffing it produces false
    findings. A campaign on P02 Rev E started while the editor was still
    synchronising (report campaign-...-213933, 2026-09-22 21:39) hit this:
    EMC read the empty geometry as GP-002 CRITICAL "No ground plane
    detected" (verdict FAIL, action #1) while DC-004 reported 17 rails
    "differs ... (100% of it)" against the saved file, both from a capture
    that simply had not finished arriving.
    """
    return track_count == 0 and via_count == 0 and zone_count == 0 and pad_count == 0


def file_layers_to_live(file_layers, live_copper, layer_order=None):
    """``{canonical name: live layer id}`` for a board file's copper layers.

    *file_layers* lists ``(ordinal, canonical name, designer name)`` top to
    bottom. Paired by canonical name, then by the designer's name, which is
    all the live stackup knows a renamed layer by, then by position when both
    stacks hold as many copper layers. A layer nothing pairs is left out
    rather than kept under its file ordinal, which in KiCad 10 can be another
    live layer's id: on P02 Rev E the file's In2.Cu is 6, the IPC id of In3.Cu,
    so the In2 VBUS_SYS pour was read as In3 copper and every saved pour looked
    edited. With no live stackup at all the file ordinals are all there is.
    """
    if not live_copper:
        return {name: identifier for identifier, name, _user in file_layers}
    by_name = {}
    for layer_id, properties in live_copper.items():
        name = str((properties or {}).get('name', '') or '')
        if name:
            by_name.setdefault(name, int(layer_id))
    paired = {}
    for _identifier, name, user_name in file_layers:
        live_id = by_name.get(name)
        if live_id is None and user_name:
            live_id = by_name.get(user_name)
        if live_id is not None and live_id not in paired.values():
            paired[name] = live_id
    order = [int(layer_id) for layer_id in (layer_order or live_copper.keys())]
    if len(paired) < len(file_layers) and len(order) == len(file_layers):
        used = set(paired.values())
        for (_identifier, name, _user), live_id in zip(file_layers, order):
            if name not in paired and live_id not in used:
                paired[name] = live_id
                used.add(live_id)
    return paired


class GeometryExtractor:
    def __init__(self, board, debug=False, log_callback=None, board_path=None):
        self.board = board
        self.board_path = str(board_path) if board_path else None
        self.debug = debug
        self.log_callback = log_callback
        self.logger = logging.getLogger('KiPIDA.GeometryExtractor')
        # Stackup data belongs to one live board. A class-level cache can leak
        # data between projects opened in the same KiCad process.
        self._stackup_cache = None
        self._net_item_caches = {}
        self._file_zone_geometry_cache = None
        if debug:
            self.logger.setLevel(logging.DEBUG)
            # Clear any existing handlers to avoid duplicates
            self.logger.handlers.clear()
            if log_callback:
                # Use custom handler that calls the UI log callback
                class UILogHandler(logging.Handler):
                    def __init__(self, callback):
                        super().__init__()
                        self.callback = callback
                    def emit(self, record):
                        msg = self.format(record)
                        self.callback(msg)
                
                handler = UILogHandler(log_callback)
                handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
                self.logger.addHandler(handler)
            else:
                # Fallback to stderr
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
                self.logger.addHandler(handler)
    def _get_val(self, obj, attr_name, default=None):
        """Robustly get attribute value from object (property or getter)."""
        if obj is None: return default
        if hasattr(obj, attr_name):
            val = getattr(obj, attr_name)
            if val is not None: return val
        for prefix in ["get_", ""]:
            method_name = prefix + attr_name
            if hasattr(obj, method_name):
                try:
                    val = getattr(obj, method_name)()
                    if val is not None: return val
                except Exception:
                    # A getter this kipy version lacks or refuses: the next
                    # spelling is tried, then the default.
                    pass
        return default

    def _get_board_items(self, attr_name):
        """Robustly get items from board (property or getter)."""
        method_name = f"get_{attr_name}"
        if hasattr(self.board, method_name):
            try:
                result = getattr(self.board, method_name)()
                if result is not None:
                    return result
            except Exception:
                # The attribute spelling below is tried next.
                pass
        if hasattr(self.board, attr_name):
            result = getattr(self.board, attr_name)
            # kipy 0.3 exposes board.footprints / board.zones as operation
            # facades.  Their get_all() method, not the facade itself, is the
            # iterable collection.
            get_all = getattr(result, "get_all", None)
            if callable(get_all):
                try:
                    return get_all()
                except Exception as exc:
                    if self.log_callback:
                        self.log_callback(f"Board {attr_name} could not be read ({exc}).")
            try:
                iter(result)
                return result
            except TypeError:
                pass
        return []

    def _item_matches_net(self, item, net_name):
        """Match kipy net objects with a small legacy numeric-net fallback."""
        net = self._get_val(item, 'net')
        item_name = self._get_val(net, 'name', '')
        if item_name:
            return item_name == net_name
        item_code = self._get_val(item, 'net_code', self._get_val(item, 'net_number', None))
        if item_code is None:
            return False
        finder = getattr(self.board, 'FindNet', None) or getattr(self.board, 'find_net', None)
        if not callable(finder):
            return False
        try:
            target = finder(net_name)
            target_code = self._get_val(target, 'code', self._get_val(target, 'number', None))
            if target_code is None and hasattr(target, 'GetNetCode'):
                target_code = target.GetNetCode()
            return int(item_code) == int(target_code)
        except Exception:
            return False

    def _get_items_for_net(self, attr_name, net_name):
        """Index live board objects once per run instead of rescanning for every rail."""
        cache = self._net_item_caches.get(attr_name)
        if cache is None:
            started = time.perf_counter()
            cache = {}
            if attr_name == "pads":
                items = []
                for footprint in self._get_board_items("footprints"):
                    pads = self._get_val(footprint, "pads")
                    if pads is None:
                        definition = self._get_val(footprint, "definition")
                        pads = self._get_val(definition, "pads", [])
                    items.extend(list(pads or []))
            else:
                items = list(self._get_board_items(attr_name) or [])
            unresolved = []
            for item in items:
                net = self._get_val(item, "net")
                name = self._get_val(net, "name", "")
                if name:
                    cache.setdefault(str(name), []).append(item)
                else:
                    unresolved.append(item)
            cache[None] = unresolved
            self._net_item_caches[attr_name] = cache
            if self.log_callback:
                self.log_callback(
                    f"[GEOMETRY] Indexed {len(items):,} {attr_name} in "
                    f"{time.perf_counter() - started:.3f} s."
                )
        matches = list(cache.get(net_name, []))
        matches.extend(
            item for item in cache.get(None, []) if self._item_matches_net(item, net_name)
        )
        return matches
    # Cache for stackup data
    _stackup_cache = None

    def invalidate_stackup_cache(self):
        """Force the next stackup request to query the live KiCad board."""
        self._stackup_cache = None

    @staticmethod
    def _edge_cuts_bounds_from_file(board_path):
        """Read common Edge.Cuts geometry when IPC omits drawing objects."""
        try:
            text = Path(board_path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return None
        points = []
        graphic_start = re.compile(r'\(gr_(?:rect|line|arc|poly|curve|circle)\b')
        coordinate = re.compile(
            r'\((?:start|end|mid|center|xy)\s+'
            r'([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)'
        )
        for match in graphic_start.finditer(text):
            depth = 0
            end = None
            for index in range(match.start(), len(text)):
                depth += (text[index] == '(') - (text[index] == ')')
                if depth == 0:
                    end = index + 1
                    break
            if end is None:
                continue
            graphic = text[match.start():end]
            if not re.search(r'\(layer\s+"Edge\.Cuts"\)', graphic):
                continue
            points.extend((float(x), float(y)) for x, y in coordinate.findall(graphic))
        if not points:
            return None
        return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)

    @staticmethod
    def _edge_cuts_outline_from_file(board_path):
        """Return the physical Edge.Cuts polygon when it can be read exactly.

        Thermal meshing needs the board *shape*, not merely its bounding box.
        In particular, a rounded ``gr_rect`` must not create thermal cells in
        its corner cut-outs.
        """
        if box is None:
            return None
        try:
            text = Path(board_path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return None
        shapes = []
        graphic_start = re.compile(r'\(gr_(?:rect|poly|circle)\b')
        coordinate = re.compile(
            r'\((?:start|end|center|xy)\s+'
            r'([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)'
        )
        for match in graphic_start.finditer(text):
            depth = 0
            end = None
            for index in range(match.start(), len(text)):
                depth += (text[index] == '(') - (text[index] == ')')
                if depth == 0:
                    end = index + 1
                    break
            if end is None:
                continue
            graphic = text[match.start():end]
            if not re.search(r'\(layer\s+"Edge\.Cuts"\)', graphic):
                continue
            points = [(float(x), float(y)) for x, y in coordinate.findall(graphic)]
            if graphic.startswith('(gr_rect') and len(points) >= 2:
                min_x, max_x = sorted((points[0][0], points[1][0]))
                min_y, max_y = sorted((points[0][1], points[1][1]))
                shape = box(min_x, min_y, max_x, max_y)
                radius_match = re.search(r'\(radius\s+([-+]?\d*\.?\d+)', graphic)
                radius = float(radius_match.group(1)) if radius_match else 0.0
                if radius > 0.0 and 2.0 * radius < min(max_x - min_x, max_y - min_y):
                    # Erode then restore the rectangle: this preserves its
                    # outer dimensions while forming the KiCad corner radius.
                    shape = shape.buffer(-radius).buffer(radius)
                shapes.append(shape)
            elif graphic.startswith('(gr_poly') and len(points) >= 3 and Polygon is not None:
                shape = Polygon(points)
                if shape.is_valid and not shape.is_empty:
                    shapes.append(shape)
            elif graphic.startswith('(gr_circle') and len(points) >= 2 and Point is not None:
                center, edge = points[0], points[1]
                radius = math.hypot(edge[0] - center[0], edge[1] - center[1])
                if radius > 0.0:
                    shapes.append(Point(center).buffer(radius))
        if not shapes:
            return None
        return unary_union(shapes) if unary_union is not None else shapes[0]

    @staticmethod
    def _stackup_from_file(board_path):
        """Read per-dielectric er/material/loss tangent from the saved
        .kicad_pcb stackup table, in physical top-to-bottom order.

        Needed because the IPC API's BoardStackupDielectricLayer.layers list
        comes back empty on some KiCad 10 installs (see get_epsilon_r in
        _get_stackup_protobuf) even though the same stackup is written
        correctly to the board file. P02 Rev E, run 20260922-2156: IPC
        reported er 4.4 "FR4" tan d 0 on all 5 dielectrics; the file has
        4.1 / 4.6 / 4.16 / 4.6 / 4.1 with materials 3313 / FR4 / 2116 / FR4 /
        3313 (audit-2026-09-23.md SI-1).
        """
        try:
            text = Path(board_path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return None
        stackup_start = text.find('(stackup')
        if stackup_start < 0:
            return None
        depth = 0
        stackup_end = None
        for index in range(stackup_start, len(text)):
            depth += (text[index] == '(') - (text[index] == ')')
            if depth == 0:
                stackup_end = index + 1
                break
        if stackup_end is None:
            return None
        block = text[stackup_start:stackup_end]

        dielectrics = []
        layer_start = re.compile(r'\(layer\s+"')
        for match in layer_start.finditer(block):
            depth = 0
            end = None
            for index in range(match.start(), len(block)):
                depth += (block[index] == '(') - (block[index] == ')')
                if depth == 0:
                    end = index + 1
                    break
            if end is None:
                continue
            layer_text = block[match.start():end]
            eps_match = re.search(r'\(epsilon_r\s+([-+]?\d*\.?\d+)', layer_text)
            if not eps_match:
                continue  # copper / mask / silk / paste: no dielectric constant
            thickness_match = re.search(r'\(thickness\s+([-+]?\d*\.?\d+)', layer_text)
            material_match = re.search(r'\(material\s+"((?:\\.|[^"\\])*)"', layer_text)
            loss_match = re.search(r'\(loss_tangent\s+([-+]?\d*\.?\d+)', layer_text)
            dielectrics.append({
                'thickness_mm': float(thickness_match.group(1)) if thickness_match else 0.0,
                'material': material_match.group(1) if material_match else '',
                'epsilon_r': float(eps_match.group(1)),
                'loss_tangent': float(loss_match.group(1)) if loss_match else 0.0,
            })
        return dielectrics

    def get_board_outline(self, board_file_path=None):
        """Return an Edge.Cuts polygon for thermal meshing without padding."""
        outline = self._edge_cuts_outline_from_file(board_file_path) if board_file_path else None
        if outline is not None:
            return outline
        bounds = self.get_board_bounds(margin_mm=0.0, board_file_path=board_file_path)
        return box(*bounds) if bounds is not None and box is not None else None

    def get_board_bounds(self, margin_mm=1.0, board_file_path=None):
        """Return live full-board XY bounds, not just the currently analysed net."""
        file_bounds = self._edge_cuts_bounds_from_file(board_file_path) if board_file_path else None
        if file_bounds is not None:
            min_x, min_y, max_x, max_y = file_bounds
            return (min_x - margin_mm, min_y - margin_mm, max_x + margin_mm, max_y + margin_mm)
        points = []
        def point(value):
            if value is None:
                return
            x = self._get_val(value, 'x', None)
            y = self._get_val(value, 'y', None)
            if x is not None and y is not None:
                points.append((to_mm(x), to_mm(y)))
        for collection in ('drawings', 'graphic_items', 'tracks', 'vias', 'footprints'):
            for item in self._get_board_items(collection) or []:
                point(self._get_val(item, 'start'))
                point(self._get_val(item, 'end'))
                point(self._get_val(item, 'position'))
                point(self._get_val(item, 'center'))
                for pad in self._get_val(item, 'pads', []) or []:
                    point(self._get_val(pad, 'position'))
        for zone in self._get_board_items('zones') or []:
            for polygon_list in (self._get_val(zone, 'filled_polygons', {}) or {}).values():
                for polygon in polygon_list if isinstance(polygon_list, (list, tuple)) else [polygon_list]:
                    outline = self._get_val(polygon, 'outline')
                    for node in self._get_val(outline, 'nodes', self._get_val(outline, 'points', [])) or []:
                        point(self._get_val(node, 'point', node))
        if not points:
            return None
        min_x, min_y = min(x for x, _ in points), min(y for _, y in points)
        max_x, max_y = max(x for x, _ in points), max(y for _, y in points)
        return (min_x - margin_mm, min_y - margin_mm, max_x + margin_mm, max_y + margin_mm)

    @staticmethod
    def _solder_masks_from_file(board_path):
        """Top/bottom solder-mask thickness (and er when written) from the file.

        IPC's stackup is read for copper and dielectrics only, so the mask the
        differential solver models above an outer-layer pair fell back to a
        0.02 mm / er 3.3 default; P02 Rev E's file says 0.01 mm on both sides
        (audit-2026-09-23.md SI-6). KiCad writes epsilon_r for a mask only when
        the user set one, so it is optional.
        """
        try:
            text = Path(board_path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError, TypeError):
            return {}
        masks = {}
        pattern = re.compile(
            r'\(layer\s+"([FB])\.Mask"\s*\(type\s+"[^"]*Solder Mask"\)(.*?)\n\s*\)',
            re.S,
        )
        for match in pattern.finditer(text):
            body = match.group(2)
            thickness = re.search(r'\(thickness\s+([-+]?\d*\.?\d+)', body)
            epsilon = re.search(r'\(epsilon_r\s+([-+]?\d*\.?\d+)', body)
            if not thickness:
                continue
            masks["top" if match.group(1) == "F" else "bottom"] = {
                'thickness_mm': float(thickness.group(1)),
                'epsilon_r': float(epsilon.group(1)) if epsilon else 0.0,
            }
        return masks

    def get_board_stackup(self):
        """
        Extracts physical stackup properties.
        Returns a dict with copper data and substrate segments.
        """
        # Return cached stackup if available
        if self._stackup_cache is not None:
            return self._stackup_cache
        
        # Try Protobuf API first
        if HAS_KIPY:
            try:
                stackup = self._get_stackup_protobuf()
                if stackup:
                    self._stackup_cache = stackup
                    return stackup
            except Exception as e:
                msg = f"Protobuf API stackup extraction failed: {e}. Falling back to defaults."
                if self.debug:
                    self.logger.warning(msg)
                else:
                    self.logger.debug(msg)
        
        return self._get_stackup_defaults()

    def _get_stackup_protobuf(self):
        """Extract stackup using the new KiCad Protobuf API (kipy)."""
        if self.debug:
            self.logger.debug("Attempting stackup extraction via Protobuf API (kicad-python)...")
            
        try:
            # Note: We assume self.board is already a kipy Board object connected to KiCad
            if not self.board:
                return None
            
            stackup = self.board.get_stackup()

            rho_copper = COPPER_RESISTIVITY_OHM_MM
            copper_data = {}
            layer_order = []
            substrates = []
            
            last_copper = None
            
            # Helper to find dielectric constant
            def get_epsilon_r(dielectric_layer):
                # Check for sub-layers (KiCad 9 structure)
                # BoardStackupDielectricLayer -> layers (list of BoardStackupDielectricProperties)
                if hasattr(dielectric_layer, 'layers') and dielectric_layer.layers:
                    return dielectric_layer.layers[0].epsilon_r
                return 4.4 # Default FR4

            def get_material(dielectric_layer):
                if hasattr(dielectric_layer, 'layers') and dielectric_layer.layers:
                    return dielectric_layer.layers[0].material_name
                return "FR4"

            def get_loss_tangent(dielectric_layer):
                if hasattr(dielectric_layer, 'layers') and dielectric_layer.layers:
                    return getattr(dielectric_layer.layers[0], 'loss_tangent', 0.0)
                return 0.0

            def has_ipc_dielectric_properties(dielectric_layer):
                # SI-1: KiCad IPC returns an empty .layers list on some KiCad
                # 10 installs; the three helpers above then fall back to
                # 4.4/"FR4"/0 silently. This flags that fallback so the
                # stackup builder below can re-read the real values from the
                # saved board file instead of trusting the IPC default.
                return bool(getattr(dielectric_layer, 'layers', None))

            # Iterate layers
            for i, layer in enumerate(stackup.layers):
                # layer is BoardStackupLayer
                
                type_val = layer.type
                thickness_mm = layer.thickness / 1e6
                
                is_copper = False
                is_dielectric = False
                
                # Check layer type
                # type 1 = Copper, type 2 = Dielectric
                if type_val == 1 or str(type_val) == 'BS_COPPER' or 'COPPER' in str(type_val):
                    is_copper = True
                elif type_val == 2 or str(type_val) == 'BS_DIELECTRIC' or 'DIELECTRIC' in str(type_val):
                    is_dielectric = True
                # Ignore other types (silkscreen, mask, paste, etc.)

                if is_copper:
                    lid = layer.layer # The actual KiCad Layer ID 
                    name = layer.user_name
                    
                    copper_data[lid] = {
                        'name': name,
                        'canonical_name': canonical_copper_name(lid),
                        'thickness_mm': thickness_mm if thickness_mm > 0 else DEFAULT_COPPER_THICKNESS_MM
                    }
                    layer_order.append(lid)
                    
                    # Link pending substrate
                    if len(substrates) > 0 and substrates[-1]['between'][1] is None:
                        substrates[-1]['between'][1] = lid
                        
                    last_copper = lid
                    
                elif is_dielectric:
                    if last_copper is not None:
                        if len(substrates) == 0 or substrates[-1]['between'][1] is not None:
                            d_layer = layer.dielectric
                            eps = get_epsilon_r(d_layer)
                            mat = get_material(d_layer)
                            loss = get_loss_tangent(d_layer)

                            substrates.append({
                                'thickness_mm': thickness_mm,
                                'between': [last_copper, None],
                                'material': mat,
                                'epsilon_r': eps,
                                'loss_tangent': loss,
                                '_ipc_dielectric_ok': has_ipc_dielectric_properties(d_layer),
                            })
                        else:
                            substrates[-1]['thickness_mm'] += thickness_mm

            warnings = []
            missing = [sub for sub in substrates if not sub.get('_ipc_dielectric_ok', True)]
            if missing:
                # Re-read the dielectrics IPC could not give us from the
                # saved board file, which carries the real values (SI-1).
                file_dielectrics = self._stackup_from_file(self.board_path) if self.board_path else None
                if file_dielectrics and len(file_dielectrics) == len(substrates):
                    for sub, file_sub in zip(substrates, file_dielectrics):
                        if sub.get('_ipc_dielectric_ok', True):
                            continue
                        sub['epsilon_r'] = file_sub['epsilon_r']
                        sub['material'] = file_sub['material'] or sub['material']
                        sub['loss_tangent'] = file_sub['loss_tangent']
                        if file_sub['thickness_mm'] > 0:
                            sub['thickness_mm'] = file_sub['thickness_mm']
                else:
                    names = []
                    for sub in missing:
                        l1, l2 = sub['between']
                        n1 = copper_data.get(l1, {}).get('name', str(l1))
                        n2 = copper_data.get(l2, {}).get('name', str(l2)) if l2 is not None else "Bottom"
                        names.append(f"{n1}-{n2}")
                    warnings.append(
                        "IPC gave no dielectric constant for " + ", ".join(names)
                        + " and the board file was unavailable to fall back to; "
                        "using default er 4.4 'FR4'. Impedance results on these "
                        "layers are not trustworthy."
                    )

            for sub in substrates:
                sub.pop('_ipc_dielectric_ok', None)

            result = {
                'copper': copper_data,
                'layer_order': layer_order,
                'substrate': substrates,
                'resistivity': rho_copper,
                'source': 'KICAD_IPC',
                'trustworthy': bool(copper_data and substrates) and not warnings,
                'warnings': warnings,
                'solder_masks': (
                    self._solder_masks_from_file(self.board_path) if self.board_path else {}
                ),
            }
            
            if self.debug:
                self.logger.debug("Extracted Stackup Details:")
                for lid in layer_order:
                    data = copper_data[lid]
                    self.logger.debug(f"  Copper Layer {lid} ('{data['name']}'): thickness = {data['thickness_mm']:.4f} mm")
                
                for i, sub in enumerate(substrates):
                    l1, l2 = sub['between']
                    n1 = copper_data.get(l1, {}).get('name', str(l1))
                    n2 = copper_data.get(l2, {}).get('name', str(l2)) if l2 is not None else "Bottom"
                    self.logger.debug(f"  Dielectric {i+1} (between {n1} and {n2}): thickness = {sub['thickness_mm']:.4f} mm, eps_r = {sub['epsilon_r']:.2f}")
            
            return result
        except Exception as e:
            msg = str(e)
            if "Timed out" in msg:
                 self.logger.warning("Protobuf API timed out (Deadlock). Falling back.")
            if self.debug:
                self.logger.debug(f"Protobuf extraction error: {e}")
            raise e

    def _get_stackup_defaults(self):
        """Returns a default 2-layer stackup (Top/Bottom Copper, FR4 dielectric)."""
        msg = "Using hardcoded default stackup (2-Layer)."
        if self.debug:
            self.logger.warning(msg)

        rho_copper = COPPER_RESISTIVITY_OHM_MM

        # Default to F.Cu (0) and B.Cu (31)
        f_cu_id = 0
        b_cu_id = 31

        copper_data = {
            f_cu_id: {'name': 'F.Cu', 'thickness_mm': DEFAULT_COPPER_THICKNESS_MM},
            b_cu_id: {'name': 'B.Cu', 'thickness_mm': DEFAULT_COPPER_THICKNESS_MM}
        }

        layer_order = [f_cu_id, b_cu_id]

        substrates = [{
            # The dielectric of a default board, not the board: the two copper
            # layers take their share of the finished thickness.
            'thickness_mm': DEFAULT_BOARD_THICKNESS_MM - 2 * DEFAULT_COPPER_THICKNESS_MM,
            'between': [f_cu_id, b_cu_id],
            'material': 'FR4',
            'epsilon_r': 4.4
        }]
        
        result = {
            'copper': copper_data,
            'layer_order': layer_order,
            'substrate': substrates,
            'resistivity': rho_copper,
            'source': 'DEFAULT',
            'trustworthy': False,
            'warnings': [
                'KiCad stackup unavailable; using a generic 2-layer FR4 profile.',
                'Differential impedance values from this profile are estimates only.',
            ],
        }
        
        self._stackup_cache = result
        return result

    def get_stackup_profile(self):
        """Return the legacy stackup dictionary as an ordered typed profile."""
        try:
            from .models import StackupLayerModel, StackupProfile
        except (ImportError, ValueError):
            from models import StackupLayerModel, StackupProfile

        data = self.get_board_stackup()
        copper = data.get('copper', {})
        substrates = data.get('substrate', [])
        order = list(data.get('layer_order', [])) or sorted(copper)
        substrate_by_pair = {
            tuple(item.get('between', [])): item for item in substrates
            if len(item.get('between', [])) == 2
        }
        layers = []
        for index, layer_id in enumerate(order):
            layer = copper.get(layer_id, {})
            layers.append(StackupLayerModel(
                name=layer.get('name', str(layer_id)),
                kind='COPPER',
                thickness_mm=float(layer.get('thickness_mm', DEFAULT_COPPER_THICKNESS_MM)),
                layer_id=int(layer_id),
                material='Copper',
                epsilon_r=1.0,
            ))
            if index + 1 < len(order):
                next_id = order[index + 1]
                dielectric = substrate_by_pair.get((layer_id, next_id))
                if dielectric is None and index < len(substrates):
                    dielectric = substrates[index]
                dielectric = dielectric or {}
                layers.append(StackupLayerModel(
                    name=f'Dielectric {index + 1}',
                    kind='DIELECTRIC',
                    thickness_mm=float(dielectric.get('thickness_mm', 0.0)),
                    material=dielectric.get('material', 'FR4'),
                    epsilon_r=float(dielectric.get('epsilon_r', 4.4)),
                    loss_tangent=float(dielectric.get('loss_tangent', 0.0)),
                ))
        masks = data.get('solder_masks') or {}
        for side, name in (("top", "F.Mask"), ("bottom", "B.Mask")):
            mask = masks.get(side)
            if not mask:
                continue
            layer = StackupLayerModel(
                name=name, kind='SOLDER_MASK',
                thickness_mm=float(mask.get('thickness_mm', 0.0)),
                material='Solder mask', epsilon_r=float(mask.get('epsilon_r', 0.0)),
            )
            if side == "top":
                layers.insert(0, layer)
            else:
                layers.append(layer)
        warnings = list(data.get('warnings', []))
        if any(layer.kind == 'DIELECTRIC' and layer.thickness_mm <= 0 for layer in layers):
            warnings.append('One or more dielectric thicknesses are missing.')
        return StackupProfile(
            layers=layers,
            source=data.get('source', 'DEFAULT'),
            trustworthy=bool(data.get('trustworthy', False)) and not warnings,
            warnings=warnings,
        )

    def get_net_tracks(self, net_name):
        """Extract immutable same-net route segments for Phase 5 analysis."""
        result = []
        for track in self._get_items_for_net('tracks', net_name):
            start = self._get_val(track, 'start')
            end = self._get_val(track, 'end')
            if start is None or end is None:
                continue
            x0 = to_mm(self._get_val(start, 'x', 0))
            y0 = to_mm(self._get_val(start, 'y', 0))
            x1 = to_mm(self._get_val(end, 'x', 0))
            y1 = to_mm(self._get_val(end, 'y', 0))
            width = to_mm(self._get_val(track, 'width', 0))
            if width <= 0:
                continue
            result.append({
                'start': (x0, y0),
                'end': (x1, y1),
                'width_mm': width,
                'layer_id': int(self._get_val(track, 'layer', -1)),
                'length_mm': math.hypot(x1 - x0, y1 - y0),
            })
        return result

    def get_track_summary(self, net_name):
        """Count, narrowest width and oblique count of a net's tracks.

        What a raster result needs to state its own error: the narrowest track
        in grid steps, and whether any track -- an arc always -- runs off the
        grid axes.
        """
        try:
            from .raster_fidelity import is_oblique
        except (ImportError, ValueError):
            from raster_fidelity import is_oblique
        count, finest, oblique = 0, 0.0, 0
        for track in self._get_items_for_net('tracks', net_name):
            start = self._get_val(track, 'start')
            end = self._get_val(track, 'end')
            width_mm = to_mm(self._get_val(track, 'width', 0.0))
            if start is None or end is None or width_mm <= 0.0:
                continue
            count += 1
            finest = width_mm if count == 1 else min(finest, width_mm)
            p0 = (to_mm(self._get_val(start, 'x', 0)), to_mm(self._get_val(start, 'y', 0)))
            p1 = (to_mm(self._get_val(end, 'x', 0)), to_mm(self._get_val(end, 'y', 0)))
            if self._get_val(track, 'mid') is not None or is_oblique(p0, p1):
                oblique += 1
        return {"track_count": count, "finest_width_mm": finest, "oblique_count": oblique}

    def get_track_geometry(self, net_name):
        """Return physical routed-copper geometry grouped by layer.

        This intentionally excludes the mesher's capture safety buffer so it
        can classify solved branches as route, zone, overlap, or pad/other.
        """
        if LineString is None or unary_union is None:
            return {}
        shapes = {}
        for track in self._get_items_for_net('tracks', net_name):
            start = self._get_val(track, 'start')
            end = self._get_val(track, 'end')
            if start is None or end is None:
                continue
            width_mm = to_mm(self._get_val(track, 'width', 0.0))
            if width_mm <= 0.0:
                continue
            points = [
                (to_mm(self._get_val(start, 'x', 0)), to_mm(self._get_val(start, 'y', 0))),
                (to_mm(self._get_val(end, 'x', 0)), to_mm(self._get_val(end, 'y', 0))),
            ]
            mid = self._get_val(track, 'mid')
            if mid is not None:
                # The arc itself, as the offline reader draws it. Start, mid
                # and end alone cut a quarter-turn's corner by 7.6 % of its
                # radius, and this copper is compared with the saved file's.
                points = arc_polyline(
                    points[0],
                    (to_mm(self._get_val(mid, 'x', 0)), to_mm(self._get_val(mid, 'y', 0))),
                    points[1],
                )
            if len(set(points)) == 1:
                geometry = Point(points[0]).buffer(width_mm / 2.0)
            else:
                geometry = LineString(points).buffer(width_mm / 2.0, cap_style=1)
            layer_id = int(self._get_val(track, 'layer', -1))
            shapes.setdefault(layer_id, []).append(geometry)
        return {
            layer_id: unary_union(layer_shapes)
            for layer_id, layer_shapes in shapes.items() if layer_shapes
        }

    def get_zone_geometry(self, net_name):
        """Return actual filled-zone copper only, grouped by copper layer."""
        if Polygon is None or unary_union is None:
            return {}
        shapes = {}
        for zone in self._get_items_for_net('zones', net_name):
            net = self._get_val(zone, 'net')
            filled = self._get_val(zone, 'filled_polygons', {})
            if not isinstance(filled, dict):
                continue
            for layer_id, polygons in filled.items():
                if not isinstance(polygons, (list, tuple)):
                    polygons = [polygons]
                for polygon in polygons:
                    outline = self._get_val(polygon, 'outline')
                    nodes = self._get_val(outline, 'nodes') or self._get_val(outline, 'points', [])
                    points = []
                    for node in nodes or []:
                        point = self._get_val(node, 'point', node)
                        if point is not None:
                            points.append((
                                to_mm(self._get_val(point, 'x', 0)),
                                to_mm(self._get_val(point, 'y', 0)),
                            ))
                    if len(points) < 3:
                        continue
                    shape = Polygon(points)
                    if not shape.is_valid:
                        shape = shape.buffer(0)
                    for hole in self._get_val(polygon, 'holes', []) or []:
                        hole_nodes = self._get_val(hole, 'nodes') or self._get_val(hole, 'points', [])
                        hole_points = []
                        for node in hole_nodes or []:
                            point = self._get_val(node, 'point', node)
                            if point is not None:
                                hole_points.append((
                                    to_mm(self._get_val(point, 'x', 0)),
                                    to_mm(self._get_val(point, 'y', 0)),
                                ))
                        if len(hole_points) >= 3:
                            shape = shape.difference(Polygon(hole_points))
                    if not shape.is_empty:
                        shapes.setdefault(int(layer_id), []).append(shape)
        merged = {
            layer_id: unary_union(layer_shapes)
            for layer_id, layer_shapes in shapes.items() if layer_shapes
        }
        # KiCad's IPC facade can omit filled polygons or return only part of a
        # multi-layer zone.  Merge the saved, already-filled copper with the
        # live polygons after remapping its layer name to the IPC layer ID.
        for layer_id, geometry in self._zone_geometry_from_file(net_name).items():
            if layer_id not in merged or merged[layer_id].is_empty:
                merged[layer_id] = geometry
            elif not geometry.is_empty:
                merged[layer_id] = unary_union((merged[layer_id], geometry))
        return merged

    @staticmethod
    def _balanced_blocks(text, token):
        """Yield balanced S-expression blocks beginning with *token*."""
        start = 0
        while True:
            begin = text.find(token, start)
            if begin < 0:
                return
            depth = 0
            quoted = False
            escaped = False
            for index in range(begin, len(text)):
                character = text[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == '"':
                        quoted = False
                    continue
                if character == '"':
                    quoted = True
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        yield text[begin:index + 1]
                        start = index + 1
                        break
            else:
                return

    def _load_file_zone_geometry(self):
        """Parse saved filled polygons as a fallback for incomplete IPC data."""
        if self._file_zone_geometry_cache is not None:
            return self._file_zone_geometry_cache
        self._file_zone_geometry_cache = {}
        if Polygon is None or unary_union is None or not self.board_path:
            return self._file_zone_geometry_cache
        try:
            text = Path(self.board_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return self._file_zone_geometry_cache

        # The file's copper layers, top to bottom: ordinal, canonical name and
        # the designer's name when one was given ("PWR.L3" for In2.Cu).
        file_layers = [
            (int(identifier), name, user_name)
            for identifier, name, user_name in re.findall(
                r'^\s*\((\d+)\s+"([^"]+)"\s+(?:signal|power|mixed|jumper)(?:\s+"([^"]*)")?',
                text, flags=re.MULTILINE,
            )
        ]
        # KiCad 10's IPC API exposes protobuf layer enum values which do not
        # necessarily equal the numeric IDs serialized in the .kicad_pcb file
        # (for example B.Cu can be 8 live but 2 on disk).  Differential tracks
        # and stackup use the live IDs, so remap saved-zone layers to them.
        try:
            stackup = self.get_board_stackup() or {}
        except Exception:
            stackup = {}
        layer_ids = file_layers_to_live(
            file_layers, stackup.get('copper', {}) or {}, stackup.get('layer_order'),
        )
        by_net = {}
        for zone in self._balanced_blocks(text, "(zone"):
            net_match = re.search(r'\(net\s+"((?:\\.|[^"])*)"\)', zone)
            if net_match is None:
                continue
            net_name = net_match.group(1).replace(r'\"', '"').replace(r'\\', '\\')
            for filled in self._balanced_blocks(zone, "(filled_polygon"):
                layer_match = re.search(r'\(layer\s+"([^"]+)"\)', filled)
                if layer_match is None or layer_match.group(1) not in layer_ids:
                    continue
                points = [
                    (float(x), float(y))
                    for x, y in re.findall(
                        r'\(xy\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+'
                        r'([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\)',
                        filled,
                    )
                ]
                if len(points) < 3:
                    continue
                geometry = Polygon(points)
                if not geometry.is_valid:
                    geometry = geometry.buffer(0)
                if not geometry.is_empty:
                    by_net.setdefault(net_name, {}).setdefault(
                        layer_ids[layer_match.group(1)], []
                    ).append(geometry)
        self._file_zone_geometry_cache = {
            net_name: {
                layer_id: unary_union(polygons)
                for layer_id, polygons in layers.items()
            }
            for net_name, layers in by_net.items()
        }
        return self._file_zone_geometry_cache

    def _zone_geometry_from_file(self, net_name):
        return dict(self._load_file_zone_geometry().get(net_name, {}))

    def pad_polygon(self, pad):
        """A pad's copper outline in board mm: a circle, or its rectangle turned by its angle.

        Its size is the pad's own or, failing that, its padstack's first
        copper layer's; a pad reporting neither is taken as 1 x 1 mm.
        """
        # Position (Absolute)
        pos = self._get_val(pad, 'position')
        x_mm = to_mm(self._get_val(pos, 'x', 0))
        y_mm = to_mm(self._get_val(pos, 'y', 0))

        # Size & Shape
        w_mm = 0
        h_mm = 0
        shape_type = 0 # 0=Circle, 1=Rect, 2=RoundRect/Oval? Need verification.

        # Try direct size first
        size = self._get_val(pad, 'size')
        if size:
            w_mm = to_mm(self._get_val(size, 'x', 0))
            h_mm = to_mm(self._get_val(size, 'y', 0))
            shape_type = self._get_val(pad, 'shape', 1)

        # Fallback to padstack
        if w_mm == 0:
            ps = self._get_val(pad, 'padstack')
            if ps:
                cls = self._get_val(ps, 'copper_layers', [])
                if cls and len(cls) > 0:
                    l0 = cls[0]
                    sz = self._get_val(l0, 'size')
                    w_mm = to_mm(self._get_val(sz, 'x', 0))
                    h_mm = to_mm(self._get_val(sz, 'y', 0))
                    shape_type = self._get_val(l0, 'shape', 1)

        if w_mm == 0: w_mm = 1.0
        if h_mm == 0: h_mm = 1.0

        # Rotation
        pad_rot = self._get_val(pad, 'rotation', 0)
        if pad_rot == 0:
             ps = self._get_val(pad, 'padstack')
             pad_rot = self._get_val(ps, 'angle', 0)
        try: pad_rot = float(pad_rot)
        except: pad_rot = 0.0

        # Create Shape
        # Shape types (Empirical/Guess based on kipy enum):
        # PSS_CIRCLE = 0, PSS_RECT = 1, PSS_ROUNDRECT = 2, PSS_OVAL = 3
        # For now, treat Circle as Circle, everything else as Box/Rect
        # If only one dimension is relevant for circle, usually x==y

        # Note: 'shape' attribute value 2 seen in inspection for Rect/RoundRect pads

        if str(shape_type) == 'PSS_CIRCLE' or shape_type == 0:
            # Circle
            return Point(x_mm, y_mm).buffer(w_mm / 2)
        # Rectangle / Oval / RoundRect
        base_box = box(-w_mm / 2, -h_mm / 2, w_mm / 2, h_mm / 2)
        rotated_shape = affinity.rotate(base_box, -pad_rot, origin=(0, 0))
        return affinity.translate(rotated_shape, x_mm, y_mm)

    def pad_layers(self, pad):
        """The layer ids a pad declares, its padstack's when it states none itself."""
        layers = self._get_val(pad, 'layers')
        if not layers:
            ps = self._get_val(pad, 'padstack')
            if ps:
                layers = self._get_val(ps, 'layers')
                if not layers:
                     layers = self._get_val(ps, 'copper_layers')
        return layers or []

    def get_net_geometry(self, net_name, merge=True):
        """
        Extracts and merges geometry for a specific net.
        Returns a dictionary: { layer_id: shapely.geometry.Polygon }
        """
        layer_shapes = {} # { layer_id: [shapely_objects] }

        def add_shape(layer, shape):
            if layer not in layer_shapes:
                layer_shapes[layer] = []
            layer_shapes[layer].append(shape)
            
        def is_copper(lid):
            stackup = self.get_board_stackup()
            return lid in stackup['copper']
            
        # 1. Process Tracks
        tracks = self._get_items_for_net('tracks', net_name)
        
        # safety_buffer was 0.05 mm; zeroed to remove systematic resistance under-estimation.
        # HybridMesher uses exact analytical geometry; the legacy raster path is unaffected
        # in terms of topology (grid nodes still land inside tracks at normal grid steps).
        safety_buffer = 0.0
        
        for track in tracks:
            start = self._get_val(track, 'start')
            end = self._get_val(track, 'end')
            width_mm = to_mm(self._get_val(track, 'width', 0))
            layer = self._get_val(track, 'layer', -1)
            
            if width_mm <= 0: width_mm = 0.2
            
            p0 = (to_mm(self._get_val(start, 'x', 0)), to_mm(self._get_val(start, 'y', 0)))
            p1 = (to_mm(self._get_val(end, 'x', 0)), to_mm(self._get_val(end, 'y', 0)))
            
            # Check for Arc
            # Attributes: center, radius, start_angle, end_angle (or angle)
            is_arc = False
            mid = self._get_val(track, 'mid')
            if mid:
                 is_arc = True
            
            if is_arc:
                # Discretize arc with higher resolution
                pm = (to_mm(self._get_val(mid, 'x', 0)), to_mm(self._get_val(mid, 'y', 0)))
                
                center = self._get_val(track, 'center')
                radius = self._get_val(track, 'radius')
                
                # Check directly for angles
                start_angle = self._get_val(track, 'start_angle')
                end_angle = self._get_val(track, 'end_angle')
                
                points = []
                
                if center and radius and start_angle is not None and end_angle is not None:
                    cx = to_mm(self._get_val(center, 'x', 0))
                    cy = to_mm(self._get_val(center, 'y', 0))
                    r_mm = to_mm(radius)
                    
                    # Convert 0.1 degrees (KiCad default) to radians? 
                    # Need to verify unit. Usually integers in 1/10 degree or similar, but
                    # kipy might normalize. 
                    
                    # Safe approach: Calculate angles ourselves from coordinates to be sure
                    # atan2 returns radians -pi to pi
                    a_start = math.atan2(p0[1] - cy, p0[0] - cx)
                    a_mid = math.atan2(pm[1] - cy, pm[0] - cx)
                    a_end = math.atan2(p1[1] - cy, p1[0] - cx)
                    
                    # Handle wrapping. 
                    # Cross product to determine direction? 
                    # Simple way: check if mid is between start/end in one direction
                    
                    # Normalize angles to 0-2pi
                    if a_start < 0: a_start += 2*math.pi
                    if a_mid < 0: a_mid += 2*math.pi
                    if a_end < 0: a_end += 2*math.pi
                    
                    # Determine sort order
                    # Case 1: Start < Mid < End
                    # Case 2: Wrap around 0/2pi
                    
                    # Just discretize from Start to End passing through Mid
                    # Total swept angle?
                    
                    # Vector arithmetic:
                    v_start = (p0[0]-cx, p0[1]-cy)
                    v_mid = (pm[0]-cx, pm[1]-cy)
                    v_end = (p1[0]-cx, p1[1]-cy)
                    
                    # We can pick N points. 
                    # Just use 8 segments (9 points) for smoothness
                    points = [p0]
                    
                    # We need to know which WAY to go (CW or CCW). Mid tells us.
                    # We can just construct two arcs (Start->Mid) and (Mid->End)
                    # 4 segments for Start->Mid, 4 for Mid->End = 8 total
                    
                    for (ps, pe) in [(p0, pm), (pm, p1)]:
                        a1 = math.atan2(ps[1] - cy, ps[0] - cx)
                        a2 = math.atan2(pe[1] - cy, pe[0] - cx)
                        
                        # Find shortest diff? kipy arcs usually shortest path?
                        # Actually arc direction is defined.
                        # Simple interpolation:
                        # But wait, atan2 discontinuity.
                        
                        diff = a2 - a1
                        if diff > math.pi: diff -= 2*math.pi
                        if diff < -math.pi: diff += 2*math.pi
                        
                        steps = 4
                        for i in range(1, steps + 1):
                            ang = a1 + diff * (i / steps)
                            px = cx + r_mm * math.cos(ang)
                            py = cy + r_mm * math.sin(ang)
                            points.append((px, py))
                            
                else:
                    # Fallback to 3 points
                    points = [p0, pm, p1]
                    
                line = LineString(points)
                # Cap style 1 (Round) is good for arcs
                poly = line.buffer(width_mm / 2 + safety_buffer, cap_style=1)
            else:
                if p0 == p1:
                    # Circular pad (VIA-like track?)
                    poly = Point(p0).buffer(width_mm / 2 + safety_buffer)
                else:
                    line = LineString([p0, p1])
                    poly = line.buffer(width_mm / 2 + safety_buffer, cap_style=1)
            
            if is_copper(layer):
                add_shape(layer, poly)

        # 1b. Process Vias (Essental for vertical connectivity mesh nodes)
        vias = self._get_items_for_net('vias', net_name)
        for via in vias:
            net = self._get_val(via, 'net')
            v_net_name = self._get_val(net, 'name', "")
            
            pos = self._get_val(via, 'position')
            x_mm = to_mm(self._get_val(pos, 'x', 0))
            y_mm = to_mm(self._get_val(pos, 'y', 0))
            
            width_mm = to_mm(self._get_val(via, 'width', 0.6*1e6))
            poly = Point(x_mm, y_mm).buffer(width_mm / 2 + safety_buffer)
            
            # Vias exist on all layers in their pair (usually through-all)
            layers = self._get_val(via, 'layers')
            if not layers:
                ps = self._get_val(via, 'padstack')
                if ps:
                    layers = self._get_val(ps, 'layers')
            
            if not layers:
                lp = self._get_val(via, 'layer_pair')
                if lp:
                    s_lid, e_lid = min(lp), max(lp)
                    stackup = self.get_board_stackup()
                    layers = [l for l in stackup['copper'].keys() if s_lid <= l <= e_lid]
                else:
                    # Default to all copper layers if no info
                    stackup = self.get_board_stackup()
                    layers = list(stackup['copper'].keys())
            
            for lid in layers:
                if is_copper(lid):
                    add_shape(lid, poly)
                    
        # 2. Process Pads (Footprints)
        for pad in self._get_items_for_net('pads', net_name):
                pad_poly = self.pad_polygon(pad)
                for lid in self.pad_layers(pad):
                   if is_copper(lid):
                       add_shape(lid, pad_poly)

        # 3. Process Zones
        zones = self._get_items_for_net('zones', net_name)
        for zone in zones:
            net = self._get_val(zone, 'net')
            z_net_name = self._get_val(net, 'name', "")
            
            # Filled Polygons (dict mapping layer_id -> list of polygon objects)
            filled_polygons = self._get_val(zone, 'filled_polygons', {})
            
            # filled_polygons is a dict: {layer_id: [polygon_object, ...]}
            if isinstance(filled_polygons, dict):
                for layer_id, poly_list in filled_polygons.items():
                    if not is_copper(layer_id):
                        continue
                    
                    if not isinstance(poly_list, (list, tuple)):
                        poly_list = [poly_list]
                        
                    for poly_obj in poly_list:
                        pts = []
                        
                        # Get outline from polygon object
                        outline = self._get_val(poly_obj, 'outline')
                        if outline:
                            # Try 'nodes' first (PolyLine has nodes), fallback to 'points'
                            points_list = self._get_val(outline, 'nodes')
                            if not points_list:
                                points_list = self._get_val(outline, 'points', [])
                                
                            for node in points_list:
                                # Each node might be PolyLineNode or just have 'point'
                                point = self._get_val(node, 'point')
                                if point:
                                    x = self._get_val(point, 'x', 0)
                                    y = self._get_val(point, 'y', 0)
                                    pts.append((to_mm(x), to_mm(y)))
                        
                        if len(pts) >= 3:
                            p = Polygon(pts)
                            if not p.is_valid: 
                                p = p.buffer(0)
                            
                            # Handle holes if present
                            holes_data = self._get_val(poly_obj, 'holes', [])
                            if holes_data:
                                for hole_outline in holes_data:
                                    h_pts = []
                                    # Try 'nodes' then 'points'
                                    h_points_list = self._get_val(hole_outline, 'nodes')
                                    if not h_points_list:
                                        h_points_list = self._get_val(hole_outline, 'points', [])
                                        
                                    for h_node in h_points_list:
                                        h_point = self._get_val(h_node, 'point')
                                        if h_point:
                                            h_pts.append((to_mm(self._get_val(h_point, 'x', 0)), 
                                                         to_mm(self._get_val(h_point, 'y', 0))))
                                    
                                    if len(h_pts) >= 3:
                                        hole_poly = Polygon(h_pts)
                                        if hole_poly.is_valid:
                                            p = p.difference(hole_poly)
                            
                            if p.is_valid and not p.is_empty:
                                add_shape(layer_id, p)

        # 4. Merge
        merged_geometry = {}
        for layer, shapes in layer_shapes.items():
            if not shapes:
                continue
            # Bulk thermal extraction visits every net on every copper layer.
            # Logging each tiny unmerged collection can enqueue thousands of
            # wx events and make the GUI appear frozen.  Keep only meaningful
            # geometry summaries; indexing and thermal-layer timings remain
            # visible in the log.
            if self.log_callback and len(shapes) >= 100:
                action = "Merging" if merge else "Collecting"
                self.log_callback(
                    f"[GEOMETRY] {action} {len(shapes):,} shape(s) for {net_name} "
                    f"on layer {layer}."
                )
            merged_geometry[layer] = (
                unary_union(shapes) if merge else GeometryCollection(shapes)
            )
            
        return merged_geometry

    def plot_geometry(self, geometry_by_layer):
        """Visualize the extracted geometry as a 2D plot."""
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(10, 8))
            layer_colors = {0: 'red', 2: 'blue', 31: 'green'}
            
            for layer_id, geom in geometry_by_layer.items():
                if geom.is_empty: continue
                color = layer_colors.get(layer_id, 'gray')
                
                if geom.geom_type == 'Polygon': polys = [geom]
                elif geom.geom_type == 'MultiPolygon': polys = list(geom.geoms)
                else: continue
                
                for poly in polys:
                    x, y = poly.exterior.xy
                    ax.fill(x, y, alpha=0.5, fc=color, ec='black', linewidth=0.5, label=f'Layer {layer_id}')
                    for interior in poly.interiors:
                        x, y = interior.xy
                        ax.fill(x, y, alpha=1.0, fc='white', ec='black', linewidth=0.5)
            
            ax.set_aspect('equal')
            ax.invert_yaxis()
            plt.tight_layout()
            import os
            output_path = os.path.join(os.path.dirname(__file__), 'debug_geometry.png')
            plt.savefig(output_path, format='png')
            if self.debug:
                 self.logger.debug(f"Saved debug plot to {output_path}")
            plt.close(fig)
            return output_path
        except Exception as e:
            if self.debug: self.logger.error(f"Plotting failed: {e}")
            return None
