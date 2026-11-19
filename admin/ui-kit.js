/*
 * Itura UI Kit — Phase 2 frontend infrastructure
 * ------------------------------------------------
 * Dependency-free (no build step, no external library — matches the rest of
 * admin/index.html). Loaded as a plain <script src> BEFORE the main inline
 * <script> block, so every function here is available to it.
 *
 * Everything is exposed on a single global, `UI`, so it never collides with
 * the ~9,000 lines of existing global functions/variables in index.html.
 *
 * This file does not read `adminKey` / `agentRole` at load time (they are
 * declared later, by the inline script) — only inside functions that run
 * after the page has finished loading, by which point those globals exist.
 * Top-level `let`/`const` in one classic <script> tag are visible by name to
 * classic <script> tags loaded both before and after it in the same
 * document, so this works without any window.* wiring.
 */
(function (global) {
  "use strict";

  // ---------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------

  function _esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function _getGlobal(name) {
    try {
      // eslint-disable-next-line no-eval
      return eval("typeof " + name + " !== 'undefined' ? " + name + " : undefined");
    } catch (e) {
      return undefined;
    }
  }

  function _el(tag, className, html) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  // ---------------------------------------------------------------------
  // Authentication / API requests
  // ---------------------------------------------------------------------

  function getAdminKey() {
    const k = _getGlobal("adminKey");
    return k || "";
  }

  /**
   * apiFetch(path, opts)
   *   opts.method       default 'GET'
   *   opts.body         plain object -> JSON-stringified with the right header
   *   opts.params       object of extra query params (merged with ?key=)
   *   opts.toastOnError default true — shows a danger toast automatically on failure
   *   opts.raw          default false — when true, returns the Response instead of parsed JSON
   *
   * Resolves with the parsed JSON body on 2xx.
   * Rejects with { status, message, data } on any failure (network or non-2xx),
   * so callers can still catch and handle it themselves regardless of toastOnError.
   */
  async function apiFetch(path, opts) {
    opts = opts || {};
    const method = opts.method || "GET";
    const toastOnError = opts.toastOnError !== false;

    const url = new URL(path, global.location.origin);
    const key = getAdminKey();
    if (key && !url.searchParams.has("key")) url.searchParams.set("key", key);
    if (opts.params) {
      Object.keys(opts.params).forEach((k) => {
        if (opts.params[k] !== undefined && opts.params[k] !== null) {
          url.searchParams.set(k, opts.params[k]);
        }
      });
    }

    const fetchOpts = { method };
    if (opts.body !== undefined) {
      fetchOpts.headers = { "Content-Type": "application/json" };
      fetchOpts.body = JSON.stringify(opts.body);
    }

    let resp;
    try {
      resp = await fetch(url.toString(), fetchOpts);
    } catch (networkErr) {
      const err = { status: 0, message: "Network error — check your connection.", data: null };
      if (toastOnError) toast(err.message, "danger");
      throw err;
    }

    if (opts.raw) return resp;

    let data = null;
    const text = await resp.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (e) {
        data = text;
      }
    }

    if (!resp.ok) {
      const message =
        (data && typeof data === "object" && (data.detail || data.message)) ||
        `Request failed (${resp.status})`;
      const err = { status: resp.status, message: String(message), data };
      if (toastOnError) toast(err.message, "danger");
      throw err;
    }

    return data;
  }

  // ---------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------

  function _ensureToastStack() {
    let stack = document.querySelector(".ds-toast-stack");
    if (!stack) {
      stack = _el("div", "ds-toast-stack");
      document.body.appendChild(stack);
    }
    return stack;
  }

  /** toast(message, type, opts) — type: 'info' | 'success' | 'warning' | 'danger' */
  function toast(message, type, opts) {
    type = type || "info";
    opts = opts || {};
    const duration = opts.duration || 4000;

    const stack = _ensureToastStack();
    const node = _el(
      "div",
      "ds-toast ds-toast-" + type,
      `<span class="ds-toast-msg">${_esc(message)}</span><button class="ds-toast-close" aria-label="Dismiss">&times;</button>`
    );
    stack.appendChild(node);
    requestAnimationFrame(() => node.classList.add("ds-show"));

    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      node.classList.remove("ds-show");
      setTimeout(() => node.remove(), 200);
    };
    node.querySelector(".ds-toast-close").addEventListener("click", dismiss);
    if (duration > 0) setTimeout(dismiss, duration);
    return { dismiss };
  }

  // ---------------------------------------------------------------------
  // Modal / confirmation dialog
  // ---------------------------------------------------------------------

  /**
   * openModal({title, bodyHtml, actionsHtml}) -> { close(), el }
   * bodyHtml/actionsHtml are trusted HTML strings built by the caller
   * (same convention as the rest of the codebase's innerHTML-based rendering).
   */
  function openModal(config) {
    config = config || {};
    const overlay = _el(
      "div",
      "ds-modal-overlay",
      `<div class="ds-modal" role="dialog" aria-modal="true">
        ${config.title ? `<div class="ds-modal-title">${_esc(config.title)}</div>` : ""}
        <div class="ds-modal-body">${config.bodyHtml || ""}</div>
        <div class="ds-modal-actions">${config.actionsHtml || ""}</div>
      </div>`
    );
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add("ds-open"));

    function close() {
      overlay.classList.remove("ds-open");
      setTimeout(() => overlay.remove(), 200);
    }
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    const escHandler = (e) => {
      if (e.key === "Escape") {
        close();
        document.removeEventListener("keydown", escHandler);
      }
    };
    document.addEventListener("keydown", escHandler);

    return { close, el: overlay };
  }

  /**
   * confirmDialog({title, message, confirmLabel, cancelLabel, danger}) -> Promise<boolean>
   * Resolves true if the user confirms, false if they cancel/dismiss.
   */
  function confirmDialog(config) {
    config = config || {};
    return new Promise((resolve) => {
      const confirmLabel = config.confirmLabel || "Confirm";
      const cancelLabel = config.cancelLabel || "Cancel";
      const confirmBtnClass = config.danger ? "ds-btn ds-btn-danger ds-btn-md" : "ds-btn ds-btn-primary ds-btn-md";

      const overlay = _el(
        "div",
        "ds-modal-overlay",
        `<div class="ds-modal ${config.danger ? "ds-confirm-danger" : ""}" role="alertdialog" aria-modal="true">
          <div class="ds-modal-title">${_esc(config.title || "Are you sure?")}</div>
          <div class="ds-modal-body">${_esc(config.message || "")}</div>
          <div class="ds-modal-actions">
            <button class="ds-btn ds-btn-secondary ds-btn-md" data-act="cancel">${_esc(cancelLabel)}</button>
            <button class="${confirmBtnClass}" data-act="confirm">${_esc(confirmLabel)}</button>
          </div>
        </div>`
      );
      document.body.appendChild(overlay);
      requestAnimationFrame(() => overlay.classList.add("ds-open"));

      let settled = false;
      function finish(result) {
        if (settled) return;
        settled = true;
        overlay.classList.remove("ds-open");
        setTimeout(() => overlay.remove(), 200);
        resolve(result);
      }

      overlay.querySelector('[data-act="confirm"]').addEventListener("click", () => finish(true));
      overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => finish(false));
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) finish(false);
      });
      document.addEventListener("keydown", function escHandler(e) {
        if (e.key === "Escape") {
          document.removeEventListener("keydown", escHandler);
          finish(false);
        }
      });
    });
  }

  // ---------------------------------------------------------------------
  // Drawer
  // ---------------------------------------------------------------------

  /** openDrawer({title, bodyHtml, width}) -> { close(), el } */
  function openDrawer(config) {
    config = config || {};
    const overlay = _el("div", "ds-drawer-overlay");
    const drawer = _el(
      "div",
      "ds-drawer",
      `<div class="ds-drawer-hdr">
        <span class="ds-drawer-title">${_esc(config.title || "")}</span>
        <button class="ds-drawer-close" aria-label="Close">&times;</button>
      </div>
      <div class="ds-drawer-body">${config.bodyHtml || ""}</div>`
    );
    if (config.width) drawer.style.width = config.width;
    overlay.appendChild(drawer);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add("ds-open"));

    function close() {
      overlay.classList.remove("ds-open");
      setTimeout(() => overlay.remove(), 200);
    }
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    drawer.querySelector(".ds-drawer-close").addEventListener("click", close);

    return { close, el: overlay };
  }

  // ---------------------------------------------------------------------
  // Loading / empty / error states
  // ---------------------------------------------------------------------

  /** Returns an HTML string — assign to `container.innerHTML` (matches the
   *  existing codebase's string-building rendering style). */
  function skeletonHTML(kind, count) {
    kind = kind || "rows";
    count = count || 3;
    if (kind === "block") return '<div class="ds-skeleton ds-skeleton-block"></div>';
    if (kind === "metric-grid") {
      let html = '<div class="ds-metric-grid">';
      for (let i = 0; i < count; i++) {
        html += '<div class="ds-metric-card"><div class="ds-skeleton ds-skeleton-text" style="width:50%"></div><div class="ds-skeleton ds-skeleton-text" style="height:22px;width:70%"></div></div>';
      }
      return html + "</div>";
    }
    // 'rows' (default): a handful of label/value skeleton lines
    let html = "";
    for (let i = 0; i < count; i++) {
      html += `<div class="ds-skeleton-row"><div class="ds-skeleton ds-skeleton-text"></div><div class="ds-skeleton ds-skeleton-text"></div></div>`;
    }
    return html;
  }

  /**
   * emptyState(container, {icon, title, sub, actionHtml})
   * Reuses the existing global emptyState()/.ui-empty pattern already used
   * across the app (Broadcast, Products, etc.) instead of introducing a
   * second, competing empty-state component.
   */
  function _renderEmptyState(container, opts) {
    opts = opts || {};
    // NOTE: _getGlobal uses `eval`, which resolves through the lexical scope
    // chain first — if this function were itself named `emptyState`, the
    // lookup below would find *this* function (not index.html's global one)
    // and call it recursively forever. Keep this name distinct from the
    // exported `UI.emptyState` for exactly that reason.
    const globalEmptyState = _getGlobal("emptyState");
    const html =
      typeof globalEmptyState === "function"
        ? globalEmptyState(opts.icon || "", opts.title || "", opts.sub || "", opts.actionHtml || "")
        : `<div class="ui-empty"><div class="ui-empty-icon">${opts.icon || ""}</div><div class="ui-empty-title">${_esc(opts.title || "")}</div>${
            opts.sub ? `<div class="ui-empty-sub">${_esc(opts.sub)}</div>` : ""
          }${opts.actionHtml || ""}</div>`;
    if (container) container.innerHTML = html;
    return html;
  }

  /**
   * errorState(container, {title, message, onRetry})
   * The genuine-failure sibling of emptyState — always offers a retry action
   * when onRetry is given. Built with real DOM nodes (not an HTML string) so
   * the retry callback can be wired directly, with no global registration.
   */
  function errorState(container, opts) {
    opts = opts || {};
    const wrap = _el(
      "div",
      "ds-error-state",
      `<div class="ds-error-icon">!</div>
       <div class="ds-error-title">${_esc(opts.title || "Something went wrong")}</div>
       ${opts.message ? `<div class="ds-error-sub">${_esc(opts.message)}</div>` : ""}`
    );
    if (opts.onRetry) {
      const btn = _el("button", "ds-btn ds-btn-secondary ds-btn-sm", "Try again");
      btn.style.marginTop = "6px";
      btn.addEventListener("click", opts.onRetry);
      wrap.appendChild(btn);
    }
    if (container) {
      container.innerHTML = "";
      container.appendChild(wrap);
    }
    return wrap;
  }

  // ---------------------------------------------------------------------
  // Permission checks
  // ---------------------------------------------------------------------
  // Populated by _applyPerms() in index.html (one additive line added there
  // — see ITURA phase 2 report). Until that's called at least once, can()
  // returns true (fail-open for UX only; every real permission boundary is
  // still enforced server-side, per PRODUCTION_SAFETY.md — this is never the
  // actual security check).

  let _perms = null;

  function setPermissions(perms) {
    _perms = perms || {};
  }

  function can(tab) {
    if (!_perms) return true;
    return _perms[tab] !== false;
  }

  // ---------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------
  // timeAgo()/formatTime() already exist as globals in index.html and are
  // left as-is — these two fill the actual gap (currency + full dates).

  function formatNumber(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return Number(n).toLocaleString("en-NG");
  }

  function formatCurrency(amount, opts) {
    opts = opts || {};
    if (amount === null || amount === undefined || isNaN(amount)) return "—";
    const symbol = opts.symbol || "₦"; // ₦
    const decimals = opts.decimals || 0;
    return (
      symbol +
      Number(amount).toLocaleString("en-NG", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })
    );
  }

  function formatDate(iso, opts) {
    if (!iso) return "";
    opts = opts || {};
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(d.getTime())) return "";
    const fmt = { day: "numeric", month: "short", year: "numeric" };
    if (opts.withTime) {
      fmt.hour = "2-digit";
      fmt.minute = "2-digit";
    }
    return d.toLocaleString("en-GB", fmt);
  }

  // ---------------------------------------------------------------------
  // Minimal pub/sub store — for small pieces of shared UI state
  // (e.g. "which filter is active") without pulling in a state library.
  // ---------------------------------------------------------------------

  function store(initial) {
    let state = initial || {};
    const subscribers = [];
    return {
      get: () => state,
      set: (patch) => {
        state = Object.assign({}, state, patch);
        subscribers.forEach((fn) => fn(state));
      },
      subscribe: (fn) => {
        subscribers.push(fn);
        return () => {
          const i = subscribers.indexOf(fn);
          if (i > -1) subscribers.splice(i, 1);
        };
      },
    };
  }

  // ---------------------------------------------------------------------
  // Export
  // ---------------------------------------------------------------------

  global.UI = {
    apiFetch,
    getAdminKey,
    toast,
    openModal,
    confirmDialog,
    openDrawer,
    skeletonHTML,
    emptyState: _renderEmptyState,
    errorState,
    setPermissions,
    can,
    formatNumber,
    formatCurrency,
    formatDate,
    store,
  };
})(window);
