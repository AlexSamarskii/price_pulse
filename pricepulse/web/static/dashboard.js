"use strict";

(function dashboard() {
  function formatMoney(amount, currency) {
    if (amount === null || amount === undefined || amount === "") {
      return "unknown";
    }
    return `${amount} ${currency || "unknown"}`;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function deriveDashboardState(input) {
    const errors = input.errors || {};
    const loading = Boolean(input.loading);
    const state = {
      loading,
      status: loading ? "loading" : Object.values(errors).some(Boolean) ? "error" : "ready",
      error: Object.values(errors).find(Boolean)?.message || "",
      blocked: [],
      sources: asArray(input.sources),
      products: asArray(input.products),
      variants: asArray(input.variants),
      history: asArray(input.history),
      jobs: [],
      changes: [],
      alerts: asArray(input.alertsEvents),
      quality: [],
      stale: [],
    };

    for (const policy of asArray(input.policies)) {
      if (policy && policy.live_enabled === false) {
        state.blocked.push("Live collection is disabled by the source policy.");
      }
    }
    for (const error of Object.values(errors)) {
      if (error && (error.status === 403 || error.status === 429)) {
        state.blocked.push(error.message || "The requested operation is blocked.");
      }
    }

    state.selectedProduct = state.products.find((product) => product.variant_id) || state.products[0] || null;
    state.selectedVariant = state.variants.find((variant) => variant.variant_id === (state.selectedProduct || {}).variant_id) || state.variants[0] || null;
    state.selectedVariantId = state.selectedVariant ? state.selectedVariant.variant_id : null;
    state.jobs = asArray(input.jobs);
    state.quality = asArray(input.qualityIssues);

    const orderedHistory = [...state.history].sort(
      (left, right) => String(left.observed_at).localeCompare(String(right.observed_at)) || (left.observation_id || 0) - (right.observation_id || 0),
    );
    state.stale = orderedHistory.filter((observation) => observation.status === "stale");
    for (let index = 1; index < orderedHistory.length; index += 1) {
      const previous = orderedHistory[index - 1];
      const current = orderedHistory[index];
      if (previous.price_amount === null || current.price_amount === null) {
        continue;
      }
      const delta = Number(current.price_amount) - Number(previous.price_amount);
      state.changes.push({
        variant_id: current.variant_id,
        observation_id: current.observation_id,
        observed_at: current.observed_at,
        from: previous.price_amount,
        to: current.price_amount,
        delta: Number.isFinite(delta) ? delta.toFixed(2) : "",
        currency: current.price_currency,
      });
    }
    return state;
  }

  function setText(element, text) {
    element.textContent = text === null || text === undefined ? "" : String(text);
  }

  function itemLabel(left, right) {
    const item = document.createElement("li");
    const strong = document.createElement("span");
    strong.className = "strong";
    setText(strong, left);
    item.appendChild(strong);
    if (right) {
      const muted = document.createElement("span");
      muted.className = "muted";
      setText(muted, right);
      item.appendChild(muted);
    }
    return item;
  }

  function tagItem(label, tagText, detail) {
    const item = itemLabel(label, detail);
    const tag = document.createElement("span");
    tag.className = `tag ${tagText}`;
    setText(tag, tagText);
    item.appendChild(tag);
    return item;
  }

  function render(element, items, createItem, emptyElement, emptyText) {
    element.replaceChildren();
    emptyElement.textContent = emptyText;
    emptyElement.hidden = items.length > 0;
    for (const item of items) element.appendChild(createItem(item));
  }

  function renderDashboard(root, state) {
    const stateElement = root.querySelector("#dashboard-state");
    const errorElement = root.querySelector("#dashboard-error");
    const blockedElement = root.querySelector("#dashboard-blocked");
    stateElement.textContent = state.loading ? "Loading persisted data…" : state.error ? "Dashboard data is unavailable." : "Persisted dashboard data is current.";
    setText(errorElement, state.error);
    errorElement.hidden = !state.error;
    setText(blockedElement, state.blocked.join(" "));
    blockedElement.hidden = state.blocked.length === 0;

    render(
      root.querySelector("#sources"),
      state.sources,
      (source) => itemLabel(source.name || source.source_id, source.base_url),
      root.querySelector("#sources-empty"),
      "No owned sources are stored yet.",
    );

    render(
      root.querySelector("#products"),
      state.products,
      (product) => itemLabel(product.title || product.product_id, product.sku || product.variant_key),
      root.querySelector("#products-empty"),
      "No products are stored yet.",
    );

    const historyItems = [...state.history].sort(
      (left, right) => String(right.observed_at).localeCompare(String(left.observed_at)) || (right.observation_id || 0) - (left.observation_id || 0),
    );
    render(
      root.querySelector("#history"),
      historyItems,
      (observation) => tagItem(
        formatMoney(observation.price_amount, observation.price_currency),
        observation.status,
        observation.observed_at,
      ),
      root.querySelector("#history-empty"),
      "No observations are stored for the selected product.",
    );

    render(
      root.querySelector("#changes"),
      state.changes,
      (change) => itemLabel(
        `${change.from} → ${change.to} (${change.delta >= 0 ? "+" : ""}${change.delta})`,
        change.currency,
      ),
      root.querySelector("#changes-empty"),
      "At least two observations are required to show a price change.",
    );

    render(
      root.querySelector("#jobs"),
      state.jobs,
      (job) => tagItem(job.job_id, job.status, job.updated_at || job.created_at),
      root.querySelector("#jobs-empty"),
      "No collection jobs are stored yet.",
    );

    render(
      root.querySelector("#quality"),
      state.quality,
      (issue) => tagItem(issue.issue_id, issue.issue_type, issue.created_at),
      root.querySelector("#quality-empty"),
      "No persisted quality issues are available.",
    );

    render(
      root.querySelector("#alerts"),
      state.alerts,
      (event) => itemLabel(
        `${event.delta_amount} ${event.delta_currency}`,
        `${event.rule_id} · ${event.occurred_at}`,
      ),
      root.querySelector("#alerts-empty"),
      "No persisted alert events are available.",
    );

    root.querySelector("#stale-empty").hidden = state.stale.length > 0;
  }

  async function fetchJson(url, errors, key) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.error) message = payload.error;
      } catch (parseError) {
        message = `HTTP ${response.status}`;
      }
      errors[key] = { status: response.status, message };
      return [];
    }
    return response.json();
  }

  async function loadDashboardData() {
    const errors = {};
    const sources = await fetchJson("/sources?limit=100", errors, "sources");
    const source = sources[0] || null;
    const policies = source ? await fetchJson(`/sources/${encodeURIComponent(source.source_id)}/policies`, errors, "policies") : [];
    const products = source ? await fetchJson(`/products?source_id=${encodeURIComponent(source.source_id)}&limit=100`, errors, "products") : [];
    const variants = products[0] ? await fetchJson(`/products/${encodeURIComponent(products[0].product_id)}/variants`, errors, "variants") : [];
    const variant = variants[0] || null;
    const history = variant ? await fetchJson(`/variants/${encodeURIComponent(variant.variant_id)}/history?limit=100`, errors, "history") : [];
    const alertsEvents = await fetchJson("/alerts/events?limit=100", errors, "alertsEvents");
    const jobs = await fetchJson("/jobs?limit=100", errors, "jobs");
    const qualityIssues = await fetchJson("/quality-issues?limit=100", errors, "qualityIssues");
    const state = deriveDashboardState({
      sources,
      policies,
      products,
      variants,
      history,
      jobs,
      alertsEvents,
      qualityIssues,
      errors,
    });
    return state;
  }

  async function loadDashboard(root) {
    const state = await loadDashboardData();
    renderDashboard(root, state);
  }

  function initDashboard(root, loader) {
    return (loader || loadDashboard)(root).catch((error) => {
      const state = deriveDashboardState({
        errors: { sources: { status: error.status || 0, message: error.message || "Unknown dashboard error" } },
      });
      renderDashboard(root, state);
    });
  }

  const exposed = { deriveDashboardState, formatMoney, loadDashboardData, renderDashboard };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exposed;
  }
  if (typeof window !== "undefined" && window.document) {
    window.pricepulseDashboard = exposed;
    window.addEventListener("DOMContentLoaded", () => {
      const root = document.querySelector("main.dashboard");
      if (root) initDashboard(root);
    });
  }
})();
