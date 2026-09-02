"""Write selected Ki-PIDA differential recommendations into KiCad project rules."""

import json
import re
from pathlib import Path


def _class_name(recommendation):
    token = re.sub(r"[^A-Za-z0-9_]+", "_", recommendation.pair_name.upper()).strip("_")
    return f"KiPIDA_DIFF_{token or 'PAIR'}"


class DifferentialRuleInjector:
    """Update only Ki-PIDA-owned net classes and preserve user project settings."""

    def __init__(self, project_path, project_api=None):
        path = Path(project_path)
        if path.suffix.lower() not in {".kicad_pro", ".pro"}:
            raise ValueError("KiCad project file path is required to inject design rules.")
        self.project_path = path
        self.project_api = project_api
        self.live_applied = False
        self.live_error = None

    @staticmethod
    def _recommended_items(recommendations):
        return [item for item in recommendations if item.recommended_width_mm > 0 and item.recommended_gap_mm > 0]

    def apply(self, recommendations, minimum_width_mm=0.0, minimum_gap_mm=0.0,
              minimum_ground_clearance_mm=0.0):
        recommendations = self._recommended_items(recommendations)
        if not recommendations:
            raise ValueError("Select at least one recommendation with a feasible width and gap.")
        violations = []
        for item in recommendations:
            failed = []
            if item.recommended_width_mm + 1e-12 < minimum_width_mm:
                failed.append(f"W {item.recommended_width_mm:.3f} < {minimum_width_mm:.3f}")
            if item.recommended_gap_mm + 1e-12 < minimum_gap_mm:
                failed.append(f"G {item.recommended_gap_mm:.3f} < {minimum_gap_mm:.3f}")
            if item.recommended_ground_clearance_mm + 1e-12 < minimum_ground_clearance_mm:
                failed.append(
                    f"GND {item.recommended_ground_clearance_mm:.3f} < "
                    f"{minimum_ground_clearance_mm:.3f}"
                )
            if failed:
                violations.append(f"{item.pair_name}: " + ", ".join(failed))
        if violations:
            raise ValueError(
                "Recommendations below the configured W/G/GND minima were not applied: "
                + "; ".join(violations)
            )
        try:
            project = json.loads(self.project_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"KiCad project file was not found: {self.project_path}") from exc
        net_settings = project.setdefault("net_settings", {})
        classes = list(net_settings.get("classes") or [])
        patterns = list(net_settings.get("netclass_patterns") or [])
        existing_names = {str(entry.get("name", "")) for entry in classes}
        base = next((entry for entry in classes if entry.get("name") == "Default"), {})
        # KiCad reserves INT32_MAX for the Default class.  Including that
        # sentinel used to create priority 2147483648, which KiCad rejects.
        ordinary_priorities = [
            int(entry.get("priority", 0)) for entry in classes
            if isinstance(entry, dict) and entry.get("name") != "Default"
            and 0 <= int(entry.get("priority", 0)) < 2147483647
        ]
        next_priority = min(max(ordinary_priorities, default=-1) + 1, 2147483646)
        applied = []
        for recommendation in recommendations:
            class_name = _class_name(recommendation)
            pair = recommendation.pair_name
            # The GUI keeps the matching result under the pair signature; split it
            # here so each class is assigned to its exact two KiCad net names.
            nets = [net for net in recommendation.pair_signature.split("|") if net]
            if len(nets) != 2:
                continue
            rule = dict(base)
            rule.update({
                "name": class_name,
                "track_width": round(float(recommendation.recommended_width_mm), 6),
                "diff_pair_width": round(float(recommendation.recommended_width_mm), 6),
                "diff_pair_gap": round(float(recommendation.recommended_gap_mm), 6),
                "clearance": round(float(recommendation.recommended_ground_clearance_mm), 6),
                "priority": next_priority if class_name not in existing_names else next(
                    (entry.get("priority", next_priority) for entry in classes if entry.get("name") == class_name), next_priority
                ),
                "pcb_color": "rgba(100, 60, 200, 0.800)",
                "schematic_color": "rgba(100, 60, 200, 0.800)",
            })
            classes = [entry for entry in classes if entry.get("name") != class_name]
            classes.append(rule)
            patterns = [entry for entry in patterns if not (
                entry.get("netclass") == class_name and entry.get("pattern") in nets
            )]
            patterns.extend({"netclass": class_name, "pattern": net} for net in nets)
            applied.append((pair, class_name, rule["track_width"], rule["diff_pair_gap"]))
            existing_names.add(class_name)
            next_priority += 1
        if not applied:
            raise ValueError("Selected recommendations did not contain valid differential net pairs.")
        net_settings["classes"] = classes
        net_settings["netclass_patterns"] = patterns
        board_settings = project.setdefault("board", {}).setdefault("design_settings", {})
        widths = list(board_settings.get("track_widths") or [0.0])
        dimensions = list(board_settings.get("diff_pair_dimensions") or [{"width": 0.0, "gap": 0.0, "via_gap": 0.0}])
        for _, _, width, gap in applied:
            if not any(abs(float(value) - width) < 1e-9 for value in widths):
                widths.append(width)
            if not any(abs(float(item.get("width", 0.0)) - width) < 1e-9 and abs(float(item.get("gap", 0.0)) - gap) < 1e-9 for item in dimensions):
                dimensions.append({"width": width, "gap": gap, "via_gap": max(gap, 0.2)})
        board_settings["track_widths"] = sorted(widths)
        board_settings["diff_pair_dimensions"] = sorted(dimensions, key=lambda item: (item.get("width", 0.0), item.get("gap", 0.0)))
        self.project_path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
        if self.project_api is not None:
            try:
                self._apply_live(applied, classes, patterns)
            except Exception as exc:
                # The file update remains useful on older/incomplete IPC APIs.
                self.live_error = str(exc)
        return applied

    def _apply_live(self, applied, classes, patterns):
        """Mirror generated classes into the open KiCad project through IPC."""
        if not all(hasattr(self.project_api, name) for name in (
            "get_net_classes", "set_net_classes",
            "get_net_class_assignments", "set_net_class_assignments",
        )):
            return
        try:
            from kipy.project_types import NetClass
        except ImportError:
            return
        generated_names = {class_name for _, class_name, _, _ in applied}
        generated_rules = {
            entry["name"]: entry for entry in classes
            if isinstance(entry, dict) and entry.get("name") in generated_names
        }
        existing = {item.name: item for item in self.project_api.get_net_classes()}
        live_classes = []
        for class_name, rule in generated_rules.items():
            net_class = existing.get(class_name) or NetClass()
            net_class.name = class_name
            net_class.priority = int(rule["priority"])
            net_class.clearance = int(round(float(rule["clearance"]) * 1_000_000))
            net_class.track_width = int(round(float(rule["track_width"]) * 1_000_000))
            net_class.diff_pair_track_width = int(round(float(rule["diff_pair_width"]) * 1_000_000))
            net_class.diff_pair_gap = int(round(float(rule["diff_pair_gap"]) * 1_000_000))
            net_class.diff_pair_via_gap = int(round(float(rule.get("diff_pair_via_gap", rule["diff_pair_gap"])) * 1_000_000))
            live_classes.append(net_class)
        self.project_api.set_net_classes(live_classes)
        current_assignments = list(self.project_api.get_net_class_assignments() or [])
        generated_pairs = {
            (entry.get("pattern"), entry.get("netclass"))
            for entry in patterns if entry.get("netclass") in generated_names
        }
        current_assignments = [
            entry for entry in current_assignments
            if entry.get("netclass") not in generated_names
        ]
        current_assignments.extend(
            {"pattern": pattern, "netclass": netclass}
            for pattern, netclass in sorted(generated_pairs)
        )
        self.project_api.set_net_class_assignments(current_assignments)
        self.live_applied = True
