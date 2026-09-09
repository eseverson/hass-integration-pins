/* Integration Pins sidebar panel.
 * Plain web component: no build step, no framework. Uses Home Assistant's
 * CSS variables and a few of its elements (ha-card, ha-button, ha-icon) so it
 * matches the active theme.
 */

const STATUS_LABEL = {
  active: "Active",
  pending: "Pending restart",
  out_of_range: "Out of range",
  missing: "Missing",
  foreign: "Replaced",
};

const STATUS_HELP = {
  active: "Override is loaded and the running core is inside the pin's range.",
  pending:
    "Files are in place, but Home Assistant only scans custom_components at startup, so the bundled version is still running. Restart to load the pinned code.",
  out_of_range:
    "Override is still loaded, but the running core is outside the range you set. Verify it, then widen the range or unpin.",
  missing: "Pin is recorded but custom_components/<domain> is gone; the bundled version is running.",
  foreign: "custom_components/<domain> exists but was not written by this pin (marker mismatch).",
};

const fmtBytes = (n) => {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class IntegrationPinsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._snapshot = null;
    this._domains = [];
    this._inUseDomains = [];
    this._onlyInUse = true;
    this._domainInfo = null; // dependency info for the domain currently typed in
    this._domainInfoTimer = null;
    this._compare = null; // result of the last "Compare" against the running code
    this._versions = [];
    this._prereleases = false;
    this._busy = null; // text shown while a long operation runs
    this._error = null;
    this._notice = null;
    this._editing = null; // domain currently being edited inline
    this._adopting = null; // domain currently being adopted
    this._unsub = null;
    this._rendered = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._connect();
  }
  get hass() {
    return this._hass;
  }

  set narrow(v) {
    this._narrow = v;
  }
  set panel(v) {
    this._panel = v;
  }

  disconnectedCallback() {
    if (this._unsub) {
      this._unsub.then((u) => u()).catch(() => {});
      this._unsub = null;
    }
  }

  async _connect() {
    this._render();
    try {
      this._unsub = this._hass.connection.subscribeMessage(
        (snap) => {
          this._snapshot = snap;
          this._render();
        },
        { type: "integration_pins/subscribe" }
      );
      const d = await this._hass.callWS({ type: "integration_pins/core_domains" });
      this._domains = d.domains;
      this._inUseDomains = d.in_use || [];
      this._render();
      await this._loadVersions();
    } catch (e) {
      this._error = e.message || String(e);
      this._render();
    }
  }

  async _loadVersions() {
    try {
      const r = await this._hass.callWS({
        type: "integration_pins/versions",
        include_prereleases: this._prereleases,
      });
      this._versions = r.versions;
    } catch (e) {
      this._versions = [];
      this._error = `Could not list releases from PyPI: ${e.message || e}`;
    }
    this._render();
  }

  async _call(msg, busyText) {
    this._error = null;
    this._notice = null;
    this._busy = busyText;
    this._render();
    try {
      const r = await this._hass.callWS(msg);
      return r;
    } catch (e) {
      this._error = e.message || String(e);
      return null;
    } finally {
      this._busy = null;
      this._render();
    }
  }

  // ---- actions -----------------------------------------------------------

  async _pin(form) {
    const domain = form.domain.value.trim();
    const version = form.version.value.trim();
    const core_range = form.core_range.value.trim();
    const reason = form.reason.value.trim();
    if (!domain || !version) {
      this._error = "Domain and version are required.";
      this._render();
      return;
    }
    const r = await this._call(
      { type: "integration_pins/pin", domain, version, core_range, reason },
      `Downloading Home Assistant ${version} and extracting ${domain}… this can take a minute.`
    );
    if (r) {
      this._notice = `Pinned ${domain} to ${version}. Restart Home Assistant to load it.`;
      form.reset();
      this._render();
    }
  }

  async _unpin(domain) {
    const pin = this._snapshot?.pins.find((p) => p.domain === domain);
    const fate = pin && pin.source === "pypi"
      ? `custom_components/${domain}/ is deleted; it came from a release on PyPI, so re-pinning fetches it again.`
      : `custom_components/${domain}/ is moved to integration_pins_retired/, since this copy exists nowhere else.`;
    if (!confirm(`Unpin ${domain}?\n\n${fate}\nThe bundled version is used after the next restart.`)) return;
    const r = await this._call({ type: "integration_pins/unpin", domain }, `Unpinning ${domain}…`);
    if (r !== null) this._notice = `Unpinned ${domain}. Restart Home Assistant to apply.`;
    this._render();
  }

  async _runCompare() {
    const form = this.shadowRoot.querySelector("#add-form");
    const domain = form?.domain.value.trim().toLowerCase();
    const version = form?.version.value;
    if (!domain || !version) {
      this._error = "Pick an integration and a release first.";
      return this._render();
    }
    const r = await this._call(
      { type: "integration_pins/compare", domain, version },
      `Reading ${version}'s file list…`
    );
    this._compare = r;
    this._render();
  }

  _compareHtml() {
    const c = this._compare;
    if (!c) return "";
    if (c.identical) {
      return `
        <div class="ok-box">
          <ha-icon icon="mdi:check-circle-outline"></ha-icon>
          <div><strong>${esc(c.domain)}</strong> in ${esc(c.version)} is byte-identical to
          ${esc(c.compared_with)}. Pinning it would change nothing.</div>
        </div>`;
    }
    const total = c.added.length + c.removed.length + c.changed.length;
    const list = (label, names) => names.length
      ? `<div><span class="muted">${label}</span> <span class="mono small">${esc(names.slice(0, 12).join(", "))}${names.length > 12 ? `, +${names.length - 12} more` : ""}</span></div>`
      : "";
    return `
      <div class="info-box">
        <ha-icon icon="mdi:file-compare"></ha-icon>
        <div>
          <div><strong>${total}</strong> file${total === 1 ? "" : "s"} differ between
          ${esc(c.version)} and ${esc(c.compared_with)} (${c.unchanged} unchanged).</div>
          ${list("changed:", c.changed)}
          ${list("only in " + c.version + ":", c.added)}
          ${list("only in " + c.compared_with + ":", c.removed)}
        </div>
      </div>`;
  }

  async _deleteRetired(name) {
    if (!confirm(`Delete ${name}?\n\nThis removes the retired copy from disk for good.`)) return;
    await this._call({ type: "integration_pins/delete_retired", name }, `Deleting ${name}…`);
  }

  async _clearRetired() {
    const items = this._snapshot?.retired || [];
    const total = fmtBytes(items.reduce((a, r) => a + r.bytes, 0));
    if (!confirm(`Delete all ${items.length} retired copies (${total})?\n\nThis cannot be undone.`)) return;
    const r = await this._call({ type: "integration_pins/clear_retired" }, "Clearing…");
    if (r !== null) this._notice = `Deleted ${r.removed} retired ${r.removed === 1 ? "copy" : "copies"}.`;
    this._render();
  }

  async _saveEdit(domain, form) {
    const r = await this._call(
      {
        type: "integration_pins/update",
        domain,
        core_range: form.core_range.value.trim(),
        reason: form.reason.value.trim(),
      },
      "Saving…"
    );
    if (r) this._editing = null;
    this._render();
  }

  async _repin(pin) {
    const r = await this._call(
      {
        type: "integration_pins/pin",
        domain: pin.domain,
        version: pin.pinned_version,
        core_range: pin.core_range,
        reason: pin.reason,
      },
      `Re-downloading ${pin.domain} from Home Assistant ${pin.pinned_version}…`
    );
    if (r) this._notice = `Restored ${pin.domain} from ${pin.pinned_version}. Restart to load it.`;
    this._render();
  }

  async _adopt(domain, form) {
    const r = await this._call(
      {
        type: "integration_pins/adopt",
        domain,
        pinned_version: form.pinned_version.value.trim(),
        core_range: form.core_range.value.trim(),
        reason: form.reason.value.trim(),
      },
      "Adopting…"
    );
    if (r) {
      this._adopting = null;
      this._notice = `Now managing custom_components/${domain}.`;
    }
    this._render();
  }

  async _restart() {
    if (!confirm("Restart Home Assistant now?")) return;
    try {
      await this._hass.callService("homeassistant", "restart");
      this._notice = "Restart requested.";
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._render();
  }

  async _refresh() {
    await this._call({ type: "integration_pins/check" }, "Checking…");
  }

  // ---- rendering ---------------------------------------------------------

  _render() {
    if (!this._rendered) {
      this.shadowRoot.innerHTML = `<style>${this._css()}</style><div id="root"></div>`;
      this._rendered = true;
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      this.shadowRoot.addEventListener("submit", (ev) => this._onSubmit(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
      this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
    }
    const root = this.shadowRoot.getElementById("root");
    // Preserve focus/values in the add form across re-renders.
    const addForm = root.querySelector("#add-form");
    const saved = addForm ? Object.fromEntries(new FormData(addForm)) : null;
    root.innerHTML = this._html();
    if (saved) {
      const f = root.querySelector("#add-form");
      for (const [k, v] of Object.entries(saved)) if (f && f[k] && f[k].value === "" && k !== "version") f[k].value = v;
      if (f && saved.version && f.version) f.version.value = saved.version;
    }
    this._paintDomainWarning();
    this._paintCompare();
  }

  _paintCompare() {
    const box = this.shadowRoot.getElementById("compare-result");
    if (box) box.innerHTML = this._compareHtml();
  }

  /* Written straight into the DOM rather than through _render(): the domain field is
   * being typed into, and re-rendering the card would drop focus on every keystroke. */
  _paintDomainWarning() {
    const box = this.shadowRoot.getElementById("domain-warning");
    if (box) box.innerHTML = this._domainWarningHtml();
  }

  _onInput(ev) {
    if (ev.target.name !== "domain") return;
    const domain = ev.target.value.trim().toLowerCase();
    if (this._compare && this._compare.domain !== domain) {
      this._compare = null;
      this._paintCompare();
    }
    clearTimeout(this._domainInfoTimer);
    this._domainInfoTimer = setTimeout(() => this._loadDomainInfo(domain), 250);
  }

  async _loadDomainInfo(domain) {
    if (!domain || !this._domains.includes(domain)) {
      this._domainInfo = null;
      this._paintDomainWarning();
      return;
    }
    try {
      this._domainInfo = await this._hass.callWS({ type: "integration_pins/domain_info", domain });
    } catch (e) {
      this._domainInfo = null;
    }
    this._paintDomainWarning();
  }

  _domainWarningHtml() {
    const info = this._domainInfo;
    if (!info || !info.loaded_dependents.length) return "";
    const loaded = info.loaded_dependents;
    const shown = loaded.slice(0, 6).join(", ");
    const rest = loaded.length > 6 ? `, +${loaded.length - 6} more` : "";
    return `
      <div class="warn-box">
        <ha-icon icon="mdi:alert"></ha-icon>
        <div>
          <strong>${esc(info.domain)}</strong> is infrastructure here: ${info.dependents.length}
          core integration${info.dependents.length === 1 ? "" : "s"} depend on it, and
          ${loaded.length} of them ${loaded.length === 1 ? "is" : "are"} loaded on this instance
          (${esc(shown)}${esc(rest)}).
          Pinning it runs all of them against code from the release you pick, and a bad pin here
          takes them down with it.
        </div>
      </div>`;
  }

  _onClick(ev) {
    // ha-button is not form-associated, so submit its form by hand.
    const submit = ev.target.closest('[type="submit"]');
    if (submit && submit.tagName !== "BUTTON" && !submit.hasAttribute("disabled")) {
      ev.preventDefault();
      submit.closest("form")?.requestSubmit();
      return;
    }
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const { action, domain } = btn.dataset;
    const pin = this._snapshot?.pins.find((p) => p.domain === domain);
    switch (action) {
      case "unpin":
        return this._unpin(domain);
      case "edit":
        this._editing = domain;
        return this._render();
      case "cancel-edit":
        this._editing = null;
        return this._render();
      case "repin":
        return this._repin(pin);
      case "adopt":
        this._adopting = domain;
        return this._render();
      case "cancel-adopt":
        this._adopting = null;
        return this._render();
      case "delete-retired":
        return this._deleteRetired(btn.dataset.name);
      case "clear-retired":
        return this._clearRetired();
      case "compare":
        return this._runCompare();
      case "restart":
        return this._restart();
      case "refresh":
        return this._refresh();
      case "dismiss":
        this._error = null;
        this._notice = null;
        return this._render();
    }
  }

  _onSubmit(ev) {
    ev.preventDefault();
    const form = ev.target;
    const kind = form.dataset.form;
    if (kind === "add") return this._pin(form);
    if (kind === "edit") return this._saveEdit(form.dataset.domain, form);
    if (kind === "adopt") return this._adopt(form.dataset.domain, form);
  }

  _onChange(ev) {
    if (ev.target.name === "prereleases") {
      this._prereleases = ev.target.checked;
      this._loadVersions();
    }
    if (ev.target.name === "version" && this._compare) {
      this._compare = null;
      this._render();
    }
    if (ev.target.name === "only_in_use") {
      this._onlyInUse = ev.target.checked;
      this._render();
    }
  }

  _html() {
    const snap = this._snapshot;
    if (!snap) {
      return `<div class="page"><h1>Integration Pins</h1>${this._banners()}<p class="muted">Loading…</p></div>`;
    }
    return `
      <div class="page">
        <div class="header">
          <h1>Integration Pins</h1>
          <div class="header-right">
            <span class="muted">Core <b>${esc(snap.core_version)}</b></span>
            <ha-button data-action="refresh" ${this._busy ? "disabled" : ""}>Re-check</ha-button>
          </div>
        </div>
        ${snap.restart_required ? `
          <div class="alert warning">
            <ha-icon icon="mdi:restart-alert"></ha-icon>
            <span>Pins changed. Home Assistant needs a restart before the change takes effect.</span>
            <ha-button data-action="restart">Restart now</ha-button>
          </div>` : ""}
        ${this._banners()}
        ${this._pinsCard(snap)}
        ${this._addCard(snap)}
        ${this._unmanagedCard(snap)}
        ${this._customIntegrationsCard(snap)}
        ${this._retiredCard(snap)}
        <p class="muted small">
          How it works: Home Assistant loads <code>custom_components/&lt;domain&gt;</code> in
          preference to the bundled integration of the same name. Pinning extracts that
          integration from the chosen release's wheel on PyPI into <code>custom_components/</code>.
          Unpinning moves it to <code>integration_pins_retired/</code>. Nothing is ever changed automatically
          on upgrade: if the running core leaves a pin's range you get a Repair warning and the pinned
          code keeps running until you act.
        </p>
      </div>`;
  }

  _banners() {
    let out = "";
    if (this._busy) out += `<div class="alert info"><ha-icon icon="mdi:progress-download"></ha-icon><span>${esc(this._busy)}</span></div>`;
    if (this._error) out += `<div class="alert error"><ha-icon icon="mdi:alert-circle"></ha-icon><span>${esc(this._error)}</span><button class="link" data-action="dismiss">dismiss</button></div>`;
    if (this._notice) out += `<div class="alert success"><ha-icon icon="mdi:check-circle"></ha-icon><span>${esc(this._notice)}</span><button class="link" data-action="dismiss">dismiss</button></div>`;
    return out;
  }

  _pinsCard(snap) {
    if (!snap.pins.length) {
      return `<ha-card header="Pinned integrations"><div class="card-content muted">Nothing is pinned. Every integration is running the version bundled with core ${esc(snap.core_version)}.</div></ha-card>`;
    }
    const rows = snap.pins.map((p) => this._pinRow(p, snap)).join("");
    return `
      <ha-card header="Pinned integrations">
        <div class="card-content">
          <div class="table">
            <div class="thead">
              <div>Integration</div><div>Pinned from</div><div>Valid for core</div><div>Status</div><div></div>
            </div>
            ${rows}
          </div>
        </div>
      </ha-card>`;
  }

  _pinRow(p, snap) {
    const reqDiff = this._reqDiff(p);
    const editing = this._editing === p.domain;
    const actions = editing
      ? ""
      : `
        <ha-button data-action="edit" data-domain="${esc(p.domain)}" ${this._busy ? "disabled" : ""}>Edit</ha-button>
        ${p.status === "missing" || p.status === "foreign" ? `<ha-button data-action="repin" data-domain="${esc(p.domain)}" ${this._busy ? "disabled" : ""}>Re-pin</ha-button>` : ""}
        <ha-button class="danger" data-action="unpin" data-domain="${esc(p.domain)}" ${this._busy ? "disabled" : ""}>Unpin</ha-button>`;
    return `
      <div class="row ${esc(p.status)}">
        <div class="cell"><b>${esc(p.domain)}</b>${p.source === "adopted" ? ' <span class="tag">adopted</span>' : ""}</div>
        <div class="cell mono">${esc(p.pinned_version)}</div>
        <div class="cell mono">${esc(p.core_range || "any")}</div>
        <div class="cell"><span class="chip ${esc(p.status)}" title="${esc(STATUS_HELP[p.status] || "")}">${esc(STATUS_LABEL[p.status] || p.status)}</span></div>
        <div class="cell actions">${actions}</div>
        <div class="detail">
          ${p.reason ? `<div><span class="muted">Reason:</span> ${esc(p.reason)}</div>` : ""}
          <div class="muted small">Pinned on ${esc((p.created || "").slice(0, 10))} while running core ${esc(p.core_at_pin || "?")}.</div>
          ${p.status !== "active" ? `<div class="small">${esc(STATUS_HELP[p.status])}</div>` : ""}
          ${reqDiff}
          ${editing ? this._editForm(p) : ""}
        </div>
      </div>`;
  }

  _reqDiff(p) {
    const ov = p.override || {};
    if (!ov.present) return "";
    const a = new Set(ov.requirements || []);
    const b = new Set(ov.core_requirements || []);
    const onlyPinned = [...a].filter((x) => !b.has(x));
    const onlyCore = [...b].filter((x) => !a.has(x));
    if (!onlyPinned.length && !onlyCore.length) return `<div class="small muted">Python requirements identical to the bundled version.</div>`;
    return `
      <div class="small">
        <span class="muted">Requirements differ from bundled version:</span>
        ${onlyPinned.map((r) => `<span class="tag pinned mono">${esc(r)}</span>`).join(" ")}
        ${onlyCore.map((r) => `<span class="tag core mono">core: ${esc(r)}</span>`).join(" ")}
        <div class="muted">HA installs the pinned integration's requirements at startup. If another integration needs the newer library this can flip-flop between restarts.</div>
      </div>`;
  }

  _editForm(p) {
    return `
      <form data-form="edit" data-domain="${esc(p.domain)}" class="inline-form">
        <label>Valid for core <input name="core_range" value="${esc(p.core_range)}" placeholder="any" class="mono"></label>
        <label>Reason <input name="reason" value="${esc(p.reason)}"></label>
        <div class="form-actions">
          <ha-button type="submit" ${this._busy ? "disabled" : ""}>Save</ha-button>
          <ha-button data-action="cancel-edit">Cancel</ha-button>
        </div>
        <div class="muted small">Range syntax is PEP 440: <code>==2026.9.1</code>, <code>~=2026.9.0</code> (any 2026.9.x), <code>&gt;=2026.9.0,&lt;2026.11.0</code>. Leave empty for any.</div>
      </form>`;
  }

  _addCard(snap) {
    const opts = this._versions.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    const domains = this._onlyInUse && this._inUseDomains.length ? this._inUseDomains : this._domains;
    return `
      <ha-card header="Pin an integration">
        <div class="card-content">
          <form id="add-form" data-form="add" class="grid-form">
            <label>Integration domain
              <input name="domain" list="core-domains" placeholder="e.g. hue" required autocomplete="off" class="mono">
              <datalist id="core-domains">${domains.map((d) => `<option value="${esc(d)}">`).join("")}</datalist>
              <span class="small"><input type="checkbox" name="only_in_use" ${this._onlyInUse ? "checked" : ""}> only integrations in use (${this._inUseDomains.length} of ${this._domains.length})</span>
            </label>
            <div id="domain-warning" class="span-all"></div>
            <div id="compare-result" class="span-all"></div>
            <label>Take code from release
              <select name="version" required class="mono">
                <option value="">${this._versions.length ? "select…" : "loading from PyPI…"}</option>${opts}
              </select>
              <span class="small"><input type="checkbox" name="prereleases" ${this._prereleases ? "checked" : ""}> include betas</span>
              <ha-button data-action="compare" ${this._busy ? "disabled" : ""}>Compare with running code</ha-button>
            </label>
            <label>Valid for core
              <input name="core_range" value="==${esc(snap.core_version)}" class="mono">
            </label>
            <label>Reason
              <input name="reason" placeholder="what broke, link to the issue…">
            </label>
            <div class="form-actions">
              <ha-button type="submit" raised ${this._busy ? "disabled" : ""}>Pin</ha-button>
            </div>
          </form>
          <div class="muted small">
            Tip: pick the last release where the integration worked. The default range is only the core you are
            running now, so after your next core upgrade you get a warning and can decide whether to keep the pin.
          </div>
        </div>
      </ha-card>`;
  }

  _unmanagedCard(snap) {
    if (!snap.unmanaged_overrides.length) return "";
    const rows = snap.unmanaged_overrides.map((o) => {
      const adopting = this._adopting === o.domain;
      return `
        <div class="row">
          <div class="cell"><b>${esc(o.domain)}</b></div>
          <div class="cell mono">${esc(o.manifest_version || "?")}</div>
          <div class="cell muted small" style="grid-column: span 2">custom_components/${esc(o.domain)} shadows the bundled integration but is not managed here.</div>
          <div class="cell actions">${adopting ? "" : `<ha-button data-action="adopt" data-domain="${esc(o.domain)}">Adopt</ha-button>`}</div>
          ${adopting ? `
            <div class="detail">
              <form data-form="adopt" data-domain="${esc(o.domain)}" class="inline-form">
                <label>Which HA release is this code from? <input name="pinned_version" value="${esc(o.manifest_version || "")}" class="mono" required></label>
                <label>Valid for core <input name="core_range" value="==${esc(snap.core_version)}" class="mono"></label>
                <label>Reason <input name="reason"></label>
                <div class="form-actions">
                  <ha-button type="submit">Adopt</ha-button>
                  <ha-button data-action="cancel-adopt">Cancel</ha-button>
                </div>
              </form>
            </div>` : ""}
        </div>`;
    }).join("");
    return `
      <ha-card header="Other core overrides in custom_components">
        <div class="card-content">
          <div class="table">
            <div class="thead"><div>Integration</div><div>Manifest version</div><div></div><div></div><div></div></div>
            ${rows}
          </div>
        </div>
      </ha-card>`;
  }

  _retiredCard(snap) {
    const items = snap.retired || [];
    if (!items.length) return "";
    const total = fmtBytes(items.reduce((a, r) => a + r.bytes, 0));
    const rows = items.map((r) => `
        <div class="row">
          <div class="cell mono">${esc(r.name)}</div>
          <div class="cell mono">${esc(fmtBytes(r.bytes))}</div>
          <div class="cell actions">
            <ha-button data-action="delete-retired" data-name="${esc(r.name)}" ${this._busy ? "disabled" : ""}>Delete</ha-button>
          </div>
        </div>`).join("");
    return `
      <ha-card header="Retired overrides (${total})">
        <div class="card-content">
          <div class="table cols-3">
            <div class="thead"><div>Directory</div><div>Size</div><div></div></div>
            ${rows}
          </div>
          <div class="form-actions">
            <ha-button data-action="clear-retired" ${this._busy ? "disabled" : ""}>Clear all</ha-button>
          </div>
          <div class="muted small">
            Copies kept in <code>integration_pins_retired/</code> when an override was taken out of
            <code>custom_components/</code>. Unpinning something pinned from PyPI deletes it instead of
            keeping a copy, so only hand-placed code ends up here. Nothing is removed automatically.
          </div>
        </div>
      </ha-card>`;
  }

  _customIntegrationsCard(snap) {
    const items = snap.custom_integrations || [];
    if (!items.length) return "";
    const rows = items.map((c) => `
        <div class="row">
          <div class="cell"><b>${esc(c.domain)}</b></div>
          <div class="cell mono">${esc(c.version || "?")}</div>
          <div class="cell">${c.source === "hacs"
            ? `<span class="chip hacs">HACS</span> <span class="mono small">${esc(c.source_detail)}</span>`
            : `<span class="muted small">manual / unknown</span>`}</div>
        </div>`).join("");
    return `
      <ha-card header="Other custom integrations">
        <div class="card-content">
          <div class="table cols-3">
            <div class="thead"><div>Integration</div><div>Version</div><div>Source</div></div>
            ${rows}
          </div>
          <div class="muted small">
            Third-party integrations in <code>custom_components/</code> that do not shadow a core
            domain, so nothing here is pinnable. Source is read from HACS's own storage where it is
            available; anything HACS does not know about is listed as manual.
          </div>
        </div>
      </ha-card>`;
  }

  _css() {
    return `
      :host { display: block; }
      .page { max-width: 1100px; margin: 0 auto; padding: 16px; color: var(--primary-text-color); }
      h1 { font-size: 24px; font-weight: 400; margin: 8px 0 16px; }
      .header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
      .header-right { display: flex; align-items: center; gap: 16px; }
      ha-card { display: block; margin-bottom: 16px; }
      .card-content { padding: 0 16px 16px; }
      .muted { color: var(--secondary-text-color); }
      .small { font-size: 0.85em; }
      .mono { font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); }
      code { font-family: var(--code-font-family, ui-monospace, Menlo, monospace); background: var(--secondary-background-color); padding: 0 4px; border-radius: 3px; }
      .alert { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-radius: var(--ha-card-border-radius, 12px); margin-bottom: 16px; }
      .alert span { flex: 1; }
      .alert.warning { background: rgba(255, 152, 0, .15); color: var(--warning-color, #ff9800); }
      .alert.error { background: rgba(219, 68, 55, .15); color: var(--error-color, #db4437); }
      .alert.success { background: rgba(67, 160, 71, .15); color: var(--success-color, #43a047); }
      .alert.info { background: rgba(3, 169, 244, .15); color: var(--info-color, #039be5); }
      .alert ha-icon { --mdc-icon-size: 24px; }
      button.link { background: none; border: none; color: inherit; text-decoration: underline; cursor: pointer; font: inherit; }
      .table { display: grid; grid-template-columns: 1.2fr 1fr 1.4fr 0.9fr auto; gap: 0; }
      .table.cols-3 { grid-template-columns: 1.2fr 1fr 2.4fr; }
      .thead { display: contents; }
      .thead > div { font-size: 0.8em; text-transform: uppercase; letter-spacing: .04em; color: var(--secondary-text-color); padding: 8px 8px 4px; border-bottom: 1px solid var(--divider-color); }
      .row { display: contents; }
      .row > .cell { padding: 10px 8px 4px; border-top: 1px solid var(--divider-color); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
      .row > .detail { grid-column: 1 / -1; padding: 0 8px 10px; }
      .row:first-of-type > .cell { border-top: none; }
      .actions { justify-content: flex-end; }
      .chip { padding: 2px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 500; white-space: nowrap; }
      .chip.active { background: rgba(67,160,71,.2); color: var(--success-color, #43a047); }
      .warn-box { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border-radius: 8px;
        background: rgba(255,152,0,.12); border: 1px solid rgba(255,152,0,.4); font-size: 0.92em; }
      .warn-box ha-icon { color: var(--warning-color, #ff9800); flex: none; }
      .ok-box, .info-box { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px;
        border-radius: 8px; font-size: 0.92em; }
      .ok-box { background: rgba(67,160,71,.12); border: 1px solid rgba(67,160,71,.4); }
      .ok-box ha-icon { color: var(--success-color, #43a047); flex: none; }
      .info-box { background: var(--secondary-background-color); border: 1px solid var(--divider-color); }
      .info-box ha-icon { color: var(--secondary-text-color); flex: none; }
      .chip.hacs { background: rgba(3,155,229,.18); color: var(--info-color, #039be5); }
      .chip.pending { background: rgba(3,155,229,.2); color: var(--info-color, #039be5); }
      .chip.out_of_range { background: rgba(255,152,0,.2); color: var(--warning-color, #ff9800); }
      .chip.missing, .chip.foreign { background: rgba(219,68,55,.2); color: var(--error-color, #db4437); }
      .tag { font-size: 0.75em; padding: 1px 6px; border-radius: 4px; background: var(--secondary-background-color); }
      .tag.pinned { background: rgba(3,169,244,.15); }
      .tag.core { background: var(--secondary-background-color); text-decoration: line-through; opacity: .8; }
      form label { display: flex; flex-direction: column; gap: 4px; font-size: 0.9em; color: var(--secondary-text-color); }
      input, select { font: inherit; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 8px; }
      input[type=checkbox] { padding: 0; }
      .grid-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; align-items: start; margin-bottom: 8px; }
      .inline-form { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px; padding: 12px; background: var(--secondary-background-color); border-radius: 8px; }
      .inline-form .muted, .form-actions, .span-all { grid-column: 1 / -1; }
      .span-all:empty { display: none; }
      .form-actions { display: flex; gap: 8px; }
      ha-button.danger { --mdc-theme-primary: var(--error-color); }
      @media (max-width: 800px) {
        .table { grid-template-columns: 1fr 1fr; }
        .thead { display: none; }
        .row > .cell { border-top: none; }
        .row > .cell:first-child { border-top: 1px solid var(--divider-color); grid-column: 1 / -1; }
        .actions { grid-column: 1 / -1; justify-content: flex-start; }
        .inline-form { grid-template-columns: 1fr; }
      }
    `;
  }
}

customElements.define("integration-pins-panel", IntegrationPinsPanel);
