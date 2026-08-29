"use strict";

const $ = id => document.getElementById(id);
const pct = value => `${(100 * value).toFixed(1)}%`;
const rate = value => `${value.toFixed(2)}%`;
const bp = value => `${value >= 0 ? "+" : ""}${value.toFixed(1)} bp`;
const month = value => new Date(`${value}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const labels = ["50+ bps decrease", "25 bps decrease", "No change", "25 bps increase", "50+ bps increase"];

function outcomeLabel(change) {
  if (change <= -37.5) return labels[0];
  if (change <= -12.5) return labels[1];
  if (change < 12.5) return labels[2];
  if (change < 37.5) return labels[3];
  return labels[4];
}

function dateOnly(value) {
  return new Date(value).toISOString().slice(0, 10);
}

function scoreAtHorizon(replay, days) {
  const results = [];
  replay.actual_target_upper.forEach(decision => {
    const target = new Date(`${decision.date}T00:00:00Z`).getTime() - days * 86_400_000;
    const candidates = replay.vintages
      .filter(vintage => new Date(vintage.generated_at).getTime() <= target)
      .filter(vintage => vintage.meetings.some(meeting => meeting.date === decision.date));
    const vintage = candidates.at(-1);
    if (!vintage) return;
    const meeting = vintage.meetings.find(item => item.date === decision.date);
    const probabilities = labels.map(label => Number(meeting.probabilities?.[label] || 0));
    const total = probabilities.reduce((sum, value) => sum + value, 0);
    if (!(total > 0)) return;
    const normalized = probabilities.map(value => value / total);
    const actualIndex = labels.indexOf(outcomeLabel(decision.change_bp));
    const realized = Math.max(1e-12, normalized[actualIndex]);
    results.push({
      realized,
      brier: normalized.reduce((sum, value, index) => sum + (value - (index === actualIndex ? 1 : 0)) ** 2, 0),
      logLoss: -Math.log(realized),
    });
  });
  const mean = key => results.reduce((sum, item) => sum + item[key], 0) / results.length;
  return { days, n: results.length, realized: mean("realized"), brier: mean("brier"), logLoss: mean("logLoss") };
}

function renderCurrent(dashboard) {
  const policy = dashboard.policy;
  const meeting = policy.meetings[0];
  const prices = meeting.prices || Object.entries(meeting.probabilities || {}).map(([label, probability]) => ({ label, probability, raw_probability: probability }));
  const raw = prices.map(item => item.raw_probability);
  const normalized = prices.map(item => item.probability);
  $("worked-example").innerHTML = `<span>CURRENT WORKED EXAMPLE · ${month(meeting.date)}</span><div>${prices.map((item, index) => `<p><b>${item.label}</b><em>${pct(raw[index])} raw</em><strong>${pct(normalized[index])} normalized</strong></p>`).join("")}</div><small>Raw Yes sum ${(100 * meeting.raw_total).toFixed(1)}¢ · expected incremental action ${bp(meeting.expected_change_bp)} · expected upper bound after meeting ${rate(meeting.expected_target_upper_after)}</small>`;
  const terminal = policy.terminal_anchor;
  const actionRate = policy.meetings.filter(item => item.date <= terminal.date).reduce((value, item) => value + item.expected_change_bp / 100, policy.target_upper_bound_baseline);
  const gap = 100 * (terminal.expected_target_upper - actionRate);
  $("method-coherence").innerHTML = `<div><span>Meeting-action view</span><strong>${rate(actionRate)}</strong><small>Expected level through ${month(terminal.date)}</small></div><div><span>Year-end market view</span><strong>${rate(terminal.expected_target_upper)}</strong><small>Expected value of 15 rate buckets</small></div><div class="coherence-alert"><span>Cross-market gap</span><strong>${bp(gap)}</strong><small>Year-end minus action-implied</small></div>`;
}

function renderResults(replay) {
  const horizons = [90, 30, 7, 1].map(days => scoreAtHorizon(replay, days)).filter(item => item.n);
  const resolved = replay.actual_target_upper.filter(decision => replay.vintages.some(vintage => vintage.meetings.some(meeting => meeting.date === decision.date)));
  const counts = Object.fromEntries(labels.map(label => [label, 0]));
  resolved.forEach(decision => counts[outcomeLabel(decision.change_bp)] += 1);
  $("sample-summary").textContent = `${resolved.length} resolved meetings have usable replay coverage: ${counts["No change"]} unchanged decisions, ${counts["25 bps decrease"]} quarter-point cuts, ${counts["50+ bps decrease"]} larger cut, and ${counts["25 bps increase"] + counts["50+ bps increase"]} hikes. That imbalance makes a broad calibration claim premature.`;
  $("results-body").innerHTML = horizons.map(item => `<tr><td>${item.days} ${item.days === 1 ? "day" : "days"} before</td><td>${item.n}</td><td>${pct(item.realized)}</td><td>${item.brier.toFixed(3)}</td><td>${item.logLoss.toFixed(3)}</td></tr>`).join("");
  const maxLoss = Math.max(...horizons.map(item => item.logLoss));
  $("score-chart").innerHTML = horizons.map(item => `<div class="score-row"><span>${item.days}D</span><div><i style="width:${100 * item.logLoss / maxLoss}%"></i></div><strong>${item.logLoss.toFixed(3)}</strong><small>LOG LOSS · N=${item.n}</small></div>`).join("");
}

async function main() {
  const [dashboardResponse, replayResponse] = await Promise.all([
    fetch("data/dashboard.json", { cache: "no-store" }),
    fetch("data/forecast-replay.json", { cache: "no-store" }),
  ]);
  if (!dashboardResponse.ok || !replayResponse.ok) throw new Error("Public forecast data are unavailable");
  renderCurrent(await dashboardResponse.json());
  renderResults(await replayResponse.json());
}

main().catch(error => {
  $("worked-example").textContent = String(error);
  $("sample-summary").textContent = "Evaluation data are temporarily unavailable.";
});
