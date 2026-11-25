"use strict";

const state = {
  sites: [],
  siteId: null,
  dashboard: null,
  series: [],
  selectedSeries: "",
  rangeHours: 24,
  loading: false,
};

const $ = (id) => document.getElementById(id);
const qualityNames = ["Good", "Uncertain", "Bad", "Stale"];
const unitByTag = {
  p_ac: "kW",
  p_dc: "kW",
  p_active: "kW",
  p_dc_est: "kW",
  soc: "%",
  soh: "%",
  soh_est: "%",
  t_max: "°C",
  t_min: "°C",
  v_dc: "V",
  i_dc: "A",
  f_ac: "Hz",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function formatNumber(value, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(number)
    : "—";
}

function formatDate(value, options = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, options).format(date);
}

function timeAgo(value) {
  if (!value) return "No samples";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Unknown";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      // Keep the HTTP status when the server does not return JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

function showError(message) {
  $("error-message").textContent = message;
  $("error-panel").classList.remove("hidden");
  $("dashboard-content").classList.add("hidden");
}

function hideError() {
  $("error-panel").classList.add("hidden");
  $("dashboard-content").classList.remove("hidden");
}

async function loadSites() {
  const select = $("site-select");
  try {
    state.sites = await getJson("/v1/sites");
    select.replaceChildren();
    if (state.sites.length === 0) {
      select.add(new Option("No registered sites", ""));
      select.disabled = true;
      $("no-sites-panel").classList.remove("hidden");
      $("kpi-grid").classList.add("hidden");
      $("site-title").textContent = "No sites registered";
      $("site-subtitle").textContent = "Register a site and ingest telemetry to begin monitoring.";
      return;
    }
    state.sites.forEach((site) => {
      select.add(new Option(`${site.name} · ${site.site_id}`, site.site_id));
    });
    select.disabled = false;
    const storedSite = localStorage.getItem("vra-dashboard-site");
    state.siteId = state.sites.some((site) => site.site_id === storedSite)
      ? storedSite
      : state.sites[0].site_id;
    select.value = state.siteId;
    await loadDashboard();
  } catch (error) {
    showError(error.message);
  }
}

async function loadDashboard() {
  if (!state.siteId || state.loading) return;
  state.loading = true;
  $("refresh-button").disabled = true;
  try {
    state.dashboard = await getJson(`/v1/sites/${encodeURIComponent(state.siteId)}/dashboard`);
    hideError();
    renderDashboard();
    await loadChart();
    $("last-updated").textContent = `Refreshed ${formatDate(state.dashboard.snapshot_at, { hour: "2-digit", minute: "2-digit" })}`;
  } catch (error) {
    showError(error.message);
  } finally {
    state.loading = false;
    $("refresh-button").disabled = false;
  }
}

function latestReadingMap() {
  return new Map((state.dashboard?.latest_readings || []).map((reading) => [
    `${reading.asset_id}.${reading.tag}`,
    reading,
  ]));
}

function resolveReading(assetId, tag) {
  const reading = latestReadingMap().get(`${assetId}.${tag}`);
  const assetState = state.dashboard?.twin_state?.assets?.[assetId];
  const twinValue = assetState?.values?.[tag] || assetState?.derived?.[tag];
  if (twinValue && twinValue.v !== null && twinValue.v !== undefined) {
    return {
      value: twinValue.v,
      unit: twinValue.u || unitByTag[tag] || "",
      quality: twinValue.q,
      timestamp: twinValue.t || reading?.ts,
      provenance: twinValue.p || "measured",
    };
  }
  if (reading && (reading.value_num !== null || reading.value_txt !== null)) {
    return {
      value: reading.value_num ?? reading.value_txt,
      unit: unitByTag[tag] || "",
      quality: reading.quality,
      timestamp: reading.ts,
      provenance: "measured",
    };
  }
  return null;
}

function resolvePower(key, assetId, tag) {
  const balance = state.dashboard?.twin_state?.energy_balance;
  if (balance && Number.isFinite(Number(balance[key]))) return Number(balance[key]);
  const reading = resolveReading(assetId, tag);
  return reading && Number.isFinite(Number(reading.value)) ? Number(reading.value) : null;
}

function setKpi(id, value, digits = 1) {
  $(id).textContent = value === null || value === undefined ? "—" : formatNumber(value, digits);
}

function statusForGateway(gateway) {
  if (!gateway?.last_seen_at) return { label: "No telemetry received", className: "status-unknown" };
  const age = Math.max(0, (Date.now() - new Date(gateway.last_seen_at).getTime()) / 1000);
  if (Number.isFinite(age) && age <= 120) return { label: "Telemetry recent", className: "status-online" };
  if (gateway.status === "online") return { label: "Telemetry stale", className: "status-warning" };
  return { label: "No recent telemetry", className: "status-offline" };
}

function renderDashboard() {
  const data = state.dashboard;
  const site = data.site;
  const twin = data.twin_state;
  const selectedSite = state.sites.find((item) => item.site_id === state.siteId);
  $("no-sites-panel").classList.add("hidden");
  $("kpi-grid").classList.remove("hidden");
  $("site-title").textContent = site.name || site.site_id;
  $("site-subtitle").textContent = `${site.site_id} · ${site.timezone || "Site timezone not configured"}`;
  $("site-location").textContent = Number.isFinite(Number(site.latitude)) && Number.isFinite(Number(site.longitude))
    ? `${Number(site.latitude).toFixed(3)}, ${Number(site.longitude).toFixed(3)}`
    : "Location not configured";
  if (selectedSite?.name && selectedSite.name !== site.name) $("site-title").textContent = selectedSite.name;

  const link = statusForGateway(data.gateway);
  $("link-indicator").className = `status-pill ${link.className}`;
  $("link-indicator").innerHTML = `<span class="status-dot"></span>${escapeHtml(link.label)}`;

  const pv = resolvePower("pv_kw", "PV_INV_01", "p_ac");
  const load = resolvePower("load_kw", "MTR_PCC", "p_active");
  const battery = resolvePower("bess_kw", "BESS_01", "p_dc");
  const imbalance = twin?.energy_balance
    ? Number(twin.energy_balance.imbalance_kw)
    : (pv !== null && load !== null && battery !== null ? pv + battery - load : null);
  const soc = resolveReading("BESS_01", "soc");
  const soh = resolveReading("BESS_01", "soh") || resolveReading("BESS_01", "soh_est");

  setKpi("kpi-pv", pv);
  setKpi("kpi-load", load);
  setKpi("kpi-soc", soc ? soc.value : null);
  setKpi("kpi-imbalance", imbalance);
  $("battery-status").textContent = soh
    ? `SOH ${formatNumber(soh.value)}% · ${soh.provenance}`
    : soc ? `BESS · ${soc.provenance}` : "BESS · waiting for readings";
  $("flow-pv").textContent = pv === null ? "—" : `${formatNumber(pv)} kW`;
  $("flow-battery").textContent = battery === null
    ? "—"
    : `${formatNumber(battery)} kW · ${battery < 0 ? "charging" : "discharging"}`;
  $("flow-load").textContent = load === null ? "—" : `${formatNumber(load)} kW`;
  const maxFlow = Math.max(1, Math.abs(pv || 0), Math.abs(battery || 0), Math.abs(load || 0));
  [["bar-pv", pv], ["bar-battery", battery], ["bar-load", load]].forEach(([id, value]) => {
    $(id).style.width = value === null ? "0%" : `${Math.max(3, Math.abs(value) / maxFlow * 100)}%`;
  });
  $("balance-asof").textContent = twin?.asof ? `Twin · ${timeAgo(twin.asof)}` : "No live twin state";

  renderAssets(data.assets || [], data.latest_readings || [], twin?.assets || {});
  renderEvents(data.recent_events || []);
  renderReadings(data.latest_readings || []);
  $("samples-count").textContent = `${formatNumber(data.telemetry_samples_24h, 0)} samples in 24h`;
  populateSeries(data.latest_readings || []);
}

function qualityName(quality) {
  if (typeof quality === "string") return quality.charAt(0).toUpperCase() + quality.slice(1);
  const value = Number(quality);
  return Number.isInteger(value) ? qualityNames[value] || `Code ${value}` : "Unknown";
}

function qualityClass(quality) {
  const label = qualityName(quality).toLowerCase();
  if (label === "uncertain") return "quality-uncertain";
  if (label === "estimated" || label === "substituted") return "quality-estimated";
  if (label === "bad") return "quality-bad";
  if (label === "stale") return "quality-stale";
  return "";
}

function renderAssets(assets, readings, twinAssets) {
  $("asset-count").textContent = `${assets.length} asset${assets.length === 1 ? "" : "s"}`;
  const byAsset = new Map();
  readings.forEach((reading) => {
    const current = byAsset.get(reading.asset_id);
    if (!current || new Date(reading.ts) > new Date(current.ts)) byAsset.set(reading.asset_id, reading);
  });
  const rows = assets.map((asset) => {
    const twin = twinAssets[asset.asset_id];
    const latest = byAsset.get(asset.asset_id);
    let status = twin?.status || (latest ? "warning" : "unknown");
    let age = twin?.freshness_s;
    if (age === undefined && latest) {
      age = Math.max(0, (Date.now() - new Date(latest.ts).getTime()) / 1000);
      if (age >= 120) status = "offline";
    }
    const quality = latest ? qualityName(latest.quality) : "No data";
    const statusClass = ["ok", "warning", "fault", "offline"].includes(status) ? status : "unknown";
    return `<tr>
      <td><span class="asset-name">${escapeHtml(asset.name || asset.asset_id)}</span><span class="asset-subtitle">${escapeHtml(asset.asset_id)} · ${escapeHtml(asset.asset_type)}</span></td>
      <td><span class="asset-status asset-status-${statusClass}">${escapeHtml(status)}</span></td>
      <td>${escapeHtml(latest ? `${timeAgo(latest.ts)}${Number.isFinite(Number(age)) ? ` · ${formatNumber(age, 0)}s` : ""}` : "No telemetry")}</td>
      <td><span class="quality-pill ${qualityClass(latest?.quality)}">${escapeHtml(quality)}</span></td>
    </tr>`;
  });
  $("asset-table").innerHTML = rows.length
    ? rows.join("")
    : '<tr><td colspan="4" class="table-empty">No assets are registered for this site.</td></tr>';
}

function renderEvents(events) {
  $("event-count").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  const entries = events.slice(0, 6).map((event) => {
    const severity = String(event.severity || "info").toLowerCase();
    const severityClass = ["info", "warning", "critical", "error"].includes(severity) ? severity : "info";
    const detail = [event.asset_id, event.code].filter(Boolean).join(" · ");
    return `<article class="event-item">
      <span class="event-severity ${severityClass}"></span>
      <div><div class="event-title">${escapeHtml(event.message || event.kind || "Site event")}</div>
      <div class="event-detail">${escapeHtml(detail || event.kind || "Event")}</div>
      <div class="event-time">${escapeHtml(timeAgo(event.ts))} · ${escapeHtml(severity)}</div></div>
    </article>`;
  });
  $("event-list").innerHTML = entries.length ? entries.join("") : '<div class="list-empty">No events have been received.</div>';
}

function renderReadings(readings) {
  $("reading-count").textContent = `${readings.length} signal${readings.length === 1 ? "" : "s"}`;
  const rows = readings.slice(0, 40).map((reading) => {
    const unit = unitByTag[reading.tag] || "";
    const value = reading.value_num === null ? reading.value_txt : formatNumber(reading.value_num, 3);
    return `<tr>
      <td><span class="asset-name">${escapeHtml(reading.asset_id)}.${escapeHtml(reading.tag)}</span></td>
      <td>${escapeHtml(value ?? "—")}${unit ? ` <span class="metric-source">${escapeHtml(unit)}</span>` : ""}</td>
      <td><span class="quality-pill ${qualityClass(reading.quality)}">${escapeHtml(qualityName(reading.quality))}</span></td>
      <td>${escapeHtml(reading.resolution || "raw")}</td>
      <td title="${escapeHtml(reading.ts)}">${escapeHtml(formatDate(reading.ts, { dateStyle: "medium", timeStyle: "short" }))}</td>
    </tr>`;
  });
  $("readings-table").innerHTML = rows.length
    ? rows.join("")
    : '<tr><td colspan="5" class="table-empty">No telemetry has been received for this site.</td></tr>';
}

function populateSeries(readings) {
  const select = $("signal-select");
  const numeric = readings.filter((reading) => reading.value_num !== null && reading.value_num !== undefined);
  state.series = numeric.map((reading) => ({
    asset: reading.asset_id,
    tag: reading.tag,
    label: `${reading.asset_id}.${reading.tag}`,
    unit: unitByTag[reading.tag] || "",
  }));
  select.replaceChildren();
  if (!state.series.length) {
    select.add(new Option("No numeric signals", ""));
    select.disabled = true;
    state.selectedSeries = "";
    $("chart-meta").textContent = "Select a signal to explore received samples.";
    return;
  }
  state.series.forEach((series) => select.add(new Option(series.label, `${series.asset}.${series.tag}`)));
  if (!state.series.some((series) => `${series.asset}.${series.tag}` === state.selectedSeries)) {
    const preferred = state.series.find((series) => series.tag === "p_ac") || state.series[0];
    state.selectedSeries = `${preferred.asset}.${preferred.tag}`;
  }
  select.value = state.selectedSeries;
  select.disabled = false;
}

async function loadChart() {
  const series = state.series.find((item) => `${item.asset}.${item.tag}` === state.selectedSeries);
  if (!series) {
    $("timeseries-chart").innerHTML = '<div class="chart-empty">Telemetry history will appear when numeric samples are available.</div>';
    return;
  }
  const to = new Date();
  const from = new Date(to.getTime() - state.rangeHours * 60 * 60 * 1000);
  $("chart-meta").textContent = `${series.label}${series.unit ? ` · ${series.unit}` : ""} · Past ${state.rangeHours}h`;
  $("timeseries-chart").innerHTML = '<div class="chart-empty">Loading telemetry history…</div>';
  try {
    const params = new URLSearchParams({
      site: state.siteId,
      asset: series.asset,
      tag: series.tag,
      from_ts: from.toISOString(),
      to_ts: to.toISOString(),
      limit: "500",
    });
    const points = await getJson(`/v1/timeseries?${params}`);
    renderChart(points, series.unit);
  } catch (error) {
    $("timeseries-chart").innerHTML = `<div class="chart-empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderChart(points, unit) {
  const valid = points
    .map((point) => ({ time: new Date(point.ts).getTime(), value: Number(point.value_num), quality: point.quality }))
    .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));
  if (!valid.length) {
    $("timeseries-chart").innerHTML = '<div class="chart-empty">No samples in this time range. Check whether telemetry has been ingested.</div>';
    return;
  }

  const width = 720;
  const height = 212;
  const left = 44;
  const right = 10;
  const top = 12;
  const bottom = 28;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const min = Math.min(...valid.map((point) => point.value));
  const max = Math.max(...valid.map((point) => point.value));
  const span = max - min || Math.max(1, Math.abs(max) * 0.1);
  const yMin = min - span * 0.12;
  const yMax = max + span * 0.12;
  const firstTime = valid[0].time;
  const lastTime = valid[valid.length - 1].time;
  const timeSpan = lastTime - firstTime || 1;
  const x = (time) => left + ((time - firstTime) / timeSpan) * chartWidth;
  const y = (value) => top + (1 - (value - yMin) / (yMax - yMin)) * chartHeight;
  const path = valid.map((point, index) => `${index ? "L" : "M"}${x(point.time).toFixed(1)},${y(point.value).toFixed(1)}`).join(" ");
  const areaPath = `${path} L${x(lastTime).toFixed(1)},${(top + chartHeight).toFixed(1)} L${left},${(top + chartHeight).toFixed(1)} Z`;
  const yTicks = Array.from({ length: 4 }, (_, index) => yMin + ((yMax - yMin) * index) / 3);
  const grid = yTicks.map((tick) => `
    <line class="chart-grid" x1="${left}" y1="${y(tick).toFixed(1)}" x2="${width - right}" y2="${y(tick).toFixed(1)}"></line>
    <text class="chart-axis" x="${left - 7}" y="${(y(tick) + 3).toFixed(1)}" text-anchor="end">${escapeHtml(formatNumber(tick))}${unit ? ` ${escapeHtml(unit)}` : ""}</text>`).join("");
  const startLabel = formatDate(firstTime, { hour: "2-digit", minute: "2-digit" });
  const endLabel = formatDate(lastTime, { hour: "2-digit", minute: "2-digit" });
  const point = valid[valid.length - 1];
  $("timeseries-chart").innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Telemetry line chart with ${valid.length} samples">
    <defs><linearGradient id="chartFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#35a991" stop-opacity=".20"></stop><stop offset="100%" stop-color="#35a991" stop-opacity="0"></stop></linearGradient></defs>
    ${grid}
    <path class="chart-area-fill" d="${areaPath}"></path>
    <path class="chart-line" d="${path}"></path>
    <circle class="chart-point" cx="${x(point.time).toFixed(1)}" cy="${y(point.value).toFixed(1)}" r="4"></circle>
    <text class="chart-axis" x="${left}" y="${height - 5}">${escapeHtml(startLabel)}</text>
    <text class="chart-axis" x="${width - right}" y="${height - 5}" text-anchor="end">${escapeHtml(endLabel)}</text>
  </svg>`;
}

$("site-select").addEventListener("change", async (event) => {
  state.siteId = event.target.value;
  localStorage.setItem("vra-dashboard-site", state.siteId);
  await loadDashboard();
});
$("refresh-button").addEventListener("click", loadDashboard);
$("retry-button").addEventListener("click", loadSites);
$("signal-select").addEventListener("change", async (event) => {
  state.selectedSeries = event.target.value;
  await loadChart();
});
document.querySelectorAll(".range-tabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    state.rangeHours = Number(button.dataset.hours);
    document.querySelectorAll(".range-tabs button").forEach((item) => item.classList.toggle("selected", item === button));
    await loadChart();
  });
});

loadSites();
window.setInterval(() => {
  if (state.siteId && !state.loading) loadDashboard();
}, 15000);
