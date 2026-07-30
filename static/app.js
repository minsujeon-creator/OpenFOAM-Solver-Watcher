(() => {
  "use strict";

  const VIEW_IDS = [
    "overview-view",
    "residuals-view",
    "transient-view",
    "physical-view",
    "stationarity-view",
    "diagnostics-view",
  ];
  const COLORS = ["#57d9df", "#65e6a5", "#f1bd62", "#7fb7ff", "#c09cff", "#ff7d77"];
  const MAX_TABLE_ROWS = 100;
  const state = {
    activeView: "overview-view",
    snapshot: null,
    token: null,
    sessionPromise: null,
    pollTimer: null,
    refreshPromise: null,
    expandedSeries: new Set(),
    seriesDetails: new Map(),
    seriesErrors: new Map(),
    pendingSeries: new Set(),
    saveQueue: Promise.resolve(),
    charts: new Map(),
    resizeFrame: null,
  };

  const byId = (id) => document.getElementById(id);

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function formatNumber(value, digits = 4) {
    if (!finite(value)) {
      return "—";
    }
    const magnitude = Math.abs(value);
    if ((magnitude > 0 && magnitude < 0.001) || magnitude >= 100000) {
      return value.toExponential(2);
    }
    return new Intl.NumberFormat(undefined, {
      maximumFractionDigits: digits,
    }).format(value);
  }

  function formatDuration(seconds) {
    if (!finite(seconds) || seconds < 0) {
      return "unavailable";
    }
    if (seconds < 60) {
      return `${Math.round(seconds)} s`;
    }
    if (seconds < 3600) {
      return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    }
    const hours = Math.floor(seconds / 3600);
    return `${hours}h ${Math.round((seconds % 3600) / 60)}m`;
  }

  function titleCase(value) {
    if (typeof value !== "string" || !value) {
      return "Unknown";
    }
    return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function baseName(value) {
    if (typeof value !== "string" || !value) {
      return "Unknown case";
    }
    const parts = value.split(/[\\/]/).filter(Boolean);
    return parts.at(-1) || value;
  }

  function toneFor(value) {
    const normalized = String(value || "").toLowerCase();
    if (
      ["running", "completed", "passing", "converged", "healthy", "plateau", "periodic", "statistically_stationary"].includes(normalized)
    ) {
      return "good";
    }
    if (["failed", "diverged", "unhealthy", "error"].includes(normalized)) {
      return "bad";
    }
    return "warning";
  }

  function badge(value) {
    const node = element("span", "badge", titleCase(value));
    node.dataset.tone = toneFor(value);
    return node;
  }

  function tableCell(text, className) {
    return element("td", className || "", String(text));
  }

  function emptyTable(body, columns, message) {
    const row = element("tr", "empty-row");
    const cell = tableCell(message);
    cell.colSpan = columns;
    row.append(cell);
    body.replaceChildren(row);
  }

  async function fetchJSON(url, options) {
    const response = await fetch(url, {
      cache: "no-store",
      credentials: "same-origin",
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      throw new Error(`The server returned an unreadable response (${response.status}).`);
    }
    if (!response.ok) {
      const code = payload && typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`;
      throw new Error(code.replaceAll("_", " "));
    }
    return payload;
  }

  function announce(message) {
    byId("live-region").textContent = message;
  }

  function setConnection(label, mode) {
    const output = byId("connection-state");
    output.textContent = label;
    const container = output.closest(".connection-block");
    container.classList.toggle("is-connected", mode === "connected");
    container.classList.toggle("is-disconnected", mode === "disconnected");
  }

  function bindTabs() {
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    for (const tab of tabs) {
      tab.addEventListener("click", () => activateView(tab.getAttribute("aria-controls"), true));
      tab.addEventListener("keydown", (event) => {
        const available = tabs.filter((item) => !item.hidden);
        const current = available.indexOf(tab);
        let target = null;
        if (event.key === "ArrowRight") {
          target = available[(current + 1) % available.length];
        } else if (event.key === "ArrowLeft") {
          target = available[(current - 1 + available.length) % available.length];
        } else if (event.key === "Home") {
          target = available[0];
        } else if (event.key === "End") {
          target = available.at(-1);
        }
        if (target) {
          event.preventDefault();
          activateView(target.getAttribute("aria-controls"), true);
          target.focus();
        }
      });
    }

    for (const link of document.querySelectorAll("[data-open-view]")) {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        activateView(link.dataset.openView, true);
      });
    }
  }

  function activateView(viewId, updateHash) {
    if (!VIEW_IDS.includes(viewId)) {
      return;
    }
    const requestedTab = document.querySelector(`[aria-controls="${viewId}"]`);
    if (!requestedTab || requestedTab.hidden) {
      viewId = "overview-view";
    }
    state.activeView = viewId;
    for (const id of VIEW_IDS) {
      const panel = byId(id);
      const tab = document.querySelector(`[aria-controls="${id}"]`);
      const active = id === viewId;
      panel.hidden = !active;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    if (updateHash && history.replaceState) {
      history.replaceState(null, "", `#${viewId}`);
    }
    scheduleChartDraw();
  }

  function bindInteractions() {
    bindTabs();
    byId("refresh-button").addEventListener("click", () => {
      void refreshSnapshot(true);
    });
    window.addEventListener("resize", scheduleChartDraw, { passive: true });
    const requested = location.hash.slice(1);
    if (VIEW_IDS.includes(requested)) {
      state.activeView = requested;
    }
    activateView(state.activeView, false);
  }

  function schedulePoll(seconds) {
    if (state.pollTimer !== null) {
      clearTimeout(state.pollTimer);
    }
    const bounded = finite(seconds) ? Math.min(300, Math.max(0.5, seconds)) : 2;
    state.pollTimer = setTimeout(() => {
      void refreshSnapshot(false);
    }, bounded * 1000);
  }

  function refreshSnapshot(manual) {
    if (state.refreshPromise) {
      return state.refreshPromise;
    }
    if (state.pollTimer !== null) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    const button = byId("refresh-button");
    button.disabled = true;
    setConnection("Refreshing", "pending");
    state.refreshPromise = fetchJSON("/api/snapshot")
      .then((snapshot) => {
        state.snapshot = snapshot;
        renderSnapshot(snapshot);
        setConnection("Connected", "connected");
        if (manual) {
          announce("Dashboard snapshot refreshed.");
        }
        return snapshot;
      })
      .catch((error) => {
        setConnection("Disconnected", "disconnected");
        byId("last-refresh").textContent = "Snapshot unavailable";
        announce(`Dashboard refresh failed: ${error.message}`);
        return null;
      })
      .finally(() => {
        button.disabled = false;
        state.refreshPromise = null;
        const interval = state.snapshot && state.snapshot.refreshSeconds;
        schedulePoll(interval);
      });
    return state.refreshPromise;
  }

  function updateTransientVisibility(snapshot) {
    const mode = String(snapshot.case && snapshot.case.mode || "");
    const transient = mode.includes("transient") || mode.includes("pimple") || mode.includes("piso");
    const tab = byId("tab-transient");
    tab.hidden = !transient;
    if (!transient && state.activeView === "transient-view") {
      activateView("overview-view", true);
    }
  }

  function renderSnapshot(snapshot) {
    updateTransientVisibility(snapshot);
    renderHeader(snapshot);
    renderCards(snapshot);
    renderOverview(snapshot);
    renderResiduals(snapshot);
    renderTransient(snapshot);
    renderPhysical(snapshot);
    renderStationarity(snapshot);
    renderDiagnostics(snapshot);
    pruneCharts();
    scheduleChartDraw();
  }

  function renderHeader(snapshot) {
    const caseData = snapshot.case || {};
    byId("case-name").textContent = baseName(caseData.caseDir);
    const application = caseData.application || "Unknown solver";
    const version = caseData.openfoamVersion ? ` · ${caseData.openfoamVersion}` : "";
    byId("application-version").textContent = `${application}${version}`;
    byId("mode-label").textContent = titleCase(caseData.mode);
    const generated = new Date(snapshot.generatedAt);
    byId("last-refresh").textContent = Number.isNaN(generated.getTime())
      ? "Snapshot time unavailable"
      : `Updated ${generated.toLocaleTimeString()}`;
  }

  function setCard(id, value, detail, tone) {
    const card = byId(id);
    card.querySelector("[data-value]").textContent = value;
    card.querySelector("[data-detail]").textContent = detail;
    card.dataset.tone = tone;
  }

  function renderCards(snapshot) {
    const process = snapshot.process || {};
    const processDetail = process.pid
      ? `PID ${process.pid} · ${process.source || "process evidence"}`
      : `${titleCase(process.source || "no source")} evidence · age ${formatDuration(process.logAgeSeconds)}`;
    setCard("card-process", titleCase(process.state), processDetail, toneFor(process.state));

    const numerics = snapshot.numerics || {};
    setCard(
      "card-numerics",
      titleCase(numerics.status),
      numerics.summary || "No numerical assessment available.",
      toneFor(numerics.status),
    );

    const aggregate = snapshot.physical && snapshot.physical.aggregate || {};
    setCard(
      "card-physical",
      titleCase(aggregate.state),
      aggregate.summary || "No monitored quantities.",
      toneFor(aggregate.state),
    );

    const progress = snapshot.solver && snapshot.solver.progress || {};
    const percent = finite(progress.percent) ? `${formatNumber(progress.percent, 1)}%` : "—";
    const rate = finite(progress.simulatedSecondsPerWallSecond)
      ? `${formatNumber(progress.simulatedSecondsPerWallSecond, 3)} sim s / wall s`
      : "rate unavailable";
    setCard(
      "card-progress",
      percent,
      `${rate} · ETA ${formatDuration(progress.etaSeconds)}`,
      finite(progress.percent) ? "good" : "warning",
    );
  }

  function targetForField(field, targets) {
    for (const target of Array.isArray(targets) ? targets : []) {
      if (!finite(target.threshold) || typeof target.pattern !== "string") {
        continue;
      }
      if (target.pattern === field) {
        return target.threshold;
      }
      try {
        if (new RegExp(`^(?:${target.pattern})$`).test(field)) {
          return target.threshold;
        }
      } catch {
        continue;
      }
    }
    return null;
  }

  function residualRows(snapshot) {
    const residuals = snapshot.solver && snapshot.solver.residuals;
    return Array.isArray(residuals) ? residuals : [];
  }

  function catalogRows(snapshot) {
    return Array.isArray(snapshot.seriesCatalog) ? snapshot.seriesCatalog : [];
  }

  function selectedIds(snapshot) {
    const values = snapshot.configuration && snapshot.configuration.selectedSeries;
    return new Set(Array.isArray(values) ? values : []);
  }

  function renderOverview(snapshot) {
    const solver = snapshot.solver || {};
    const app = snapshot.case && snapshot.case.application || "Solver";
    const time = finite(solver.currentTime) ? `time ${formatNumber(solver.currentTime)}` : "no parsed time";
    byId("overview-summary").textContent = `${app} · ${time} · ${titleCase(snapshot.process && snapshot.process.state)}`;

    const residualBody = byId("overview-residual-body");
    const recentByField = new Map();
    for (const sample of residualRows(snapshot)) {
      if (sample && typeof sample.field === "string") {
        recentByField.set(sample.field, sample);
      }
    }
    const residualTargets = snapshot.case && snapshot.case.residualTargets;
    const residualTableRows = Array.from(recentByField.values()).slice(-10).reverse();
    if (!residualTableRows.length) {
      emptyTable(residualBody, 4, "No residual samples have been parsed.");
    } else {
      residualBody.replaceChildren(...residualTableRows.map((sample) => {
        const row = element("tr");
        row.append(
          tableCell(sample.field),
          tableCell(formatNumber(sample.simulationTime), "number"),
          tableCell(formatNumber(sample.initial), "number"),
          tableCell(formatNumber(targetForField(sample.field, residualTargets)), "number"),
        );
        return row;
      }));
    }

    const quantityBody = byId("overview-quantity-body");
    const selected = selectedIds(snapshot);
    const physicalResults = snapshot.physical && snapshot.physical.results || {};
    const quantities = catalogRows(snapshot).filter((item) => selected.has(item.id));
    if (!quantities.length) {
      emptyTable(quantityBody, 4, "Select a physical quantity to monitor.");
    } else {
      quantityBody.replaceChildren(...quantities.map((item) => {
        const result = physicalResults[item.id] || {};
        const row = element("tr");
        const stateCell = element("td");
        stateCell.append(badge(result.state || "indeterminate"));
        row.append(
          tableCell(item.label || item.id),
          tableCell(`${formatNumber(item.currentValue)}${item.units ? ` ${item.units}` : ""}`, "number"),
          stateCell,
          tableCell(formatNumber(item.sampleCount, 0), "number"),
        );
        return row;
      }));
    }

    const verdicts = byId("verdict-summary");
    verdicts.replaceChildren(
      verdictItem("Numerical verdict", snapshot.numerics && snapshot.numerics.status, snapshot.numerics && snapshot.numerics.summary),
      verdictItem(
        "Physical verdict",
        snapshot.physical && snapshot.physical.aggregate && snapshot.physical.aggregate.state,
        snapshot.physical && snapshot.physical.aggregate && snapshot.physical.aggregate.summary,
      ),
    );
  }

  function verdictItem(label, status, summary) {
    const item = element("div", "verdict-item");
    item.append(element("h3", "", label), badge(status || "indeterminate"), element("p", "", summary || "Evidence is not yet sufficient."));
    return item;
  }

  function residualSeries(snapshot) {
    const groups = new Map();
    for (const sample of residualRows(snapshot)) {
      if (
        !sample ||
        typeof sample.field !== "string" ||
        !finite(sample.simulationTime) ||
        !finite(sample.initial) ||
        sample.initial <= 0
      ) {
        continue;
      }
      if (!groups.has(sample.field)) {
        groups.set(sample.field, []);
      }
      groups.get(sample.field).push({ x: sample.simulationTime, y: sample.initial });
    }
    return Array.from(groups, ([label, points], index) => ({
      label,
      color: COLORS[index % COLORS.length],
      points,
    })).slice(0, COLORS.length);
  }

  function renderLegend(containerId, series) {
    const container = byId(containerId);
    if (!series.length) {
      container.replaceChildren(element("span", "empty-copy", "No chart series available"));
      return;
    }
    container.replaceChildren(...series.map((item, index) => {
      const wrapper = element("span", "legend-item");
      const swatch = element("span", `legend-swatch palette-${index % COLORS.length}`);
      wrapper.append(swatch, document.createTextNode(item.label));
      return wrapper;
    }));
  }

  function renderResiduals(snapshot) {
    const series = residualSeries(snapshot);
    const targets = [];
    const seen = new Set();
    for (const target of snapshot.case && snapshot.case.residualTargets || []) {
      if (finite(target.threshold) && target.threshold > 0 && !seen.has(target.threshold)) {
        seen.add(target.threshold);
        targets.push({
          value: target.threshold,
          label: `${target.pattern} target`,
        });
      }
    }
    renderLegend("residual-legend", series);
    registerChart(byId("residual-chart"), {
      series,
      targets,
      yScale: "log",
      emptyMessage: "No positive finite residual samples",
      xLabel: "Simulation time",
      yLabel: "Initial residual (log)",
    });

    const body = byId("residual-table-body");
    const rows = residualRows(snapshot).slice(-MAX_TABLE_ROWS).reverse();
    if (!rows.length) {
      emptyTable(body, 6, "No residual samples have been parsed.");
      return;
    }
    body.replaceChildren(...rows.map((sample) => {
      const target = targetForField(sample.field, snapshot.case && snapshot.case.residualTargets);
      const row = element("tr");
      row.append(
        tableCell(sample.field || "Unknown"),
        tableCell(formatNumber(sample.simulationTime), "number"),
        tableCell(formatNumber(sample.initial), "number"),
        tableCell(formatNumber(sample.final), "number"),
        tableCell(formatNumber(sample.iterations, 0), "number"),
        tableCell(formatNumber(target), "number"),
      );
      return row;
    }));
  }

  function timeStepRows(snapshot) {
    const rows = snapshot.solver && snapshot.solver.timeSteps;
    return Array.isArray(rows) ? rows : [];
  }

  function renderTransient(snapshot) {
    const rows = timeStepRows(snapshot);
    const series = [
      {
        label: "Courant max",
        color: COLORS[0],
        points: rows.filter((row) => finite(row.simulationTime) && finite(row.courantMax)).map((row) => ({ x: row.simulationTime, y: row.courantMax })),
      },
      {
        label: "Courant mean",
        color: COLORS[1],
        points: rows.filter((row) => finite(row.simulationTime) && finite(row.courantMean)).map((row) => ({ x: row.simulationTime, y: row.courantMean })),
      },
      {
        label: "deltaT",
        color: COLORS[2],
        points: rows.filter((row) => finite(row.simulationTime) && finite(row.deltaT)).map((row) => ({ x: row.simulationTime, y: row.deltaT })),
      },
    ].filter((item) => item.points.length);
    const maxCo = snapshot.case && snapshot.case.maxCo;
    registerChart(byId("transient-chart"), {
      series,
      targets: finite(maxCo) ? [{ value: maxCo, label: "maxCo" }] : [],
      yScale: "linear",
      emptyMessage: "No transient step samples",
      xLabel: "Simulation time",
      yLabel: "Courant / deltaT",
    });
    byId("transient-summary").textContent = snapshot.numerics && snapshot.numerics.summary || "Transient health evidence is unavailable.";

    const body = byId("transient-table-body");
    const visible = rows.slice(-MAX_TABLE_ROWS).reverse();
    if (!visible.length) {
      emptyTable(body, 6, "No transient step samples have been parsed.");
      return;
    }
    body.replaceChildren(...visible.map((sample) => {
      const row = element("tr");
      row.append(
        tableCell(formatNumber(sample.simulationTime), "number"),
        tableCell(formatNumber(sample.deltaT), "number"),
        tableCell(formatNumber(sample.courantMean), "number"),
        tableCell(formatNumber(sample.courantMax), "number"),
        tableCell(formatNumber(sample.continuityGlobal), "number"),
        tableCell(formatNumber(sample.outerCorrectors, 0), "number"),
      );
      return row;
    }));
  }

  function catalogChartSeries(snapshot, stationarityWindow) {
    const selected = selectedIds(snapshot);
    const results = snapshot.physical && snapshot.physical.results || {};
    return catalogRows(snapshot)
      .filter((item) => selected.has(item.id))
      .slice(0, COLORS.length)
      .map((item, index) => {
        const preview = item.preview || {};
        const times = Array.isArray(preview.times) ? preview.times : [];
        const values = Array.isArray(preview.values) ? preview.values : [];
        let start = 0;
        if (stationarityWindow) {
          const result = results[item.id] || {};
          const windowSamples = result.evidence && result.evidence.windowSamples;
          if (finite(windowSamples)) {
            start = Math.max(0, Math.min(times.length, values.length) - Math.floor(windowSamples));
          }
        }
        const points = [];
        for (let offset = start; offset < Math.min(times.length, values.length); offset += 1) {
          if (finite(times[offset]) && finite(values[offset])) {
            points.push({ x: times[offset], y: values[offset] });
          }
        }
        return {
          label: item.label || item.id,
          color: COLORS[index % COLORS.length],
          points,
        };
      });
  }

  function renderPhysical(snapshot) {
    const series = catalogChartSeries(snapshot, false);
    renderLegend("physical-legend", series);
    registerChart(byId("physical-chart"), {
      series,
      targets: [],
      yScale: "linear",
      emptyMessage: "Select a physical quantity to draw its history",
      xLabel: "Simulation time",
      yLabel: "Normalized display range",
    });

    const body = byId("series-table-body");
    const catalog = catalogRows(snapshot);
    if (!catalog.length) {
      emptyTable(body, 7, "No numeric post-processing series were discovered.");
      return;
    }

    const selected = selectedIds(snapshot);
    const results = snapshot.physical && snapshot.physical.results || {};
    const nodes = [];
    catalog.forEach((item, index) => {
      const template = byId("series-row-template");
      const row = template.content.firstElementChild.cloneNode(true);
      const checkbox = row.querySelector(".series-toggle");
      const toggleText = row.querySelector(".series-toggle + span");
      checkbox.checked = selected.has(item.id);
      checkbox.disabled = state.pendingSeries.has(item.id);
      toggleText.textContent = `Monitor ${item.label || item.id}`;
      checkbox.addEventListener("change", () => queueSeriesSelection(item.id, checkbox));

      const nameCell = element("td");
      nameCell.append(document.createTextNode(item.label || item.id));
      if (item.candidate && item.candidate.recommended) {
        nameCell.append(document.createTextNode(" "), badge("recommended"));
      }
      const stateCell = element("td");
      stateCell.append(badge(results[item.id] && results[item.id].state || "indeterminate"));
      const detailCell = element("td");
      const button = element("button", "detail-button", state.expandedSeries.has(item.id) ? "Close" : "Open");
      const detailId = `series-detail-${index}`;
      button.type = "button";
      button.setAttribute("aria-expanded", String(state.expandedSeries.has(item.id)));
      button.setAttribute("aria-controls", detailId);
      button.addEventListener("click", () => {
        void toggleSeriesDetail(item.id);
      });
      detailCell.append(button);
      row.append(
        nameCell,
        tableCell(`${formatNumber(item.currentValue)}${item.units ? ` ${item.units}` : ""}`, "number"),
        stateCell,
        tableCell(formatNumber(item.sampleCount, 0), "number"),
        tableCell(item.source || "Unknown"),
        detailCell,
      );
      nodes.push(row);

      if (state.expandedSeries.has(item.id)) {
        nodes.push(seriesDetailRow(item, detailId));
      }
    });
    body.replaceChildren(...nodes);
  }

  function seriesDetailRow(item, detailId) {
    const row = element("tr", "detail-row");
    row.id = detailId;
    const cell = element("td");
    cell.colSpan = 7;
    const data = state.seriesDetails.get(item.id);
    const error = state.seriesErrors.get(item.id);
    if (error) {
      cell.append(element("p", "empty-copy", `Detail unavailable: ${error}`));
    } else if (!data) {
      cell.append(element("p", "empty-copy", "Loading up to 2,000 samples…"));
    } else {
      const canvas = element("canvas");
      canvas.setAttribute("aria-label", `Detailed history for ${item.label || item.id}`);
      canvas.textContent = "Detailed series history requires Canvas support.";
      cell.append(canvas);
      const points = [];
      const times = Array.isArray(data.times) ? data.times : [];
      const values = Array.isArray(data.values) ? data.values : [];
      for (let index = 0; index < Math.min(times.length, values.length); index += 1) {
        if (finite(times[index]) && finite(values[index])) {
          points.push({ x: times[index], y: values[index] });
        }
      }
      registerChart(canvas, {
        series: [{ label: data.label || item.label || item.id, color: COLORS[0], points }],
        targets: [],
        yScale: "linear",
        emptyMessage: "No finite samples in this series",
        xLabel: "Simulation time",
        yLabel: data.units || "Value",
      });
      const metadata = element(
        "p",
        "empty-copy",
        `${formatNumber(data.returnedSamples, 0)} of ${formatNumber(data.totalSamples, 0)} samples${data.downsampled ? " · min/max envelope" : ""}`,
      );
      cell.append(metadata);
    }
    row.append(cell);
    return row;
  }

  async function toggleSeriesDetail(seriesId) {
    if (state.expandedSeries.has(seriesId)) {
      state.expandedSeries.delete(seriesId);
      renderPhysical(state.snapshot);
      pruneCharts();
      return;
    }
    state.expandedSeries.add(seriesId);
    renderPhysical(state.snapshot);
    if (state.seriesDetails.has(seriesId)) {
      return;
    }
    state.seriesErrors.delete(seriesId);
    try {
      const data = await fetchJSON(`/api/series?id=${encodeURIComponent(seriesId)}&limit=2000`);
      state.seriesDetails.set(seriesId, data);
    } catch (error) {
      state.seriesErrors.set(seriesId, error.message);
      announce(`Series detail failed: ${error.message}`);
    }
    if (state.snapshot && state.expandedSeries.has(seriesId)) {
      renderPhysical(state.snapshot);
      pruneCharts();
      scheduleChartDraw();
    }
  }

  function configurationPayload(configuration) {
    const source = configuration || {};
    return {
      version: Number.isInteger(source.version) ? source.version : 1,
      selectedLog: typeof source.selectedLog === "string" ? source.selectedLog : null,
      selectedSeries: Array.isArray(source.selectedSeries) ? [...source.selectedSeries] : [],
      overrides: source.overrides && typeof source.overrides === "object" ? source.overrides : {},
      acceptedStates: Array.isArray(source.acceptedStates) ? [...source.acceptedStates] : [],
    };
  }

  function queueSeriesSelection(seriesId, checkbox) {
    const desired = checkbox.checked;
    checkbox.disabled = true;
    state.pendingSeries.add(seriesId);
    state.saveQueue = state.saveQueue.then(async () => {
      try {
        const payload = configurationPayload(state.snapshot && state.snapshot.configuration);
        const selected = new Set(payload.selectedSeries);
        if (desired) {
          selected.add(seriesId);
        } else {
          selected.delete(seriesId);
        }
        payload.selectedSeries = Array.from(selected);
        if (!state.token) {
          await state.sessionPromise;
        }
        if (!state.token) {
          throw new Error("session token unavailable");
        }
        const saved = await fetchJSON("/api/config", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Watcher-Token": state.token,
          },
          body: JSON.stringify(payload),
        });
        state.snapshot.configuration = saved;
        synchronizeCatalogSelection(saved.selectedSeries);
        announce(`${desired ? "Monitoring" : "Stopped monitoring"} ${seriesLabel(seriesId)}.`);
      } catch (error) {
        checkbox.checked = !desired;
        announce(`Could not save ${seriesLabel(seriesId)}: ${error.message}`);
      } finally {
        state.pendingSeries.delete(seriesId);
        if (state.snapshot) {
          renderOverview(state.snapshot);
          renderCards(state.snapshot);
          renderPhysical(state.snapshot);
          renderStationarity(state.snapshot);
          pruneCharts();
          scheduleChartDraw();
        }
      }
    });
  }

  function synchronizeCatalogSelection(values) {
    const selected = new Set(Array.isArray(values) ? values : []);
    for (const item of catalogRows(state.snapshot)) {
      item.selected = selected.has(item.id);
    }
  }

  function seriesLabel(seriesId) {
    const item = catalogRows(state.snapshot).find((candidate) => candidate.id === seriesId);
    return item && item.label || seriesId;
  }

  function renderStationarity(snapshot) {
    const series = catalogChartSeries(snapshot, true);
    renderLegend("stationarity-legend", series);
    registerChart(byId("stationarity-chart"), {
      series,
      targets: [],
      yScale: "linear",
      emptyMessage: "Select a quantity to inspect its analysis window",
      xLabel: "Recent simulation time",
      yLabel: "Window value",
    });

    const body = byId("stationarity-table-body");
    const selected = selectedIds(snapshot);
    const results = snapshot.physical && snapshot.physical.results || {};
    const rows = catalogRows(snapshot).filter((item) => selected.has(item.id));
    if (!rows.length) {
      emptyTable(body, 7, "Select a physical quantity to evaluate stationarity.");
      return;
    }
    body.replaceChildren(...rows.map((item) => {
      const result = results[item.id] || {};
      const evidence = result.evidence || {};
      const stateCell = element("td");
      stateCell.append(badge(result.state || "indeterminate"));
      const row = element("tr");
      row.append(
        tableCell(item.label || item.id),
        stateCell,
        tableCell(formatNumber(evidence.windowSamples, 0), "number"),
        tableCell(formatNumber(evidence.effectiveSamples, 1), "number"),
        tableCell(formatNumber(evidence.normalizedSlope), "number"),
        tableCell(formatNumber(evidence.normalizedMeanShift), "number"),
        tableCell(result.summary || "Insufficient evidence"),
      );
      return row;
    }));
  }

  function renderDiagnostics(snapshot) {
    const facts = byId("diagnostic-facts");
    const entries = [
      ["Hostname", snapshot.host && snapshot.host.hostname],
      ["Platform", snapshot.host && snapshot.host.platform],
      ["CPU count", snapshot.host && snapshot.host.cpuCount],
      ["Load average", Array.isArray(snapshot.host && snapshot.host.loadAverage) ? snapshot.host.loadAverage.map((value) => formatNumber(value, 2)).join(" / ") : null],
      ["Selected log", snapshot.logSelection && snapshot.logSelection.selected],
      ["Current segment", snapshot.solver && snapshot.solver.currentSegment],
      ["Residual samples", snapshot.solver && snapshot.solver.residualCount],
      ["Time-step samples", snapshot.solver && snapshot.solver.timeStepCount],
    ];
    const factNodes = [];
    for (const [label, value] of entries) {
      factNodes.push(element("dt", "", label), element("dd", "", value === null || value === undefined ? "—" : String(value)));
    }
    facts.replaceChildren(...factNodes);

    const noticeList = byId("diagnostic-notices");
    const notices = Array.isArray(snapshot.notices) ? snapshot.notices : [];
    if (!notices.length) {
      noticeList.replaceChildren(element("li", "", "No collector notices."));
    } else {
      noticeList.replaceChildren(...notices.map((notice) => {
        const item = element("li");
        item.dataset.tone = toneFor(notice.severity);
        item.append(
          element("span", "notice-source", `${notice.source || "collector"} · ${titleCase(notice.severity || "info")}`),
          document.createTextNode(notice.message || "No message"),
        );
        return item;
      }));
    }

    const logBody = byId("diagnostic-log-body");
    const candidates = snapshot.logSelection && snapshot.logSelection.candidates;
    if (!Array.isArray(candidates) || !candidates.length) {
      emptyTable(logBody, 4, "No solver log candidates were found.");
      return;
    }
    const selected = snapshot.logSelection.selected;
    logBody.replaceChildren(...candidates.map((candidate) => {
      const row = element("tr");
      const reasons = Array.isArray(candidate.reasons) ? candidate.reasons.join("; ") : "No ranking reason";
      row.append(
        tableCell(candidate.relativePath === selected ? "Yes" : "No"),
        tableCell(candidate.relativePath || "Unknown"),
        tableCell(formatNumber(candidate.score, 0), "number"),
        tableCell(reasons),
      );
      return row;
    }));
  }

  function registerChart(canvas, model) {
    if (canvas) {
      state.charts.set(canvas, model);
    }
  }

  function pruneCharts() {
    for (const canvas of state.charts.keys()) {
      if (!canvas.isConnected) {
        state.charts.delete(canvas);
      }
    }
  }

  function scheduleChartDraw() {
    if (state.resizeFrame !== null) {
      return;
    }
    state.resizeFrame = requestAnimationFrame(() => {
      state.resizeFrame = null;
      pruneCharts();
      for (const [canvas, model] of state.charts) {
        if (!canvas.closest("[hidden]")) {
          drawChart(canvas, model);
        }
      }
    });
  }

  function drawChart(canvas, model) {
    const context = canvas.getContext("2d");
    if (!context) {
      return;
    }
    const bounds = canvas.getBoundingClientRect();
    const width = Math.max(280, Math.floor(bounds.width || canvas.clientWidth || 600));
    const height = Math.max(220, Math.floor(bounds.height || canvas.clientHeight || 340));
    const ratio = Math.min(3, Math.max(1, window.devicePixelRatio || 1));
    const pixelWidth = Math.floor(width * ratio);
    const pixelHeight = Math.floor(height * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#091411";
    context.fillRect(0, 0, width, height);

    const logarithmic = model.yScale === "log";
    const cleanSeries = model.series.map((series) => ({
      ...series,
      points: series.points.filter((point) => finite(point.x) && finite(point.y) && (!logarithmic || point.y > 0)),
    })).filter((series) => series.points.length);
    const allPoints = cleanSeries.flatMap((series) => series.points);
    const cleanTargets = (model.targets || []).filter((target) => finite(target.value) && (!logarithmic || target.value > 0));
    if (!allPoints.length) {
      drawEmptyChart(context, width, height, model.emptyMessage || "No finite data");
      canvas.setAttribute("aria-label", model.emptyMessage || "No chart data");
      return;
    }

    const margin = { left: 70, right: 22, top: 22, bottom: 48 };
    const plotWidth = Math.max(1, width - margin.left - margin.right);
    const plotHeight = Math.max(1, height - margin.top - margin.bottom);
    let minX = Math.min(...allPoints.map((point) => point.x));
    let maxX = Math.max(...allPoints.map((point) => point.x));
    const transformedValues = allPoints.map((point) => logarithmic ? Math.log10(point.y) : point.y);
    transformedValues.push(...cleanTargets.map((target) => logarithmic ? Math.log10(target.value) : target.value));
    let minY = Math.min(...transformedValues);
    let maxY = Math.max(...transformedValues);
    if (minX === maxX) {
      minX -= 0.5;
      maxX += 0.5;
    }
    if (minY === maxY) {
      const padding = Math.abs(minY || 1) * 0.05;
      minY -= padding;
      maxY += padding;
    }
    const xPixel = (value) => margin.left + ((value - minX) / (maxX - minX)) * plotWidth;
    const yValue = (value) => logarithmic ? Math.log10(value) : value;
    const yPixel = (value) => margin.top + (1 - (yValue(value) - minY) / (maxY - minY)) * plotHeight;

    drawAxes(context, { margin, plotWidth, plotHeight, minX, maxX, minY, maxY, logarithmic, model });
    for (const target of cleanTargets) {
      const y = yPixel(target.value);
      context.save();
      context.strokeStyle = "#f1bd62";
      context.fillStyle = "#f1bd62";
      context.setLineDash([5, 5]);
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(margin.left, y);
      context.lineTo(margin.left + plotWidth, y);
      context.stroke();
      context.setLineDash([]);
      context.font = "10px Cascadia Mono, Consolas, monospace";
      context.textAlign = "right";
      context.fillText(target.label || formatTick(target.value), margin.left + plotWidth, Math.max(10, y - 4));
      context.restore();
    }

    context.save();
    context.beginPath();
    context.rect(margin.left, margin.top, plotWidth, plotHeight);
    context.clip();
    for (const series of cleanSeries) {
      const bounded = minMaxEnvelope(series.points, Math.max(4, Math.floor(plotWidth)));
      context.strokeStyle = series.color || COLORS[0];
      context.lineWidth = 1.6;
      context.lineJoin = "round";
      context.beginPath();
      bounded.forEach((point, index) => {
        const x = xPixel(point.x);
        const y = yPixel(point.y);
        if (index === 0) {
          context.moveTo(x, y);
        } else {
          context.lineTo(x, y);
        }
      });
      context.stroke();
    }
    context.restore();
    canvas.setAttribute(
      "aria-label",
      `${model.yScale === "log" ? "Logarithmic" : "Linear"} chart with ${cleanSeries.length} series and ${allPoints.length} finite points`,
    );
  }

  function drawAxes(context, values) {
    const { margin, plotWidth, plotHeight, minX, maxX, minY, maxY, logarithmic, model } = values;
    const tickIntervals = 5;
    context.save();
    context.font = "10px Cascadia Mono, Consolas, monospace";
    context.lineWidth = 1;
    for (let index = 0; index <= tickIntervals; index += 1) {
      const fraction = index / tickIntervals;
      const x = margin.left + fraction * plotWidth;
      const y = margin.top + (1 - fraction) * plotHeight;
      context.strokeStyle = "#1c3731";
      context.beginPath();
      context.moveTo(x, margin.top);
      context.lineTo(x, margin.top + plotHeight);
      context.moveTo(margin.left, y);
      context.lineTo(margin.left + plotWidth, y);
      context.stroke();

      context.fillStyle = "#8fa9a2";
      context.textAlign = "center";
      context.fillText(formatTick(minX + fraction * (maxX - minX)), x, margin.top + plotHeight + 18);
      context.textAlign = "right";
      const rawY = minY + fraction * (maxY - minY);
      const displayY = logarithmic ? 10 ** rawY : rawY;
      context.fillText(formatTick(displayY), margin.left - 9, y + 3);
    }
    context.strokeStyle = "#376158";
    context.strokeRect(margin.left, margin.top, plotWidth, plotHeight);
    context.fillStyle = "#8fa9a2";
    context.textAlign = "center";
    context.fillText(model.xLabel || "x", margin.left + plotWidth / 2, margin.top + plotHeight + 38);
    context.save();
    context.translate(15, margin.top + plotHeight / 2);
    context.rotate(-Math.PI / 2);
    context.fillText(model.yLabel || "y", 0, 0);
    context.restore();
    context.restore();
  }

  function formatTick(value) {
    if (!finite(value)) {
      return "—";
    }
    const magnitude = Math.abs(value);
    if ((magnitude > 0 && magnitude < 0.01) || magnitude >= 10000) {
      return value.toExponential(1);
    }
    return formatNumber(value, 2);
  }

  function minMaxEnvelope(points, limit) {
    if (points.length <= limit) {
      return points;
    }
    if (limit <= 2) {
      return [points[0], points.at(-1)];
    }
    const result = [points[0]];
    const interior = points.slice(1, -1);
    const bucketCount = Math.max(1, Math.floor((limit - 2) / 2));
    const bucketSize = interior.length / bucketCount;
    for (let bucket = 0; bucket < bucketCount; bucket += 1) {
      const start = Math.floor(bucket * bucketSize);
      const stop = Math.max(start + 1, Math.floor((bucket + 1) * bucketSize));
      const entries = interior.slice(start, stop);
      let minimum = entries[0];
      let maximum = entries[0];
      for (const point of entries) {
        if (point.y < minimum.y) {
          minimum = point;
        }
        if (point.y > maximum.y) {
          maximum = point;
        }
      }
      if (minimum === maximum) {
        result.push(minimum);
      } else if (minimum.x <= maximum.x) {
        result.push(minimum, maximum);
      } else {
        result.push(maximum, minimum);
      }
    }
    result.push(points.at(-1));
    return result.slice(0, limit);
  }

  function drawEmptyChart(context, width, height, message) {
    context.fillStyle = "#8fa9a2";
    context.font = "13px system-ui, sans-serif";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(message, width / 2, height / 2);
  }

  function initialize() {
    bindInteractions();
    state.sessionPromise = fetchJSON("/api/session")
      .then((session) => {
        state.token = typeof session.token === "string" ? session.token : null;
      })
      .catch((error) => {
        announce(`Configuration saves unavailable: ${error.message}`);
      });
    void refreshSnapshot(false);
  }

  initialize();
})();
