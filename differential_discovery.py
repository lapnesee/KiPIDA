"""Deterministic differential-pair discovery for KiCad IPC board objects.

Two conventions carry a designer's intent (docs/plan-universalite.md, 5.4):

* KiCad's own pairing of net names, which its differential-pair router
  applies: past any trailing digits and underscores, a name ends in "+" or
  "P" and its complement in "-" or "N" (``kicad_pair_complement``);
* netclasses, the native declaration: a pair whose two nets share a netclass
  other than Default was grouped by the designer, and a class named for an
  impedance ("90ohm", "85Ohm-diff_PCIE") states its target.

Other naming habits (USB_DP/USB_DM, _POS/_NEG) are still recognised, as
suspicions KiCad itself would not route as a pair.
"""

import fnmatch
import json
import re
from collections import defaultdict, deque

try:
    from .models import DifferentialPairCandidate
except (ImportError, ValueError):
    from models import DifferentialPairCandidate


INTERFACE_DEFAULTS = {
    "USB": (90.0, "no"),
    "PCIE": (85.0, "yes"),
    "SATA": (90.0, "no"),
    "HDMI": (100.0, "no"),
    "MIPI": (100.0, "no"),
    "ETHERNET": (100.0, "unknown"),
    "LVDS": (100.0, "unknown"),
    "CAN": (120.0, "no"),
    "RS485": (120.0, "no"),
    "DDR": (100.0, "no"),
    "GENERIC": (100.0, "unknown"),
}


_POLARITY_COMPLEMENTS = {"+": ("-", "P"), "-": ("+", "N"), "P": ("N", "P"), "N": ("P", "N")}
_IMPEDANCE_IN_NAME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*ohm", re.I)


def kicad_pair_complement(net_name):
    """``(complement, polarity)`` by KiCad's differential-pair naming rule, or None.

    The rule of KiCad's router (PNS_KICAD_IFACE_BASE::matchDpSuffix): walking
    back from the end past digits and underscores, the first other character
    must be +, -, P or N; the complement swaps it for its opposite and keeps
    everything else. "USB_D+" pairs with "USB_D-", "LANE0_P" with "LANE0_N",
    "TX_P_1" with "TX_N_1", "CLKP" with "CLKN".
    """
    text = str(net_name or "")
    index = len(text) - 1
    while index >= 0 and (text[index].isdigit() or text[index] == "_"):
        index -= 1
    if index < 1 or text[index] not in _POLARITY_COMPLEMENTS:
        return None
    opposite, polarity = _POLARITY_COMPLEMENTS[text[index]]
    return text[:index] + opposite + text[index + 1:], polarity


def netclass_membership(project_path, net_names):
    """``{net: netclass}`` for the named nets a .kicad_pro assigns to a netclass other than Default.

    Explicit assignments first, then netclass patterns in order, matched as
    KiCad's wildcards without regard to case.
    """
    try:
        with open(project_path, encoding="utf-8") as handle:
            settings = (json.load(handle) or {}).get("net_settings") or {}
    except (OSError, ValueError):
        return {}
    membership = {}
    for net, classes in (settings.get("netclass_assignments") or {}).items():
        names = classes if isinstance(classes, list) else [classes]
        chosen = next((name for name in names if name and name != "Default"), None)
        if chosen:
            membership[net] = chosen
    patterns = [
        (str(item.get("pattern", "")).upper(), str(item.get("netclass", "")))
        for item in settings.get("netclass_patterns") or () if isinstance(item, dict)
    ]
    for net in net_names:
        if net in membership:
            continue
        for pattern, netclass in patterns:
            if pattern and netclass and netclass != "Default" and fnmatch.fnmatchcase(net.upper(), pattern):
                membership[net] = netclass
                break
    return membership


class DifferentialPairDiscoverer:
    """Find likely differential pairs without mixing them with power rails."""

    _passive_prefixes = ("R", "C", "L", "FB")

    def __init__(self, board, log_callback=None, netclasses=None):
        self.board = board
        self.log_callback = log_callback
        # A callable ``(net_names) -> {net: netclass}``, from the project file.
        self.netclasses = netclasses

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _get_val(obj, name, default=None):
        if obj is None:
            return default
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None and not callable(value):
                return value
        method = getattr(obj, f"get_{name}", None)
        if callable(method):
            try:
                value = method()
                if value is not None:
                    return value
            except Exception:
                pass
        return default

    def _board_items(self, name):
        value = self._get_val(self.board, name, [])
        if isinstance(value, dict):
            return list(value.values())
        return list(value or [])

    def _footprint_reference(self, footprint):
        reference = self._get_val(footprint, "reference", self._get_val(footprint, "ref_des", ""))
        if reference:
            return str(reference)
        field = self._get_val(footprint, "reference_field")
        text = self._get_val(field, "text")
        return str(self._get_val(text, "value", "") or "")

    def _pads(self, footprint):
        pads = self._get_val(footprint, "pads")
        if pads is None:
            pads = self._get_val(self._get_val(footprint, "definition"), "pads", [])
        return list(pads or [])

    def _net_name(self, item):
        return str(self._get_val(self._get_val(item, "net"), "name", "") or "")

    @staticmethod
    def _pair_key(label):
        """Return a normalized base and P/N polarity for common conventions."""
        if not label:
            return None
        text = str(label).strip()
        upper = text.upper()

        match = re.match(r"^(.+?)([+-])$", upper)
        if match:
            return match.group(1), "P" if match.group(2) == "+" else "N"

        match = re.match(r"^(.+?)(?:[_\-.]?D)(P|M)$", upper)
        if match:
            return f"{match.group(1)}D", "P" if match.group(2) == "P" else "N"

        match = re.match(r"^(.+?)([_\-.])([PN])$", upper)
        if match:
            return match.group(1), match.group(3)

        match = re.match(r"^(.+?(?:TX|RX|CLK|DQS|DATA|LANE|USB|LVDS|MDI))([PN])$", upper)
        if match:
            return match.group(1), match.group(2)

        match = re.match(r"^(.+?)(?:[_\-.]?)(POS|NEG)$", upper)
        if match:
            return match.group(1), "P" if match.group(2) == "POS" else "N"
        return None

    @staticmethod
    def _classify_interface(*labels):
        text = " ".join(str(label).upper() for label in labels if label)
        rules = (
            ("PCIE", ("PCIE", "PCI_E", "PERST")),
            ("SATA", ("SATA",)),
            ("HDMI", ("HDMI", "TMDS")),
            ("MIPI", ("MIPI", "D-PHY", "DPHY")),
            ("ETHERNET", ("ETH", "MDI", "TRD", "1000BASE")),
            ("LVDS", ("LVDS",)),
            ("CAN", ("CANH", "CANL", "CAN_")),
            ("RS485", ("RS485", "RS422")),
            ("DDR", ("DDR", "DQS", "CK_T", "CK_C")),
            ("USB", ("USB", "D+", "D-", "DP", "DM")),
        )
        for interface, tokens in rules:
            if any(token in text for token in tokens):
                return interface
        return "GENERIC"

    def _all_net_names(self):
        names = set()
        for collection in ("tracks", "zones", "vias"):
            for item in self._board_items(collection):
                name = self._net_name(item)
                if name:
                    names.add(name)
        for footprint in self._board_items("footprints"):
            for pad in self._pads(footprint):
                name = self._net_name(pad)
                if name:
                    names.add(name)
        for net in self._board_items("nets"):
            name = str(self._get_val(net, "name", "") or "")
            if name:
                names.add(name)
        return sorted(names)

    def _passive_graph(self):
        graph = defaultdict(set)
        for footprint in self._board_items("footprints"):
            reference = self._footprint_reference(footprint).upper()
            if not reference.startswith(self._passive_prefixes):
                continue
            nets = []
            for pad in self._pads(footprint):
                name = self._net_name(pad)
                if name and name not in nets:
                    nets.append(name)
            if len(nets) == 2:
                graph[nets[0]].add((nets[1], reference))
                graph[nets[1]].add((nets[0], reference))
        return graph

    @staticmethod
    def _reachable_series_nets(net_name, graph, depth=2):
        found = {net_name: []}
        queue = deque([(net_name, [])])
        while queue:
            current, path = queue.popleft()
            if len(path) >= depth:
                continue
            for neighbor, reference in graph.get(current, set()):
                if neighbor in found:
                    continue
                new_path = path + [reference]
                found[neighbor] = new_path
                queue.append((neighbor, new_path))
        return found

    def _name_candidates(self):
        names = self._all_net_names()
        present = set(names)
        candidates, paired = [], set()
        for net_name in names:
            match = kicad_pair_complement(net_name)
            if match is None or match[1] != "P" or match[0] not in present:
                continue
            negative = match[0]
            # Drop the polarity character to name the pair.
            index = len(net_name) - 1
            while net_name[index].isdigit() or net_name[index] == "_":
                index -= 1
            base = net_name[:index] + net_name[index + 1:]
            candidates.append(self._make_candidate(
                base, net_name, negative, confidence="LIKELY",
                evidence=["kicad_pair_suffix"],
            ))
            paired.update((net_name, negative))

        grouped = defaultdict(dict)
        for net_name in names:
            if net_name in paired:
                continue
            parsed = self._pair_key(net_name)
            if parsed:
                base, polarity = parsed
                grouped[base][polarity] = net_name
        for base, polarities in grouped.items():
            if "P" not in polarities or "N" not in polarities:
                continue
            candidates.append(self._make_candidate(
                base, polarities["P"], polarities["N"],
                confidence="SUSPECTED",
                evidence=["matching-net-names"],
            ))
        return candidates

    def _apply_netclasses(self, candidates):
        """Raise pairs whose nets share a declared netclass, and take a target its name states."""
        if self.netclasses is None or not candidates:
            return
        try:
            membership = self.netclasses(sorted({net for c in candidates for net in (c.positive_net, c.negative_net)}))
        except Exception as exc:
            self.log(f"Netclasses could not be read for the pair scan ({exc}); names alone were used.")
            return
        for candidate in candidates:
            netclass = membership.get(candidate.positive_net)
            if not netclass or membership.get(candidate.negative_net) != netclass:
                continue
            candidate.evidence.append(f"netclass:{netclass}")
            if candidate.confidence == "SUSPECTED":
                candidate.confidence = "LIKELY"
            impedance = _IMPEDANCE_IN_NAME_RE.search(netclass)
            if impedance:
                candidate.target_impedance_ohm = float(impedance.group(1).replace(",", "."))
                candidate.evidence.append(f"target-from-netclass-name:{netclass}")

    def _pin_function_candidates(self):
        candidates = []
        graph = self._passive_graph()
        for footprint in self._board_items("footprints"):
            reference = self._footprint_reference(footprint)
            grouped = defaultdict(dict)
            for pad in self._pads(footprint):
                function = ""
                for attr in ("pin_function", "pinfunction", "function", "pin_name"):
                    function = self._get_val(pad, attr, "")
                    if function:
                        break
                parsed = self._pair_key(function)
                net_name = self._net_name(pad)
                if parsed and net_name:
                    base, polarity = parsed
                    grouped[base][polarity] = (net_name, str(function))
            for base, polarities in grouped.items():
                if "P" not in polarities or "N" not in polarities:
                    continue
                positive, positive_function = polarities["P"]
                negative, negative_function = polarities["N"]
                positive_reachable = self._reachable_series_nets(positive, graph)
                negative_reachable = self._reachable_series_nets(negative, graph)
                traced = None
                for p_net, p_path in positive_reachable.items():
                    p_key = self._pair_key(p_net)
                    if not p_key or p_key[1] != "P":
                        continue
                    for n_net, n_path in negative_reachable.items():
                        n_key = self._pair_key(n_net)
                        if n_key and n_key[1] == "N" and p_key[0] == n_key[0]:
                            traced = (p_net, n_net, p_path + n_path)
                            break
                    if traced:
                        break
                evidence = [f"pin-functions:{reference}:{positive_function}/{negative_function}"]
                if traced and (traced[0], traced[1]) != (positive, negative):
                    positive, negative = traced[0], traced[1]
                    evidence.append("series-path:" + ",".join(sorted(set(traced[2]))))
                candidates.append(self._make_candidate(
                    base, positive, negative, confidence="LIKELY", evidence=evidence,
                ))
        return candidates

    def _make_candidate(self, base, positive, negative, confidence, evidence):
        interface = self._classify_interface(base, positive, negative, *evidence)
        target, swappable = INTERFACE_DEFAULTS[interface]
        name = str(base).strip("_-. ") or f"{positive}/{negative}"
        return DifferentialPairCandidate(
            name=name,
            positive_net=positive,
            negative_net=negative,
            interface=interface,
            target_impedance_ohm=target,
            confidence=confidence,
            evidence=list(evidence),
            source="auto",
            polarity_swappable=swappable,
        )

    def discover(self, existing_pairs=None, ignored_signatures=None):
        """Discover and merge candidates while preserving user decisions."""
        ignored = set(ignored_signatures or [])
        merged = {}
        found = self._name_candidates() + self._pin_function_candidates()
        self._apply_netclasses(found)
        for candidate in found:
            if candidate.signature in ignored:
                continue
            previous = merged.get(candidate.signature)
            if previous is None:
                merged[candidate.signature] = candidate
                continue
            for evidence in candidate.evidence:
                if evidence not in previous.evidence:
                    previous.evidence.append(evidence)
            if candidate.confidence == "LIKELY":
                previous.confidence = "LIKELY"
            if previous.interface == "GENERIC" and candidate.interface != "GENERIC":
                previous.interface = candidate.interface
                previous.target_impedance_ohm = candidate.target_impedance_ohm
                previous.polarity_swappable = candidate.polarity_swappable

        existing = {pair.signature: pair for pair in (existing_pairs or [])}
        for signature, candidate in list(merged.items()):
            saved = existing.get(signature)
            if saved is None:
                continue
            candidate.name = saved.name
            candidate.interface = saved.interface
            candidate.target_impedance_ohm = saved.target_impedance_ohm
            candidate.enabled = saved.enabled
            candidate.polarity_swappable = saved.polarity_swappable
            if saved.confidence in {"CONFIRMED", "MANUAL"}:
                candidate.confidence = saved.confidence
            if saved.source == "manual":
                candidate.source = "manual"
        for signature, saved in existing.items():
            if signature not in merged and saved.source == "manual" and signature not in ignored:
                merged[signature] = saved

        result = sorted(merged.values(), key=lambda pair: (pair.interface, pair.name))
        self.log(f"Differential scan complete. Discovered {len(result)} pair candidates.")
        return result
