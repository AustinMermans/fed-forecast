(function (root, factory) {
  "use strict";
  const api = Object.freeze(factory());
  if (root) root.FedForecastBranch = api;
  if (typeof module === "object" && module.exports) module.exports = api;
}(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const categories = Object.freeze(["down_50plus", "down_25", "unchanged", "up_25", "up_50plus"]);
  const actions = Object.freeze([-50, -25, 0, 25, 50]);
  const nodeIdForPath = path => path.length ? path.join("_") : "root";
  const startsWith = (path, prefix) => prefix.every((value, index) => path[index] === value);

  function descendantDistribution(tree, prefix, meetingDepth) {
    if (!tree || !Array.isArray(tree.nodes) || meetingDepth <= prefix.length) throw new Error("invalid descendant distribution request");
    const node = tree.nodes.find(item => item.id === nodeIdForPath(prefix));
    if (!node || !(node.probability > 0)) throw new Error("prefix node is unavailable");
    const masses = Object.fromEntries(categories.map(category => [category, 0]));
    tree.nodes.forEach(candidate => {
      if (candidate.depth === meetingDepth && startsWith(candidate.path, prefix)) {
        const category = candidate.path[meetingDepth - 1];
        if (Object.hasOwn(masses, category)) masses[category] += candidate.probability;
      }
    });
    const values = categories.map(category => masses[category] / node.probability);
    const total = values.reduce((sum, value) => sum + value, 0);
    if (!(total > 0)) throw new Error("descendant mass is unavailable");
    return values.map(value => value / total);
  }

  const expectedAction = probabilities => probabilities.reduce((sum, value, index) => sum + value * actions[index], 0);

  const LEGACY_FAILURES = ["minimum_transition_count_not_met", "minimum_down_row_count_not_met", "minimum_up_row_count_not_met", "timing_destination_not_identified", "replay_validation_failed"];
  const SOURCE_HASHES = {"historical_transitions.py":"a6f45ba6ab13d084e32c441694edd9212175c3086cef440ff3717186aea08e23","historical_transitions_cli.py":"a0c6a5a64d888b56d3b391d8ec9db4715a29c585c358af683ddbcf29ab68e358","historical_transitions_client.py":"e8fb3dd6b3a231535c322fa31f6c349d1fe9b447449e2cb461dd84f212dc9242","historical_transitions_reporting.py":"c42a6066708a8ef01113d2f55a78edbf99dc2948593730ab65d92eae9f6536f3"};
  function expectedEvidence(generatedAt) {
    return {
      schema_version:2, summary_generated_at:generatedAt, summary_builder_version:"evidence-summary-v2",
      live:{meeting_marginals:{status:"market_observed",source_policy:"clob_midpoint_gamma_fallback",normalization:"proportional_yes_sum"},conditional_response:{status:"structural_assumption",model:"five_outcome_persistence_ipf",model_version:"structural_meeting_persistence_ipf-v1",exact_enumeration:true,learned_transition_active:false,marginals_preserved:true}},
      legacy_canonical_15_30:{status:"diagnostic_only_legacy_window",active_in_tree:false,canonical_cutoff_date:"2026-07-29",estimator_version:"fomc-dhu-v1-15:30",event_target:{window_role:"full_communications_robustness",post_cutoff_ny:"15:30"},transition_count:13,row_counts:{down:3,unchanged:10,up:0},unique_meeting_count:15,calendar_start:"2024-07-31",calendar_end:"2026-07-29",sample_selection:{candidate_adjacent_edges:35,usable_adjacent_edges:13,excluded_adjacent_edges:22,exclusions_by_primary_reason:{missing_consecutive_primary_topology:21,missing_synchronized_quote:1}},surface_synchronization:{status:"legacy_600s_diagnostic",configured_max_coordinate_dispersion_seconds:600,warning:"Ten-minute surfaces may be synthetic and non-simultaneous.",edge_synchronization_aggregates:{edge_count:13,coordinate_timestamp_dispersion_seconds:{median:1,p90:4,max:8},maximum_coordinate_quote_age_seconds:{median:55,p90:58,max:59},tight_60s_eligible_edge_count:13,loose_600s_only_edge_count:0}},walk_forward:{scored_folds:0,skipped_folds:13,status:"unavailable_insufficient_scored_transitions"},dhu_smoothed_surprise_support:{scale:"smoothed_dhu_estimator_s25",standard_move_bp:25,observed_min_25bp_units:-0.030363364858,observed_max_25bp_units:0.028250857496,estimator_support_floor:0.005,comparable_to_live_five_outcome:false},stored_production_gate:{eligible:false,failures:LEGACY_FAILURES},limitations:["total_announcement_conditioned_dependence","timing_destination_not_identified","adjacent_edge_only","serially_dependent_transitions","no_realized_hike_rows","dhu_support_not_comparable_to_live_five_outcome"],provenance:{snapshot_sha256:"ceaa5e1bbdf3263ad709c44a78f576f64c13c5f0a5fe508323fdd9b676851994",model_sha256:"815b2379dffdb16744030ddbdf5fd30176d15a8aaa78f994d4edfa3873ae54c7",config_canonical_sha256:"c78c20c2ff6eea32ad7d5e4b2862447e1bd8e74218a0e093e5b260a9bc954f83",config_file_sha256:"f1707f20601fba5dbbc7b5f256f34c3402db7bab7678f00a42d8b7650fedebe8",manifest_sha256:"2073e23c94b3618713c7f71ba4cdf02a80eb44c9a05d200bd7df3c81913f0446",manifest_schema_version:1,snapshot_schema_version:1,model_schema_version:1,code_commit_sha:null,code_commit_status:"unavailable_legacy_artifact_source_hashes_retained",source_sha256:SOURCE_HASHES,snapshot_fetched_at:"2026-08-29T00:18:35.902772Z",run_generated_at:"2026-08-29T00:18:36.831584Z",data_cutoff_at:"2026-07-29T15:30:00-04:00"}},
      stage1_primary_14_15:{status:"new_run_required",active_in_tree:false,estimator_version:"fomc-native5-primary-14:15-v1",event_target:{window_role:"primary_action_window",post_cutoff_ny:"14:15",lower_bound:"strictly_after_recorded_official_decision_timestamp"},sample_selection:null,surface_synchronization:{status:"pending_new_run",primary_max_coordinate_dispersion_seconds:60,primary_max_quote_age_seconds:60,edge_synchronization_aggregates:null},native_five_outcome_surprise_support:{status:"pending_new_run",scale:"unsmoothed_native_five_outcome_s25",standard_move_bp:25,observed_min_25bp_units:null,observed_max_25bp_units:null},walk_forward:null,stored_production_gate:null,provenance:null},
      stage1_reproducibility_gates:{status:"pending_new_run",names:["manifest_valid","provenance_complete","primary_14_15_timing_valid","native_five_outcome_support_status_resolved","synchronization_aggregates_complete","synthetic_replay_tests_pass"]},exploratory_support_assessment:{status:"not_created_stage1a",design_artifact:null,eligible_for_activation:false},prospective_activation_gate:{status:"not_registered",active:false,required_design:"future_versioned_shadow_or_holdout_registered_before_new_meetings_resolve"},frictions:{probabilities_fee_adjusted:false,spread_adjusted:false,slippage_adjusted:false,funding_adjusted:false,rewards_adjusted:false,interpretation:"market context only"},futures_benchmark:{status:"not_connected",comparison_claim_allowed:false,required_source:"licensed_cme_fedwatch_eod_api",redistribution_clearance:false}
    };
  }
  function deepExact(actual, expected) {
    if (expected === null || typeof expected !== "object") return typeof actual === typeof expected && (typeof expected !== "number" || Number.isFinite(actual)) && actual === expected;
    if (Array.isArray(expected)) return Array.isArray(actual) && actual.length === expected.length && expected.every((item, index) => deepExact(actual[index], item));
    if (!actual || typeof actual !== "object" || Array.isArray(actual)) return false;
    const actualKeys = Object.keys(actual).sort();
    const expectedKeys = Object.keys(expected).sort();
    return actualKeys.length === expectedKeys.length && actualKeys.every((key, index) => key === expectedKeys[index]) && expectedKeys.every(key => deepExact(actual[key], expected[key]));
  }

  function validateEvidencePayload(value, contract, actualSha256) {
    const contractKeys = ["generated_at","legacy_cutoff_at","legacy_model_sha256","schema_version","sha256","url"];
    if (!contract || typeof contract !== "object" || Array.isArray(contract) || Object.keys(contract).sort().join("|") !== contractKeys.join("|") || contract.schema_version !== 2 || contract.url !== "data/evidence-summary.json" || typeof actualSha256 !== "string" || !/^[a-f0-9]{64}$/.test(actualSha256)) return { status: "unavailable", reason: "malformed" };
    if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.summary_generated_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value.summary_generated_at) || !deepExact(value, expectedEvidence(value.summary_generated_at))) return { status: "unavailable", reason: "malformed" };
    const legacy = value.legacy_canonical_15_30;
    if (actualSha256 !== contract.sha256 || value.summary_generated_at !== contract.generated_at || legacy.provenance.model_sha256 !== contract.legacy_model_sha256 || legacy.provenance.data_cutoff_at !== contract.legacy_cutoff_at) return { status: "unavailable", reason: "stale_contract" };
    return { status: "available", value };
  }

  async function loadEvidenceSummary(contract, fetcher, hasher) {
    if (typeof fetcher !== "function" || typeof hasher !== "function") return { status: "unavailable", reason: "malformed" };
    try {
      const response = await fetcher(contract?.url, { cache: "no-store" });
      if (!response || response.ok !== true || typeof response.arrayBuffer !== "function") return { status: "unavailable", reason: "fetch_error" };
      const bytes = await response.arrayBuffer();
      if (!(bytes instanceof ArrayBuffer) || bytes.byteLength > 16 * 1024) return { status: "unavailable", reason: "oversize" };
      const actualSha256 = await hasher(bytes);
      let value;
      try { value = JSON.parse(new TextDecoder().decode(bytes)); }
      catch (_) { return { status: "unavailable", reason: "malformed" }; }
      return validateEvidencePayload(value, contract, actualSha256);
    } catch (_) {
      return { status: "unavailable", reason: "fetch_error" };
    }
  }

  function decomposeSelection(tree, meetings, priorPrefix, selectedCategory, evidence) {
    const node = tree.nodes.find(item => item.id === nodeIdForPath(priorPrefix));
    if (!node) throw new Error("selected prefix is unavailable");
    const branch = node.branches.find(item => item.category === selectedCategory);
    if (!branch) throw new Error("selected branch is unavailable");
    const probabilities = categories.map(category => {
      const item = node.branches.find(candidate => candidate.category === category);
      return item ? Number(item.conditional_probability) : 0;
    });
    const conditionalExpected = probabilities.reduce((sum, value, index) => {
      const item = node.branches.find(candidate => candidate.category === categories[index]);
      return sum + value * Number(item.representative_action_bp);
    }, 0);
    const selectedAction = Number(branch.representative_action_bp);
    const selectedProbability = Number(branch.conditional_probability);
    const selectedPrefix = [...priorPrefix, selectedCategory];
    const later = [];
    for (let meetingIndex = priorPrefix.length + 1; meetingIndex < meetings.length; meetingIndex += 1) {
      const depth = meetingIndex + 1;
      const before = descendantDistribution(tree, priorPrefix, depth);
      const after = descendantDistribution(tree, selectedPrefix, depth);
      later.push({
        meeting_date: meetings[meetingIndex].date,
        before_probabilities: before,
        after_probabilities: after,
        delta_probability_points: before.map((value, index) => 100 * (after[index] - value)),
        before_expected_action_bp: expectedAction(before),
        after_expected_action_bp: expectedAction(after),
        delta_expected_action_bp: expectedAction(after) - expectedAction(before),
      });
    }
    const primary = evidence && evidence.stage1_primary_14_15;
    const support = primary && primary.status === "diagnostic_only"
      ? { status: "comparable_diagnostic", ...primary.native_five_outcome_surprise_support }
      : { status: "unavailable_pending_comparable_14_15_run" };
    return {
      selected_meeting_date: meetings[priorPrefix.length].date,
      selected_category: selectedCategory,
      selected_action_bp: selectedAction,
      conditional_pre_event_expected_action_bp: conditionalExpected,
      magnitude_surprise_bp: selectedAction - conditionalExpected,
      magnitude_surprise_25bp_units: (selectedAction - conditionalExpected) / 25,
      ex_ante_probability: selectedProbability,
      information_surprise_bits: -Math.log2(selectedProbability),
      information_surprise_nats: -Math.log(selectedProbability),
      selected_probability_source: priorPrefix.length ? "model_assumed_conditional" : "market_observed_marginal",
      diagnostic_surprise_support: support,
      response_source: "structural_assumption",
      historical_transition_active: false,
      later_repricing: later,
    };
  }

  return { categories, actions, nodeIdForPath, descendantDistribution, decomposeSelection, validateEvidencePayload, loadEvidenceSummary };
}));
