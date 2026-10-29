"use strict";

(function dashboard() {
  const array = value => Array.isArray(value) ? value : [];
  const number = value => value !== null && value !== undefined && String(value).length <= 100 && /^[+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?$/.test(String(value)) && Number.isFinite(Number(value)) && Math.abs(Number(value)) <= 1e12 && (Number(value) === 0 ? !/[1-9]/.test(String(value).split(/[eE]/)[0]) : Math.abs(Number(value)) >= .000001);
  const count = value => new Intl.NumberFormat("ru-RU").format(value);
  const date = value => Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "Нет данных";
  const labels = { succeeded: "Завершён", failed: "Ошибка", running: "Выполняется", leased: "Выполняется", queued: "В очереди", pending: "В очереди", cancelled: "Отменён", confirmed: "Подтверждено", stale: "Устарело", available: "В наличии", unavailable: "Нет в наличии", out_of_stock: "Нет в наличии", unknown: "Неизвестно" };
  const label = value => labels[value] || value || "Нет данных";
  const plural = (value, forms) => forms[new Intl.PluralRules("ru").select(value)] || forms.many;
  const variantsLabel = value => `${value} ${plural(value, { one: "вариант", few: "варианта", many: "вариантов" })}`;

  function formatMoney(amount, currency) {
    if (!number(amount) || !/^[A-Z]{3}$/.test(currency || "")) return "Цена неизвестна";
    try { return new Intl.NumberFormat("ru-RU", { style: "currency", currency, maximumFractionDigits: Number(amount) !== 0 && Math.abs(Number(amount)) < .01 ? 6 : 2 }).format(Number(amount)); }
    catch { return `${amount} ${currency}`; }
  }

  function safeLink(value) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }

  function dimension(observation) {
    const quantity = decimalIdentity(observation?.unit_quantity);
    if (!quantity || !/^[A-Z]{3}$/.test(observation?.price_currency || "") || !(observation?.unit_name === null || typeof observation?.unit_name === "string" && observation.unit_name.length > 0)) return null;
    return JSON.stringify([observation?.price_currency, observation?.unit_name, quantity]);
  }

  function decimalIdentity(value) {
    const match = /^\+?(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(String(value));
    if (!match) return null;
    let digits = (match[1] + (match[2] || "")).replace(/^0+/, "");
    if (!digits) return null;
    let exponent = Number(match[3] || 0) - (match[2] || "").length;
    if (!Number.isSafeInteger(exponent)) return null;
    const trailing = digits.length - digits.replace(/0+$/, "").length;
    digits = digits.slice(0, digits.length - trailing); exponent += trailing;
    return `${digits}e${exponent}`;
  }

  function deriveDashboardState(input, selection = {}) {
    const errors = input.errors || {};
    const products = array(input.products);
    const selectedProduct = products.find(p => p.product_id === selection.productId) || products[0] || null;
    const variants = array(selectedProduct?.variants).length ? selectedProduct.variants : array(input.variants);
    const selectedVariant = variants.find(v => v.variant_id === selection.variantId) || variants[0] || null;
    const history = array(input.history);
    const changes = [];
    const ordered = [...history].sort((a, b) => String(a.observed_at).localeCompare(String(b.observed_at)) || a.observation_id - b.observation_id);
    for (let i = 1; i < ordered.length; i += 1) {
      const a = ordered[i - 1], b = ordered[i];
      const valid = o => o.status === "confirmed" && !o.stale && o.price_state === "present" && number(o.price_amount) && Number(o.price_amount) > 0 && Number.isFinite(Date.parse(o.observed_at)) && Date.parse(o.observed_at) <= Date.now() + 120000;
      if (valid(a) && valid(b) && a.variant_id === b.variant_id && dimension(a) !== null && dimension(a) === dimension(b) && decimalIdentity(a.price_amount) !== decimalIdentity(b.price_amount)) {
        changes.push({ from: a.price_amount, to: b.price_amount, currency: b.price_currency, observed_at: b.observed_at });
      }
    }
    const blocked = array(input.policies).some(p => p.live_enabled === false) || array(input.sources).some(s => s.live_enabled === false) ? ["Сбор отключён политикой одного из источников."] : [];
    for (const error of Object.values(errors)) if ([403, 429].includes(error?.status)) blocked.push(error.message);
    return { ...input, loading: Boolean(input.loading), status: input.loading ? "loading" : Object.values(errors).some(Boolean) ? "error" : "ready",
      error: Object.values(errors).find(Boolean)?.message || "", blocked, sources: array(input.sources), products, variants,
      selectedProduct, selectedVariant, selectedVariantId: selectedVariant?.variant_id || null, history, changes,
      stale: history.filter(o => o.status === "stale" || o.stale), jobs: array(input.jobs),
      alerts: array(input.alerts || input.alertsEvents), quality: array(input.quality || input.qualityIssues),
      totals: input.totals || { products: products.length, variants: variants.length, observations: history.length } };
  }

  function visibleProducts(products, controls = {}) {
    const query = (controls.search || "").trim().toLocaleLowerCase("ru");
    return products.filter(product => {
      const variants = array(product.variants);
      const searchable = [product.title, product.product_id, ...variants.map(v => v.sku)].join(" ").toLocaleLowerCase("ru");
      if (query && !searchable.includes(query)) return false;
      if (controls.filter === "available" && !variants.some(v => v.latest?.availability === "available" && !v.latest.stale && v.latest.status === "confirmed")) return false;
      if (controls.filter === "changed" && !variants.some(v => number(v.delta) && Number(v.delta) !== 0 && !v.latest?.stale)) return false;
      if (controls.filter === "stale" && !variants.some(v => v.latest?.stale)) return false;
      return true;
    }).sort((a, b) => controls.sort === "fresh" ? latestTime(b) - latestTime(a) || String(a.title).localeCompare(String(b.title), "ru") : String(a.title).localeCompare(String(b.title), "ru"));
  }

  function latestTime(product) {
    return Math.max(0, ...array(product.variants).map(v => Date.parse(v.latest?.observed_at) || 0));
  }

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function badge(text, kind = "") { return el("span", text, `pill ${kind}`); }
  function activity(title, detail, status, kind) {
    const item = el("li"), text = el("div", undefined, "item-text");
    text.append(el("span", title, "strong"), el("span", detail, "muted"));
    item.append(text);
    if (status) item.append(badge(status, kind));
    return item;
  }
  function list(root, id, items, make) {
    root.querySelector(`#${id}`).replaceChildren(...items.map(make));
    const empty = root.querySelector(`#${id}-empty`);
    if (empty) empty.hidden = items.length > 0;
  }
  function availability(variants) {
    const latest = variants.map(v => v.latest).filter(Boolean);
    if (!latest.length) return ["Нет данных", ""];
    if (latest.some(o => o.stale || o.status !== "confirmed")) return ["Требует обновления", "warn"];
    if (latest.some(o => o.availability === "available")) return ["В наличии", "good"];
    if (latest.every(o => ["unavailable", "out_of_stock"].includes(o.availability))) return ["Нет в наличии", ""];
    return ["Неизвестно", ""];
  }

  function chartPoints(state, period, now = Date.now()) {
    const reference = state.selectedVariant?.latest || [...state.history].sort((a, b) => String(b.observed_at).localeCompare(String(a.observed_at)))[0];
    return state.history.filter(o => o.variant_id === state.selectedVariantId && o.status === "confirmed" && !o.stale && o.price_state === "present" && number(o.price_amount) && Number(o.price_amount) > 0 && dimension(o) !== null && dimension(o) === dimension(reference) && Date.parse(o.observed_at) >= now - period * 86400000 && Date.parse(o.observed_at) <= now + 120000)
      .sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at) || a.observation_id - b.observation_id);
  }

  function renderChart(root, state, period) {
    const chart = root.querySelector("#chart"), points = chartPoints(state, period);
    root.querySelector("#history-warning").hidden = !state.historyError;
    chart.replaceChildren();
    chart.hidden = !points.length;
    const empty = root.querySelector("#history-empty");
    empty.hidden = points.length > 0;
    empty.textContent = state.historyLoading ? "Загрузка истории…" : state.historyError ? "История недоступна. Повторите обновление." : "За выбранный период нет подтверждённых цен этого варианта.";
    root.querySelector("#history-count").textContent = `${points.length} ${plural(points.length, { one: "точка", few: "точки", many: "точек" })}${state.historyTruncated ? " · последние 100 наблюдений" : ""}`;
    root.querySelector("#chart-dimension").textContent = state.selectedVariant?.latest?.price_currency || "";
    if (!points.length) return;
    const width = Math.max(260, chart.clientWidth || 640), height = Math.max(160, chart.clientHeight || 206);
    const left = 66, right = width - 12, bottom = height - 34;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `История цены: ${points.length} наблюдений. ${points.length === 1 ? "Одна точка" : "От " + formatMoney(points[0].price_amount, points[0].price_currency) + " до " + formatMoney(points.at(-1).price_amount, points.at(-1).price_currency)}`);
    const shape = (tag, attrs, text) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
      if (text !== undefined) node.textContent = text;
      svg.append(node); return node;
    };
    const values = points.map(p => Number(p.price_amount)), low = Math.min(...values), high = Math.max(...values);
    const padding = Math.max((high - low) * .2, high * .01, .01), min = Math.max(0, low - padding), max = high + padding;
    const start = Date.parse(points[0].observed_at), end = Date.parse(points.at(-1).observed_at);
    const x = p => end === start ? (left + right) / 2 : left + (Date.parse(p.observed_at) - start) / (end - start) * (right - left);
    const y = value => bottom - (value - min) / (max - min) * (bottom - 15);
    for (let i = 0; i < 4; i += 1) {
      const value = min + (max - min) * i / 3, position = y(value);
      shape("line", { x1: left, x2: right, y1: position, y2: position });
      shape("text", { x: left - 9, y: position + 3, "text-anchor": "end" }, count(Math.round(value * 100) / 100));
    }
    const coords = points.map(p => `${x(p)},${y(Number(p.price_amount))}`);
    if (points.length > 1) shape("polyline", { points: coords.join(" "), class: "price-path" });
    points.forEach(p => {
      const dot = shape("circle", { cx: x(p), cy: y(Number(p.price_amount)), r: points.length === 1 ? 4 : 2.5 });
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${date(p.observed_at)} · ${formatMoney(p.price_amount, p.price_currency)}`; dot.append(title);
    });
    shape("text", { x: left, y: height - 7 }, date(points[0].observed_at));
    if (points.length > 1) shape("text", { x: right, y: height - 7, "text-anchor": "end" }, date(points.at(-1).observed_at));
    chart.append(svg);
  }

  function renderDashboard(root, state, controls = {}, actions = {}) {
    const text = (id, value) => { root.querySelector(`#${id}`).textContent = value; };
    const error = root.querySelector("#dashboard-error");
    error.textContent = state.error ? `Не удалось обновить данные: ${state.error}. Показана последняя загруженная выборка.` : "";
    error.hidden = !state.error;
    text("dashboard-state", state.loading ? "Загрузка сохранённых данных…" : state.error ? "Данные не обновлены" : `Загружено ${date(state.generated_at)}${state.products.some(p => array(p.variants).some(v => v.latest?.stale)) ? " · есть устаревшие цены" : ""}`);
    text("dashboard-blocked", state.blocked.join(" "));
    root.querySelector("#dashboard-blocked").hidden = !state.blocked.length;
    root.querySelector("#truncation").hidden = !Object.values(state.truncated || {}).some(Boolean) && !state.historyTruncated;
    for (const key of ["products", "variants", "observations"]) text(`metric-${key}`, state.loading && !state.generated_at ? "—" : count(state.totals[key] || 0));
    text("metric-changes", state.loading && !state.generated_at ? "—" : state.products.flatMap(p => array(p.variants)).filter(v => number(v.delta) && Number(v.delta) !== 0 && !v.latest?.stale).length);
    text("nav-count", count(state.totals.products || 0));
    text("catalog-count", count(state.totals.products || 0));
    const sourceSelect = root.querySelector("#source-select");
    const options = controls.sourceOptions || state.sources;
    const signature = JSON.stringify(options.map(s => [s.source_id, s.name]));
    if (sourceSelect.dataset.signature !== signature) {
      sourceSelect.replaceChildren(new Option("Все источники", ""), ...options.map(s => new Option(s.name || s.source_id, s.source_id)));
      sourceSelect.dataset.signature = signature;
    }
    sourceSelect.value = controls.sourceId || "";
    const visible = visibleProducts(state.products, controls);
    text("visible-count", `Показано ${visible.length} из ${state.products.length}`);
    list(root, "products", visible, product => {
      const row = el("tr"); row.className = product.product_id === state.selectedProduct?.product_id ? "selected" : "";
      const cell = el("td"), wrap = el("div", undefined, "product-cell"), names = el("div");
      const button = el("button", product.title || product.product_id, "product-title");
      button.setAttribute("aria-pressed", String(product.product_id === state.selectedProduct?.product_id));
      button.addEventListener("click", () => actions.selectProduct?.(product.product_id));
      const sku = product.variants?.[0]?.sku || "";
      const article = sku && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sku) ? `${sku} · ` : "";
      names.append(button, el("span", `${article}${variantsLabel(product.variant_total ?? array(product.variants).length)}${product.partial ? " · выборка неполная" : ""}`, "product-meta"));
      wrap.append(names); cell.append(wrap); row.append(cell);
      const price = product.mixed_dimensions ? "Разные единицы / валюты" : product.price_min === product.price_max ? formatMoney(product.price_min, product.currency) : `${formatMoney(product.price_min, product.currency)} – ${formatMoney(product.price_max, product.currency)}`;
      row.append(el("td", price, "price-cell"));
      const deltas = array(product.variants).filter(v => number(v.delta) && Number(v.delta) !== 0 && !v.latest?.stale);
      const delta = deltas.length ? variantsLabel(deltas.length) : "—";
      row.append(el("td", delta, deltas.length ? "delta-up" : "muted"));
      const stock = el("td"); stock.append(badge(...availability(array(product.variants)))); row.append(stock);
      row.append(el("td", latestTime(product) ? date(new Date(latestTime(product)).toISOString()) : "Нет данных", "muted"));
      const more = el("td"), open = el("button", "↗", "row-action"); open.setAttribute("aria-label", `История: ${product.title || product.product_id}`);
      open.addEventListener("click", () => { actions.selectProduct?.(product.product_id); root.querySelector("#analytics").scrollIntoView({ behavior: "smooth", block: "start" }); }); more.append(open); row.append(more);
      return row;
    });
    const product = state.selectedProduct, variant = state.selectedVariant, latest = variant?.latest;
    text("selected-title", product?.title || "Выберите товар");
    text("selected-price", latest ? formatMoney(latest.price_amount, latest.price_currency) : "Цена неизвестна");
    const status = root.querySelector("#selected-status");
    const stateLabel = latest?.stale ? ["Данные устарели", "warn"] : latest?.status !== "confirmed" ? ["Нет подтверждения", "warn"] : availability(variant ? [variant] : []);
    status.textContent = stateLabel[0]; status.className = `pill ${stateLabel[1]}`;
    const link = root.querySelector("#product-link"), url = safeLink(product?.canonical_url);
    link.hidden = !url; if (url) link.href = url; else link.removeAttribute("href");
    root.querySelector("#variant-options").replaceChildren(...state.variants.map(v => {
      const title = [v.attributes?.size, v.attributes?.size_system, v.attributes?.color].filter(Boolean).join(" · ") || v.sku || v.variant_id;
      const button = el("button", title); button.setAttribute("aria-pressed", String(v.variant_id === state.selectedVariantId));
      button.addEventListener("click", () => actions.selectVariant?.(v.variant_id)); return button;
    }));
    for (const format of ["csv", "json"]) {
      const link = root.querySelector(`#export-${format}`); link.hidden = !state.selectedVariantId;
      if (state.selectedVariantId) link.href = `/variants/${encodeURIComponent(state.selectedVariantId)}/export?format=${format}`;
      else link.removeAttribute("href");
    }
    list(root, "sources", state.sources, source => {
      const item = el("div", undefined, "source-item"), title = el("div", undefined, "source-name");
      const active = state.jobs.some(j => j.source_id === source.source_id && ["running", "leased"].includes(j.status));
      const status = source.cooldown ? ["Сбор приостановлен", "warn"] : source.has_error ? ["Ошибка сбора", "bad"] : active ? ["Выполняется", "good"] : source.enabled ? ["По расписанию", "good"] : ["Отключён", ""];
      title.append(el("span", source.name || source.source_id), badge(...status)); item.append(title);
      const details = [["Интервал", source.enabled && source.interval_seconds ? `${source.interval_seconds / 60} мин` : "Не активен"], ["Следующий запуск", active ? "После текущего сбора" : source.next_run_at ? Date.parse(source.next_run_at) <= Date.now() ? "Ожидает планировщика" : date(source.next_run_at) : "Не запланирован"], ["Успешный сбор", date(source.last_success_at)]];
      details.forEach(([key, value]) => { const line = el("div", undefined, "source-detail"); line.append(el("span", key), el("b", value)); item.append(line); }); return item;
    });
    list(root, "jobs", state.jobs, job => activity(label(job.status), `${job.source_id || ""} · ${date(job.updated_at || job.created_at)}`, `#${job.job_id.slice(-8)}`, job.status === "failed" ? "bad" : ""));
    list(root, "alerts", state.alerts, event => activity(event.title || event.rule_id, `${date(event.occurred_at)} · ${event.rule_id}`, formatMoney(event.delta_amount, event.delta_currency), Number(event.delta_amount) < 0 ? "good" : "warn"));
    list(root, "quality", state.quality, issue => activity(issue.issue_type, `${issue.source_id || ""} · ${date(issue.created_at)}`, "Замечание", "warn"));
    text("alert-count", state.alerts.length); text("quality-count", state.quality.length);
    list(root, "history", state.history.slice(0, 6), observation => activity(formatMoney(observation.price_amount, observation.price_currency), date(observation.observed_at), label(observation.status), observation.status === "stale" ? "warn" : ""));
    list(root, "changes", state.changes.slice(-3).reverse(), change => activity(`${formatMoney(change.from, change.currency)} → ${formatMoney(change.to, change.currency)}`, date(change.observed_at)));
    root.querySelector("#stale-empty").hidden = state.stale.length > 0 || !state.history.length;
    renderChart(root, state, controls.period || 7);
  }

  async function fetchJson(url, signal) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener("abort", cancel, { once: true });
    const timeout = setTimeout(cancel, 10000);
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" }, signal: controller.signal });
      if (!response.ok) { const error = new Error(`HTTP ${response.status}`); error.status = response.status; throw error; }
      return await response.json();
    } catch (error) {
      if (controller.signal.aborted && !signal?.aborted) throw new Error("Превышено время ожидания API");
      throw error;
    } finally { clearTimeout(timeout); signal?.removeEventListener("abort", cancel); }
  }

  async function loadDashboardData(selection = {}) {
    const snapshot = await fetchJson(`/dashboard/data${selection.sourceId ? `?source_id=${encodeURIComponent(selection.sourceId)}` : ""}`, selection.signal);
    let state = deriveDashboardState(snapshot, selection);
    if (state.selectedVariantId) {
      try {
        const history = await fetchJson(`/variants/${encodeURIComponent(state.selectedVariantId)}/history?limit=100`, selection.signal);
        state = deriveDashboardState({ ...snapshot, history, historyTruncated: history.length === 100 }, selection);
      } catch (error) {
        if (error.name === "AbortError") throw error;
        state.historyError = true;
      }
    }
    return state;
  }

  function createController(root, loader = loadDashboardData) {
    let state = deriveDashboardState({ loading: true }), generation = 0, pending = false, abort;
    const controls = { sourceId: "", productId: null, variantId: null, search: "", filter: "all", sort: "title", period: 7, sourceOptions: [] };
    const actions = {
      selectProduct(id) { controls.productId = id; controls.variantId = null; select(); },
      selectVariant(id) { controls.variantId = id; select(); },
    };
    const render = () => renderDashboard(root, state, controls, actions);
    function select() {
      state = deriveDashboardState({ ...state, history: [], historyLoading: true, historyError: false }, controls);
      render(); refresh(true);
    }
    async function refresh(force = false) {
      if (pending && !force) return;
      const token = ++generation; pending = true;
      abort?.abort(); abort = new AbortController();
      root.querySelector("#refresh").disabled = true;
      try {
        const next = await loader({ ...controls, signal: abort.signal });
        if (token !== generation) return;
        if (next.historyError && next.selectedVariantId === state.selectedVariantId && controls.sourceId === (state.sourceId || "")) {
          const history = state.history;
          state = deriveDashboardState({ ...next, history, historyError: true }, controls);
        } else state = next;
        state.sourceId = controls.sourceId;
        controls.productId = state.selectedProduct?.product_id || null; controls.variantId = state.selectedVariantId;
        if (!controls.sourceId) controls.sourceOptions = state.sources;
        render();
      } catch (error) {
        if (token !== generation || error.name === "AbortError") return;
        state = { ...state, loading: false, historyLoading: false, status: "error", error: error.message || "Ошибка сети" }; render();
      } finally { if (token === generation) { pending = false; root.querySelector("#refresh").disabled = false; } }
    }
    root.querySelector("#source-select").addEventListener("change", event => {
      controls.sourceId = event.target.value; controls.productId = null; controls.variantId = null;
      state = deriveDashboardState({ loading: true }); render(); refresh(true);
    });
    root.querySelector("#search").addEventListener("input", event => { controls.search = event.target.value; render(); });
    for (const key of ["filter", "sort"]) root.querySelector(`#${key}`).addEventListener("change", event => { controls[key] = event.target.value; render(); });
    root.querySelectorAll("[data-period]").forEach(button => button.addEventListener("click", () => {
      controls.period = Number(button.dataset.period);
      root.querySelectorAll("[data-period]").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
      render();
    }));
    root.querySelector("#refresh").addEventListener("click", () => refresh(true));
    render();
    return { refresh, dispose() { generation += 1; abort?.abort(); }, getState: () => state };
  }

  function startPolling(root, loader, timer) {
    let busy = false;
    const refresh = async () => { if (busy) return; busy = true; try { await loader(root); } finally { busy = false; } };
    refresh(); return (timer || window.setInterval)(refresh, 30000);
  }

  const exposed = { deriveDashboardState, formatMoney, safeLink, visibleProducts, chartPoints, loadDashboardData, renderDashboard, createController, startPolling };
  if (typeof module !== "undefined" && module.exports) module.exports = exposed;
  if (typeof window !== "undefined" && window.document) {
    window.pricepulseDashboard = exposed;
    window.addEventListener("DOMContentLoaded", () => {
      const root = document.querySelector("body");
      if (!root) return;
      const controller = createController(root), timer = startPolling(root, () => controller.refresh());
      document.querySelectorAll(".app-navigation a").forEach(link => link.addEventListener("click", () => {
        document.querySelectorAll(".app-navigation a").forEach(item => item.classList.toggle("nav-active", item === link));
      }));
      window.addEventListener("pagehide", () => { window.clearInterval(timer); controller.dispose(); }, { once: true });
    });
  }
})();
