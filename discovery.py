import re

try:
    from .models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef
    from .ingest.netlist_builder import is_ground_name
except (ImportError, ValueError):
    from models import PowerRail, UnifiedSource, UnifiedLoad, ComponentRef
    from ingest.netlist_builder import is_ground_name

# A converter's internal node or a control line often carries its domain in
# its name and is no rail: /MAIN_3V3_SW, /MAIN_3V3_FB, /J1_SAFE_PWR_EN,
# /PWR_VALID2 on the P02 Rev E board. A token like these drops a name guess;
# a rail the project declares is kept whatever it is called.
_NOT_A_RAIL_TOKEN_RE = re.compile(
    r"(?i)(^|[/_+\-.])(SW|LX|PH|FB|BOOT|BST|COMP|EN|PG|PGOOD|VALID|SENSE|GATE|ILIM|SS|SNUB)\d*([/_+\-.]|$)"
)

DISCOVERY_LABELS = {
    "power_symbol": "declared by a power symbol",
    "power_pin": "inferred from power pins",
    "name": "guessed from its name",
}
_DISCOVERY_RANK = {"power_symbol": 0, "power_pin": 1, "name": 2}


def rail_label(rail):
    """How a rail is listed: its net, its voltage, and how the scan found it if it did."""
    label = rail.net_name
    if rail.nominal_voltage:
        # A scanned rail's voltage is read off its name whatever found the rail.
        label += f" ({rail.nominal_voltage}V{', from its name' if rail.discovery else ''})"
    if rail.discovery in DISCOVERY_LABELS:
        label += f" [{DISCOVERY_LABELS[rail.discovery]}]"
    return label


class NetDiscoverer:
    """
    Scans the board to identify likely power rails and their properties.
    Uses generic attribute access compatible with KiCad Protobuf API (kipy).

    Rails come first from what the project's saved files declare -- nets a
    power symbol names, then nets with power pins -- through
    ``declared_rails``, a callable returning ``{net: source}``. Net-name
    patterns are the last resort: listed after every declared rail, and every
    rail they propose says it was guessed. They are not dropped when the files
    declare some rails: the reference board p02_alimentation names only GND
    with a power symbol and carries +3V3_MAIN, +5V_RAIL and its other rails on
    labels, which no declaration reaches. A ground is never guessed a rail,
    nor a net whose name says it is a converter node or a control line.
    """
    def __init__(self, board, log_callback=None, declared_rails=None):
        self.board = board
        self.log_callback = log_callback
        self.declared_rails = declared_rails
        
        # Regex for voltage parsing
        # Matches: +3V3, 3V3, +5V, 12V, VCC_3V3
        self.voltage_patterns = [
            r'(\d+)V(\d+)',  # 3V3 -> 3.3
            r'[+]?(\d+\.?\d*)V', # +5V, 12V -> 5.0, 12.0
            r'(\d+)V', # 5V -> 5.0
        ]
        
        # Regex for likely power names to prioritize. VBUS reaches a USB
        # connector's supply, which carries no power pin (J1_VBUS_RAW).
        self.power_name_patterns = [
            r'(?i)VCC', r'(?i)VDD', r'(?i)PWR', r'(?i)\+.*V', r'(?i)VBUS'
        ]

    def log(self, msg):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def _get_val(self, obj, attr_name, default=None):
        """Robustly get attribute value from object (property or getter)."""
        if obj is None: return default
        # Try property
        if hasattr(obj, attr_name):
            val = getattr(obj, attr_name)
            if val is not None: return val
        # Try getter
        for prefix in ["get_", ""]: # try direct call if it's a method with same name
            method_name = prefix + attr_name
            if hasattr(obj, method_name):
                try:
                    val = getattr(obj, method_name)()
                    if val is not None: return val
                except: pass
        return default

    def discover_power_nets(self):
        """
        Returns a list of PowerRail objects found on the board.
        Does NOT fully populate sources/loads automatically yet, just sets up candidates.
        Filters out nets that don't have any pads or don't have any traces/areas.
        """
        candidates = []
        
        # self.log(f"[DEBUG] Board object type: {type(self.board)}")
        # Deep inspection of board attributes
        # try:
        #     attrs = [a for a in dir(self.board) if not a.startswith('_')]
        #     self.log(f"[DEBUG] Board attributes: {', '.join(attrs[:20])}...")
        # except: pass
        
        # 1. Pre-scan connectivity
        nets_with_pads = set()   # {name}
        nets_with_copper = set() # {name}
        
        # Helper to get items robustly
        def get_board_items(attr_name):
            # Try property first (e.g. board.footprints)
            if hasattr(self.board, attr_name):
                return getattr(self.board, attr_name)
            # Try getter method (e.g. board.get_footprints())
            method_name = f"get_{attr_name}"
            if hasattr(self.board, method_name):
                try:
                    return getattr(self.board, method_name)()
                except Exception as e:
                    self.log(f"[DEBUG] Error calling {method_name}: {e}")
            return []

        # Check pads
        footprints = get_board_items('footprints')
        fp_count = 0
        for fp in footprints:
            fp_count += 1
            pads = self._get_val(fp, 'pads')
            if pads is None:
                defn = self._get_val(fp, 'definition')
                pads = self._get_val(defn, 'pads', [])
                
            for pad in pads:
                net = self._get_val(pad, 'net')
                net_name = self._get_val(net, 'name', "")
                if net_name:
                    nets_with_pads.add(net_name)
        # self.log(f"[DEBUG] Found {fp_count} footprints, {len(nets_with_pads)} unique nets with pads")
                    
        # Check traces (tracks/vias)
        tracks = get_board_items('tracks')
        track_count = 0
        for track in tracks:
            track_count += 1
            net = self._get_val(track, 'net')
            net_name = self._get_val(net, 'name', "")
            if net_name:
                nets_with_copper.add(net_name)
        # self.log(f"[DEBUG] Found {track_count} tracks, {len(nets_with_copper)} unique nets with copper from tracks")
                
        # Check zones
        zones = get_board_items('zones')
        zone_count = 0
        for zone in zones:
            zone_count += 1
            net = self._get_val(zone, 'net')
            net_name = self._get_val(net, 'name', "")
            if net_name:
                nets_with_copper.add(net_name)
        # self.log(f"[DEBUG] Found {zone_count} zones")
        
        found_nets = {} # {code: name}
        found_nets_by_name = {} # {name: code}
        
        # Helper to register net
        def reg_net(item):
            net = self._get_val(item, 'net')
            if net is None: return
            
            # Check for name/number
            name = self._get_val(net, 'name')
            code = self._get_val(net, 'number', self._get_val(net, 'code', -1))
            
            if name:
                # If we have a code (>0), use it, otherwise use a synthetic code
                if code <= 0:
                   # Try to find code from other items or just use -1
                   code = found_nets_by_name.get(name, -1)
                
                if code > 0:
                    found_nets[code] = name
                found_nets_by_name[name] = code
                    
        # Scan items for nets
        for fp in footprints:
            pads = self._get_val(fp, 'pads')
            if pads is None:
                defn = self._get_val(fp, 'definition')
                pads = self._get_val(defn, 'pads', [])
            for p in pads: reg_net(p)
                     
        for t in tracks: reg_net(t)
        for z in zones: reg_net(z)
        
        # If we have names but no codes, make synthetic codes
        synth_id = 10000
        for name, code in found_nets_by_name.items():
            if code <= 0:
                found_nets[synth_id] = name
                synth_id += 1

        declared = {}
        if self.declared_rails is not None:
            try:
                declared = dict(self.declared_rails() or {})
            except Exception as exc:
                self.log(
                    f"The project's saved files could not be read for declared rails "
                    f"({type(exc).__name__}: {exc}); rails are guessed from net names."
                )

        for nc, s_name in found_nets.items():
            if not s_name: continue

            # Filter logic
            # Use name-based sets
            has_pads = s_name in nets_with_pads
            has_copper = s_name in nets_with_copper

            if not (has_pads and has_copper):
                continue

            est_voltage = self._estimate_voltage(s_name)
            source = declared.get(s_name)
            if source is None:
                # Last resort: the name looks like power, or carries a voltage,
                # and says it is neither a ground nor a converter or control node.
                is_power_name = any(re.search(p, s_name) for p in self.power_name_patterns)
                guessed = (est_voltage is not None or is_power_name) and not (
                    is_ground_name(s_name) or _NOT_A_RAIL_TOKEN_RE.search(s_name)
                )
                source = "name" if guessed else None
            if source is None:
                continue
            rail = PowerRail(net_name=s_name, discovery=source)
            if est_voltage:
                rail.nominal_voltage = est_voltage
            candidates.append(rail)

        # Declared rails first, then inferred, then guessed; within each, by voltage and name.
        candidates.sort(key=lambda r: (
            _DISCOVERY_RANK.get(r.discovery, 3),
            -(r.nominal_voltage or -1),
            r.net_name,
        ))
        counts = {source: sum(1 for r in candidates if r.discovery == source) for source in DISCOVERY_LABELS}
        self.log(
            "Rail scan: " + ", ".join(f"{count} {DISCOVERY_LABELS[source]}" for source, count in counts.items())
            + "."
        )
        return candidates

    def _estimate_voltage(self, name):
        """
        Tries to extract a float voltage from the net name.
        Returns float or None.
        """
        # Exclude common current signals like "ISENSE_5V" if needed, 
        # but usually "5V" means 5V domain.
        
        # Pattern 1: 3V3 -> 3.3
        m = re.search(r'(\d+)V(\d+)', name, re.IGNORECASE)
        if m:
            try:
                major = int(m.group(1))
                minor = int(m.group(2))
                # infer decimal place. 'V' usually separates integer and decimal
                return float(f"{major}.{minor}")
            except: pass
            
        # Pattern 2: +12V or 5.0V
        m = re.search(r'plus(\d+\.?\d*)V', name, re.IGNORECASE) or \
            re.search(r'\+(\d+\.?\d*)V', name, re.IGNORECASE) 
        if m:
             try: return float(m.group(1))
             except: pass
             
        # Pattern 3: Simple "5V" suffix or prefix
        # Be careful not to match "V1" (version 1)
        m = re.search(r'\b(\d+\.?\d*)V\b', name, re.IGNORECASE)
        if m:
            try: return float(m.group(1))
            except: pass
            
        return None

    def get_components_on_net(self, net_name):
        """
        Returns a dict of { ref_des: [pad_obj, ...] } for all components on this net.
        """
        components = {}
        
        # Helper to get items robustly
        def get_board_items(attr_name):
            if hasattr(self.board, attr_name):
                return getattr(self.board, attr_name)
            method_name = f"get_{attr_name}"
            if hasattr(self.board, method_name):
                try: return getattr(self.board, method_name)()
                except: pass
            return []

        # Iterate all footprints, then pads
        footprints = get_board_items('footprints')
        for fp in footprints:
            # ref usually 'reference' property
            ref = self._get_val(fp, 'reference', self._get_val(fp, 'ref_des', ''))
            if not ref:
                # Try reference_field for Kipy
                ref_field = self._get_val(fp, 'reference_field')
                if ref_field:
                    text = self._get_val(ref_field, 'text')
                    if text:
                        ref = self._get_val(text, 'value', '')

            if not ref: continue
            
            pads = self._get_val(fp, 'pads')
            if pads is None:
                defn = self._get_val(fp, 'definition')
                pads = self._get_val(defn, 'pads', [])
                
            relevant_pads = []
            for pad in pads:
                net = self._get_val(pad, 'net')
                p_net_name = self._get_val(net, 'name', "")
                    
                if p_net_name == net_name:
                    relevant_pads.append(pad)
            
            if relevant_pads:
                components[ref] = relevant_pads
                
        return components

    def get_all_net_names(self):
        """
        Returns a set of all unique net names found on the board.
        """
        net_names = set()
        
        # Helper to get items robustly
        def get_board_items(attr_name):
            if hasattr(self.board, attr_name):
                return getattr(self.board, attr_name)
            method_name = f"get_{attr_name}"
            if hasattr(self.board, method_name):
                try: return getattr(self.board, method_name)()
                except: pass
            return []

        # 1. Check if board has nets collection directly (efficient)
        nets = get_board_items('nets')
        if nets:
            for n in nets:
                if isinstance(n, str):
                    net_names.add(n)
                else:
                    name = getattr(n, 'name', None)
                    if name: net_names.add(name)
            if net_names:
                self.log(f"[DEBUG] Found {len(net_names)} nets via board.nets collection")
                return list(net_names)

        def collect(item):
            net = self._get_val(item, 'net')
            name = self._get_val(net, 'name')
            if name: net_names.add(name)
                    
        # Scan items
        footprints = get_board_items('footprints')
        for fp in footprints:
            pads = self._get_val(fp, 'pads')
            if pads is None:
                defn = self._get_val(fp, 'definition')
                pads = self._get_val(defn, 'pads', [])
            for p in pads: collect(p)
                     
        for t in get_board_items('tracks'): collect(t)
        for z in get_board_items('zones'): collect(z)
              
        self.log(f"[DEBUG] Found {len(net_names)} nets by scanning items")
        return list(net_names)
