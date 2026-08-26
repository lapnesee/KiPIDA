"""wxPython configuration panel for Phase 5 differential-pair analysis."""

import wx

try:
    from differential_discovery import DifferentialPairDiscoverer, INTERFACE_DEFAULTS
    from differential_recommender import DifferentialRecommendationEngine
    from design_rule_injector import DifferentialRuleInjector
    from extractor import GeometryExtractor
    from models import DifferentialAnalysisSettings, DifferentialPairCandidate
    from stackup_io import load_stackup_profile
except (ImportError, ValueError):
    from ..differential_discovery import DifferentialPairDiscoverer, INTERFACE_DEFAULTS
    from ..differential_recommender import DifferentialRecommendationEngine
    from ..design_rule_injector import DifferentialRuleInjector
    from ..extractor import GeometryExtractor
    from ..models import DifferentialAnalysisSettings, DifferentialPairCandidate
    from ..stackup_io import load_stackup_profile


class DifferentialPairDialog(wx.Dialog):
    def __init__(self, parent, pair=None):
        super().__init__(parent, title="Edit Differential Pair" if pair else "Add Differential Pair")
        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1, 1)
        self.name = wx.TextCtrl(self)
        self.positive = wx.TextCtrl(self)
        self.negative = wx.TextCtrl(self)
        self.interface = wx.Choice(self, choices=sorted(INTERFACE_DEFAULTS))
        self.interface.SetStringSelection("GENERIC")
        self.target = wx.TextCtrl(self, value="100")
        for label, control in (
            ("Name:", self.name),
            ("Positive net:", self.positive),
            ("Negative net:", self.negative),
            ("Interface:", self.interface),
            ("Target Zdiff (ohm):", self.target),
        ):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((430, 280))
        if pair is not None:
            self.name.SetValue(pair.name)
            self.positive.SetValue(pair.positive_net)
            self.negative.SetValue(pair.negative_net)
            self.interface.SetStringSelection(pair.interface if pair.interface in INTERFACE_DEFAULTS else "GENERIC")
            self.target.SetValue(f"{pair.target_impedance_ohm:g}")

    def get_pair(self):
        positive = self.positive.GetValue().strip()
        negative = self.negative.GetValue().strip()
        if not positive or not negative or positive == negative:
            raise ValueError("Enter two different, non-empty net names.")
        target = float(self.target.GetValue())
        if target <= 0:
            raise ValueError("Target impedance must be positive.")
        interface = self.interface.GetStringSelection() or "GENERIC"
        return DifferentialPairCandidate(
            name=self.name.GetValue().strip() or f"{positive}/{negative}",
            positive_net=positive,
            negative_net=negative,
            interface=interface,
            target_impedance_ohm=target,
            confidence="MANUAL",
            evidence=["user-defined"],
            source="manual",
            polarity_swappable=INTERFACE_DEFAULTS[interface][1],
        )


class DifferentialAnalysisPanel(wx.Panel):
    """Separate detected-pair and stackup configuration surface."""

    def __init__(self, parent, board, project=None, log_callback=None):
        super().__init__(parent)
        self.board = board
        self.project = project
        self.log_callback = log_callback
        self.settings = DifferentialAnalysisSettings()
        self.discoverer = DifferentialPairDiscoverer(board, log_callback=log_callback)
        self.extractor = GeometryExtractor(board, log_callback=log_callback)
        self.stackup = None
        self.results = {}
        self.recommendations = {}
        self._recommendation_rows = []
        self._init_ui()

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _init_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        stack_box = wx.StaticBoxSizer(wx.VERTICAL, self, "PCB Stackup")
        stack_parent = stack_box.GetStaticBox()
        stack_row = wx.BoxSizer(wx.HORIZONTAL)
        self.stackup_status = wx.StaticText(stack_parent, label="Stackup not loaded")
        self.btn_stackup_refresh = wx.Button(stack_parent, label="Refresh from KiCad")
        self.btn_stackup_import = wx.Button(stack_parent, label="Import JSON")
        stack_row.Add(self.stackup_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        stack_row.Add(self.btn_stackup_refresh, 0, wx.RIGHT, 5)
        stack_row.Add(self.btn_stackup_import, 0)
        stack_box.Add(stack_row, 0, wx.EXPAND | wx.ALL, 6)
        self.stackup_list = wx.ListCtrl(stack_parent, style=wx.LC_REPORT)
        for index, (title, width) in enumerate((
            ("Layer", 160), ("Kind", 90), ("Thickness (mm)", 120),
            ("Material", 120), ("Er", 65), ("KiCad ID", 75),
        )):
            self.stackup_list.InsertColumn(index, title, width=width)
        stack_box.Add(self.stackup_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        main.Add(stack_box, 0, wx.EXPAND | wx.ALL, 5)

        pair_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Detected Differential Pairs")
        pair_parent = pair_box.GetStaticBox()
        self.pair_list = wx.ListCtrl(pair_parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (title, width) in enumerate((
            ("Use", 48), ("Pair", 115), ("Positive net", 145), ("Negative net", 145),
            ("Interface", 85), ("Confidence", 90), ("Target", 70),
            ("Zdiff", 75), ("Status", 85), ("Recommendation", 130),
        )):
            self.pair_list.InsertColumn(index, title, width=width)
        pair_box.Add(self.pair_list, 1, wx.EXPAND | wx.ALL, 5)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_scan = wx.Button(pair_parent, label="Scan Board")
        self.btn_add = wx.Button(pair_parent, label="Add Manual Pair")
        self.btn_edit = wx.Button(pair_parent, label="Edit Selected")
        self.btn_confirm = wx.Button(pair_parent, label="Confirm Selected")
        self.btn_toggle = wx.Button(pair_parent, label="Enable / Disable")
        self.btn_ignore = wx.Button(pair_parent, label="Ignore Selected")
        for button in (self.btn_scan, self.btn_add, self.btn_edit, self.btn_confirm, self.btn_toggle, self.btn_ignore):
            buttons.Add(button, 0, wx.RIGHT, 5)
        pair_box.Add(buttons, 0, wx.ALL, 5)
        main.Add(pair_box, 1, wx.EXPAND | wx.ALL, 5)

        recommendation_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Geometry Recommendations")
        recommendation_parent = recommendation_box.GetStaticBox()
        # wx.ListCtrl permits multiple selections unless LC_SINGLE_SEL is set.
        self.recommendation_list = wx.ListCtrl(recommendation_parent, style=wx.LC_REPORT)
        for index, (title, width) in enumerate((
            ("Pair", 120), ("Layer", 85), ("Action", 145), ("Current W/G", 105),
            ("Suggested W/G", 120), ("Predicted Z", 90), ("Ground clearance", 120), ("Confidence", 90),
        )):
            self.recommendation_list.InsertColumn(index, title, width=width)
        recommendation_box.Add(self.recommendation_list, 0, wx.EXPAND | wx.ALL, 5)
        recommendation_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_recommend = wx.Button(recommendation_parent, label="Generate Recommendations")
        self.btn_apply_rules = wx.Button(recommendation_parent, label="Apply Selected to KiCad Rules")
        recommendation_buttons.Add(self.btn_recommend, 0, wx.RIGHT, 5)
        recommendation_buttons.Add(self.btn_apply_rules, 0)
        recommendation_box.Add(recommendation_buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        main.Add(recommendation_box, 0, wx.EXPAND | wx.ALL, 5)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(wx.StaticText(self, label="Target tolerance (%):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.txt_tolerance = wx.TextCtrl(self, value="10", size=(65, -1))
        controls.Add(self.txt_tolerance, 0, wx.RIGHT, 16)
        controls.Add(wx.StaticText(self, label="Ground reference nets:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.txt_reference_nets = wx.TextCtrl(self, value="GND, AGND, DGND, PGND")
        controls.Add(self.txt_reference_nets, 1)
        controls.Add(wx.StaticText(self, label="Min W/G/GND (mm):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
        self.txt_min_geometry = wx.TextCtrl(self, value="0.10 / 0.10 / 0.15", size=(130, -1))
        controls.Add(self.txt_min_geometry, 0)
        main.Add(controls, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(main)

        self.btn_scan.Bind(wx.EVT_BUTTON, lambda event: self.refresh(force_scan=True))
        self.btn_add.Bind(wx.EVT_BUTTON, self._on_add)
        self.btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        self.btn_confirm.Bind(wx.EVT_BUTTON, self._on_confirm)
        self.btn_toggle.Bind(wx.EVT_BUTTON, self._on_toggle)
        self.btn_ignore.Bind(wx.EVT_BUTTON, self._on_ignore)
        self.btn_stackup_refresh.Bind(wx.EVT_BUTTON, self._on_stackup_refresh)
        self.btn_stackup_import.Bind(wx.EVT_BUTTON, self._on_stackup_import)
        self.btn_recommend.Bind(wx.EVT_BUTTON, self._on_recommend)
        self.btn_apply_rules.Bind(wx.EVT_BUTTON, self._on_apply_rules)

    def _selected_pair(self):
        index = self.pair_list.GetFirstSelected()
        if index < 0 or index >= len(self.settings.pairs):
            return None
        return self.settings.pairs[index]

    def _update_pair_list(self):
        self.pair_list.DeleteAllItems()
        for pair in self.settings.pairs:
            result = self.results.get(pair.signature)
            row = self.pair_list.InsertItem(self.pair_list.GetItemCount(), "Yes" if pair.enabled else "No")
            values = (
                pair.name, pair.positive_net, pair.negative_net, pair.interface,
                pair.confidence, f"{pair.target_impedance_ohm:g}",
                f"{result.weighted_impedance_ohm:.2f}" if result else "-",
                result.status if result else "Not run",
                (result.recommendations[0].action if result and result.recommendations else "Not run"),
            )
            for column, value in enumerate(values, start=1):
                self.pair_list.SetItem(row, column, str(value))

    def _update_recommendation_list(self):
        self.recommendation_list.DeleteAllItems()
        self._recommendation_rows = []
        for result in self.results.values():
            for recommendation in result.recommendations:
                row = self.recommendation_list.InsertItem(self.recommendation_list.GetItemCount(), recommendation.pair_name)
                values = (
                    recommendation.layer_name or "-", recommendation.action,
                    f"{recommendation.current_width_mm:.3f}/{recommendation.current_gap_mm:.3f}",
                    f"{recommendation.recommended_width_mm:.3f}/{recommendation.recommended_gap_mm:.3f}" if recommendation.recommended_width_mm else "-",
                    f"{recommendation.predicted_impedance_ohm:.2f}" if recommendation.predicted_impedance_ohm else "-",
                    f"{recommendation.recommended_ground_clearance_mm:.3f}" if recommendation.recommended_ground_clearance_mm else "-",
                    recommendation.confidence,
                )
                for column, value in enumerate(values, start=1):
                    self.recommendation_list.SetItem(row, column, value)
                self._recommendation_rows.append(recommendation)

    def _update_stackup_list(self):
        self.stackup_list.DeleteAllItems()
        if self.stackup is None:
            self.stackup_status.SetLabel("Stackup not loaded")
            return
        trust = "trusted" if self.stackup.trustworthy else "estimate only"
        self.stackup_status.SetLabel(f"Source: {self.stackup.source} ({trust})")
        for layer in self.stackup.layers:
            row = self.stackup_list.InsertItem(self.stackup_list.GetItemCount(), layer.name)
            for column, value in enumerate((
                layer.kind, f"{layer.thickness_mm:g}", layer.material,
                f"{layer.epsilon_r:g}", "" if layer.layer_id is None else str(layer.layer_id),
            ), start=1):
                self.stackup_list.SetItem(row, column, value)

    def refresh(self, force_scan=False):
        if self.stackup is None:
            self.stackup = self.settings.stackup_override or self.extractor.get_stackup_profile()
            self._update_stackup_list()
        if force_scan or not self.settings.pairs:
            self.settings.pairs = self.discoverer.discover(
                existing_pairs=self.settings.pairs,
                ignored_signatures=self.settings.ignored_pair_signatures,
            )
            self.results = {}
            self.recommendations = {}
        self._update_pair_list()
        self._update_recommendation_list()

    def _on_add(self, event):
        dialog = DifferentialPairDialog(self)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            pair = dialog.get_pair()
            if any(existing.signature == pair.signature for existing in self.settings.pairs):
                raise ValueError("This net pair already exists.")
            self.settings.pairs.append(pair)
            self.settings.pairs.sort(key=lambda item: (item.interface, item.name))
            self._update_pair_list()
            self._update_recommendation_list()
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid Differential Pair", wx.OK | wx.ICON_ERROR)
        finally:
            dialog.Destroy()

    def _on_confirm(self, event):
        pair = self._selected_pair()
        if pair:
            pair.confidence = "CONFIRMED"
            if "user-confirmed" not in pair.evidence:
                pair.evidence.append("user-confirmed")
            self._update_pair_list()
            self._update_recommendation_list()

    def _on_edit(self, event):
        pair = self._selected_pair()
        if pair is None:
            return
        index = self.settings.pairs.index(pair)
        dialog = DifferentialPairDialog(self, pair=pair)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            edited = dialog.get_pair()
            if any(
                existing.signature == edited.signature and existing is not pair
                for existing in self.settings.pairs
            ):
                raise ValueError("This net pair already exists.")
            edited.evidence = list(pair.evidence)
            if "user-edited" not in edited.evidence:
                edited.evidence.append("user-edited")
            edited.enabled = pair.enabled
            edited.confidence = "CONFIRMED"
            self.settings.pairs[index] = edited
            self.results = {}
            self.recommendations = {}
            self._update_pair_list()
            self._update_recommendation_list()
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid Differential Pair", wx.OK | wx.ICON_ERROR)
        finally:
            dialog.Destroy()

    def _on_toggle(self, event):
        pair = self._selected_pair()
        if pair:
            pair.enabled = not pair.enabled
            self._update_pair_list()

    def _on_ignore(self, event):
        pair = self._selected_pair()
        if pair:
            if pair.signature not in self.settings.ignored_pair_signatures:
                self.settings.ignored_pair_signatures.append(pair.signature)
            self.settings.pairs.remove(pair)
            self.results.pop(pair.signature, None)
            self._update_pair_list()
            self._update_recommendation_list()

    def _on_stackup_refresh(self, event):
        try:
            self.extractor.invalidate_stackup_cache()
            self.stackup = self.extractor.get_stackup_profile()
            self.settings.stackup_override = None
            self._update_stackup_list()
            for warning in self.stackup.warnings:
                self.log(f"Stackup warning: {warning}")
        except Exception as exc:
            wx.MessageBox(str(exc), "Stackup Error", wx.OK | wx.ICON_ERROR)

    def _on_stackup_import(self, event):
        dialog = wx.FileDialog(
            self, message="Import Ki-PIDA stackup", wildcard="JSON files (*.json)|*.json",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            self.stackup = load_stackup_profile(dialog.GetPath())
            self.settings.stackup_override = self.stackup
            self._update_stackup_list()
            self.log(f"Imported stackup from {dialog.GetFilename()} ({len(self.stackup.layers)} layers).")
        except Exception as exc:
            wx.MessageBox(str(exc), "Stackup Import Error", wx.OK | wx.ICON_ERROR)
        finally:
            dialog.Destroy()

    def get_settings(self):
        tolerance = float(self.txt_tolerance.GetValue())
        if tolerance <= 0:
            raise ValueError("Target tolerance must be positive.")
        self.settings.target_tolerance_pct = tolerance
        try:
            minimums = [float(value.strip()) for value in self.txt_min_geometry.GetValue().split("/")]
            if len(minimums) != 3 or min(minimums) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Min W/G/GND must contain three positive mm values, e.g. 0.10 / 0.10 / 0.15.")
        self.settings.minimum_width_mm, self.settings.minimum_gap_mm, self.settings.minimum_ground_clearance_mm = minimums
        self.settings.reference_net_names = [
            item.strip() for item in self.txt_reference_nets.GetValue().split(",") if item.strip()
        ]
        if not self.settings.reference_net_names:
            raise ValueError("Enter at least one ground reference net.")
        self.settings.stackup_override = self.stackup if self.stackup and self.stackup.source == "IMPORTED" else None
        return self.settings

    def set_settings(self, settings):
        self.settings = settings or DifferentialAnalysisSettings()
        self.stackup = self.settings.stackup_override
        self.txt_tolerance.SetValue(f"{self.settings.target_tolerance_pct:g}")
        self.txt_reference_nets.SetValue(", ".join(self.settings.reference_net_names))
        self.txt_min_geometry.SetValue(
            f"{self.settings.minimum_width_mm:g} / {self.settings.minimum_gap_mm:g} / {self.settings.minimum_ground_clearance_mm:g}"
        )
        self.results = {}
        self.recommendations = {}
        if settings is not None:
            self.refresh(force_scan=False)
        else:
            self._update_pair_list()
            self._update_stackup_list()

    def get_stackup(self):
        if self.stackup is None:
            self.refresh(force_scan=False)
        return self.stackup

    def apply_results(self, results):
        self.results = {result.pair.signature: result for result in results}
        DifferentialRecommendationEngine(self.settings).recommend(results)
        self.recommendations = {
            result.pair.signature: list(result.recommendations) for result in results
        }
        self._update_pair_list()
        self._update_recommendation_list()

    def _on_recommend(self, event):
        if not self.results:
            wx.MessageBox("Run differential impedance analysis first.", "Recommendations", wx.OK | wx.ICON_INFORMATION)
            return
        DifferentialRecommendationEngine(self.settings).recommend(self.results.values())
        self._update_pair_list()
        self._update_recommendation_list()

    def _selected_recommendations(self):
        selected = []
        row = self.recommendation_list.GetFirstSelected()
        while row >= 0:
            if row < len(self._recommendation_rows):
                selected.append(self._recommendation_rows[row])
            row = self.recommendation_list.GetNextSelected(row)
        return selected

    def _on_apply_rules(self, event):
        recommendations = self._selected_recommendations()
        if not recommendations:
            wx.MessageBox("Select one or more geometry recommendations first.", "KiCad Rules", wx.OK | wx.ICON_INFORMATION)
            return
        project_path = getattr(self.project, "path", None)
        if not project_path:
            wx.MessageBox("KiCad project path is unavailable; rules were not changed.", "KiCad Rules", wx.OK | wx.ICON_ERROR)
            return
        preview = "\n".join(
            f"- {item.pair_name}: {item.recommended_width_mm:.3f} mm / {item.recommended_gap_mm:.3f} mm"
            for item in recommendations
        )
        if wx.MessageBox(
            "Create/update KiPIDA_DIFF net classes and predefined differential sizes in this project?\n\n" + preview,
            "Apply KiCad Design Rules", wx.YES_NO | wx.ICON_QUESTION,
        ) != wx.YES:
            return
        try:
            applied = DifferentialRuleInjector(project_path).apply(recommendations)
            self.log("Applied KiCad differential classes: " + ", ".join(name for _, name, _, _ in applied))
            wx.MessageBox(
                "KiCad project rules were updated. Open Board Setup or rerun DRC to inspect the new KiPIDA_DIFF classes.\n"
                "The current Ki-PIDA session can continue analysing immediately.",
                "KiCad Rules Applied", wx.OK | wx.ICON_INFORMATION,
            )
        except Exception as exc:
            self.log(f"Failed to apply KiCad differential rules: {exc}")
            wx.MessageBox(str(exc), "KiCad Rules", wx.OK | wx.ICON_ERROR)

    def refresh_live_board(self):
        """Drop derived live-board caches while retaining user confirmation and overrides."""
        self.extractor.invalidate_stackup_cache()
        if self.settings.stackup_override is None:
            self.stackup = None
        self.refresh(force_scan=True)
