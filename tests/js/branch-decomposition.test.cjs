const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const api = require("../../site/assets/branch-decomposition.js");

const categories = api.categories;
const rootProbabilities = [.1, .2, .4, .2, .1];
const selectedConditional = [.05, .1, .25, .4, .2];

function tree() {
  const branches = categories.map((category, index) => ({
    category,
    conditional_probability: rootProbabilities[index],
    representative_action_bp: api.actions[index],
    child_node_id: category,
  }));
  const nodes = [{ id: "root", depth: 0, path: [], probability: 1, branches }];
  categories.forEach((first, firstIndex) => {
    const conditional = first === "up_25" ? selectedConditional : rootProbabilities;
    nodes.push({
      id: first,
      depth: 1,
      path: [first],
      probability: rootProbabilities[firstIndex],
      branches: categories.map((category, index) => ({
        category,
        conditional_probability: conditional[index],
        representative_action_bp: api.actions[index],
        child_node_id: `${first}_${category}`,
      })),
    });
    categories.forEach((second, secondIndex) => nodes.push({
      id: `${first}_${second}`,
      depth: 2,
      path: [first, second],
      probability: rootProbabilities[firstIndex] * conditional[secondIndex],
      branches: [],
    }));
  });
  return { root: "root", nodes };
}

test("classic script exposes the same frozen CommonJS/global API", () => {
  assert.equal(globalThis.FedForecastBranch, api);
  assert.ok(Object.isFrozen(api));
});

test("root selection uses observed marginal and exact surprise arithmetic", () => {
  const result = api.decomposeSelection(tree(), [{ date: "2026-09-16" }, { date: "2026-10-28" }], [], "up_25", null);
  assert.equal(result.selected_probability_source, "market_observed_marginal");
  assert.equal(result.conditional_pre_event_expected_action_bp, 0);
  assert.equal(result.magnitude_surprise_bp, 25);
  assert.equal(result.magnitude_surprise_25bp_units, 1);
  assert.ok(Math.abs(result.information_surprise_bits + Math.log2(.2)) < 1e-12);
  assert.equal(result.diagnostic_surprise_support.status, "unavailable_pending_comparable_14_15_run");
  assert.equal(result.later_repricing.length, 1);
  assert.ok(Math.abs(result.later_repricing[0].after_probabilities.reduce((a, b) => a + b, 0) - 1) < 1e-12);
  const row = result.later_repricing[0];
  assert.ok(Math.abs(row.before_probabilities.reduce((a, b) => a + b, 0) - 1) < 1e-12);
  assert.ok(Math.abs(row.delta_probability_points.reduce((a, b) => a + b, 0)) < 1e-10);
  assert.ok(Math.abs(row.delta_expected_action_bp - (row.after_expected_action_bp - row.before_expected_action_bp)) < 1e-12);
});

test("deeper selection is model assumed and inputs are not mutated", () => {
  const input = tree();
  const serialized = JSON.stringify(input);
  const result = api.decomposeSelection(input, [{ date: "2026-09-16" }, { date: "2026-10-28" }], ["up_25"], "up_50plus", null);
  assert.equal(result.selected_probability_source, "model_assumed_conditional");
  assert.equal(result.ex_ante_probability, .2);
  assert.equal(result.conditional_pre_event_expected_action_bp, 15);
  assert.equal(result.response_source, "structural_assumption");
  assert.equal(JSON.stringify(input), serialized);
  assert.equal("inverse_probability" in result, false);
});

test("evidence payload fails unavailable when malformed or stale", () => {
  const bytes = fs.readFileSync(path.join(__dirname, "../../site/data/evidence-summary.json"));
  const value = JSON.parse(bytes);
  const sha = crypto.createHash("sha256").update(bytes).digest("hex");
  const contract = { schema_version: 2, url: "data/evidence-summary.json", sha256: sha, generated_at: value.summary_generated_at, legacy_model_sha256: value.legacy_canonical_15_30.provenance.model_sha256, legacy_cutoff_at: value.legacy_canonical_15_30.provenance.data_cutoff_at };
  assert.deepEqual(api.validateEvidencePayload(null, contract, sha), { status: "unavailable", reason: "malformed" });
  assert.deepEqual(api.validateEvidencePayload(value, contract, "0".repeat(64)), { status: "unavailable", reason: "stale_contract" });
  assert.equal(api.validateEvidencePayload(value, contract, sha).status, "available");
  for (const [field, replacement, reason] of [
    ["url", "https://evil.test/evidence.json", "malformed"], ["schema_version", 3, "malformed"],
    ["sha256", "0".repeat(64), "stale_contract"], ["generated_at", "2000-01-01T00:00:00Z", "stale_contract"],
    ["legacy_model_sha256", "0".repeat(64), "stale_contract"], ["legacy_cutoff_at", "2000-01-01T00:00:00Z", "stale_contract"],
  ]) {
    assert.deepEqual(api.validateEvidencePayload(value, { ...contract, [field]: replacement }, sha), { status: "unavailable", reason });
  }
  assert.deepEqual(api.validateEvidencePayload(value, { ...contract, extra: true }, sha), { status: "unavailable", reason: "malformed" });
  const injected = structuredClone(value);
  injected.legacy_canonical_15_30.xss = '<img src=x onerror=alert(1)>';
  assert.deepEqual(api.validateEvidencePayload(injected, contract, sha), { status: "unavailable", reason: "malformed" });
});

test("async evidence loader fails closed and succeeds only for pinned bytes", async () => {
  const bytes = fs.readFileSync(path.join(__dirname, "../../site/data/evidence-summary.json"));
  const value = JSON.parse(bytes);
  const sha = crypto.createHash("sha256").update(bytes).digest("hex");
  const contract = { schema_version: 2, url: "data/evidence-summary.json", sha256: sha, generated_at: value.summary_generated_at, legacy_model_sha256: value.legacy_canonical_15_30.provenance.model_sha256, legacy_cutoff_at: value.legacy_canonical_15_30.provenance.data_cutoff_at };
  const arrayBuffer = buffer => buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
  const hasher = async data => crypto.createHash("sha256").update(Buffer.from(data)).digest("hex");
  assert.equal((await api.loadEvidenceSummary(contract, async () => ({ ok: false }), hasher)).reason, "fetch_error");
  assert.equal((await api.loadEvidenceSummary(contract, async () => ({ ok: true, arrayBuffer: async () => arrayBuffer(Buffer.from("{")) }), hasher)).reason, "malformed");
  assert.equal((await api.loadEvidenceSummary(contract, async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(16 * 1024 + 1) }), hasher)).reason, "oversize");
  assert.equal((await api.loadEvidenceSummary(contract, async () => ({ ok: true, arrayBuffer: async () => arrayBuffer(bytes) }), async () => "0".repeat(64))).reason, "stale_contract");
  assert.equal((await api.loadEvidenceSummary(contract, async () => ({ ok: true, arrayBuffer: async () => arrayBuffer(bytes) }), hasher)).status, "available");
});
