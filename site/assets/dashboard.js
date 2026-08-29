"use strict";

const NS = "http://www.w3.org/2000/svg";
const DAY_MS = 86_400_000;
const DEFAULT_REPLAY_WINDOW_DAYS = 183;
const AXIS_TICK_PP = .25;
const AXIS_VIEW_SPAN_PP = 2.25;
const AXIS_PADDING_PP = .125;
const AXIS_MAX_SHIFT_PER_DAY_PP = .05;
const AXIS_CAMERA_MAX_PP_PER_SECOND = .26;
const AXIS_CAMERA_BASE_TIME_CONSTANT_MS = 260;
const AXIS_CAMERA_FAST_TIME_CONSTANT_MS = 34;
const FAN_AXIS_VIEW_SPAN_PP = AXIS_VIEW_SPAN_PP;
const FAN_AXIS_PADDING_PP = AXIS_PADDING_PP;
const FAN_TRANSITION_MS = 720;
const buckets = [
  { label: "−50 bp or more", key: "50+ bps decrease", color: "var(--down50)" },
  { label: "−25 bp", key: "25 bps decrease", color: "var(--down25)" },
  { label: "No change", key: "No change", color: "var(--hold)" },
  { label: "+25 bp", key: "25 bps increase", color: "var(--up25)" },
  { label: "+50 bp or more", key: "50+ bps increase", color: "var(--up50)" },
];
const categoryLabel = {
  down_50plus: "−50 bp or more",
  down_25: "−25 bp",
  unchanged: "No change",
  up_25: "+25 bp",
  up_50plus: "+50 bp or more",
  down: "Lower (grouped)",
  up: "Higher (grouped)",
};
const categoryLetter = {
  down_50plus: "−50",
  down_25: "−25",
  unchanged: "0",
  up_25: "+25",
  up_50plus: "+50",
  down: "D",
  up: "U",
};

const state = {
  dashboard: null,
  replay: null,
  index: [],
  vintages: [],
  position: 0,
  meetingDate: null,
  branchPath: [],
  timer: null,
  animationFrame: null,
  animationToken: 0,
  playing: false,
  playbackSpeed: 5,
  axisCenter: null,
  fanAxisCenter: null,
  fanAnimationFrame: null,
  fanAnimationToken: 0,
};

const $ = id => document.getElementById(id);
const pct = value => value == null ? "—" : `${(100 * value).toFixed(value < .01 ? 2 : 1)}%`;
const pp = value => `${value >= 0 ? "+" : ""}${value.toFixed(1)} pp`;
const bp = value => `${value >= 0 ? "+" : ""}${value.toFixed(1)} bp`;
const rate = value => `${value.toFixed(2)}%`;
const money = value => Number.isFinite(value)
  ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: value >= 1_000_000 ? 1 : 0 }).format(value)
  : "—";
const month = date => new Date(`${date}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const timestamp = value => new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short", timeZone: "America/New_York" }).format(new Date(value));
const calendarDate = value => new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "America/New_York" }).format(new Date(value));
const dateKey = value => {
  const parts = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: "America/New_York" }).formatToParts(new Date(value));
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
};
const dayValue = value => typeof value === "number" ? value : new Date(`${value}T12:00:00Z`).getTime();
const pointValue = point => point.time ?? dayValue(point.date);
const replayWindowDays = () => Math.max(30, Number(state.replay?.window_days) || DEFAULT_REPLAY_WINDOW_DAYS);
const replayWindowEnd = vintage => pointValue(vintage.horizon[0]) + replayWindowDays() * DAY_MS;
const visibleHorizonFor = (vintage, end = replayWindowEnd(vintage)) => {
  const visible = vintage.horizon.filter(point => pointValue(point) <= end + 1);
  return visible.length ? visible : [vintage.horizon[0]];
};
function axisTargetCenter(vintage) {
  const halfSpan = AXIS_VIEW_SPAN_PP / 2;
  const realized = realizedFor(vintage);
  const values = visibleHorizonFor(vintage).flatMap(point => [point.q05, point.q95, point.mean, point.q50]);
  values.push(vintage.baseline_target_upper, ...realized.flatMap(item => [item.before_upper, item.after_upper]));
  const minimumCenter = Math.max(...values) + AXIS_PADDING_PP - halfSpan;
  const maximumCenter = Math.min(...values) - AXIS_PADDING_PP + halfSpan;
  if (minimumCenter > maximumCenter) return (Math.min(...values) + Math.max(...values)) / 2;
  return Math.max(minimumCenter, Math.min(maximumCenter, vintage.baseline_target_upper));
}
function prepareAxisCenters(vintages) {
  let displayed = axisTargetCenter(vintages[0]);
  let priorTime = pointValue(vintages[0].horizon[0]);
  vintages.forEach((vintage, index) => {
    const currentTime = pointValue(vintage.horizon[0]);
    if (index) {
      const elapsedDays = Math.max(1, (currentTime - priorTime) / DAY_MS);
      const maximumShift = AXIS_MAX_SHIFT_PER_DAY_PP * elapsedDays;
      const difference = axisTargetCenter(vintage) - displayed;
      displayed += Math.max(-maximumShift, Math.min(maximumShift, difference));
    }
    vintage.axisCenter = displayed;
    priorTime = currentTime;
  });
}
const easeInOut = progress => progress < .5 ? 4 * progress ** 3 : 1 - ((-2 * progress + 2) ** 3) / 2;
const svg = (name, attrs = {}) => {
  const node = document.createElementNS(NS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
};
const meetingByDate = (vintage, date) => vintage?.meetings?.find(item => item.date === date);
const pricesByKey = meeting => meeting?.probabilities || Object.fromEntries((meeting?.prices || []).map(item => [item.label, item.probability]));

function setTooltip(event, lines) {
  const tip = $("tooltip");
  tip.innerHTML = lines.join("<br>");
  tip.style.display = "block";
  tip.style.left = `${Math.min(event.clientX + 13, window.innerWidth - 245)}px`;
  tip.style.top = `${Math.max(8, event.clientY - 35)}px`;
}
function hideTooltip() { $("tooltip").style.display = "none"; }

function drawReplayTimeline() {
  const holder = $("timeline");
  holder.replaceChildren();
  const denominator = Math.max(1, state.index.length - 1);
  state.index.forEach((item, index) => {
    const dot = document.createElement("i");
    dot.style.left = `${100 * index / denominator}%`;
    if (index && item.model_version !== state.index[index - 1].model_version) dot.className = "model-break";
    else if (item.kind === "event_checkpoint") dot.className = "event";
    else if (index && Math.abs(item.baseline_target_upper - state.index[index - 1].baseline_target_upper) > 1e-9) dot.className = "decision";
    holder.appendChild(dot);
  });
}

function renderMeetingTabs(vintage = state.vintages[state.position]) {
  const dates = vintage.meetings.map(item => item.date);
  if (!state.meetingDate || !dates.includes(state.meetingDate)) state.meetingDate = dates[0];
  const holder = $("meeting-tabs");
  holder.replaceChildren();
  dates.forEach(date => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = month(date);
    button.setAttribute("aria-pressed", String(date === state.meetingDate));
    button.onclick = () => {
      state.meetingDate = date;
      renderMeetingTabs();
      renderSelectedState();
    };
    holder.appendChild(button);
  });
}

function renderHeader(vintage) {
  const item = state.index[state.position];
  const isEvent = item.kind === "event_checkpoint";
  const isHistorical = item.kind === "historical_daily";
  const hasHistoricalTerminal = isHistorical && vintage.horizon.some(point => point.kind === "terminal");
  const priorBaseline = state.position ? state.index[state.position - 1].baseline_target_upper : item.baseline_target_upper;
  const isDecisionRoll = isHistorical && Math.abs(item.baseline_target_upper - priorBaseline) > 1e-9;
  const windowEnd = replayWindowEnd(vintage);
  const visibleHorizon = visibleHorizonFor(vintage, windowEnd);
  const future = visibleHorizon.filter(point => point.kind !== "vintage");
  const first = future[0] || visibleHorizon[0];
  const last = future.at(-1) || visibleHorizon.at(-1);
  const coverageEnd = pointValue(visibleHorizon.at(-1));
  const coverageGap = windowEnd - coverageEnd > 14 * DAY_MS;
  let coverageCopy = coverageGap
    ? `Observable market coverage ends ${month(visibleHorizon.at(-1).date)}; the remaining window is intentionally blank.`
    : `Observable market coverage reaches the end of the six-month window.`;
  const carriedMeetings = vintage.meetings.filter(meeting => meeting.quote_status === "carried_forward");
  if (carriedMeetings.length) {
    coverageCopy += ` ${carriedMeetings.map(meeting => month(meeting.date)).join(" and ")} ${carriedMeetings.length === 1 ? "is" : "are"} held at the last archived pre-window quote because the intraday collector did not include ${carriedMeetings.length === 1 ? "that contract" : "those contracts"}.`;
  }
  const modelVersion = item.model_version || "historical-native-meeting-v1";
  const modelCopy = state.replay.model_versions?.[modelVersion] || "Versioned forward-fan model.";
  const isLatest = state.position === state.index.length - 1;
  const firstMeeting = vintage.meetings[0];
  const firstPrices = pricesByKey(firstMeeting);
  const hold = firstPrices["No change"] || 0;
  const cut = (firstPrices["50+ bps decrease"] || 0) + (firstPrices["25 bps decrease"] || 0);
  const hike = (firstPrices["25 bps increase"] || 0) + (firstPrices["50+ bps increase"] || 0);
  $("selected-time").textContent = isHistorical
    ? `${calendarDate(item.generated_at)} · DAILY RECONSTRUCTED MARK`
    : `${timestamp(item.generated_at)} · ${isLatest ? "LATEST ARCHIVED SNAPSHOT" : isEvent ? "EVENT CHECKPOINT" : "FORECAST VINTAGE"}`;
  $("selected-label").textContent = isLatest && firstMeeting
    ? `${month(firstMeeting.date)}: ${pct(cut)} lower · ${pct(hold)} unchanged · ${pct(hike)} higher`
    : isEvent
      ? `${item.label}: the forward cone reprices`
      : isHistorical
        ? `Daily reconstructed mark — ${calendarDate(item.generated_at)}`
        : `Forecast made ${timestamp(item.generated_at)}`;
  $("selected-context").textContent = isEvent
    ? `${item.event}. The chart holds the vintage fixed at this checkpoint inside the same six-month window used for every replay frame. ${coverageCopy}`
    : isHistorical
      ? `This is a reconstructed daily Polymarket vintage in a fixed six-month rolling window. The forecast uses only meetings with observable markets that day; the realized Fed path is added afterward for comparison. Model ${modelVersion}: ${modelCopy} ${coverageCopy}`
      : `The slider selects when the forecast was made. Every frame then covers the same six-month duration, and the realized Fed path is overlaid only where outcomes are known. ${coverageCopy}`;
  const readout = [
    [rate(vintage.baseline_target_upper), "target upper at vintage", "realized starting point"],
    [rate(first.q50), `${month(first.date)} median`, `${rate(first.q25)}–${rate(first.q75)} middle 50%`],
    [rate(last.q50), `${month(last.date)} median`, `${rate(last.q05)}–${rate(last.q95)} 90% range`],
    [`${state.position + 1}/${state.index.length}`, "archive position", isEvent ? "intraday event stop" : "one observation per day"],
  ];
  $("top-readout").innerHTML = readout.map(([value, label, detail]) => `<div class="readout"><span>${label}</span><strong>${value}</strong><small>${detail}</small></div>`).join("");
  $("position").textContent = `${state.position + 1} / ${state.index.length} · ${timestamp(item.generated_at)}`;
  $("event-note").innerHTML = isEvent
    ? `<strong>EVENT WINDOW</strong> ${item.event} · timing is observable; causal attribution is not clean because scheduled data arrived concurrently.`
    : `<strong>${isDecisionRoll ? "POST-MEETING ROLL" : isHistorical ? "HISTORICAL DAILY MARK" : "VINTAGE DATE"}</strong> ${isHistorical ? calendarDate(item.generated_at) : timestamp(item.generated_at)} · ${isHistorical ? `${isDecisionRoll ? `the official target upper bound moved from ${rate(priorBaseline)} to ${rate(item.baseline_target_upper)}; ` : ""}${vintage.meetings.length} observable future meeting market${vintage.meetings.length === 1 ? "" : "s"}; model ${modelVersion}${hasHistoricalTerminal ? " with quoted terminal endpoint" : " meeting-only"}.` : "the cone looks forward from this observation; use ← / → or Play to move one day at a time."} Fixed window through ${month(new Date(windowEnd).toISOString().slice(0, 10))}.`;
  $("forward-title").textContent = `Forward distribution as seen on ${timestamp(item.generated_at)}`;
  $("forward-caption").textContent = `Fixed six-month viewport with a scrolling 0.25-point rate grid. The ${rate(last.q50)} median is modeled from quoted marginals under ${modelVersion}; bands stop at ${month(last.date)} and blank space is not extrapolated.`;
  $("footer-time").textContent = `Selected ${timestamp(item.generated_at)}`;
  const age = (Date.now() - new Date(state.dashboard.forecast_generated_at)) / 36e5;
  const freshness = $("freshness");
  freshness.className = `status ${age < 9 ? "" : age < 18 ? "warning" : "stale"}`.trim();
  freshness.innerHTML = `<i></i> Latest snapshot ${age < 9 ? "current" : age < 18 ? "aging" : "stale"} · ${timestamp(state.dashboard.forecast_generated_at)}`;
}

function realizedFor(vintage) {
  const start = dayValue(vintage.horizon[0].date);
  const end = replayWindowEnd(vintage);
  const today = dateKey(state.dashboard.forecast_generated_at);
  return state.replay.actual_target_upper.filter(item => item.date > vintage.horizon[0].date && item.date <= today && dayValue(item.date) <= end && dayValue(item.date) >= start);
}

function forwardDomain(vintage) {
  const xMin = pointValue(vintage.horizon[0]);
  const xMax = replayWindowEnd(vintage);
  const axisCenter = Number.isFinite(vintage.axisCenter) ? vintage.axisCenter : axisTargetCenter(vintage);
  return {
    xMin,
    xMax,
    yMin: axisCenter - AXIS_VIEW_SPAN_PP / 2,
    yMax: axisCenter + AXIS_VIEW_SPAN_PP / 2,
  };
}

function domainAtAxisCenter(vintage, axisCenter = state.axisCenter) {
  const domain = forwardDomain(vintage);
  const center = Number.isFinite(axisCenter)
    ? axisCenter
    : (Number.isFinite(vintage.axisCenter) ? vintage.axisCenter : axisTargetCenter(vintage));
  return {
    ...domain,
    yMin: center - AXIS_VIEW_SPAN_PP / 2,
    yMax: center + AXIS_VIEW_SPAN_PP / 2,
  };
}

function axisCameraTimeConstant() {
  return AXIS_CAMERA_BASE_TIME_CONSTANT_MS
    + AXIS_CAMERA_FAST_TIME_CONSTANT_MS * Math.max(0, state.playbackSpeed - 5);
}

function advanceAxisCamera(target, elapsedMs) {
  if (!Number.isFinite(state.axisCenter)) state.axisCenter = target;
  const deltaMs = Math.max(0, Math.min(64, elapsedMs));
  const response = 1 - Math.exp(-deltaMs / axisCameraTimeConstant());
  const proposedShift = (target - state.axisCenter) * response;
  const maximumShift = AXIS_CAMERA_MAX_PP_PER_SECOND * deltaMs / 1000;
  state.axisCenter += Math.max(-maximumShift, Math.min(maximumShift, proposedShift));
}

function drawForward(vintage = state.vintages[state.position], transient = false, suppliedDomain = null) {
  const element = $("forward-chart");
  const width = Math.max(320, element.parentElement.clientWidth);
  const height = Math.max(340, element.parentElement.clientHeight - 8);
  const narrow = width < 600;
  const margin = { top: 38, right: 20, bottom: 52, left: narrow ? 48 : 60 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  element.setAttribute("viewBox", `0 0 ${width} ${height}`);
  [...element.children].filter(node => !["title", "desc"].includes(node.tagName)).forEach(node => node.remove());
  const domain = suppliedDomain || forwardDomain(vintage);
  const firstDate = domain.xMin;
  const lastDate = domain.xMax;
  const visibleHorizon = visibleHorizonFor(vintage, lastDate);
  const coverageEnd = pointValue(visibleHorizon.at(-1));
  const x = value => margin.left + Math.max(0, Math.min(1, ((typeof value === "number" ? value : dayValue(value)) - firstDate) / Math.max(1, lastDate - firstDate))) * plotWidth;
  const today = dateKey(state.dashboard.forecast_generated_at);
  const realizedDecisions = realizedFor(vintage);
  const minimum = domain.yMin;
  const maximum = domain.yMax;
  const y = value => margin.top + (maximum - value) * plotHeight / Math.max(.25, maximum - minimum);
  const firstTick = Math.ceil((minimum - 1e-9) / AXIS_TICK_PP) * AXIS_TICK_PP;
  for (let value = firstTick; value <= maximum + 1e-9; value += AXIS_TICK_PP) {
    const normalized = Math.round(value / AXIS_TICK_PP) * AXIS_TICK_PP;
    const major = Math.abs(normalized * 2 - Math.round(normalized * 2)) < 1e-9;
    element.appendChild(svg("line", { x1: margin.left, x2: width - margin.right, y1: y(normalized), y2: y(normalized), class: major ? "gridline gridline-major" : "gridline" }));
    const label = svg("text", { x: margin.left - 8, y: y(normalized) + 3, "text-anchor": "end", class: major ? "axis-label axis-label-major" : "axis-label" });
    label.textContent = rate(normalized);
    element.appendChild(label);
  }
  const meetingDates = [...new Set(state.replay?.meeting_calendar?.length
    ? state.replay.meeting_calendar
    : vintage.meetings.map(item => item.date))]
    .filter(date => dayValue(date) >= firstDate && dayValue(date) <= lastDate);
  meetingDates.forEach(date => {
    const meetingX = x(date);
    element.appendChild(svg("line", { x1: meetingX, x2: meetingX, y1: margin.top, y2: height - margin.bottom, class: "meeting-rule" }));
    const label = svg("text", { x: meetingX + 5, y: margin.top + 11, class: "meeting-rule-label" });
    label.textContent = "FOMC";
    element.appendChild(label);
  });
  const area = (low, high) => `M${visibleHorizon.map(point => `${x(pointValue(point))},${y(point[high])}`).join(" L")} L${[...visibleHorizon].reverse().map(point => `${x(pointValue(point))},${y(point[low])}`).join(" L")} Z`;
  if (visibleHorizon.length > 1) {
    element.appendChild(svg("path", { d: area("q05", "q95"), class: "forecast-area-90" }));
    element.appendChild(svg("path", { d: area("q25", "q75"), class: "forecast-area-50" }));
  }
  element.appendChild(svg("polyline", { points: visibleHorizon.map(point => `${x(pointValue(point))},${y(point.q50)}`).join(" "), class: "forecast-median" }));
  if (lastDate - coverageEnd > 14 * DAY_MS) {
    element.appendChild(svg("line", { x1: x(coverageEnd), x2: x(coverageEnd), y1: margin.top, y2: height - margin.bottom, class: "coverage-line" }));
    const coverageLabel = svg("text", { x: x(coverageEnd) + 5, y: margin.top + 12, class: "coverage-label" });
    coverageLabel.textContent = "MARKET COVERAGE ENDS";
    element.appendChild(coverageLabel);
  }
  const actualPoints = [{ date: pointValue(visibleHorizon[0]), rate: vintage.baseline_target_upper }];
  let actualRate = vintage.baseline_target_upper;
  realizedDecisions.forEach(decision => {
    actualPoints.push({ date: decision.date, rate: actualRate });
    actualRate = decision.after_upper;
    actualPoints.push({ date: decision.date, rate: actualRate });
  });
  const todayOnChart = Math.min(dayValue(today), lastDate) === lastDate ? lastDate : dayValue(today);
  actualPoints.push({ date: todayOnChart, rate: actualRate });
  element.appendChild(svg("polyline", { points: actualPoints.map(point => `${x(point.date)},${y(point.rate)}`).join(" "), class: "realized-path" }));
  element.appendChild(svg("circle", { cx: x(todayOnChart), cy: y(actualRate), r: 4.5, class: "realized-dot" }));
  visibleHorizon.forEach((point, index) => {
    const text = svg("text", { x: x(pointValue(point)), y: height - 18, "text-anchor": index === 0 ? "start" : "middle", class: "axis-label" });
    text.textContent = index === 0 ? `Vintage ${month(point.date)}` : month(point.date);
    element.appendChild(text);
  });
  const windowLabel = svg("text", { x: width - margin.right, y: height - 18, "text-anchor": "end", class: "window-label" });
  windowLabel.textContent = `6M WINDOW · ${month(new Date(lastDate).toISOString().slice(0, 10))}`;
  element.appendChild(windowLabel);
  if (!transient) $("horizon-strip").innerHTML = visibleHorizon.slice(1).map(point => `<div class="horizon-cell"><span>${month(point.date)} distribution</span><strong>${rate(point.q50)}</strong><small>50% ${rate(point.q25)}–${rate(point.q75)} · mean ${rate(point.mean)}</small></div>`).join("");
}

function renderMeetingTable(vintage) {
  const urls = state.dashboard.policy.source_urls || {};
  const historicalMapping = vintage.kind === "historical_daily" || vintage.meetings.some(meeting => Array.isArray(meeting.native_outcomes));
  $("market-note").textContent = historicalMapping
    ? "Historical contracts retain their native labels in each cell tooltip. The fixed five-column view is a canonical display mapping; open-ended 25+ outcomes are not evidence of an exact 25 bp move."
    : "Five separate Yes/No contracts form each event. We divide each Yes price by their sum to create one distribution; activity and quote-quality metadata are stamped when observed.";
  $("meeting-table").innerHTML = vintage.meetings.map(meeting => {
    const values = pricesByKey(meeting);
    const nativeLabels = Array.isArray(meeting.native_outcomes) ? meeting.native_outcomes.map(item => item.label).join(" · ") : null;
    const cells = buckets.map(bucket => `<td data-bucket${nativeLabels ? ` title="Canonical display mapping; native event labels: ${nativeLabels}"` : ""} style="--width:${100 * (values[bucket.key] || 0)}%;--bucket:${bucket.color}">${pct(values[bucket.key])}</td>`).join("");
    const url = meeting.event_url || urls[meeting.event_slug];
    const activity = meeting.activity;
    const activityTitle = activity?.as_of ? `Polymarket activity snapshot: ${timestamp(activity.as_of)}` : "Polymarket activity snapshot";
    const quote = meeting.quote_quality;
    const quoteCopy = quote ? `${quote.source === "clob_midpoint" ? "CLOB MID" : quote.source === "gamma" ? "GAMMA" : "MIXED"} · ${String(quote.quality).toUpperCase()}` : null;
    const rawCopy = Number.isFinite(meeting.raw_total) ? `ΣYES ${(100 * meeting.raw_total).toFixed(1)}¢` : null;
    const spreadCopy = Number.isFinite(quote?.max_spread) ? `MAX SPREAD ${(100 * quote.max_spread).toFixed(1)}¢` : null;
    const activityCell = activity
      ? `<td class="market-activity" title="${activityTitle}${quote?.as_of ? ` · quote ${timestamp(quote.as_of)}` : ""}"><strong>${money(activity.volume_24h)}</strong><small>24H · ${money(activity.liquidity)} LIQ.</small>${quoteCopy ? `<small>${quoteCopy}</small>` : ""}${rawCopy ? `<small>${rawCopy}${spreadCopy ? ` · ${spreadCopy}` : ""}</small>` : ""}</td>`
      : `<td class="market-activity unavailable"><strong>—</strong><small>NOT ARCHIVED</small></td>`;
    const held = meeting.quote_status === "carried_forward"
      ? `<small class="held-mark" title="Held from ${timestamp(meeting.carried_forward_from)}">HELD</small>`
      : "";
    const mapped = nativeLabels ? `<small class="held-mark" title="${nativeLabels}">MAPPED</small>` : "";
    return `<tr class="${meeting.date === state.meetingDate ? "focus" : ""}"><td class="meeting-name">${month(meeting.date)}${held}${mapped}</td>${cells}${activityCell}<td>${url ? `<a class="market-link" href="${url}" target="_blank" rel="noopener">OPEN ↗</a>` : "—"}</td></tr>`;
  }).join("");
}

function renderChange(vintage) {
  const current = meetingByDate(vintage, state.meetingDate);
  let priorIndex = state.position - 1;
  while (priorIndex >= 0 && !meetingByDate(state.vintages[priorIndex], state.meetingDate)) priorIndex -= 1;
  const prior = priorIndex >= 0 ? meetingByDate(state.vintages[priorIndex], state.meetingDate) : null;
  $("change-title").textContent = `${month(state.meetingDate)} versus prior stop`;
  if (!current) {
    $("change-sub").textContent = "This meeting was not archived at the selected checkpoint.";
    $("change-bars").innerHTML = "";
    return;
  }
  if (!prior) {
    $("change-sub").textContent = "First archived observation; no earlier comparison is available.";
    $("change-bars").innerHTML = buckets.map(bucket => `<div class="change-row"><span>${bucket.label}</span><div class="diverge"></div><strong>—</strong></div>`).join("");
    return;
  }
  const currentPrices = pricesByKey(current);
  const priorPrices = pricesByKey(prior);
  const changes = buckets.map(bucket => 100 * ((currentPrices[bucket.key] || 0) - (priorPrices[bucket.key] || 0)));
  const scale = Math.max(1, ...changes.map(Math.abs));
  $("change-sub").textContent = `${timestamp(state.index[priorIndex].generated_at)} → ${timestamp(state.index[state.position].generated_at)}`;
  $("change-bars").innerHTML = buckets.map((bucket, index) => {
    const change = changes[index];
    const width = 50 * Math.abs(change) / scale;
    const left = change >= 0 ? 50 : 50 - width;
    return `<div class="change-row"><span>${bucket.label}</span><div class="diverge"><i style="left:${left}%;width:${width}%;--bucket:${bucket.color}"></i></div><strong>${pp(change)}</strong></div>`;
  }).join("");
}

function renderCoherence(visible) {
  $("coherence").hidden = !visible;
  $("coherence").style.display = visible ? "" : "none";
  if (!visible) return;
  const policy = state.dashboard.policy;
  const terminal = policy.terminal_anchor;
  const terminalDate = terminal.date;
  const actionRate = policy.meetings
    .filter(meeting => meeting.date <= terminalDate)
    .reduce((level, meeting) => level + meeting.expected_change_bp / 100, policy.target_upper_bound_baseline);
  const terminalRate = terminal.expected_target_upper;
  const gap = 100 * (terminalRate - actionRate);
  const quality = terminal.quote_quality || {};
  const activity = terminal.activity || {};
  $("coherence").innerHTML = `
    <div><span>MEETING-ACTION PATH</span><strong>${rate(actionRate)}</strong><small>Cumulative expected meeting moves through ${month(terminalDate)}</small></div>
    <div><span>YEAR-END RATE MARKET</span><strong>${rate(terminalRate)}</strong><small>Independent 15-bucket target-rate distribution</small></div>
    <div class="${Math.abs(gap) >= 12.5 ? "coherence-alert" : ""}"><span>CROSS-MARKET GAP</span><strong>${bp(gap)}</strong><small>Shown as disagreement—not a Fed action</small></div>
    <div><span>YEAR-END MARKET QUALITY</span><strong>${Number.isFinite(terminal.raw_total) ? `${(100 * terminal.raw_total).toFixed(1)}¢` : "—"}</strong><small>ΣYes · ${Number.isFinite(quality.max_spread) ? `${(100 * quality.max_spread).toFixed(1)}¢ max spread` : "spread unavailable"}${Number.isFinite(activity.liquidity) ? ` · ${money(activity.liquidity)} liquidity` : ""}</small></div>`;
}

function weightedQuantile(points, q) {
  const ordered = [...points].sort((a, b) => a.rate - b.rate);
  const total = ordered.reduce((sum, point) => sum + point.weight, 0);
  let cumulative = 0;
  for (const point of ordered) {
    cumulative += point.weight / total;
    if (cumulative >= q - 1e-12) return point.rate;
  }
  return ordered.at(-1).rate;
}

function renderTree(vintage, animateFromPath = null) {
  const tree = vintage.policy.tree;
  const unavailable = $("tree-availability");
  const content = $("path-content");
  if (!tree) {
    if (state.fanAnimationFrame != null) window.cancelAnimationFrame(state.fanAnimationFrame);
    state.fanAnimationFrame = null;
    state.fanAnimationToken += 1;
    unavailable.hidden = false;
    unavailable.innerHTML = `<strong>The historical forward fan above is available; the clickable branch tree is current-only.</strong> Historical vintages preserve quoted meeting marginals and a compact percentile cone, not every conditional node. Move the replay to Latest to explore today’s full meeting-by-meeting branch tree.`;
    content.hidden = true;
    return;
  }
  unavailable.hidden = true;
  content.hidden = false;
  const meetings = vintage.policy.meetings;
  const nodes = new Map(tree.nodes.map(node => [node.id, node]));
  if (state.branchPath.length > meetings.length) state.branchPath = [];
  const idForPath = path => path.length ? path.join("_") : "root";
  const selectedNode = nodes.get(idForPath(state.branchPath));
  const depth = state.branchPath.length;
  $("branch-step").textContent = depth < meetings.length ? `NEXT DECISION · ${depth + 1} OF ${meetings.length}` : "COMPLETE CONDITIONAL PATH";
  $("branch-meeting").textContent = depth < meetings.length ? month(meetings[depth].date) : "Terminal state";
  const selectedPath = state.branchPath.map(value => categoryLetter[value]).join(" → ");
  if (!depth) {
    $("branch-path").textContent = `Start at ${rate(vintage.policy.target_upper_bound_baseline)}. Choose a realized outcome to update every later branch.`;
  } else {
    $("branch-path").textContent = `${selectedPath} · ${(100 * selectedNode.probability).toFixed(2)}% unconditional path mass · ${rate(selectedNode.rate)} action-implied path level`;
  }
  const holder = $("branch-buttons");
  holder.replaceChildren();
  if (depth < meetings.length) {
    selectedNode.branches.forEach(branch => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "branch-button";
      const branchDetail = branch.category === "down_50plus" || branch.category === "up_50plus"
        ? `open-ended quoted tail · modeled at ${bp(branch.representative_action_bp)}`
        : branch.category === "unchanged" ? "quoted 0 bp action" : "quoted exact action contract";
      button.innerHTML = `<span class="letter">${categoryLetter[branch.category]}</span><span><b>${categoryLabel[branch.category]}</b><small>${branchDetail}</small></span><strong>${pct(branch.conditional_probability)}</strong>`;
      button.onclick = () => {
        const previousPath = [...state.branchPath];
        state.branchPath.push(branch.category);
        renderTree(vintage, previousPath);
      };
      holder.appendChild(button);
    });
  }
  const back = $("branch-back");
  back.hidden = state.branchPath.length === 0;
  back.onclick = () => {
    const previousPath = [...state.branchPath];
    state.branchPath.pop();
    renderTree(vintage, previousPath);
  };
  drawFan(tree, meetings, nodes, animateFromPath);
}

const fanNodeId = path => path.length ? path.join("_") : "root";

function fanRowsForPath(tree, meetings, nodes, prefix) {
  return Array.from({ length: meetings.length + 1 }, (_, depth) => {
    let candidates;
    if (depth <= prefix.length) {
      candidates = [nodes.get(fanNodeId(prefix.slice(0, depth)))];
    } else {
      candidates = tree.nodes.filter(node => node.depth === depth && prefix.every((value, index) => node.path[index] === value));
    }
    const points = candidates.flatMap(node => {
      const distribution = Array.isArray(node.rate_distribution) && node.rate_distribution.length
        ? node.rate_distribution
        : [{ rate: node.rate, probability: 1 }];
      return distribution.map(point => ({
        rate: point.rate,
        weight: node.probability * point.probability,
        node,
      }));
    });
    return { depth, q05: weightedQuantile(points, .05), q25: weightedQuantile(points, .25), q50: weightedQuantile(points, .5), q75: weightedQuantile(points, .75), q95: weightedQuantile(points, .95) };
  });
}

function fanSelectedNodes(nodes, prefix) {
  return [nodes.get("root"), ...prefix.map((_, index) => nodes.get(fanNodeId(prefix.slice(0, index + 1))))];
}

function fanAxisTargetCenter(rows, selectedNodes) {
  const values = rows.flatMap(row => [row.q05, row.q25, row.q50, row.q75, row.q95]);
  values.push(...selectedNodes.map(node => node.rate));
  const halfSpan = FAN_AXIS_VIEW_SPAN_PP / 2;
  const minimumCenter = Math.max(...values) + FAN_AXIS_PADDING_PP - halfSpan;
  const maximumCenter = Math.min(...values) - FAN_AXIS_PADDING_PP + halfSpan;
  if (minimumCenter > maximumCenter) return (Math.min(...values) + Math.max(...values)) / 2;
  const preferred = selectedNodes.at(-1).rate;
  return Math.max(minimumCenter, Math.min(maximumCenter, preferred));
}

function selectedPoint(node, depth) { return { depth, rate: node.rate }; }

function interpolateFanRows(fromRows, toRows, progress) {
  const keys = ["q05", "q25", "q50", "q75", "q95"];
  return toRows.map((row, index) => ({
    depth: row.depth,
    ...Object.fromEntries(keys.map(key => [key, fromRows[index][key] + (row[key] - fromRows[index][key]) * progress])),
  }));
}

function transitionalSelectedPoints(fromNodes, toNodes, progress) {
  if (progress >= 1) return toNodes.map(selectedPoint);
  if (toNodes.length > fromNodes.length) {
    const points = toNodes.slice(0, -1).map(selectedPoint);
    const parent = fromNodes.at(-1);
    const child = toNodes.at(-1);
    points.push({
      depth: fromNodes.length - 1 + progress,
      rate: parent.rate + (child.rate - parent.rate) * progress,
    });
    return points;
  }
  if (toNodes.length < fromNodes.length) {
    const points = toNodes.map(selectedPoint);
    const removed = fromNodes.at(-1);
    const parent = toNodes.at(-1);
    points.push({
      depth: fromNodes.length - 1 - progress,
      rate: removed.rate + (parent.rate - removed.rate) * progress,
    });
    return points;
  }
  return toNodes.map((node, depth) => ({
    depth,
    rate: fromNodes[depth].rate + (node.rate - fromNodes[depth].rate) * progress,
  }));
}

function drawFanFrame(meetings, rows, selectedPoints, axisCenter) {
  const element = $("fan-chart");
  const width = Math.max(320, element.parentElement.clientWidth);
  const height = Math.max(320, element.parentElement.clientHeight - 25);
  const narrow = width < 620;
  const margin = { top: 22, right: 16, bottom: 52, left: narrow ? 46 : 58 };
  const x = depth => margin.left + depth * (width - margin.left - margin.right) / meetings.length;
  const minimum = axisCenter - FAN_AXIS_VIEW_SPAN_PP / 2;
  const maximum = axisCenter + FAN_AXIS_VIEW_SPAN_PP / 2;
  const y = value => height - margin.bottom - (value - minimum) * (height - margin.top - margin.bottom) / (maximum - minimum);
  const area = (low, high) => `M${rows.map(row => `${x(row.depth)},${y(row[high])}`).join(" L")} L${[...rows].reverse().map(row => `${x(row.depth)},${y(row[low])}`).join(" L")} Z`;
  element.setAttribute("viewBox", `0 0 ${width} ${height}`);
  element.replaceChildren();
  const firstTick = Math.ceil((minimum - 1e-9) / AXIS_TICK_PP) * AXIS_TICK_PP;
  for (let value = firstTick; value <= maximum + 1e-9; value += AXIS_TICK_PP) {
    const normalized = Math.round(value / AXIS_TICK_PP) * AXIS_TICK_PP;
    const major = Math.abs(normalized * 2 - Math.round(normalized * 2)) < 1e-9;
    element.appendChild(svg("line", { x1: margin.left, x2: width - margin.right, y1: y(normalized), y2: y(normalized), class: `gridline${major ? " gridline-major" : ""}` }));
    const label = svg("text", { x: margin.left - 8, y: y(normalized) + 3, "text-anchor": "end", class: major ? "axis-label axis-label-major" : "axis-label" });
    label.textContent = rate(normalized);
    element.appendChild(label);
  }
  element.appendChild(svg("path", { d: area("q05", "q95"), fill: "rgba(89,209,199,.11)" }));
  element.appendChild(svg("path", { d: area("q25", "q75"), fill: "rgba(89,209,199,.24)" }));
  element.appendChild(svg("polyline", { points: rows.map(row => `${x(row.depth)},${y(row.q50)}`).join(" "), fill: "none", stroke: "var(--cyan)", "stroke-width": "2.5" }));
  selectedPoints.slice(1).forEach((point, index) => {
    const previous = selectedPoints[index];
    element.appendChild(svg("line", {
      x1: x(previous.depth), y1: y(previous.rate), x2: x(point.depth), y2: y(point.rate),
      stroke: "var(--orange)", "stroke-width": "2.5",
    }));
  });
  selectedPoints.forEach(point => {
    element.appendChild(svg("circle", { cx: x(point.depth), cy: y(point.rate), r: 4, fill: "var(--orange)", stroke: "var(--bg)", "stroke-width": "1.5" }));
  });
  ["Now", ...meetings.map(item => month(item.date))].forEach((label, index) => {
    const text = svg("text", { x: x(index), y: height - 20, "text-anchor": index === 0 ? "start" : index === meetings.length ? "end" : "middle", class: "axis-label" });
    text.textContent = label;
    element.appendChild(text);
  });
}

function drawFan(tree, meetings, nodes, animateFromPath = null) {
  if (state.fanAnimationFrame != null) window.cancelAnimationFrame(state.fanAnimationFrame);
  state.fanAnimationFrame = null;
  const token = ++state.fanAnimationToken;
  const targetPath = [...state.branchPath];
  const targetRows = fanRowsForPath(tree, meetings, nodes, targetPath);
  const targetNodes = fanSelectedNodes(nodes, targetPath);
  const targetCenter = fanAxisTargetCenter(targetRows, targetNodes);
  $("fan-caption").textContent = targetPath.length
    ? `Conditional on ${targetPath.map(value => categoryLetter[value]).join(" → ")} · orange stays on the discrete meeting-action lattice`
    : "Cyan median · 50% / 90% conditional ranges · fixed 0.25-point scrolling grid";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!animateFromPath || reducedMotion) {
    state.fanAxisCenter = targetCenter;
    drawFanFrame(meetings, targetRows, targetNodes.map(selectedPoint), targetCenter);
    return;
  }
  const fromPath = [...animateFromPath];
  const fromRows = fanRowsForPath(tree, meetings, nodes, fromPath);
  const fromNodes = fanSelectedNodes(nodes, fromPath);
  const startCenter = Number.isFinite(state.fanAxisCenter) ? state.fanAxisCenter : fanAxisTargetCenter(fromRows, fromNodes);
  const startedAt = performance.now();
  const frame = now => {
    if (token !== state.fanAnimationToken) return;
    const linear = Math.min(1, (now - startedAt) / FAN_TRANSITION_MS);
    const progress = easeInOut(linear);
    state.fanAxisCenter = startCenter + (targetCenter - startCenter) * progress;
    drawFanFrame(
      meetings,
      interpolateFanRows(fromRows, targetRows, progress),
      transitionalSelectedPoints(fromNodes, targetNodes, progress),
      state.fanAxisCenter,
    );
    if (linear < 1) state.fanAnimationFrame = window.requestAnimationFrame(frame);
    else state.fanAnimationFrame = null;
  };
  state.fanAnimationFrame = window.requestAnimationFrame(frame);
}

function renderSelectedState() {
  const vintage = state.vintages[state.position];
  const atLatest = state.position === state.index.length - 1;
  renderHeader(vintage);
  renderMeetingTable(vintage);
  renderChange(vintage);
  renderCoherence(atLatest);
  renderTree(atLatest
    ? { policy: state.dashboard.policy, label: "current full-market forecast" }
    : { policy: { tree: null }, label: "historical forecast vintage" });
}

function selectPosition(position, preserveAxis = false) {
  state.position = Math.max(0, Math.min(state.index.length - 1, Number(position)));
  if (!preserveAxis || !Number.isFinite(state.axisCenter)) {
    state.axisCenter = state.vintages[state.position].axisCenter;
  }
  if (state.fanAnimationFrame != null) window.cancelAnimationFrame(state.fanAnimationFrame);
  state.fanAnimationFrame = null;
  state.fanAnimationToken += 1;
  state.fanAxisCenter = null;
  state.branchPath = [];
  $("vintage-slider").value = String(state.position);
  $("date-picker").value = dateKey(state.index[state.position].generated_at);
  renderMeetingTabs(state.vintages[state.position]);
  renderSelectedState();
  drawForward(state.vintages[state.position], false, domainAtAxisCenter(state.vintages[state.position]));
}

function sampleHorizon(horizon, time) {
  if (time <= pointValue(horizon[0])) return horizon[0];
  if (time >= pointValue(horizon.at(-1))) return horizon.at(-1);
  const rightIndex = horizon.findIndex(point => pointValue(point) >= time);
  const right = horizon[rightIndex];
  if (pointValue(right) === time) return right;
  const left = horizon[rightIndex - 1];
  const weight = (time - pointValue(left)) / Math.max(1, pointValue(right) - pointValue(left));
  const interpolate = key => left[key] + (right[key] - left[key]) * weight;
  return {
    q05: interpolate("q05"), q25: interpolate("q25"), q50: interpolate("q50"),
    q75: interpolate("q75"), q95: interpolate("q95"), mean: interpolate("mean"),
  };
}

function interpolateVintage(from, to, progress) {
  const ease = easeInOut(progress);
  const interpolate = (left, right) => left + (right - left) * ease;
  const startTime = interpolate(pointValue(from.horizon[0]), pointValue(to.horizon[0]));
  const meetingTimes = [...new Set(
    [...from.horizon.slice(1), ...to.horizon.slice(1)].map(pointValue)
  )].filter(time => time > startTime + 1).sort((left, right) => left - right);
  const times = [startTime, ...meetingTimes];
  const horizon = times.map((time, index) => {
    const left = sampleHorizon(from.horizon, time);
    const right = sampleHorizon(to.horizon, time);
    return {
      date: new Date(time).toISOString().slice(0, 10),
      time,
      kind: index === 0 ? "vintage" : "meeting",
      q05: interpolate(left.q05, right.q05),
      q25: interpolate(left.q25, right.q25),
      q50: interpolate(left.q50, right.q50),
      q75: interpolate(left.q75, right.q75),
      q95: interpolate(left.q95, right.q95),
      mean: interpolate(left.mean, right.mean),
    };
  });
  return {
    ...to,
    baseline_target_upper: interpolate(from.baseline_target_upper, to.baseline_target_upper),
    horizon,
  };
}

function interpolateDomain(from, to, progress) {
  const ease = easeInOut(progress);
  const left = forwardDomain(from);
  const right = forwardDomain(to);
  const interpolate = key => left[key] + (right[key] - left[key]) * ease;
  return { xMin: interpolate("xMin"), xMax: interpolate("xMax"), yMin: interpolate("yMin"), yMax: interpolate("yMax") };
}

function cancelAnimation() {
  state.animationToken += 1;
  if (state.animationFrame) window.cancelAnimationFrame(state.animationFrame);
  state.animationFrame = null;
}

function animateToPosition(position, onComplete = null) {
  const target = Math.max(0, Math.min(state.index.length - 1, Number(position)));
  if (target === state.position || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    selectPosition(target);
    if (onComplete) onComplete();
    return;
  }
  cancelAnimation();
  const token = state.animationToken;
  const from = state.vintages[state.position];
  const to = state.vintages[target];
  const playbackTransition = state.playing;
  const duration = playbackTransition ? Math.max(50, 1000 / state.playbackSpeed) : 420;
  const started = performance.now();
  let previousFrame = started;
  const frame = now => {
    if (token !== state.animationToken) return;
    const progress = Math.min(1, (now - started) / duration);
    const vintage = interpolateVintage(from, to, progress);
    let domain = interpolateDomain(from, to, progress);
    if (playbackTransition) {
      const ease = easeInOut(progress);
      const targetCenter = from.axisCenter + (to.axisCenter - from.axisCenter) * ease;
      advanceAxisCamera(targetCenter, now - previousFrame);
      domain = domainAtAxisCenter(vintage, state.axisCenter);
    }
    drawForward(vintage, true, domain);
    previousFrame = now;
    if (progress < 1) {
      state.animationFrame = window.requestAnimationFrame(frame);
      return;
    }
    state.animationFrame = null;
    selectPosition(target, playbackTransition);
    if (onComplete) onComplete();
  };
  state.animationFrame = window.requestAnimationFrame(frame);
}

function stopPlayback() {
  state.playing = false;
  if (state.timer) window.clearTimeout(state.timer);
  state.timer = null;
  cancelAnimation();
  const button = $("play");
  button.setAttribute("aria-pressed", "false");
  button.innerHTML = `<span aria-hidden="true">▶</span> Play`;
  if (state.vintages.length) {
    const vintage = state.vintages[state.position];
    drawForward(vintage, false, domainAtAxisCenter(vintage));
  }
}

function advancePlayback() {
  if (!state.playing) return;
  if (state.position >= state.index.length - 1) {
    stopPlayback();
    return;
  }
  animateToPosition(state.position + 1, () => {
    if (!state.playing) return;
    state.timer = window.setTimeout(advancePlayback, 0);
  });
}

function togglePlayback() {
  if (state.playing) {
    stopPlayback();
    return;
  }
  if (state.position >= state.index.length - 1) selectPosition(0);
  state.playing = true;
  const button = $("play");
  button.setAttribute("aria-pressed", "true");
  button.innerHTML = `<span aria-hidden="true">Ⅱ</span> Pause`;
  advancePlayback();
}

function installControls() {
  const slider = $("vintage-slider");
  slider.max = String(state.index.length - 1);
  slider.value = String(state.position);
  slider.oninput = event => { stopPlayback(); selectPosition(event.target.value); };
  const picker = $("date-picker");
  picker.min = dateKey(state.index[0].generated_at);
  picker.max = dateKey(state.index.at(-1).generated_at);
  picker.onchange = event => {
    stopPlayback();
    const target = new Date(`${event.target.value}T12:00:00Z`).getTime();
    const nearest = state.index.reduce((best, item, index) => {
      const distance = Math.abs(new Date(`${dateKey(item.generated_at)}T12:00:00Z`).getTime() - target);
      return distance < best.distance ? { index, distance } : best;
    }, { index: 0, distance: Infinity });
    animateToPosition(nearest.index);
  };
  $("play").onclick = togglePlayback;
  $("playback-speed").onchange = event => { state.playbackSpeed = Number(event.target.value) || 5; };
  $("previous").onclick = () => { stopPlayback(); animateToPosition(state.position - 1); };
  $("next").onclick = () => { stopPlayback(); animateToPosition(state.position + 1); };
  $("latest").onclick = () => { stopPlayback(); animateToPosition(state.index.length - 1); };
  $("oldest").onclick = () => { stopPlayback(); animateToPosition(0); };
  $("one-year").onclick = () => {
    stopPlayback();
    const target = new Date(state.index.at(-1).generated_at).getTime() - 365.25 * 864e5;
    const position = state.index.reduce((best, item, index) => Math.abs(new Date(item.generated_at).getTime() - target) < best.distance ? { index, distance: Math.abs(new Date(item.generated_at).getTime() - target) } : best, { index: 0, distance: Infinity }).index;
    animateToPosition(position);
  };
  window.addEventListener("resize", () => {
    const vintage = state.vintages[state.position];
    drawForward(vintage, false, domainAtAxisCenter(vintage));
    if (state.position === state.index.length - 1 && state.dashboard.policy.tree) renderTree({ policy: state.dashboard.policy, label: "current full-market forecast" });
  }, { passive: true });
}

async function main() {
  try {
    const response = await fetch("data/dashboard.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`dashboard HTTP ${response.status}`);
    state.dashboard = await response.json();
    const replayResponse = await fetch(state.dashboard.forecast_replay_url || "data/forecast-replay.json", { cache: "no-store" });
    if (!replayResponse.ok) throw new Error(`forecast replay HTTP ${replayResponse.status}`);
    state.replay = await replayResponse.json();
    state.index = state.replay.vintages || [];
    if (!state.index.length) throw new Error("no archived observations");
    prepareAxisCenters(state.index);
    state.vintages = state.index;
    state.position = state.index.length - 1;
    state.meetingDate = state.vintages[state.position].meetings[0].date;
    drawReplayTimeline();
    installControls();
    selectPosition(state.position);
  } catch (error) {
    $("main").innerHTML = `<section class="replay"><p class="timestamp">DATA ERROR</p><h1>Market replay unavailable</h1><p class="context">${String(error)}</p></section>`;
    $("freshness").textContent = "Tape unavailable";
  }
}

main();
