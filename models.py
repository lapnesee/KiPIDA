from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ComponentRef:
    """Reference to a specific footprint component on the board."""
    ref_des: str
    
    def __hash__(self):
        return hash(self.ref_des)
    
    def __eq__(self, other):
        if not isinstance(other, ComponentRef):
            return False
        return self.ref_des == other.ref_des

@dataclass
class UnifiedSource:
    """
    Represents a component acting as a voltage source.
    Voltage is defined by the parent PowerRail.
    """
    component_ref: ComponentRef
    pad_names: List[str] = field(default_factory=list)

@dataclass
class UnifiedLoad:
    """
    Represents a component acting as a current load.
    distribution_mode: 'UNIFORM' divides current equally among enabled pads.
    thermal_mode controls whether the electrical load is also dissipated on
    this PCB: AUTO treats connector references (J*) as exported power, LOCAL
    converts V * I to heat, and EXTERNAL excludes it from local heat sources.
    """
    component_ref: ComponentRef
    total_current: float = 0.0
    pad_names: List[str] = field(default_factory=list)
    distribution_mode: str = "UNIFORM"
    thermal_mode: str = "AUTO"

@dataclass
class VoltageRegulator:
    """
    Represents a voltage regulator connecting two PowerRails.
    Input/Output are defined by component RefDes and specific pads.
    """
    name: str  # Name of the regulator instance (e.g. "Buck 1")
    
    input_rail_name: str
    input_ref_des: str
    input_pad_names: List[str]
    
    output_rail_name: str
    output_ref_des: str
    output_pad_names: List[str]
    
    reg_type: str = "LINEAR"  # "LINEAR" or "SWITCHING"
    efficiency: float = 0.85  # Only used if SWITCHING. 0.0-1.0
    # Physical component receiving the estimated conversion loss. Connectivity
    # output references are often inductors, so they must not double as a
    # thermal placement hint. Empty means the input component.
    thermal_ref_des: str = ""
    # Optional physical loss model.  Older project files omit it and retain
    # the legacy efficiency fallback in ``efficiency``.
    loss_model: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PowerParameter:
    """A scalar used by the power model together with its evidence.

    ``source`` is deliberately free text: datasheet, footprint, user
    configuration and estimate are all useful distinctions in a report.
    """
    value: float
    source: str = "estimate"
    confidence: str = "low"
    reference: str = ""
    condition: str = ""
    typical_or_max: str = ""


@dataclass
class LossContribution:
    ref_des: str
    mechanism: str
    power_w: float
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PowerStageResult:
    """Traceable power accounting for one regulator or pass device."""
    name: str
    input_ref_des: str
    output_ref_des: str
    vin_v: float
    vout_v: float
    iin_a: float
    iout_a: float
    efficiency: float
    efficiency_provenance: str
    losses: List[LossContribution] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    balance_relative_error_pct: float = 0.0

    @property
    def total_loss_w(self) -> float:
        return sum(max(0.0, item.power_w) for item in self.losses)

@dataclass
class PowerRail:
    """
    High-level representation of a power domain.
    """
    net_name: str
    nominal_voltage: float = 0.0
    sources: List[UnifiedSource] = field(default_factory=list)
    loads: List[UnifiedLoad] = field(default_factory=list)
    # Regulators where this rail is the INPUT
    child_regulators: List[VoltageRegulator] = field(default_factory=list)
    
    def add_source(self, source: UnifiedSource):
        self.sources.append(source)
    
    def add_load(self, load: UnifiedLoad):
        self.loads.append(load)
    
    def add_child_regulator(self, reg: VoltageRegulator):
        self.child_regulators.append(reg)


@dataclass
class MeshBranch:
    """Physical branch retained for frequency-domain analysis."""
    node_a: int
    node_b: int
    resistance_ohm: float
    inductance_h: float = 0.0
    kind: str = "lateral"
    cross_section_mm2: float = 0.0
    geometry_source: str = ""


@dataclass
class ACSourceModel:
    """Small-signal source impedance between a rail and its return net."""
    ref_des: str = ""
    rail_pad_names: List[str] = field(default_factory=list)
    ground_pad_names: List[str] = field(default_factory=list)
    resistance_ohm: float = 0.01
    inductance_h: float = 1.0e-9


@dataclass
class ACMeasurementPort:
    """Differential rail-to-ground observation port."""
    ref_des: str = ""
    rail_pad_names: List[str] = field(default_factory=list)
    ground_pad_names: List[str] = field(default_factory=list)


@dataclass
class CapacitorModel:
    """Lumped RLC model for a mounted decoupling capacitor."""
    ref_des: str
    rail_pad_names: List[str] = field(default_factory=list)
    ground_pad_names: List[str] = field(default_factory=list)
    capacitance_f: float = 0.0
    esr_ohm: float = 0.01
    esl_h: float = 0.8e-9
    enabled: bool = True
    candidate: bool = False
    model_source: str = "estimated"


@dataclass
class ACAnalysisSettings:
    """Persisted settings for one rail impedance profile."""
    rail_name: str = ""
    ground_net_name: str = "GND"
    frequency_start_hz: float = 1.0e3
    frequency_stop_hz: float = 1.0e8
    frequency_points: int = 121
    mesh_resolution_mm: float = 0.5
    # Zero means "derive from the rail" (see ac_model.resolve_target_impedance).
    # The former 0.05 default decided pass/fail on every board without anyone
    # choosing it, which is the arbitrary-default problem the audit records: a
    # target only means something once a rail voltage and a load current exist
    # to compute it from.
    target_impedance_ohm: float = 0.0
    # Inputs to that derivation, both design decisions rather than board facts.
    ripple_fraction: float = 0.02
    transient_fraction: float = 0.5
    # Filled in once the target is resolved, so the report can state where the
    # number came from instead of presenting a derived value as a chosen one.
    target_impedance_provenance: str = ""
    source: ACSourceModel = field(default_factory=ACSourceModel)
    measurement_port: ACMeasurementPort = field(default_factory=ACMeasurementPort)
    capacitors: List[CapacitorModel] = field(default_factory=list)
    optimizer_values_f: List[float] = field(
        default_factory=lambda: [10e-9, 47e-9, 100e-9, 470e-9, 1e-6, 4.7e-6, 10e-6]
    )
    optimizer_max_additions: int = 8


@dataclass
class DifferentialPairCandidate:
    """One differential-pair candidate, independent from power-rail models."""
    name: str
    positive_net: str
    negative_net: str
    interface: str = "GENERIC"
    target_impedance_ohm: float = 100.0
    confidence: str = "SUSPECTED"  # CONFIRMED, LIKELY, SUSPECTED, MANUAL
    evidence: List[str] = field(default_factory=list)
    enabled: bool = True
    source: str = "auto"
    polarity_swappable: str = "unknown"

    @property
    def signature(self) -> str:
        return "|".join(sorted((self.positive_net, self.negative_net)))


@dataclass
class StackupLayerModel:
    """Ordered physical stackup layer used by transmission-line models."""
    name: str
    kind: str  # COPPER, DIELECTRIC, SOLDER_MASK
    thickness_mm: float
    layer_id: Optional[int] = None
    material: str = ""
    epsilon_r: float = 1.0
    loss_tangent: float = 0.0


@dataclass
class StackupProfile:
    """Traceable stackup snapshot or user-imported override."""
    layers: List[StackupLayerModel] = field(default_factory=list)
    source: str = "DEFAULT"  # KICAD_IPC, PCB_FILE, IMPORTED, MANUAL, DEFAULT
    trustworthy: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class DifferentialAnalysisSettings:
    """Persisted Phase 5 discovery, stackup, and impedance settings."""
    pairs: List[DifferentialPairCandidate] = field(default_factory=list)
    ignored_pair_signatures: List[str] = field(default_factory=list)
    stackup_override: Optional[StackupProfile] = None
    reference_net_names: List[str] = field(
        default_factory=lambda: ["GND", "AGND", "DGND", "PGND"]
    )
    target_tolerance_pct: float = 10.0
    include_solder_mask: bool = True
    solder_mask_thickness_mm: float = 0.02
    solder_mask_epsilon_r: float = 3.3
    fabrication_profile: str = "GENERIC"
    geometry_mode: str = "AUTO"
    coplanar_ground_gap_mm: float = 0.15
    enable_targeted_3d_refinement: bool = True
    targeted_3d_max_sections: int = 4
    targeted_3d_error_threshold_pct: float = 10.0
    minimum_width_mm: float = 0.10
    minimum_gap_mm: float = 0.10
    minimum_ground_clearance_mm: float = 0.15


@dataclass
class DifferentialSectionResult:
    """Impedance result for a same-layer coupled routing section."""
    layer_id: int
    layer_name: str
    length_mm: float
    width_mm: float
    gap_mm: float
    topology: str
    geometry_mode: str = "AUTO"
    ground_clearance_mm: float = 0.0
    reference_above: str = ""
    reference_below: str = ""
    reference_coverage_pct: float = 0.0
    single_ended_impedance_ohm: float = 0.0
    differential_impedance_ohm: float = 0.0
    copper_thickness_mm: float = 0.035
    reference_distance_mm: float = 0.0
    reference_above_distance_mm: float = 0.0
    reference_below_distance_mm: float = 0.0
    reference_above_epsilon_r: float = 4.4
    reference_below_epsilon_r: float = 4.4
    reference_epsilon_r: float = 4.4
    effective_epsilon_r: float = 0.0
    two_d_impedance_ohm: float = 0.0
    three_d_impedance_ohm: float = 0.0
    refinement_status: str = "NOT_SELECTED"
    refinement_reason: str = ""
    trustworthy: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class DifferentialRecommendation:
    """A non-destructive geometry suggestion for one differential network."""
    pair_signature: str
    pair_name: str
    layer_name: str = ""
    topology: str = ""
    geometry_mode: str = "AUTO"
    current_width_mm: float = 0.0
    current_gap_mm: float = 0.0
    current_impedance_ohm: float = 0.0
    target_impedance_ohm: float = 0.0
    recommended_width_mm: float = 0.0
    recommended_gap_mm: float = 0.0
    recommended_ground_clearance_mm: float = 0.0
    reference_distance_mm: float = 0.0
    predicted_impedance_ohm: float = 0.0
    action: str = "REVIEW"
    feasibility: str = "REVIEW"
    confidence: str = "ESTIMATE"
    warnings: List[str] = field(default_factory=list)


@dataclass
class DifferentialPairResult:
    """Aggregated routed-pair result with local section evidence."""
    pair: DifferentialPairCandidate
    sections: List[DifferentialSectionResult] = field(default_factory=list)
    weighted_impedance_ohm: float = 0.0
    minimum_impedance_ohm: float = 0.0
    maximum_impedance_ohm: float = 0.0
    error_pct: float = 0.0
    positive_length_mm: float = 0.0
    negative_length_mm: float = 0.0
    length_mismatch_mm: float = 0.0
    length_mismatch_pct: float = 0.0
    estimated_skew_ps: float = 0.0
    skew_limit_ps: float = 0.0
    maximum_length_mismatch_mm: float = 0.0
    skew_margin_ps: float = 0.0
    length_symmetry_status: str = "NO_DATA"
    shorter_net: str = ""
    status: str = "NO_DATA"
    trustworthy: bool = False
    warnings: List[str] = field(default_factory=list)
    recommendations: List[DifferentialRecommendation] = field(default_factory=list)


@dataclass
class EMCSignalSource:
    """Clock, switching node, or cable source used by the EMC risk model."""
    name: str
    net_name: str
    kind: str = "DIGITAL"
    frequency_hz: float = 0.0
    rise_time_ns: float = 5.0
    external: bool = False
    cable_length_m: float = 0.0
    enabled: bool = True
    source: str = "auto"
    voltage_swing_v: float = 3.3
    current_a: float = 0.1
    negative_net_name: str = ""
    parameter_confidence: str = "LOW"
    parameter_notes: str = "Editable engineering defaults"


@dataclass
class EMCInductorModel:
    """Traceable magnetic-emission model for one switching inductor."""
    ref_des: str
    mpn: str = ""
    source_name: str = ""
    switching_net: str = ""
    inductance_h: float = 0.0
    vin_v: float = 0.0
    vout_v: float = 0.0
    switching_frequency_hz: float = 0.0
    output_current_a: float = 0.0
    ripple_current_pp_a: float = 0.0
    width_mm: float = 0.0
    depth_mm: float = 0.0
    height_mm: float = 0.0
    isat_a: float = 0.0
    itemp_a: float = 0.0
    shield_state: str = "UNKNOWN"
    shielding_attenuation_db: Optional[float] = None
    model_level: str = "BOUNDED_ESTIMATE"
    parameter_source: str = "estimate"
    parameter_confidence: str = "LOW"
    parameter_reference: str = ""
    notes: str = ""
    enabled: bool = True


@dataclass
class EMCPhase10Settings:
    """External multi-fidelity EMC pipeline introduced in Phase 10."""
    enabled: bool = True
    spice_enabled: bool = True
    full_wave_enabled: bool = True
    auto_run_full_wave: bool = False
    full_wave_backend: str = "OPENEMS_LOCAL"
    ngspice_path: str = r"C:\Spice64\bin\ngspice_con.exe"
    spice_library_path: str = r"C:\Users\jbc66\Documents\DAW CONTROLEUR\Lib\SPICE"
    openems_root: str = r"C:\openEMS"
    openems_python_path: str = ""
    gmsh_path: str = ""
    palace_path: str = ""
    palace_remote_host: str = ""
    palace_remote_port: int = 22
    palace_remote_username: str = ""
    palace_remote_identity_file: str = ""
    palace_remote_root: str = "~/kipida-palace"
    palace_remote_executable: str = "palace"
    palace_remote_config_path: str = ""
    palace_remote_mpi_processes: int = 1
    palace_remote_host_key_policy: str = "STRICT"
    palace_remote_connect_timeout_s: float = 10.0
    palace_remote_keep_files: bool = True
    output_directory: str = ""
    maximum_regions: int = 3
    region_margin_mm: float = 5.0
    mesh_resolution_mm: float = 0.25
    maximum_cells: int = 2000000
    solver_timeout_s: float = 600.0
    openems_max_timesteps: int = 8000
    openems_end_criteria: float = 1.0e-3
    differential_excitation_mode: str = "DIFFERENTIAL"
    differential_leg_impedance_ohm: float = 45.0
    progress_interval_s: float = 5.0
    receiver_distance_m: float = 3.0
    receiver_detector: str = "QUASI_PEAK"
    receiver_rbw_hz: float = 120000.0
    include_cables: bool = True
    include_enclosure: bool = False


@dataclass
class EMCAnalysisSettings:
    """Persisted pre-compliance intent and rule-selection settings."""
    standard: str = "CISPR_32_CLASS_B"
    market: str = "EU"
    frequency_start_hz: float = 30.0e6
    frequency_stop_hz: float = 1.0e9
    reference_net_names: List[str] = field(
        default_factory=lambda: ["GND", "AGND", "DGND", "PGND"]
    )
    sources: List[EMCSignalSource] = field(default_factory=list)
    inductor_models: List[EMCInductorModel] = field(default_factory=list)
    enabled_categories: List[str] = field(default_factory=lambda: [
        "GROUND", "DECOUPLING", "IO", "SWITCHING", "CLOCK", "STACKUP",
        "DIFFERENTIAL", "BOARD_EDGE", "PDN", "RETURN_PATH", "CROSSTALK",
        "ESD", "SHIELDING", "STITCHING", "THERMAL", "EMISSIONS",
    ])
    maximum_findings_per_rule_for_score: int = 3
    external_connector_prefixes: List[str] = field(default_factory=lambda: ["J", "P", "CN"])
    field_simulation_enabled: bool = True
    field_probe_height_mm: float = 3.0
    field_grid_size_mm: float = 1.0
    field_frequency_hz: float = 0.0  # 0 = each source at its configured fundamental
    field_maximum_cells: int = 250000
    phase10: EMCPhase10Settings = field(default_factory=EMCPhase10Settings)


@dataclass
class EMCEvidence:
    """Traceable board-space evidence attached to one EMC finding."""
    source: str
    detail: str
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None
    layer_id: Optional[int] = None


@dataclass
class EMCFinding:
    """One deterministic and actionable pre-compliance finding."""
    rule_id: str
    category: str
    severity: str
    title: str
    description: str
    recommendation: str
    confidence: str = "MEDIUM"
    nets: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    evidence: List[EMCEvidence] = field(default_factory=list)


@dataclass
class EMCProbePoint:
    """Suggested near-field probe location derived from a finding."""
    x_mm: float
    y_mm: float
    reason: str
    rule_id: str = ""


@dataclass
class EMCFrequencyRisk:
    """Frequency-domain risk marker for plots and lab preparation."""
    frequency_hz: float
    level_db: float
    source_name: str
    kind: str = "HARMONIC"


@dataclass
class EMFieldSourceContribution:
    """Per-source hotspot evidence used to explain a combined field map."""
    source_name: str
    net_names: tuple = ()
    geometry_source: str = "ROUTED_TRACKS"
    geometry_confidence: str = "HIGH"
    maximum_e_v_m: float = 0.0
    maximum_h_a_m: float = 0.0
    maximum_e_position_mm: tuple = (0.0, 0.0)
    maximum_h_position_mm: tuple = (0.0, 0.0)
    relative_e_pct: float = 0.0
    relative_h_pct: float = 0.0
    analyzed_frequency_hz: float = 0.0
    harmonic_number: int = 1


@dataclass
class EMInductorFieldContribution:
    """Per-inductor magnetic-field evidence and uncertainty disclosure."""
    ref_des: str
    mpn: str = ""
    source_name: str = ""
    model_level: str = "BOUNDED_ESTIMATE"
    shield_state: str = "UNKNOWN"
    attenuation_applied_db: Optional[float] = None
    harmonic_number: int = 1
    analyzed_frequency_hz: float = 0.0
    ripple_current_pp_a: float = 0.0
    harmonic_current_peak_a: float = 0.0
    maximum_h_a_m: float = 0.0
    maximum_h_position_mm: tuple = (0.0, 0.0)
    parameter_confidence: str = "LOW"
    model_confidence: str = "LOW"
    parameter_reference: str = ""
    refinement_status: str = "NOT_REQUESTED"


@dataclass
class EMFieldSimulationResult:
    """Quasi-static PCB near-field estimate on a regular observation plane."""
    x_coordinates_mm: List[float] = field(default_factory=list)
    y_coordinates_mm: List[float] = field(default_factory=list)
    electric_field_v_m: List[List[float]] = field(default_factory=list)
    magnetic_field_a_m: List[List[float]] = field(default_factory=list)
    probe_height_mm: float = 0.0
    requested_grid_size_mm: float = 0.0
    effective_grid_size_mm: float = 0.0
    frequency_hz: float = 0.0
    frequency_mode: str = "FIRST_IN_BAND_HARMONICS"
    model_scope: str = "UNCALIBRATED_QUASI_STATIC_RANKING"
    source_count: int = 0
    segment_count: int = 0
    maximum_e_v_m: float = 0.0
    maximum_h_a_m: float = 0.0
    maximum_e_position_mm: tuple = (0.0, 0.0)
    maximum_h_position_mm: tuple = (0.0, 0.0)
    source_contributions: List[EMFieldSourceContribution] = field(default_factory=list)
    inductor_contributions: List[EMInductorFieldContribution] = field(default_factory=list)
    source_segments: List[tuple] = field(default_factory=list)
    compute_backend: str = "CPU_NUMPY"
    elapsed_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)


@dataclass
class EMCPhase10ToolStatus:
    name: str
    available: bool
    path: str = ""
    version: str = ""
    detail: str = ""


@dataclass
class EMCPhase10ExcitationResult:
    source_name: str
    status: str
    provenance: str
    peak_voltage_v: float = 0.0
    peak_current_a: float = 0.0
    maximum_dv_dt_v_s: float = 0.0
    maximum_di_dt_a_s: float = 0.0
    waveform_path: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class EMCPhase10RegionResult:
    name: str
    status: str
    bounds_mm: tuple = (0.0, 0.0, 0.0, 0.0)
    source_names: List[str] = field(default_factory=list)
    finding_ids: List[str] = field(default_factory=list)
    estimated_cells: int = 0
    geometry_path: str = ""
    solver_output_path: str = ""
    maximum_e_v_m: Optional[float] = None
    maximum_h_a_m: Optional[float] = None
    elapsed_seconds: float = 0.0
    solver_cells: int = 0
    solver_iterations: int = 0
    solver_converged: Optional[bool] = None
    solver_energy_decay_db: Optional[float] = None
    fields_extracted: bool = False
    unused_primitive_count: int = 0
    port_net_name: str = ""
    port_net_names: List[str] = field(default_factory=list)
    port_count: int = 0
    port_mode: str = ""
    port_leg_impedance_ohm: float = 0.0
    port_excitations: List[float] = field(default_factory=list)
    port_reference_layer_ids: List[int] = field(default_factory=list)
    port_geometry_source: str = ""
    port_confidence: str = ""
    warnings: List[str] = field(default_factory=list)


@dataclass
class EMCPalaceRemoteRunResult:
    """Traceable result of one Palace project executed over SSH."""
    status: str = "NOT_RUN"
    server: str = ""
    remote_job_directory: str = ""
    config_path: str = ""
    problem_type: str = "UNKNOWN"
    palace_version: str = ""
    local_artifact_directory: str = ""
    output_directory: str = ""
    csv_files: List[str] = field(default_factory=list)
    resolved_config_path: str = ""
    elapsed_seconds: float = 0.0
    return_code: Optional[int] = None
    dry_run_passed: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class EMCSpiceModelAudit:
    """Traceable model coverage for a component used by a Phase 10 source."""
    component_ref: str
    mpn: str
    source_name: str
    status: str
    model_name: str = ""
    model_path: str = ""
    catalog_status: str = ""
    used: bool = False
    fallback: str = "PARAMETRIC_SOURCE"
    wrapper_name: str = ""
    wrapper_path: str = ""
    pin_mapping: str = ""
    compatibility: str = "NOT_TESTED"
    probe_log_path: str = ""
    notes: str = ""


@dataclass
class EMCVirtualReceiverPoint:
    frequency_hz: float
    detector: str
    level_dbuv_m: float
    limit_dbuv_m: Optional[float]
    margin_db: Optional[float]
    source_name: str
    provenance: str


@dataclass
class EMCPhase10Result:
    status: str = "NOT_RUN"
    tools: List[EMCPhase10ToolStatus] = field(default_factory=list)
    excitations: List[EMCPhase10ExcitationResult] = field(default_factory=list)
    regions: List[EMCPhase10RegionResult] = field(default_factory=list)
    palace_runs: List[EMCPalaceRemoteRunResult] = field(default_factory=list)
    receiver_points: List[EMCVirtualReceiverPoint] = field(default_factory=list)
    spice_model_audit: List[EMCSpiceModelAudit] = field(default_factory=list)
    output_directory: str = ""
    elapsed_seconds: float = 0.0
    limitations: List[str] = field(default_factory=list)


@dataclass
class EMCAnalysisResult:
    """Aggregated EMI/EMC pre-compliance result."""
    findings: List[EMCFinding] = field(default_factory=list)
    risk_score: int = 100
    total_checks: int = 0
    severity_counts: Dict[str, int] = field(default_factory=dict)
    score_penalties_by_rule: Dict[str, int] = field(default_factory=dict)
    per_net_scores: Dict[str, int] = field(default_factory=dict)
    probe_points: List[EMCProbePoint] = field(default_factory=list)
    frequency_risks: List[EMCFrequencyRisk] = field(default_factory=list)
    cavity_resonances_hz: List[float] = field(default_factory=list)
    test_plan: List[str] = field(default_factory=list)
    regulatory_coverage: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    limitations: List[str] = field(default_factory=list)
    field_simulation: Optional[EMFieldSimulationResult] = None
    phase10_result: Optional[EMCPhase10Result] = None


@dataclass
class ProjectConfig:
    """Complete persisted Ki-PIDA project configuration."""
    rails: List[PowerRail] = field(default_factory=list)
    ac_profiles: Dict[str, ACAnalysisSettings] = field(default_factory=dict)
    thermal_profile: Optional["ThermalAnalysisSettings"] = None
    cfd_profile: Optional["EnclosureCFDSettings"] = None
    differential_profile: Optional[DifferentialAnalysisSettings] = None
    emc_profile: Optional[EMCAnalysisSettings] = None


@dataclass
class ImpedanceSweepResult:
    """Complex impedance sweep and derived summary values."""
    frequencies_hz: List[float]
    impedance_ohm: List[complex]
    target_impedance_ohm: float = 0.0
    worst_frequency_hz: float = 0.0
    worst_impedance_ohm: float = 0.0
    meets_target: bool = False
    compute_backend: str = "CPU"
    compute_device: str = "CPU"
    compute_solve_seconds: float = 0.0
    compute_transfer_seconds: float = 0.0
    compute_relative_residual: float = 0.0
    compute_iterations: int = 0
    compute_cache_hits: int = 0
    mesh_node_count: int = 0
    requested_grid_size_mm: float = 0.0
    effective_grid_size_mm: float = 0.0
    # Populated by a multi-port sweep: one sweep per observation point, keyed
    # by reference designator, plus which one produced this worst case. Left
    # empty by a single-port solve, so existing consumers are unaffected.
    per_port_results: Dict[str, "ImpedanceSweepResult"] = field(default_factory=dict)
    worst_port_ref_des: str = ""
    # Observation points dropped before solving, each {"ref_des", "reason"}.
    # An impedance computed on copper that never reaches the source is
    # meaningless, so such a port is removed rather than reported -- but
    # removing it silently would hide a degraded analysis, so it is carried
    # here and surfaced as a limitation.
    excluded_ports: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class OptimizationRecommendation:
    ref_des: str
    capacitance_f: float
    action: str = "populate"


@dataclass
class DecouplingOptimizationResult:
    baseline: ImpedanceSweepResult
    optimized: ImpedanceSweepResult
    recommendations: List[OptimizationRecommendation] = field(default_factory=list)
    reached_target: bool = False


@dataclass
class DCSolveResult:
    """Detailed DC result used by electro-thermal analysis."""
    voltages: Dict[int, float] = field(default_factory=dict)
    branch_currents_a: List[float] = field(default_factory=list)
    branch_losses_w: List[float] = field(default_factory=list)
    total_loss_w: float = 0.0
    compute_metadata: Any = None
    valid: bool = True
    excluded_load_node_count: int = 0
    excluded_load_references: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class AirflowSettings:
    """Convective boundary settings for the exposed PCB surfaces."""
    mode: str = "NATURAL"  # NATURAL, FORCED, CUSTOM
    velocity_m_s: float = 0.0
    direction_deg: float = 0.0
    custom_h_w_m2k: float = 10.0
    expose_top: bool = True
    expose_bottom: bool = True
    expose_edges: bool = True


@dataclass
class ThermalComponentModel:
    """Compact component-to-board thermal model and heat source."""
    ref_des: str
    power_w: float = 0.0
    width_mm: float = 3.0
    depth_mm: float = 3.0
    height_mm: float = 1.0
    theta_jb_c_per_w: float = 20.0
    max_junction_c: float = 125.0
    enabled: bool = True
    model_source: str = "estimated"
    # Physical evidence stays with the saved thermal model, while mechanisms
    # are regenerated from the electrical scenario on every refresh.
    geometry_source: str = "estimate"
    thermal_source: str = "estimate"
    thermal_condition: str = ""
    parameter_provenance: Dict[str, Any] = field(default_factory=dict)
    loss_mechanisms: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ThermalAnalysisSettings:
    """Persisted settings for steady-state 3D thermal analysis."""
    ambient_c: float = 25.0
    grid_size_mm: float = 1.0
    airflow: AirflowSettings = field(default_factory=AirflowSettings)
    include_radiation: bool = True
    emissivity: float = 0.9
    include_dc_copper_losses: bool = True
    color_map: str = "inferno"
    color_scale_minimum_mode: str = "AMBIENT"
    color_scale_minimum_c: Optional[float] = None
    color_scale_maximum_mode: str = "AUTO"
    color_scale_maximum_c: Optional[float] = None
    show_internal_copper_layers: bool = True
    coupled_iterations: int = 10
    convergence_c: float = 0.1
    relaxation: float = 0.6
    copper_temp_coefficient_per_c: float = 0.00393
    components: List[ThermalComponentModel] = field(default_factory=list)
    power_stage_reports: List[PowerStageResult] = field(default_factory=list)

    def resolved_color_scale_minimum_c(self) -> Optional[float]:
        """Return the requested lower colour bound, or None for auto."""
        mode = str(self.color_scale_minimum_mode or "AMBIENT").upper()
        if mode == "AUTO":
            return None
        if mode == "CUSTOM":
            return self.color_scale_minimum_c
        return float(self.ambient_c)

    def resolved_color_scale_maximum_c(self) -> Optional[float]:
        """Return the requested upper colour bound, or None for hotspot auto."""
        mode = str(self.color_scale_maximum_mode or "AUTO").upper()
        return self.color_scale_maximum_c if mode == "CUSTOM" else None


@dataclass
class ThermalHotspot:
    node_id: int
    x_mm: float
    y_mm: float
    z_mm: float
    temperature_c: float


@dataclass
class ComponentThermalResult:
    ref_des: str
    board_temperature_c: float
    junction_temperature_c: float
    power_w: float
    max_junction_c: float
    margin_c: float
    model_source: str = "estimated"
    theta_jb_c_per_w: float = 0.0
    thermal_source: str = "estimate"
    thermal_condition: str = ""


@dataclass
class ThermalResult:
    temperatures_c: Dict[int, float] = field(default_factory=dict)
    # Kept alongside the public node dictionary for fast electro-thermal
    # coupling.  Values follow ``mesh.nodes`` order and avoid rebuilding a
    # multi-million-entry Python mapping between coupled iterations.
    temperature_vector_c: object = None
    hotspot: Optional[ThermalHotspot] = None
    component_results: List[ComponentThermalResult] = field(default_factory=list)
    total_input_power_w: float = 0.0
    total_boundary_power_w: float = 0.0
    energy_balance_error_pct: float = 0.0
    convection_coefficient_w_m2k: float = 0.0
    iterations: int = 1
    converged: bool = True
    compute_backend: str = "CPU"
    compute_device: str = "CPU"
    compute_solve_seconds: float = 0.0
    compute_transfer_seconds: float = 0.0
    compute_relative_residual: float = 0.0
    compute_iterations: int = 1
    compute_cpu_threads: int = 1
    compute_fallback_reason: str = ""
    compute_matrix_assembly: str = "CPU_CSR"
    compute_warm_start_used: bool = False


@dataclass
class ElectroThermalResult:
    thermal: ThermalResult
    dc_results: Dict[str, DCSolveResult] = field(default_factory=dict)
    iterations: int = 1
    converged: bool = True


@dataclass
class FluidProperties:
    """Air properties used by the enclosure CFD solver (SI units)."""
    density_kg_m3: float = 1.184
    dynamic_viscosity_pa_s: float = 1.85e-5
    heat_capacity_j_kgk: float = 1007.0
    conductivity_w_mk: float = 0.0262
    thermal_expansion_per_k: float = 0.00335


@dataclass
class CFDBoundaryPatch:
    """Rectangular boundary patch on one of the six enclosure faces."""
    name: str
    kind: str = "VENT"  # WALL, INLET, OUTLET, VENT, FAN
    face: str = "XMIN"  # XMIN/XMAX/YMIN/YMAX/ZMIN/ZMAX
    center_u: float = 0.5
    center_v: float = 0.5
    size_u: float = 0.25
    size_v: float = 0.25
    velocity_m_s: float = 0.0
    temperature_c: float = 25.0
    pressure_pa: float = 0.0


@dataclass
class EnclosureGeometrySettings:
    """Axis-aligned enclosure and PCB placement settings."""
    width_mm: float = 120.0
    depth_mm: float = 100.0
    height_mm: float = 50.0
    board_orientation: str = "XY"  # XY, XZ, YZ
    board_offset_x_mm: float = 0.0
    board_offset_y_mm: float = 0.0
    board_offset_z_mm: float = 15.0
    wall_heat_transfer_w_m2k: float = 5.0


@dataclass
class CFDSolverSettings:
    """Numerical controls for steady incompressible enclosure flow."""
    cell_size_mm: float = 5.0
    max_iterations: int = 250
    tolerance: float = 1.0e-4
    relaxation: float = 0.45
    pseudo_time_step_s: float = 0.02
    pressure_iterations: int = 60
    include_buoyancy: bool = True
    gravity_x_m_s2: float = 0.0
    gravity_y_m_s2: float = 0.0
    gravity_z_m_s2: float = -9.81
    max_cells: int = 250000


@dataclass
class EnclosureCFDSettings:
    """Persisted Phase 4 enclosure, fluid, boundary, and solver settings."""
    ambient_c: float = 25.0
    geometry: EnclosureGeometrySettings = field(default_factory=EnclosureGeometrySettings)
    fluid: FluidProperties = field(default_factory=FluidProperties)
    solver: CFDSolverSettings = field(default_factory=CFDSolverSettings)
    patches: List[CFDBoundaryPatch] = field(default_factory=list)
    use_phase3_heat_sources: bool = True
    include_dc_copper_losses: bool = True


@dataclass
class CFDResidualHistory:
    continuity: List[float] = field(default_factory=list)
    momentum: List[float] = field(default_factory=list)
    energy: List[float] = field(default_factory=list)


@dataclass
class EnclosureCFDResult:
    """Volumetric CFD fields and conservation diagnostics."""
    pressure_pa: List[float] = field(default_factory=list)
    velocity_u_m_s: List[float] = field(default_factory=list)
    velocity_v_m_s: List[float] = field(default_factory=list)
    velocity_w_m_s: List[float] = field(default_factory=list)
    air_temperature_c: List[float] = field(default_factory=list)
    solid_temperature_c: List[float] = field(default_factory=list)
    residuals: CFDResidualHistory = field(default_factory=CFDResidualHistory)
    iterations: int = 0
    converged: bool = False
    mass_balance_error_pct: float = 0.0
    energy_balance_error_pct: float = 0.0
    maximum_velocity_m_s: float = 0.0
    maximum_air_temperature_c: float = 0.0
    maximum_solid_temperature_c: float = 0.0
    total_heat_w: float = 0.0
    compute_backend: str = "CPU"
    compute_device: str = "CPU"
    compute_solve_seconds: float = 0.0
    compute_relative_residual: float = 0.0
    compute_fallback_reason: str = ""

def generate_regulator_name(input_ref_des: str, output_ref_des: str, output_rail_name: str = "") -> str:
    """
    Generate a regulator name based on input and output components and rails.
    If input == output, name is "input (output_rail)".
    Otherwise, name is "input -> output (output_rail)".
    """
    name = ""
    if not input_ref_des:
        name = output_ref_des if output_ref_des else ""
    elif not output_ref_des:
        name = input_ref_des
    elif input_ref_des == output_ref_des:
        name = input_ref_des
    else:
        name = f"{input_ref_des} -> {output_ref_des}"
        
    if output_rail_name:
        name += f" ({output_rail_name})"
        
    return name

