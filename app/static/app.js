const recentToasts = new Map();
let connectionFailureLatched = false;
let lastConnectionFailureToastAt = 0;
let authRedirectInProgress = false;
const observedServerStates = new Map();

function redirectToLogin() {
  if (authRedirectInProgress || ["/login", "/login/tfa"].includes(window.location.pathname)) return;
  authRedirectInProgress = true;
  window.location.replace("/login?expired=1");
}

function handleAuthenticationResponse(response) {
  let responsePath = "";
  try {
    responsePath = new URL(response.url, window.location.href).pathname;
  } catch {
    // Ignore malformed response URLs and still check the status code.
  }

  if (response.status === 401 || (response.redirected && ["/login", "/login/tfa"].includes(responsePath))) {
    redirectToLogin();
    return true;
  }

  if (response.redirected && responsePath === "/change-password") {
    window.location.replace(response.url);
    return true;
  }
  return false;
}

function setToastMessage(content, message, link) {
  const text = String(message || "");
  content.replaceChildren();
  const label = link?.label;
  const index = label ? text.indexOf(label) : -1;
  if (index < 0) {
    content.textContent = text;
    return;
  }

  content.append(document.createTextNode(text.slice(0, index)));
  const anchor = document.createElement("a");
  anchor.className = "toast-link";
  anchor.href = link.href;
  anchor.textContent = label;
  anchor.setAttribute("hx-get", link.href);
  anchor.setAttribute("hx-target", "#page-content");
  anchor.setAttribute("hx-push-url", "true");
  content.append(anchor, document.createTextNode(text.slice(index + label.length)));
  window.htmx?.process(anchor);
}

function setToastProgress(toast, progress) {
  let track = toast.querySelector(".toast-progress-track");
  if (!progress) {
    track?.remove();
    return;
  }
  if (!track) {
    track = document.createElement("div");
    track.className = "toast-progress-track";
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", "Plugin update progress");
    const bar = document.createElement("div");
    bar.className = "toast-progress-bar";
    track.append(bar);
    toast.append(track);
  }
  const total = Number(progress.total) || 0;
  const completed = Math.max(0, Number(progress.completed) || 0);
  const indeterminate = total <= 0;
  track.classList.toggle("is-indeterminate", indeterminate);
  if (indeterminate) {
    track.removeAttribute("aria-valuenow");
    track.removeAttribute("aria-valuemax");
  } else {
    track.setAttribute("aria-valuemax", String(total));
    track.setAttribute("aria-valuenow", String(Math.min(completed, total)));
  }
  track.firstElementChild.style.width = indeterminate
    ? "35%"
    : `${Math.min(100, (completed / total) * 100)}%`;
}

function setToastTimeout(toast, timeout) {
  window.clearTimeout(toast._toastTimeout);
  window.clearTimeout(toast._toastRemoveTimeout);
  toast._toastTimeout = null;
  toast._toastRemoveTimeout = null;
  if (timeout <= 0) return;
  toast._toastTimeout = window.setTimeout(() => {
    toast.classList.remove("visible");
    toast._toastRemoveTimeout = window.setTimeout(() => toast.remove(), 200);
  }, timeout);
}

function updateToast(toast, message, type = "info", options = {}) {
  if (!toast) return;
  toast.className = `toast toast-${type} visible`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const icon = toast.querySelector(":scope > i");
  if (icon) {
    icon.className = type === "success"
      ? "fa-solid fa-circle-check"
      : type === "error"
        ? "fa-solid fa-circle-exclamation"
        : type === "warning"
          ? "fa-solid fa-triangle-exclamation"
          : "fa-solid fa-circle-info";
  }
  setToastMessage(toast.querySelector(".toast-message"), message, options.link);
  setToastProgress(toast, options.progress);
  setToastTimeout(toast, options.timeout ?? 4500);
}

function showToast(message, type = "info", timeout = 4500, options = {}) {
  const text = String(message || "").trim();
  if (!text) return;
  const now = Date.now();
  if (!options.persistent) {
    if (now - (recentToasts.get(`${type}:${text}`) || 0) < 1200) return;
    recentToasts.set(`${type}:${text}`, now);
  }

  let region = document.getElementById("toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.className = "toast-region";
    region.setAttribute("aria-live", "polite");
    document.body.append(region);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const icon = document.createElement("i");
  icon.className = type === "success"
    ? "fa-solid fa-circle-check"
    : type === "error"
      ? "fa-solid fa-circle-exclamation"
      : type === "warning"
        ? "fa-solid fa-triangle-exclamation"
      : "fa-solid fa-circle-info";
  const content = document.createElement("span");
  content.className = "toast-message";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss notification");
  close.innerHTML = "&times;";
  close.addEventListener("click", () => toast.remove());
  toast.append(icon, content, close);
  setToastMessage(content, text, options.link);
  setToastProgress(toast, options.progress);
  close.addEventListener("click", () => {
    window.clearTimeout(toast._toastTimeout);
    window.clearTimeout(toast._toastRemoveTimeout);
  });
  region.append(toast);
  requestAnimationFrame(() => toast.classList.add("visible"));
  setToastTimeout(toast, options.persistent ? 0 : timeout);
  return toast;
}

function clearFieldError(field) {
  if (!field) return;
  field.classList.remove("field-invalid");
  field.removeAttribute("aria-invalid");
  if (field.nextElementSibling?.classList.contains("field-error-message")) {
    field.nextElementSibling.remove();
  }
}

function setFieldError(field, message) {
  if (!field) return false;
  clearFieldError(field);
  field.classList.add("field-invalid");
  field.setAttribute("aria-invalid", "true");
  const error = document.createElement("small");
  error.className = "field-error-message";
  error.textContent = message;
  field.insertAdjacentElement("afterend", error);
  return true;
}

function clearFormErrors(form) {
  if (!form) return;
  form.querySelectorAll(".field-invalid").forEach((field) => clearFieldError(field));
  form.querySelectorAll(".field-error-message").forEach((error) => error.remove());
}

function fieldForServerError(form, fieldName) {
  if (!form || !fieldName) return null;
  const normalized = String(fieldName).replaceAll("_", "-");
  return form.querySelector(`[name="${CSS.escape(fieldName)}"]`)
    || form.querySelector(`#property-${CSS.escape(normalized)}`)
    || form.querySelector(`#${CSS.escape(normalized)}`);
}

function showFormError(form, message, fieldName = null) {
  const field = fieldForServerError(form, fieldName);
  if (field) {
    setFieldError(field, message);
    field.focus();
  }
  showToast(message, "error");
}

document.addEventListener("invalid", (event) => {
  const field = event.target;
  if (!(field instanceof HTMLInputElement || field instanceof HTMLSelectElement || field instanceof HTMLTextAreaElement)) return;
  setFieldError(field, field.validationMessage || "Check this value");
  showToast("Please correct the highlighted fields.", "error");
}, true);

document.addEventListener("input", (event) => clearFieldError(event.target));
document.addEventListener("change", (event) => clearFieldError(event.target));

// Existing actions that still call alert now use the common non-blocking UI.
window.alert = (message) => showToast(message, "error");

const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  try {
    const response = await nativeFetch(...args);
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) return response;

    const request = args[0];
    const options = args[1] || {};
    const method = String(options.method || request?.method || "GET").toUpperCase();
    if (!new Set(["GET", "HEAD", "OPTIONS"]).has(method)) {
      response.clone().json().then((data) => {
        if (data?.suppress_toast) return;
        const message = data?.message || data?.error;
        if (!response.ok) {
          showToast(message || "The action could not be completed.", "error");
        } else {
          showToast(message || "Action completed successfully.", "success");
        }
      }).catch(() => {
        showToast(
          response.ok ? "Action completed successfully." : "The action could not be completed.",
          response.ok ? "success" : "error",
        );
      });
    }
    return response;
  } catch (error) {
    const now = Date.now();
    if (!connectionFailureLatched && now - lastConnectionFailureToastAt >= 60000) {
      connectionFailureLatched = true;
      lastConnectionFailureToastAt = now;
      showToast("Unable to connect to the server.", "error", 6500);
    } else connectionFailureLatched = true;
    throw error;
  }
};

window.addEventListener("online", () => {
  connectionFailureLatched = false;
});

document.addEventListener("htmx:afterRequest", (event) => {
  const detail = event.detail || {};
  let responsePath = "";
  try {
    responsePath = new URL(detail.xhr?.responseURL || "", window.location.href).pathname;
  } catch {
    // Ignore malformed response URLs.
  }
  if (detail.xhr?.status === 401 || ["/login", "/login/tfa"].includes(responsePath)) {
    redirectToLogin();
    return;
  }
  if (responsePath === "/change-password") {
    window.location.replace(detail.xhr.responseURL);
    return;
  }
  const verb = String(detail.requestConfig?.verb || "GET").toUpperCase();
  if (new Set(["GET", "HEAD", "OPTIONS"]).has(verb)) return;
  let data = {};
  try {
    data = JSON.parse(detail.xhr?.responseText || "{}");
  } catch {
    data = {};
  }
  if (detail.successful) {
    clearFormErrors(detail.elt?.closest?.("form"));
    showToast(data.message || "Action completed successfully.", "success");
  } else {
    const message = data.error || "The action could not be completed.";
    showFormError(detail.elt?.closest?.("form"), message, data.field);
  }
});

function updateActiveNavigation() {
  const path = window.location.pathname;
  const links = Array.from(document.querySelectorAll(".nav-link"));
  const linkPath = (link) => {
    try {
      return new URL(link.getAttribute("href"), window.location.href).pathname;
    } catch {
      return "";
    }
  };
  const matchesPath = (href) => href && (href === path || path.startsWith(`${href}/`));
  const activeHref = links
    .map(linkPath)
    .filter(matchesPath)
    .sort((first, second) => second.length - first.length)[0];

  links.forEach((link) => {
    const active = Boolean(activeHref && linkPath(link) === activeHref);
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });

  const moreLinkIsActive = Boolean(document.querySelector("#more-pages-menu .nav-link.active"));
  document.querySelectorAll(".more-pages-toggle").forEach((toggle) => {
    toggle.classList.toggle("active", moreLinkIsActive);
  });
}

function setMorePages(open) {
  const menu = document.getElementById("more-pages-menu");
  if (!menu) return;
  menu.hidden = !open;
  document.body.classList.toggle("mobile-more-open", open && window.innerWidth <= 900);
  document.querySelectorAll(".more-pages-toggle").forEach((toggle) => {
    toggle.setAttribute("aria-expanded", String(open));
  });
}

function toggleMorePages() {
  const menu = document.getElementById("more-pages-menu");
  setMorePages(Boolean(menu?.hidden));
}

function closeMorePages() {
  setMorePages(false);
}

function normalizeButtonClasses() {
  document.querySelectorAll("button").forEach((button) =>
    button.classList.add("button")
  );
}

normalizeButtonClasses();

async function reviewServerImport() {
  const form = document.getElementById("import-server-form");
  const directory = document.getElementById("import-server-directory");
  const backend = document.getElementById("import-process-backend");
  const modal = document.getElementById("import-review-modal");
  const title = document.getElementById("import-review-title");
  const summary = document.getElementById("import-review-summary");
  const report = document.getElementById("import-inspection-report");
  const confirm = document.getElementById("confirm-import-server");
  if (!form || !directory || !backend || !modal || !report || !confirm) return;
  if (!directory.reportValidity()) return;

  modal.hidden = false;
  title.textContent = "Review server import";
  summary.textContent = "Checking the server directory and management requirements…";
  report.textContent = "Inspecting server directory...";
  confirm.hidden = true;
  confirm.disabled = true;

  try {
    const response = await fetch("/api/web/servers/import/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        directory: directory.value,
        process_backend: backend.value,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to inspect path");

    if (data.name && !document.getElementById("import-server-name").value) {
      document.getElementById("import-server-name").value = data.name;
    }
    const factItems = [];
    if (data.jar_name) {
      factItems.push(`<span>JAR <strong>${escapeHtml(data.jar_name)}</strong></span>`);
    }
    if (Number.isInteger(data.port)) {
      factItems.push(`<span>Port <strong>${data.port}</strong></span>`);
    }
    if (data.owner) {
      factItems.push(`<span>Owner <strong>${escapeHtml(data.owner)}</strong></span>`);
    }
    if (data.checked_as) {
      factItems.push(`<span>Checked as <strong>${escapeHtml(data.checked_as)}</strong></span>`);
    }
    factItems.push(`<span>Service <strong>${escapeHtml(data.service?.unit || "None detected")}</strong></span>`);
    const facts = `<div class="import-server-facts">${factItems.join("")}</div>`;
    const errors = (data.errors || []).map((message) =>
      `<p class="import-check error"><i class="fa-solid fa-circle-xmark"></i> ${escapeHtml(message)}</p>`
    ).join("");
    const warnings = (data.warnings || []).map((message) =>
      `<p class="import-check warning"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(message)}</p>`
    ).join("");
    report.innerHTML = facts + errors + warnings;
    title.textContent = data.ready ? "Server found" : "Server cannot be imported";
    summary.textContent = data.ready
      ? "Craftarr found this server and can manage it. Review the details before importing."
      : "Craftarr found the following issues. Resolve them before trying again.";
    confirm.hidden = !data.ready;
    confirm.disabled = !data.ready;
  } catch (error) {
    title.textContent = "Unable to inspect server";
    summary.textContent = "The server directory could not be checked.";
    report.textContent = error.message;
  }
}

function closeImportReviewModal() {
  document.getElementById("import-review-modal").hidden = true;
}

function confirmServerImport() {
  const form = document.getElementById("import-server-form");
  const confirm = document.getElementById("confirm-import-server");
  if (!form || !confirm || confirm.disabled) return;
  confirm.disabled = true;
  confirm.textContent = "Importing...";
  form.requestSubmit();
}

let serverZipImportLocked = false;
let serverZipImportInProgress = false;
let serverZipImportPageUrl = "";
let serverZipImportHistoryState = null;
let serverZipImportPreviousFocus = null;
let serverZipImportShellWasInert = false;

document.body.addEventListener("htmx:beforeRequest", (event) => {
  if (serverZipImportLocked) event.preventDefault();
});

window.addEventListener("popstate", (event) => {
  if (!serverZipImportLocked) return;
  event.stopImmediatePropagation();
  if (serverZipImportPageUrl) {
    window.history.pushState(serverZipImportHistoryState, "", serverZipImportPageUrl);
  }
}, true);

document.addEventListener("keydown", (event) => {
  if (!serverZipImportLocked || !["Escape", "Tab"].includes(event.key)) return;
  event.preventDefault();
  document.getElementById("file-operation-progress-dialog")?.focus({preventScroll: true});
});

function lockServerZipImportNavigation() {
  serverZipImportLocked = true;
  serverZipImportInProgress = true;
  serverZipImportPageUrl = window.location.href;
  serverZipImportHistoryState = window.history.state;
  serverZipImportPreviousFocus = document.activeElement;
  const shell = document.querySelector(".app-shell");
  serverZipImportShellWasInert = Boolean(shell?.inert);
  if (shell) shell.inert = true;
  document.getElementById("file-operation-progress-dialog")?.focus({preventScroll: true});
}

function unlockServerZipImportNavigation() {
  serverZipImportLocked = false;
  serverZipImportInProgress = false;
  const shell = document.querySelector(".app-shell");
  if (shell) shell.inert = serverZipImportShellWasInert;
  serverZipImportShellWasInert = false;
  serverZipImportPageUrl = "";
  serverZipImportHistoryState = null;
  const previousFocus = serverZipImportPreviousFocus;
  serverZipImportPreviousFocus = null;
  if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
}

async function importServerZip(event) {
  event.preventDefault();
  const form = document.getElementById("import-server-zip-form");
  const button = document.getElementById("import-server-zip-submit");
  const status = document.getElementById("import-server-zip-status");
  const file = document.getElementById("import-server-archive")?.files?.[0];
  if (!form || !button || !status || !file || !form.reportValidity()) return;
  const maxUploadBytes = Number(form.dataset.maxUploadBytes);
  if (Number.isFinite(maxUploadBytes) && maxUploadBytes > 0 && file.size > maxUploadBytes) {
    const message = `This ZIP is ${formatUploadBytes(file.size)}. The configured limit is ${formatUploadBytes(maxUploadBytes)}; no upload was started.`;
    status.textContent = message;
    showToast(message, "error", 7000);
    return;
  }

  const formData = new FormData(form);
  let lastUploadPercent = 0;
  let lastUploadDetail = `0 B of ${formatUploadBytes(file.size)} uploaded`;
  button.disabled = true;
  status.textContent = `Uploading ${file.name}…`;
  showFileOperationProgress(
    "Importing server ZIP",
    `Uploading ${file.name}. Keep this tab open while it finishes.`,
    0,
    `0 B of ${formatUploadBytes(file.size)} uploaded`,
  );
  lockServerZipImportNavigation();

  try {
    const response = await uploadFileWithProgress(
      "/api/web/servers/import/archive",
      formData,
      (loaded, total) => {
        const fraction = total > 0 ? Math.min(1, loaded / total) : 0;
        const uploadComplete = fraction >= 1;
        const percent = uploadComplete ? 100 : Math.min(99, Math.floor(fraction * 100));
        const uploaded = Math.min(file.size, Math.floor(file.size * fraction));
        const uploadedText = `${formatUploadBytes(uploaded)} of ${formatUploadBytes(file.size)} uploaded`;
        lastUploadPercent = percent;
        lastUploadDetail = uploadComplete ? `${formatUploadBytes(file.size)} uploaded` : uploadedText;
        if (uploadComplete) {
          showFileOperationProgress(
            "Checking server ZIP",
            "The upload is complete. Validating the archive and importing the server… Keep this tab open.",
            100,
            `${formatUploadBytes(file.size)} uploaded`,
          );
          status.textContent = "Upload complete. Checking the ZIP contents and importing the server…";
        } else {
          showFileOperationProgress(
            "Importing server ZIP",
            `Uploading ${file.name}. Keep this tab open while it finishes.`,
            percent,
            uploadedText,
          );
        }
      },
    );
    let data = {};
    try {
      const parsed = JSON.parse(response.responseText || "{}");
      if (parsed && typeof parsed === "object") data = parsed;
    } catch {
      // Use the status code when the server response isn't JSON.
    }
    if (response.status < 200 || response.status >= 300) {
      const message = response.status === 413
        ? "The ZIP archive is larger than this installation's upload limit."
        : data.error || "Unable to import this ZIP archive.";
      throw new Error(message);
    }
    showFileOperationProgress(
      "Checking server ZIP",
      "The upload is complete. Validating the archive and importing the server… Keep this tab open.",
      100,
      `${formatUploadBytes(file.size)} uploaded`,
    );
    status.textContent = "Upload complete. Checking the ZIP contents and importing the server…";
    const importMessage = data.settings_imported
      ? "Server and embedded Craftarr settings imported. Opening the server…"
      : "Server imported. Opening the server…";
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    status.textContent = [importMessage, ...warnings].join(" ");
    if (warnings.length) {
      showFileOperationProgress("Server imported", [importMessage, ...warnings].join(" "), 100, "Opening the server…");
      window.setTimeout(() => {
        hideFileOperationProgress();
        unlockServerZipImportNavigation();
        window.location.assign(data.redirect_url);
      }, 2200);
    } else {
      hideFileOperationProgress();
      unlockServerZipImportNavigation();
      window.location.assign(data.redirect_url);
    }
  } catch (error) {
    const message = error.message || "Unable to import this ZIP archive.";
    status.textContent = message;
    button.disabled = false;
    serverZipImportInProgress = false;
    if (/session expired/i.test(message)) {
      hideFileOperationProgress();
      unlockServerZipImportNavigation();
      return;
    }
    const failureDetail = message.toLowerCase().includes("larger than the configured upload limit")
      ? `${formatUploadBytes(file.size)} transferred. Configured limit: ${formatUploadBytes(maxUploadBytes)}.`
      : lastUploadDetail;
    showFileOperationProgress("ZIP import failed", message, lastUploadPercent, failureDetail, true);
    document.getElementById("file-operation-progress-close")?.focus({preventScroll: true});
  }
}

function formatUploadBytes(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size) || size <= 0) return "0 B";

  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${unitIndex === 0 ? Math.floor(value) : value.toFixed(1)} ${units[unitIndex]}`;
}

function suggestServerZipName(input) {
  const nameInput = document.getElementById("import-server-zip-name");
  const filename = input?.files?.[0]?.name;
  if (!nameInput || !filename || nameInput.value.trim()) return;
  nameInput.value = filename
    .replace(/\.zip$/i, "")
    .replace(/-server$/i, "")
    .replace(/[-_]+/g, " ")
    .trim();
}

let pendingServerSettingsFile = null;

async function handleServerSettingsFile(input) {
  const file = input?.files?.[0];
  const status = document.getElementById("server-settings-transfer-status");
  if (!file) return;
  if (file.size > 1024 * 1024) {
    if (status) status.textContent = "The settings export file is too large.";
    input.value = "";
    return;
  }

  try {
    const documentData = JSON.parse(await file.text());
    if (
      documentData?.format !== "craftarr-server-settings" ||
      documentData?.version !== 1 ||
      !documentData.settings ||
      typeof documentData.settings !== "object" ||
      Array.isArray(documentData.settings) ||
      !Object.keys(documentData.settings).length
    ) {
      throw new Error("Choose a supported Craftarr server settings export.");
    }
    pendingServerSettingsFile = file;
    const summary = document.getElementById("server-settings-import-summary");
    if (summary) {
      summary.textContent = `${file.name} will replace this server’s panel-managed startup settings.`;
    }
    const modal = document.getElementById("server-settings-import-modal");
    if (modal) modal.hidden = false;
    if (status) status.textContent = "Review the settings import to continue.";
  } catch (error) {
    pendingServerSettingsFile = null;
    if (status) status.textContent = error.message || "The settings export could not be read.";
    input.value = "";
  }
}

function closeServerSettingsImport() {
  const modal = document.getElementById("server-settings-import-modal");
  const input = document.getElementById("server-settings-import-file");
  if (modal) modal.hidden = true;
  if (input) input.value = "";
  pendingServerSettingsFile = null;
}

async function confirmServerSettingsImport() {
  const page = document.querySelector(".properties-page");
  const button = document.getElementById("confirm-server-settings-import");
  const status = document.getElementById("server-settings-transfer-status");
  if (!page || !button || !pendingServerSettingsFile || button.disabled) return;

  button.disabled = true;
  button.textContent = "Importing…";
  if (status) status.textContent = "Importing settings…";
  try {
    const formData = new FormData();
    formData.append("file", pendingServerSettingsFile, pendingServerSettingsFile.name);
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/settings/import`,
      { method: "POST", body: formData },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to import settings.");

    closeServerSettingsImport();
    await updatePropertiesPage();
    if (status) status.textContent = data.message;

    const restartAlert = document.getElementById("properties-restart-alert");
    if (restartAlert && data.restart_required) {
      restartAlert.hidden = false;
      const message = document.getElementById("properties-pending-message");
      const label = document.getElementById("properties-pending-label");
      if (message) message.textContent = "Imported startup settings are pending. Restart the server to apply them.";
      if (label) label.textContent = "Restart required";
    } else if (restartAlert) {
      restartAlert.hidden = true;
    }
    showToast(data.message, data.restart_required ? "warning" : "success");
  } catch (error) {
    const message = error.message || "Unable to import settings.";
    const summary = document.getElementById("server-settings-import-summary");
    if (summary) summary.textContent = message;
    if (status) status.textContent = message;
  } finally {
    button.disabled = false;
    button.textContent = "Import settings";
  }
}

function toggleServerMenu() {
  const menu = document.getElementById("server-menu");
  const toggle = document.getElementById("server-selector-toggle");
  if (!menu || !toggle) return;
  const open = !menu.classList.contains("open");
  menu.classList.toggle("open", open);
  toggle.setAttribute("aria-expanded", String(open));
}

function closeServerMenu() {
  document.getElementById("server-menu")?.classList.remove("open");
  document.getElementById("server-selector-toggle")?.setAttribute("aria-expanded", "false");
}

function setNotificationsOpen(open) {
  const panel = document.getElementById("notifications-panel");
  const toggle = document.getElementById("notifications-toggle");
  if (!panel || !toggle) return;
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  if (open) {
    closeAccountMenu();
    refreshNotifications();
  }
}

function toggleNotifications() {
  const panel = document.getElementById("notifications-panel");
  setNotificationsOpen(Boolean(panel?.hidden));
}

function closeNotifications() {
  setNotificationsOpen(false);
}

const notificationCenter = document.querySelector(".notification-center");
const notificationUserId = notificationCenter?.dataset.userId || "anonymous";
const notificationReadKey = `craftarr.notification-read.v1.${notificationUserId}`;
const notificationEventsKey = `craftarr.notification-events.v1.${notificationUserId}`;
let notificationReadIds = new Set();
let notificationEvents = [];
let updateNotifications = [];
let previousUpdateNotificationIds = null;

try {
  notificationReadIds = new Set(JSON.parse(localStorage.getItem(notificationReadKey) || "[]"));
} catch {
  notificationReadIds = new Set();
}
try {
  const storedEvents = JSON.parse(localStorage.getItem(notificationEventsKey) || "[]");
  notificationEvents = Array.isArray(storedEvents) ? storedEvents.filter((item) => item && item.id).slice(0, 50) : [];
} catch {
  notificationEvents = [];
}

function allNotifications() {
  return [...notificationEvents, ...updateNotifications]
    .sort((first, second) => String(second.checked_at || "").localeCompare(String(first.checked_at || "")));
}

function saveNotificationReadIds() {
  try {
    const ids = [...notificationReadIds].slice(-200);
    notificationReadIds = new Set(ids);
    localStorage.setItem(notificationReadKey, JSON.stringify(ids));
  } catch {
    // Notifications remain usable for this page even when storage is unavailable.
  }
}

function updateNotificationBadge() {
  const unread = allNotifications().filter((item) => !notificationReadIds.has(item.id)).length;
  const count = document.getElementById("notifications-count");
  const toggle = document.getElementById("notifications-toggle");
  const markRead = document.getElementById("notifications-mark-read");
  if (count) {
    count.hidden = unread === 0;
    count.textContent = unread > 9 ? "9+" : String(unread);
  }
  if (toggle) toggle.setAttribute("aria-label", unread ? `Notifications, ${unread} unread` : "Notifications");
  if (markRead) markRead.hidden = unread === 0;
}

function renderNotifications() {
  const list = document.getElementById("notification-list");
  const empty = document.getElementById("notification-empty");
  if (!list || !empty) return;

  const items = allNotifications();
  empty.hidden = items.length > 0;
  list.hidden = items.length === 0;
  list.innerHTML = items.map((item) => {
    const href = typeof item.url === "string" && item.url.startsWith("/") && !item.url.startsWith("//")
      ? item.url
      : "/dashboard";
    const unreadClass = notificationReadIds.has(item.id) ? "" : " is-unread";
    const icon = item.kind === "server-state"
      ? "fa-server"
      : String(item.kind).startsWith("paper-")
        ? "fa-cubes-stacked"
        : String(item.kind).startsWith("backup-")
          ? "fa-box-archive"
          : "fa-puzzle-piece";
    const time = item.checked_at && !Number.isNaN(Date.parse(item.checked_at))
      ? new Date(item.checked_at).toLocaleString()
      : "";
    return `<a class="notification-item${unreadClass}" href="${escapeHtml(href)}" role="listitem" data-notification-id="${escapeHtml(item.id)}">
      <span class="notification-item-icon" aria-hidden="true"><i class="fa-solid ${icon}"></i></span>
      <span class="notification-item-copy"><strong>${escapeHtml(item.title || "Update available")}</strong><small>${escapeHtml(item.message || "")}</small>${time ? `<time datetime="${escapeHtml(item.checked_at)}">${escapeHtml(time)}</time>` : ""}</span>
      <span class="notification-unread-dot" aria-hidden="true"></span>
    </a>`;
  }).join("");
  updateNotificationBadge();
}

function markNotificationRead(id) {
  if (!id) return;
  notificationReadIds.add(id);
  saveNotificationReadIds();
  document.querySelectorAll("[data-notification-id]").forEach((item) => {
    if (item.dataset.notificationId === id) item.classList.remove("is-unread");
  });
  updateNotificationBadge();
}

function markAllNotificationsRead() {
  allNotifications().forEach((item) => notificationReadIds.add(item.id));
  saveNotificationReadIds();
  renderNotifications();
}

function recordInAppNotification(item) {
  if (!item?.id || notificationEvents.some((existing) => existing.id === item.id)) return;
  notificationEvents.unshift(item);
  notificationEvents = notificationEvents.slice(0, 50);
  try {
    localStorage.setItem(notificationEventsKey, JSON.stringify(notificationEvents));
  } catch {
    // Keep the new event for the current page if storage is unavailable.
  }
  renderNotifications();
}

async function refreshNotifications(announceNew = true) {
  if (!document.getElementById("notification-list")) return;
  try {
    const response = await fetch("/api/web/notifications", { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    const next = Array.isArray(data.notifications) ? data.notifications : [];
    const currentIds = new Set(next.map((item) => item.id));
    if (announceNew && previousUpdateNotificationIds) {
      const added = next.filter((item) => !previousUpdateNotificationIds.has(item.id) && !notificationReadIds.has(item.id));
      const updates = added.filter((item) => ["plugin-update", "paper-update"].includes(item.kind));
      const backups = added.filter((item) => String(item.kind).startsWith("backup-"));
      if (updates.length) {
        showToast(updates.length === 1 ? updates[0].title : `${updates.length} new updates are available.`, "info", 6500);
      }
      if (backups.length) {
        const interrupted = backups.some((item) => item.kind !== "backup-complete");
        showToast(
          backups.length === 1 ? backups[0].title : `${backups.length} backup jobs finished.`,
          interrupted ? "warning" : "success",
          6500,
        );
      }
    }
    previousUpdateNotificationIds = currentIds;
    updateNotifications = next;
    renderNotifications();
  } catch {
    // The shared connection notice handles an unavailable notification poll.
  }
}

document.getElementById("notification-list")?.addEventListener("click", (event) => {
  const item = event.target.closest("[data-notification-id]");
  if (item) {
    markNotificationRead(item.dataset.notificationId);
    closeNotifications();
  }
});

renderNotifications();
refreshNotifications();
setInterval(refreshNotifications, 30000);

function setAccountMenuOpen(open) {
  const panel = document.getElementById("account-menu-panel");
  const toggle = document.getElementById("account-menu-toggle");
  if (!panel || !toggle) return;
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  if (open) closeNotifications();
}

function toggleAccountMenu() {
  const panel = document.getElementById("account-menu-panel");
  setAccountMenuOpen(Boolean(panel?.hidden));
}

function closeAccountMenu() {
  setAccountMenuOpen(false);
}

document.addEventListener(
  "click",
  function (event) {
    if (event.target.closest("#server-menu a")) {
      closeServerMenu();
    }
    if (event.target.closest("#more-pages-menu a")) closeMorePages();

    const dropdown = document.querySelector(
      ".server-selector",
    );

    if (
      dropdown &&
      !dropdown.contains(
        event.target,
      )
    ) {
      closeServerMenu();
    }

    const accountMenu = document.querySelector(".account-menu");
    if (accountMenu && !accountMenu.contains(event.target)) closeAccountMenu();

    const notifications = document.querySelector(".notification-center");
    if (notifications && !notifications.contains(event.target)) closeNotifications();

    const moreMenu = document.getElementById("more-pages-menu");
    if (
      moreMenu &&
      !moreMenu.hidden &&
      !moreMenu.contains(event.target) &&
      !event.target.closest(".more-pages-toggle")
    ) closeMorePages();

    document.querySelectorAll(".plugin-more-actions[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  },
);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (serverZipImportLocked) return;
    const fileImagePreview = document.getElementById("file-image-preview-modal");
    if (fileImagePreview && !fileImagePreview.hidden) {
      event.preventDefault();
      closeFileImagePreview();
      return;
    }
    const pluginUpdateModal = document.getElementById("plugin-update-modal");
    if (pluginUpdateModal && !pluginUpdateModal.hidden) {
      event.preventDefault();
      closePluginUpdateModal();
      return;
    }
    const pluginMonitoringModal = document.getElementById("plugin-monitoring-modal");
    if (pluginMonitoringModal && !pluginMonitoringModal.hidden) {
      event.preventDefault();
      closePluginMonitoring();
      return;
    }
    const accountOpen = !document.getElementById("account-menu-panel")?.hidden;
    const notificationsOpen = !document.getElementById("notifications-panel")?.hidden;
    closeMorePages();
    closeServerMenu();
    closeAccountMenu();
    closeNotifications();
    if (accountOpen) document.getElementById("account-menu-toggle")?.focus();
    else if (notificationsOpen) document.getElementById("notifications-toggle")?.focus();
  }
});

window.addEventListener("resize", closeMorePages);

async function updateSystemStats() {
  const cpuValue = document.getElementById("cpu-value");
  const memoryValue = document.getElementById("memory-value");
  const storageValue = document.getElementById("storage-value");

  if (!cpuValue || !memoryValue || !storageValue) {
    return;
  }

  try {
    const response = await fetch("/api/system/stats");

    if (!response.ok) {
      return;
    }

    const stats = await response.json();

    document.getElementById("cpu-value").textContent = stats.cpu.percent + "%";

    document.getElementById("cpu-bar").style.width = stats.cpu.percent + "%";

    document.getElementById("cpu-detail").textContent = stats.cpu.cores +
      " logical cores";

    document.getElementById("memory-value").textContent = stats.memory.percent +
      "%";

    document.getElementById("memory-bar").style.width = stats.memory.percent +
      "%";

    document.getElementById("memory-detail").textContent = stats.memory.used +
      " GB / " +
      stats.memory.total + " GB";

    document.getElementById("storage-value").textContent =
      stats.storage.percent + "%";

    document.getElementById("storage-bar").style.width = stats.storage.percent +
      "%";

    document.getElementById("storage-detail").textContent = stats.storage.used +
      " GB / " +
      stats.storage.total + " GB";

    const minecraft = stats.minecraft || { instances: [] };
    setText(
      "minecraft-running",
      `${minecraft.running} / ${minecraft.installed}`,
    );
    setText(
      "minecraft-installed",
      `${minecraft.running} running · ${minecraft.installed} installed`,
    );
    setText("minecraft-players", minecraft.players_online);
    const javaList = document.getElementById("system-java-list");
    if (javaList) {
      javaList.innerHTML = (stats.java_runtimes || []).length
        ? stats.java_runtimes.map((runtime) => `
            <div class="system-instance-row"><div><strong>Java ${Number(runtime.major)}</strong>
            <small>${escapeHtml(runtime.name)} · ${escapeHtml(runtime.path)}</small></div></div>`).join("")
        : '<div class="empty-message">No Java runtimes detected.</div>';
    }
    const instances = document.getElementById("system-instance-list");
    if (instances) {
      instances.innerHTML = minecraft.instances.length
        ? minecraft.instances.map((server) => `
                    <div class="system-instance-row">
                      <div><span class="server-status-dot ${
          statusClass(server.state || (server.running ? "running" : "stopped"))
        }" data-server-id="${Number(server.id)}" aria-hidden="true"></span><strong>${
          escapeHtml(server.name)
        }</strong>
                      <small class="system-instance-state">${
          escapeHtml(serverStateLabel(server.state || (server.running ? "running" : "stopped"), server.console_available))
        }</small><small>Paper ${
          escapeHtml(server.version || "unknown")
        } · Java ${server.java || "unknown"} · ${server.players} online</small></div>
                      <button class="server-control-button ${
          server.state === "stopped" || (!server.state && !server.running) ? "start" : "stop"
        }" aria-label="${server.state === "stopped" || (!server.state && !server.running) ? "Start" : "Stop"} ${escapeHtml(server.name)}" title="${server.state === "stopped" || (!server.state && !server.running) ? "Start" : "Stop"} server" ${
          server.state === "stopped" || (!server.state && !server.running)
            ? ""
            : ((server.state || (server.running ? "running" : "stopped")) === "running" && server.console_available !== false ? "" : "disabled")
        } onclick="systemServerAction(${Number(server.id)}, '${
          server.state === "stopped" || (!server.state && !server.running) ? "start" : "stop"
        }', this)"><i class="fa-solid fa-${server.state === "stopped" || (!server.state && !server.running) ? "play" : "stop"}" aria-hidden="true"></i></button>
                    </div>`).join("")
        : '<div class="empty-message">No accessible Minecraft instances.</div>';
    }
  } catch (error) {
    console.error("Stats error:", error);
  }
}

updateSystemStats();

setInterval(
  updateSystemStats,
  3000,
);

document.body.addEventListener(
  "htmx:afterSwap",
  function () {
    const serverPage = document.querySelector("#page-content > [data-server-id]");
    const topbar = document.querySelector(".topbar[data-server-id]");
    const pageServerId = serverPage?.dataset.serverId || "";
    const topbarServerId = topbar?.dataset.serverId || "";
    if (serverPage && topbar && pageServerId !== topbarServerId) {
      window.location.reload();
      return;
    }
    if (document.querySelector(".console-page")) {
      lastConsoleSignature = "";
    }

    updateActiveNavigation();
    updateDocumentTitle();

    updateSystemStats();
    updateConsolePage();
    updateServerStatus();
    updateServerDots();
    updateOverviewLogs();
    updatePlayersPage();
    updateOverviewPlayers();
    updatePluginsPage();
    updateOverviewPlugins();
    updateBackupsPage();
    updatePropertiesPage();
    loadAdvancedProperties();
    updateSMTPSettings();
    loadOffsiteBackupSettings();
    updateTFASettings();
    updateBackupJobs();
    updateServerProcessStats();
    updateConsoleVersionStatus();
    loadServerMetrics();
    loadServerSchedules();
    loadPaperVersionStatus();
    normalizeButtonClasses();
  },
);

function statusClass(state) {
  return ({
    running: "is-online",
    stopped: "is-offline",
    starting: "is-starting",
    stopping: "is-stopping",
  })[state] || "is-unknown";
}

function serverStateLabel(state, consoleAvailable = true) {
  if (state === "starting") return "Starting…";
  if (state === "stopping") return "Shutting down…";
  if (state === "stopped") return "Offline";
  if (state === "running" && consoleAvailable === false) return "Online · console unavailable";
  if (state === "running") return "Online";
  return "Status unknown";
}

function normalizeServerState(data) {
  if (["running", "stopped", "starting", "stopping"].includes(data?.state)) {
    return data.state;
  }
  if (typeof data?.running === "boolean") return data.running ? "running" : "stopped";
  return "unknown";
}

function setOverviewServerState(state, consoleAvailable = true) {
  const pill = document.getElementById("overview-server-state");
  const label = document.getElementById("overview-server-state-text");
  if (!pill || !label) return;
  pill.classList.remove("is-online", "is-offline", "is-starting", "is-stopping", "is-unknown");
  pill.classList.add(statusClass(state));
  label.textContent = serverStateLabel(state, consoleAvailable);
}

async function updateServerStatus() {
  const startButton = document.getElementById("dashboard-start");
  const restartButton = document.getElementById("dashboard-restart");
  const stopButton = document.getElementById("dashboard-stop");
  const topbar = document.querySelector(".topbar");
  const page = document.querySelector("#page-content > [data-server-id]");
  const serverId = topbar?.dataset.serverId || page?.dataset.serverId;
  if (!serverId) {
    return;
  }

  const setControls = (state, consoleAvailable) => {
    if (startButton) startButton.disabled = state !== "stopped";
    if (stopButton) stopButton.disabled = !consoleAvailable || state !== "running";
    if (restartButton) restartButton.disabled = !consoleAvailable || state !== "running";

    if (startButton) {
      startButton.title = state === "starting"
        ? "Server is starting"
        : state === "stopping"
          ? "Server is shutting down"
          : "Start server";
    }
    if (stopButton) {
      stopButton.title = state === "stopping"
        ? "Server is shutting down"
        : state === "starting"
          ? "Wait for the server to finish starting"
          : !consoleAvailable
            ? "Server console unavailable"
            : "Stop server";
    }
    if (restartButton) {
      restartButton.title = state === "starting"
        ? "Wait for the server to finish starting"
        : state === "stopping"
          ? "Server is shutting down"
          : !consoleAvailable
            ? "Server console unavailable"
            : "Restart server";
    }
  };

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/status`,
    );

    if (!response.ok) {
      throw new Error(
        "Status request failed",
      );
    }

    const data = await response.json();

    const state = normalizeServerState(data);
    const consoleAvailable = data.console_available !== false;
    const confirmedRunning = state === "running" ? true : state === "stopped" ? false : null;
    if (confirmedRunning !== null) {
      const previousState = observedServerStates.get(serverId);
      if (previousState !== undefined && previousState !== confirmedRunning) {
        const serverName = document.querySelector(".sidebar-server-copy strong")?.textContent?.trim() || "Server";
        const now = new Date().toISOString();
        const stateLabel = confirmedRunning ? "started" : "stopped";
        recordInAppNotification({
          id: `server-state:${serverId}:${confirmedRunning ? "running" : "stopped"}:${Date.now()}`,
          kind: "server-state",
          title: `${serverName} ${stateLabel}`,
          message: `${serverName} is now ${confirmedRunning ? "running" : "stopped"}.`,
          url: `/servers/${encodeURIComponent(serverId)}`,
          checked_at: now,
        });
      }
      observedServerStates.set(serverId, confirmedRunning);
    }

    const pluginsPage = document.querySelector(".plugins-page");
    if (pluginsPage?.dataset.serverId === serverId) {
      pluginServerRunning = state === "running";
      showPluginRestartAlert();
    }

    setControls(state, consoleAvailable);
    setOverviewServerState(state, data.console_available);
  } catch (error) {
    setControls("unknown", false);
    setOverviewServerState("unknown", false);
  }
}

updateServerStatus();

setInterval(
  updateServerStatus,
  3000,
);

async function updateServerDots() {
  const dots = [...document.querySelectorAll(
    ".server-choice .server-status-dot, .sidebar-server-status .server-status-dot, .server-card-status .server-status-dot, .system-instance-row .server-status-dot",
  )];
  const serverIds = [...new Set(dots.map((dot) => dot.dataset.serverId).filter(Boolean))];

  for (const serverId of serverIds) {
    try {
      const response = await fetch(
        `/api/web/servers/${serverId}/status`,
      );
      if (!response.ok) continue;
      const data = await response.json();
      const state = normalizeServerState(data);
      if (state === "unknown") continue;
      const classes = ["is-online", "is-offline", "is-starting", "is-stopping", "is-unknown"];

      dots
        .filter((dot) => dot.dataset.serverId === serverId)
        .forEach((dot) => {
          dot.classList.remove(...classes, "running");
          dot.classList.add(statusClass(state));
          dot.classList.toggle("running", state === "running");
        });

      document.querySelectorAll(
        `.server-card-status[data-server-id="${serverId}"]`,
      ).forEach((status) => {
        status.classList.remove(...classes);
        status.classList.add(statusClass(state));
        const label = status.querySelector(".server-card-status-text");
        if (label) label.textContent = serverStateLabel(state, data.console_available);
      });

      document.querySelectorAll(
        `.system-instance-row [data-server-id="${serverId}"].server-status-dot`,
      ).forEach((dot) => {
        const row = dot.closest(".system-instance-row");
        const label = row?.querySelector(".system-instance-state");
        if (label) label.textContent = serverStateLabel(state, data.console_available);
        const button = row?.querySelector(".server-control-button");
        if (button) {
          const action = button.classList.contains("start") ? "start" : "stop";
          button.disabled = action === "start"
            ? state !== "stopped"
            : data.console_available === false || state !== "running";
        }
      });

      document.querySelectorAll(
        `.server-status-text[data-server-id="${serverId}"]`,
      ).forEach((label) => {
        label.textContent = serverStateLabel(state, data.console_available);
      });
    } catch {
      // Keep the last confirmed state when one status poll fails.
    }
  }
}

updateServerDots();

setInterval(
  updateServerDots,
  3000,
);

let consoleLines = [];
const consoleLevels = ["INFO", "WARN", "ERROR", "DEBUG"];
let consoleFilters = new Set();
let lastConsoleSignature = "";
let playerData = null;
let playerFilter = "all";
let playerSort = "name-asc";
let playerPage = 1;
const PLAYER_PAGE_SIZE = 25;
let pluginData = [];
let pluginMonitoringPreviousFocus = null;
let pluginPendingRemoval = null;
let pluginRemovalInProgress = false;
let pluginPendingReplacement = null;
let pluginPendingUpdates = [];
let pluginUpdatePreviousFocus = null;
let pluginUpdateInProgress = false;
let pluginRestartRequired = false;
let pluginServerRunning = false;
let pluginDuplicateGroups = [];
let duplicatePluginFilenames = new Set();
let duplicatePluginDetails = new Map();
let latestInstalledPluginFilename = null;
let sessionAcknowledgedPluginDuplicates = new Set();
const acknowledgedPluginDuplicatesKey = "craftarr.acknowledgedPluginDuplicates";

let pendingFilePath = null;
let fileImagePreviewPreviousFocus = null;

function currentFilesPage() {
  return document.querySelector(
    ".files-page",
  );
}

function reloadFilesPage() {
  const page = currentFilesPage();

  if (!page) {
    return;
  }

  const serverId = page.dataset.serverId;

  const path = page.dataset.currentPath || "";

  htmx.ajax(
    "GET",
    `/servers/${serverId}/files?path=${encodeURIComponent(path)}`,
    {
      target: "#page-content",

      swap: "innerHTML",
    },
  );
}

function openNewFolderModal() {
  document.getElementById(
    "new-folder-modal",
  ).hidden = false;

  const input = document.getElementById(
    "new-folder-name",
  );

  input.value = "";
  input.focus();
}

function openCreateFileModal() {
  const modal = document.getElementById("create-file-modal");
  const input = document.getElementById("create-file-name");
  const error = document.getElementById("create-file-error");
  if (!modal || !input) return;
  input.value = "";
  if (error) {
    error.textContent = "";
    error.hidden = true;
  }
  modal.hidden = false;
  input.focus();
}

function closeCreateFileModal() {
  const modal = document.getElementById("create-file-modal");
  if (modal) modal.hidden = true;
}

async function createNewFile(event) {
  event.preventDefault();
  const page = currentFilesPage();
  const input = document.getElementById("create-file-name");
  const error = document.getElementById("create-file-error");
  const button = document.getElementById("create-file-submit");
  if (!page || !input || !button || !input.reportValidity()) return;
  button.disabled = true;
  if (error) {
    error.textContent = "";
    error.hidden = true;
  }
  try {
    const response = await fetch(`/api/web/servers/${page.dataset.serverId}/files/create`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: page.dataset.currentPath || "", name: input.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to create the file.");
    closeCreateFileModal();
    showToast("File created. Open it to add content.", "success");
    reloadFilesPage();
  } catch (createError) {
    if (error) {
      error.textContent = createError.message;
      error.hidden = false;
    } else {
      showToast(createError.message, "error");
    }
  } finally {
    button.disabled = false;
  }
}

function closeNewFolderModal() {
  document.getElementById(
    "new-folder-modal",
  ).hidden = true;
}

async function createNewFolder() {
  const page = currentFilesPage();

  const input = document.getElementById(
    "new-folder-name",
  );

  const name = input.value.trim();

  if (!name) {
    return;
  }

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/files/mkdir`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        path: page.dataset.currentPath || "",
        name,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to create folder",
    );

    return;
  }

  closeNewFolderModal();

  reloadFilesPage();
}

let renameFilePath = null;

function openRenameModal(
  path,
  name,
) {
  renameFilePath = path;

  const modal = document.getElementById(
    "rename-file-modal",
  );

  const input = document.getElementById(
    "rename-file-name",
  );

  input.value = name;

  modal.hidden = false;

  input.focus();
  input.select();
}

function closeRenameModal() {
  document.getElementById(
    "rename-file-modal",
  ).hidden = true;

  renameFilePath = null;
}

async function confirmRenameFile() {
  const page = currentFilesPage();

  const name = document.getElementById(
    "rename-file-name",
  ).value.trim();

  if (
    !renameFilePath ||
    !name
  ) {
    return;
  }

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/files/rename`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        path: renameFilePath,

        name,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to rename",
    );

    return;
  }

  closeRenameModal();

  reloadFilesPage();
}

function openDeleteFileModal(
  path,
  name,
) {
  pendingFilePath = path;

  document.getElementById(
    "delete-file-name",
  ).textContent = name;

  document.getElementById(
    "delete-file-modal",
  ).hidden = false;
}

function closeDeleteFileModal() {
  document.getElementById(
    "delete-file-modal",
  ).hidden = true;

  pendingFilePath = null;
}

async function confirmDeleteFile() {
  const page = currentFilesPage();

  if (!pendingFilePath) {
    return;
  }

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/files/delete`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        path: pendingFilePath,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to delete",
    );

    return;
  }

  closeDeleteFileModal();

  reloadFilesPage();
}

async function updatePluginsPage(forceDuplicatePrompt = false) {
  const page = document.querySelector(
    ".plugins-page",
  );

  if (!page) {
    return;
  }

  const serverId = page.dataset.serverId;

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/plugins`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    const currentPage = document.querySelector(".plugins-page");
    if (!page.isConnected || currentPage?.dataset.serverId !== serverId) {
      return;
    }

    pluginData = data.plugins || [];
    pluginDuplicateGroups = data.duplicates || [];
    duplicatePluginFilenames = new Set(
      pluginDuplicateGroups.flatMap((group) =>
        group.plugins.map((plugin) => plugin.filename)
      ),
    );
    duplicatePluginDetails = new Map();
    for (const group of pluginDuplicateGroups) {
      for (const plugin of group.plugins) {
        duplicatePluginDetails.set(
          plugin.filename,
          group.plugins
            .filter((candidate) => candidate.filename !== plugin.filename)
            .map((candidate) => candidate.filename),
        );
      }
    }

    pluginRestartRequired = data.restart_required === true;
    pluginServerRunning = data.running === true;

    showPluginRestartAlert();

    renderPlugins();
    showPluginDuplicatesModal(forceDuplicatePrompt);
  } catch {
    const list = document.getElementById(
      "plugin-list",
    );

    if (list) {
      list.textContent = "Unable to load plugins.";
    }
  }
}

function selectPluginJar(button) {
  const input = button.form?.elements.plugin;
  if (!input) return;
  input.value = "";
  input.click();
}

let pluginWindowDragDepth = 0;

function pluginDropPage(event) {
  const page = document.querySelector('.plugins-page[data-can-manage="true"]');
  const hasFiles = Array.from(event.dataTransfer?.types || []).includes("Files");
  return page && hasFiles ? page : null;
}

function clearPluginWindowDrag() {
  pluginWindowDragDepth = 0;
  document.querySelector(".plugins-page")
    ?.classList.remove("plugin-window-drag-active");
}

document.addEventListener("dragenter", (event) => {
  const page = pluginDropPage(event);
  if (!page) return;
  event.preventDefault();
  pluginWindowDragDepth += 1;
  page.classList.add("plugin-window-drag-active");
});

document.addEventListener("dragover", (event) => {
  if (!pluginDropPage(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});

document.addEventListener("dragleave", (event) => {
  if (!document.querySelector(".plugins-page.plugin-window-drag-active")) return;
  event.preventDefault();
  if (event.relatedTarget === null) {
    clearPluginWindowDrag();
    return;
  }
  pluginWindowDragDepth = Math.max(0, pluginWindowDragDepth - 1);
  if (pluginWindowDragDepth === 0) clearPluginWindowDrag();
});

document.addEventListener("drop", async (event) => {
  if (!pluginDropPage(event)) return;
  event.preventDefault();
  clearPluginWindowDrag();
  const files = Array.from(event.dataTransfer?.files || []);
  if (files.length !== 1 || !files[0].name.toLowerCase().endsWith(".jar")) {
    showToast("Drop one JAR file at a time.", "warning");
    return;
  }
  await installUploadedPlugin(files[0]);
});

async function uploadPluginJar(input) {
  if (!input.files?.length) return;

  const file = input.files[0];
  await installUploadedPlugin(file);
  input.value = "";
}

async function installUploadedPlugin(file, replace = false) {
  const page = document.querySelector(".plugins-page");
  const button = document.querySelector(".plugin-upload-button");
  if (!page || !file) return;

  if (button) button.disabled = true;
  showFileOperationProgress("Uploading plugin", `Uploading ${file.name}…`, 0);
  try {
    const formData = new FormData();
    formData.append("plugin", file, file.name);
    const response = await uploadFileWithProgress(
      `/api/web/servers/${page.dataset.serverId}/plugins/upload${replace ? "?replace=true" : ""}`,
      formData,
      (loaded, total) => showFileOperationProgress(
        "Uploading plugin",
        `Uploading ${file.name}…`,
        total ? (loaded / total) * 100 : 0,
      ),
    );
    let data;
    try {
      data = JSON.parse(response.responseText || "{}");
    } catch {
      data = {};
    }
    if (response.status === 409 && data.code === "plugin_file_exists") {
      hideFileOperationProgress();
      openPluginReplaceModal({ type: "upload", file }, data);
      return;
    }
    if (response.status < 200 || response.status >= 400) throw new Error(data.error || "Plugin upload failed");
    const action = replace ? "replaced" : "installed";
    showToast(`${data.plugin.name} ${action}.${data.action_requires_restart ? " Restart required." : ""}`, data.action_requires_restart ? "warning" : "success");
    latestInstalledPluginFilename = data.plugin.filename;
    pluginRestartRequired = data.restart_required === true;
    showPluginRestartAlert();
    await updatePluginsPage(true);
  } catch (error) {
    showToast(error.message || "Plugin upload failed.", "error");
  } finally {
    hideFileOperationProgress();
    if (button) button.disabled = false;
  }
}

async function downloadPluginUrl(event) {
  event.preventDefault();
  await installPluginUrl(event.currentTarget.elements.url.value);
}

async function installPluginUrl(url, replace = false) {
  const page = document.querySelector(".plugins-page");
  const form = document.querySelector('.plugin-install-form input[name="url"]')?.form;
  if (!page || !form) return;
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/plugins/url`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, replace }),
      },
    );
    const data = await response.json();
    if (response.status === 409 && data.code === "plugin_file_exists") {
      openPluginReplaceModal({ type: "url", url }, data);
      return;
    }
    if (!response.ok) throw new Error(data.error || "Plugin download failed");
    const action = replace ? "replaced" : "installed";
    showToast(`${data.plugin.name} ${action}.${data.action_requires_restart ? " Restart required." : ""}`, data.action_requires_restart ? "warning" : "success");
    latestInstalledPluginFilename = data.plugin.filename;
    form.reset();
    pluginRestartRequired = data.restart_required === true;
    showPluginRestartAlert();
    await updatePluginsPage(true);
  } catch (error) {
    showToast(error.message || "Plugin download failed.", "error");
  }
}

function openPluginReplaceModal(pending, conflict) {
  pluginPendingReplacement = pending;
  const modal = document.getElementById("plugin-replace-modal");
  const filename = document.getElementById("replace-plugin-filename");
  const state = document.getElementById("replace-plugin-state");
  if (!modal || !filename || !state) return;
  filename.textContent = conflict.filename;
  state.textContent = pluginServerRunning
    ? "The replacement changes the plugins loaded by the running server, so a restart will be required."
    : "The replacement will be loaded when the server is next started.";
  modal.hidden = false;
}

function closePluginReplaceModal() {
  const modal = document.getElementById("plugin-replace-modal");
  if (modal) modal.hidden = true;
  pluginPendingReplacement = null;
}

async function confirmPluginReplacement() {
  const pending = pluginPendingReplacement;
  const modal = document.getElementById("plugin-replace-modal");
  const button = document.getElementById("confirm-plugin-replace");
  if (!pending || !button) return;
  button.disabled = true;
  if (modal) modal.hidden = true;
  pluginPendingReplacement = null;
  try {
    if (pending.type === "upload") {
      await installUploadedPlugin(pending.file, true);
    } else {
      await installPluginUrl(pending.url, true);
    }
  } finally {
    button.disabled = false;
  }
}

function openPluginUpdateModal(filename = null) {
  if (pluginUpdateInProgress) {
    showToast("A plugin update is already in progress.", "info");
    return;
  }
  const targets = pluginData.filter((plugin) =>
    pluginHasDownloadableUpdate(plugin) && (!filename || plugin.filename === filename)
  );
  if (!targets.length) {
    showToast("There are no downloadable plugin updates to install.", "warning");
    return;
  }

  const modal = document.getElementById("plugin-update-modal");
  const title = document.getElementById("plugin-update-modal-title");
  const summary = document.getElementById("plugin-update-modal-summary");
  const list = document.getElementById("plugin-update-modal-list");
  const state = document.getElementById("plugin-update-modal-state");
  const confirm = document.getElementById("confirm-plugin-update");
  const deleteCurrent = document.getElementById("plugin-update-delete-current");
  if (!modal || !title || !summary || !list || !state || !confirm || !deleteCurrent) return;

  pluginPendingUpdates = targets.map((plugin) => ({...plugin}));
  pluginUpdatePreviousFocus = document.activeElement;
  const count = targets.length;
  title.textContent = count === 1 ? `Update ${targets[0].name}?` : `Update ${count} plugins?`;
  summary.textContent = count === 1
    ? "This will download and install the update directly on this server."
    : `This will download and install ${count} updates directly on this server, one at a time.`;
  list.innerHTML = targets.map((plugin) => {
    const installed = plugin.version || plugin.update?.installed_version || "Unknown";
    const latest = plugin.update?.latest_version || "New version";
    return `<li><strong>${escapeHtml(plugin.name)}</strong><span>${escapeHtml(installed)} <i class="fa-solid fa-arrow-right" aria-hidden="true"></i> ${escapeHtml(latest)}</span></li>`;
  }).join("");
  deleteCurrent.checked = false;
  state.textContent = pluginServerRunning
    ? "The server is running. If an enabled plugin changes, restart it to load the new version."
    : "If a plugin is currently disabled, its updated JAR will stay disabled.";
  confirm.querySelector("span").textContent = count === 1 ? "Update plugin" : `Update ${count} plugins`;
  modal.hidden = false;
  document.getElementById("cancel-plugin-update")?.focus();
}

function closePluginUpdateModal() {
  const modal = document.getElementById("plugin-update-modal");
  if (modal) modal.hidden = true;
  pluginPendingUpdates = [];
  const previousFocus = pluginUpdatePreviousFocus;
  pluginUpdatePreviousFocus = null;
  if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
}

async function confirmPluginUpdate() {
  if (!pluginPendingUpdates.length || pluginUpdateInProgress) return;
  const targets = pluginPendingUpdates;
  const deleteCurrentVersion = document.getElementById("plugin-update-delete-current")?.checked === true;
  const button = document.getElementById("confirm-plugin-update");
  if (button) button.disabled = true;
  closePluginUpdateModal();
  try {
    await installPluginUpdates(targets, deleteCurrentVersion);
  } finally {
    if (button) button.disabled = false;
  }
}

async function installPluginUpdates(plugins, deleteCurrentVersion) {
  const page = document.querySelector(".plugins-page");
  if (!page || !plugins.length) return;
  pluginUpdateInProgress = true;
  renderPlugins();
  const total = plugins.length;
  const successes = [];
  const retainedPreviousVersions = [];
  const failures = [];
  let progress = showToast(
    `Preparing ${total === 1 ? plugins[0].name : `${total} plugin updates`}…`,
    "info",
    0,
    {persistent: true, progress: {completed: 0, total}},
  );

  try {
    for (let index = 0; index < total; index += 1) {
      const plugin = plugins[index];
      updateToast(progress, `Updating ${plugin.name} (${index + 1} of ${total})…`, "info", {
        timeout: 0,
        progress: {completed: index, total},
      });
      try {
        const response = await nativeFetch(`/api/web/servers/${page.dataset.serverId}/plugins/update`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({filename: plugin.filename, delete_previous: deleteCurrentVersion}),
        });
        if (handleAuthenticationResponse(response)) {
          progress?.remove();
          return;
        }
        let data = {};
        try { data = await response.json(); } catch { /* Use the HTTP status below. */ }
        if (!response.ok) throw new Error(data.error || "Plugin update failed");
        successes.push(plugin.name);
        if (typeof data.previous_filename === "string" && data.previous_filename) {
          retainedPreviousVersions.push(data.previous_filename);
        }
        pluginRestartRequired = pluginRestartRequired || data.restart_required === true;
      } catch (error) {
        failures.push(`${plugin.name}: ${error.message || "Update failed"}`);
      }
      updateToast(progress, `Processed ${index + 1} of ${total} plugin updates.`, "info", {
        timeout: 0,
        progress: {completed: index + 1, total},
      });
    }

    showPluginRestartAlert();
    await updatePluginsPage(false);
    await refreshNotifications(false);
    const retainedSummary = retainedPreviousVersions.length
      ? ` Previous versions kept disabled: ${retainedPreviousVersions.join(", ")}.`
      : "";
    const deletedSummary = successes.length && deleteCurrentVersion
      ? " Previous versions were deleted after validation and successful installation."
      : "";
    const summary = successes.length
      ? `Updated ${successes.length} of ${total} plugin${total === 1 ? "" : "s"}.${retainedSummary}${deletedSummary}${pluginRestartRequired ? " Restart the server to load enabled updates." : ""}`
      : `No plugins were updated.`;
    const failureDetail = failures.length ? ` Failed: ${failures.join("; ")}` : "";
    if (successes.length) {
      const serverName = document.querySelector(".sidebar-server-copy strong")?.textContent?.trim() || "Server";
      recordInAppNotification({
        id: `plugin-install:${page.dataset.serverId}:${Date.now()}`,
        kind: "plugin-install",
        title: successes.length === 1 ? "Plugin update installed" : "Plugin updates installed",
        message: `${serverName}: ${successes.join(", ")}${pluginRestartRequired ? " · restart required" : ""}`,
        url: `/servers/${encodeURIComponent(page.dataset.serverId)}/plugins`,
        checked_at: new Date().toISOString(),
      });
    }
    updateToast(
      progress,
      `${summary}${failureDetail}`,
      failures.length ? (successes.length ? "warning" : "error") : (pluginRestartRequired ? "warning" : "success"),
      {timeout: 9000, progress: {completed: total, total}},
    );
  } finally {
    pluginUpdateInProgress = false;
    renderPlugins();
  }
}

function renderPlugins() {
  const list = document.getElementById(
    "plugin-list",
  );

  if (!list) {
    return;
  }

  const search = (
    document
      .getElementById(
        "plugin-search",
      )
      ?.value ||
    ""
  )
    .trim()
    .toLowerCase();

  const plugins = pluginData.filter(
    (plugin) =>
      plugin.name
        .toLowerCase()
        .includes(search) ||
      plugin.filename
        .toLowerCase()
        .includes(search),
  );

  const visiblePlugins = plugins;
  const page = document.querySelector(".plugins-page");
  const canManage = page?.dataset.canManage === "true";
  const canViewFiles = page?.dataset.canViewFiles === "true";
  const canEditFiles = page?.dataset.canEditFiles === "true";
  const updates = pluginData.filter((plugin) => pluginHasDownloadableUpdate(plugin));
  const updateAllButton = document.getElementById("plugin-update-all");
  if (updateAllButton) {
    updateAllButton.hidden = !canManage || updates.length === 0;
    const label = updateAllButton.querySelector("span");
    if (label) label.textContent = `Update all (${updates.length})`;
    updateAllButton.disabled = pluginUpdateInProgress;
  }

  const count = document.getElementById(
    "plugin-count",
  );

  if (count) {
    count.textContent = `${pluginData.length} ${
      pluginData.length === 1 ? "plugin" : "plugins"
    }`;
  }

  if (!visiblePlugins.length) {
    list.innerHTML = `<div class="empty-message">
                ${search ? "No plugin found. Try another name." : "No plugins yet. Choose a plugin file above."}
            </div>`;

    return;
  }

  list.innerHTML = visiblePlugins.map((plugin) => `
    <article class="plugin-row ${plugin.enabled ? "" : "disabled"} ${duplicatePluginFilenames.has(plugin.filename) ? "duplicate" : ""}">
      <span class="plugin-status-indicator ${plugin.enabled ? "is-enabled" : "is-disabled"}" aria-hidden="true" title="${plugin.enabled ? "Enabled" : "Disabled"}"></span>
      <div class="plugin-main">
        <div class="plugin-name-line">
          <strong>${escapeHtml(plugin.name)}</strong>
        </div>
        ${renderPluginVersionStatus(plugin, canManage)}
        <dl class="plugin-file-meta">
          <div><dt>File name:</dt><dd>${escapeHtml(plugin.filename)}</dd></div>
          <div><dt>File size:</dt><dd>${formatPluginFileSize(plugin.size)}</dd></div>
        </dl>
        ${duplicatePluginDetails.has(plugin.filename) ? `
          <p class="plugin-duplicate-label"><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> Another copy is enabled: ${escapeHtml(duplicatePluginDetails.get(plugin.filename).join(", "))}</p>
        ` : ""}
      </div>
      ${canManage ? renderPluginActions(plugin, canViewFiles, canEditFiles) : ""}
    </article>
  `)
    .join("");
}

function renderPluginActions(plugin, canViewFiles, canEditFiles) {
  const folderPath = plugin.config_directory ? `plugins/${plugin.config_directory}` : "";
  const folderAction = !canViewFiles ? "" : folderPath
    ? `<button class="plugin-menu-action" type="button" onclick="this.closest('details').open=false; openPluginFolder('${escapeJs(folderPath)}')"><i class="fa-solid fa-folder-open" aria-hidden="true"></i><span>Open Folder</span></button>`
    : `<div class="plugin-menu-empty"><i class="fa-regular fa-folder-open" aria-hidden="true"></i><span>No data folder found</span></div>`;
  const filename = escapeJs(plugin.filename);
  const stateAction = plugin.enabled ? "disable" : "enable";
  const stateLabel = plugin.enabled ? "Disable" : "Enable";

  return `
    <div class="plugin-actions">
      <details class="plugin-more-actions">
        <summary class="button"><i class="fa-solid fa-ellipsis" aria-hidden="true"></i><span>More</span></summary>
        <div class="plugin-more-menu" role="group" aria-label="More actions for ${escapeHtml(plugin.name)}">
          ${plugin.suggested_filename && plugin.enabled === true ? `<button class="plugin-menu-action" type="button" onclick="this.closest('details').open=false; correctPluginFilename('${filename}', this)"><i class="fa-solid fa-pen-to-square" aria-hidden="true"></i><span>Correct filename</span></button>` : ""}
          ${plugin.enabled === true && plugin.previous_version !== true ? `<button class="plugin-menu-action" type="button" onclick="this.closest('details').open=false; checkPluginUpdate('${filename}', this)"><i class="fa-solid fa-arrows-rotate" aria-hidden="true"></i><span>Check for updates</span></button>` : ""}
          <button class="plugin-menu-action" type="button" onclick="this.closest('details').open=false; openPluginMonitoring(${pluginData.indexOf(plugin)})"><i class="fa-solid fa-gear" aria-hidden="true"></i><span>Update settings</span></button>
          ${folderAction}
          <div class="plugin-menu-separator" role="separator"></div>
          ${renderPluginConfigActions(plugin, canEditFiles)}
          <div class="plugin-menu-separator" role="separator"></div>
          <button class="plugin-menu-action" type="button" onclick="this.closest('details').open=false; togglePlugin('${filename}', '${stateAction}')"><i class="fa-solid ${plugin.enabled ? "fa-toggle-off" : "fa-toggle-on"}" aria-hidden="true"></i><span>${stateLabel} plugin</span></button>
          <div class="plugin-menu-separator" role="separator"></div>
          <button class="plugin-menu-action is-danger" type="button" onclick="this.closest('details').open=false; openPluginRemoveModal('${filename}')"><i class="fa-solid fa-trash" aria-hidden="true"></i><span>Remove plugin</span></button>
        </div>
      </details>
    </div>`;
}

function renderPluginComparisonDetails(details) {
  if (!details) return "";
  const input = details.installed_value || "(empty)";
  const source = details.installed_source || "JAR metadata";
  const expression = details.installed_pattern
    ? `installed expression <code>${escapeHtml(details.installed_pattern)}</code>`
    : "automatic installed version detection";
  const matched = details.installed_comparison !== undefined
    ? `${details.installed_pattern ? "matched" : "detected"} as <code>${escapeHtml(details.installed_comparison)}</code>`
    : `did not match${details.match_error ? ` (${escapeHtml(details.match_error)})` : ""}`;
  const filename = details.installed_filename
    ? ` from <code>${escapeHtml(details.installed_filename)}</code>`
    : "";
  const release = details.release_value || "(empty)";
  const releaseExpression = details.release_pattern
    ? ` using release expression <code>${escapeHtml(details.release_pattern)}</code>`
    : "";
  return `<div class="plugin-update-diagnostic"><strong>Match details:</strong> ${escapeHtml(source)} value <code>${escapeHtml(input)}</code>${filename} with ${expression} ${matched}; latest release value <code>${escapeHtml(release)}</code>${releaseExpression}.</div>`;
}

function pluginHasDownloadableUpdate(plugin) {
  return plugin?.enabled === true
    && plugin?.previous_version !== true
    && plugin?.update?.update_available === true
    && typeof plugin.update.download_url === "string"
    && plugin.update.download_url.startsWith("https://");
}

function renderPluginVersionStatus(plugin, canManage = false) {
  if (plugin?.enabled !== true) return "";
  const update = plugin.update;
  const updateInstalledVersion = plugin.version_source === "filename"
    ? null
    : update?.installed_version;
  const installedVersion = [plugin.version, updateInstalledVersion]
    .filter((version, index, versions) => version && versions.indexOf(version) === index)
    .join(" | ") || "Unknown";
  const versionPill = `<span class="server-version-pill plugin-version-pill" title="Installed plugin version"><i class="fa-solid fa-cube" aria-hidden="true"></i><span class="visually-hidden">Installed version: </span>${escapeHtml(installedVersion)}</span>`;
  if (plugin.previous_version) {
    return `<div class="plugin-version-status-row">${versionPill}<span class="server-inline-update-status plugin-update-status is-pending" title="Previous JAR retained in a disabled state in case you need to restore it"><i class="fa-solid fa-clock-rotate-left" aria-hidden="true"></i>Previous version</span></div>`;
  }
  if (!update) return `<div class="plugin-version-status-row">${versionPill}</div>`;

  let label;
  let icon;
  let statusClass;
  if (update.update_available) {
    label = update.latest_version ? `New version ${update.latest_version}` : "New version available";
    icon = "fa-circle-up";
    statusClass = "is-update";
  } else if (update.status === "Current") {
    label = "Up to date";
    icon = "fa-circle-check";
    statusClass = "is-current";
  } else if (update.status === "Check failed") {
    label = "Could not check";
    icon = "fa-triangle-exclamation";
    statusClass = "is-error";
  } else if (update.status === "Incompatible") {
    label = "May not fit this server";
    icon = "fa-triangle-exclamation";
    statusClass = "is-warning";
  } else if (update.status === "Monitoring disabled") {
    label = "Update checks off";
    icon = "fa-circle-minus";
    statusClass = "is-pending";
  } else if (update.status === "Unsupported/unmonitored") {
    label = "Set up update checks";
    icon = "fa-circle-question";
    statusClass = "is-pending";
  } else {
    label = "Not checked yet";
    icon = "fa-circle-question";
    statusClass = "is-pending";
  }

  const statusTitle = update.error || label;
  const statusMarkup = `<i class="fa-solid ${icon}" aria-hidden="true"></i>${escapeHtml(label)}`;
  const statusPill = update.update_available && update.release_url?.startsWith("https://")
    ? `<a class="server-inline-update-status plugin-update-status ${statusClass}" href="${escapeHtml(update.release_url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(statusTitle)}">${statusMarkup}</a>`
    : `<span class="server-inline-update-status plugin-update-status ${statusClass}" title="${escapeHtml(statusTitle)}">${statusMarkup}</span>`;
  const download = canManage && pluginHasDownloadableUpdate(plugin)
    ? `<button class="plugin-update-install" type="button" onclick="openPluginUpdateModal('${escapeHtml(escapeJs(plugin.filename))}')" aria-label="Install update for ${escapeHtml(plugin.name)} on the server" title="Install update on server" ${pluginUpdateInProgress ? "disabled" : ""}><i class="fa-solid fa-cloud-arrow-down" aria-hidden="true"></i></button>`
    : "";
  const errorDetail = update.status === "Check failed" && update.error
    ? `<div class="plugin-update-error"><div><strong>Reason:</strong> ${escapeHtml(update.error)}</div>${renderPluginComparisonDetails(update.comparison_details)}</div>`
    : "";
  return `<div class="plugin-version-status-row">${versionPill}${statusPill}${download}${errorDetail}</div>`;
}

function formatPluginFileSize(size) {
  const bytes = Number(size);
  if (!Number.isFinite(bytes) || bytes < 0) return "Unknown";

  const units = ["Bytes", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return unitIndex === 0
    ? `${value} ${units[unitIndex]}`
    : `${value.toFixed(1)} ${units[unitIndex]}`;
}

function renderPluginConfigActions(plugin, canEditFiles) {
  const allFiles = Array.isArray(plugin.config_files) ? plugin.config_files : [];
  const pluginDirectory = plugin.config_directory ? `plugins/${plugin.config_directory}/` : "";
  const rootFiles = allFiles.filter((path) => {
    if (typeof path !== "string" || !pluginDirectory || !path.startsWith(pluginDirectory)) return false;
    const filename = path.slice(pluginDirectory.length);
    return filename && !filename.includes("/");
  });

  if (!rootFiles.length) {
    return `<div class="plugin-menu-empty">No root-level configuration files found</div>`;
  }

  if (!canEditFiles) {
    return `<div class="plugin-menu-empty">File edit access is needed</div>`;
  }

  const modifiedFiles = plugin.config_file_modified_ns || {};
  rootFiles.sort((left, right) => {
    const modifiedDifference = Number(modifiedFiles[right] || 0) / 1_000_000
      - Number(modifiedFiles[left] || 0) / 1_000_000;
    return modifiedDifference || left.localeCompare(right);
  });

  const visibleFiles = rootFiles.slice(0, 5);
  const fileActions = visibleFiles.map((path) => {
    const filename = path.slice(pluginDirectory.length);
    return `<button class="plugin-menu-action plugin-config-file" type="button" title="${escapeHtml(path)}" onclick="this.closest('.plugin-more-actions').open=false; editPluginConfig('${escapeJs(path)}')"><i class="fa-regular fa-file-lines" aria-hidden="true"></i><span>${escapeHtml(filename)}</span></button>`;
  }).join("");
  const limitNote = rootFiles.length > visibleFiles.length
    ? `<div class="plugin-menu-note">Showing the 5 most recently modified of ${rootFiles.length} root config files</div>`
    : "";
  return `${fileActions}${limitNote}`;
}

function pluginDuplicateGroupSignature(group) {
  const serverId = document.querySelector(".plugins-page")?.dataset.serverId || "";
  return `${serverId}:${JSON.stringify({
    name: group.name,
    files: group.plugins.map((plugin) => ({
      filename: plugin.filename,
      version: plugin.version || null,
      size: plugin.size ?? null,
      modified_ns: plugin.modified_ns ?? null,
    })).sort((left, right) => left.filename.localeCompare(right.filename)),
  })}`;
}

function acknowledgedPluginDuplicates() {
  const acknowledged = new Set(sessionAcknowledgedPluginDuplicates);
  try {
    const stored = JSON.parse(localStorage.getItem(acknowledgedPluginDuplicatesKey) || "[]");
    if (Array.isArray(stored)) {
      for (const signature of stored) acknowledged.add(signature);
    }
  } catch {
    // Session acknowledgements still prevent repeated prompts.
  }
  return acknowledged;
}

function storeAcknowledgedPluginDuplicates(acknowledged) {
  sessionAcknowledgedPluginDuplicates = new Set(acknowledged);
  try {
    localStorage.setItem(
      acknowledgedPluginDuplicatesKey,
      JSON.stringify(Array.from(acknowledged).slice(-100)),
    );
  } catch {
    // The warning still remains dismissed for this page load when storage is unavailable.
  }
}

function prunePluginDuplicateAcknowledgements() {
  const serverId = document.querySelector(".plugins-page")?.dataset.serverId || "";
  if (!serverId) return;
  const current = new Set(
    pluginDuplicateGroups.map((group) => pluginDuplicateGroupSignature(group)),
  );
  const acknowledged = acknowledgedPluginDuplicates();
  for (const signature of acknowledged) {
    if (signature.startsWith(`${serverId}:`) && !current.has(signature)) {
      acknowledged.delete(signature);
    }
  }
  storeAcknowledgedPluginDuplicates(acknowledged);
}

function showPluginDuplicatesModal() {
  const modal = document.getElementById("plugin-duplicates-modal");
  const container = document.getElementById("plugin-duplicate-groups");
  if (!modal || !container) return;
  prunePluginDuplicateAcknowledgements();
  if (!pluginDuplicateGroups.length) {
    modal.hidden = true;
    return;
  }
  const acknowledged = acknowledgedPluginDuplicates();
  const groupsToShow = pluginDuplicateGroups.filter(
    (group) => !acknowledged.has(pluginDuplicateGroupSignature(group)),
  );
  if (!groupsToShow.length) {
    modal.hidden = true;
    return;
  }
  modal.dataset.groupSignatures = JSON.stringify(
    groupsToShow.map((group) => pluginDuplicateGroupSignature(group)),
  );
  container.replaceChildren();
  const hasJustAddedPlugin = Boolean(
    latestInstalledPluginFilename
    && groupsToShow.some((group) => group.plugins.some(
      (plugin) => plugin.filename === latestInstalledPluginFilename,
    )),
  );
  const orderedGroups = [...groupsToShow].sort((left, right) => {
    const leftHasJustAdded = left.plugins.some(
      (plugin) => plugin.filename === latestInstalledPluginFilename,
    );
    const rightHasJustAdded = right.plugins.some(
      (plugin) => plugin.filename === latestInstalledPluginFilename,
    );
    return Number(rightHasJustAdded) - Number(leftHasJustAdded);
  });
  for (const group of orderedGroups) {
    const section = document.createElement("div");
    section.className = "plugin-duplicate-group";
    const header = document.createElement("div");
    header.className = "plugin-duplicate-group-header";
    const title = document.createElement("strong");
    title.textContent = group.name;
    const count = document.createElement("span");
    count.textContent = `${group.plugins.length} enabled`;
    header.append(title, count);
    section.appendChild(header);
    const orderedPlugins = [...group.plugins].sort((left, right) => (
      Number(right.filename === latestInstalledPluginFilename)
      - Number(left.filename === latestInstalledPluginFilename)
    ));
    for (const plugin of orderedPlugins) {
      const isJustAdded = plugin.filename === latestInstalledPluginFilename;
      const row = document.createElement("div");
      row.className = "plugin-duplicate-option";
      if (isJustAdded) row.classList.add("just-added");
      const selection = document.createElement("label");
      selection.className = "plugin-duplicate-selection";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = plugin.filename;
      checkbox.checked = hasJustAddedPlugin ? isJustAdded : true;
      checkbox.setAttribute("aria-label", `Keep ${plugin.filename} enabled`);
      checkbox.addEventListener("change", () => {
        row.classList.toggle("will-disable", !checkbox.checked);
        state.textContent = checkbox.checked ? "Enabled" : "Will be disabled";
        updateDuplicatePluginButton();
      });
      const description = document.createElement("span");
      description.className = "plugin-duplicate-description";
      const descriptionHeader = document.createElement("span");
      descriptionHeader.className = "plugin-duplicate-description-header";
      const filename = document.createElement("strong");
      filename.textContent = plugin.filename;
      descriptionHeader.appendChild(filename);
      if (isJustAdded) {
        const recent = document.createElement("span");
        recent.className = "plugin-just-added-label";
        recent.textContent = "Just added";
        descriptionHeader.appendChild(recent);
      }
      description.appendChild(descriptionHeader);
      if (plugin.version) {
        const version = document.createElement("small");
        version.textContent = `Version ${plugin.version}`;
        description.appendChild(version);
      }
      if (plugin.modified_ns) {
        const modified = document.createElement("small");
        modified.textContent = `Modified ${formatPluginModified(plugin.modified_ns)}`;
        description.appendChild(modified);
      }
      const state = document.createElement("small");
      state.className = "plugin-duplicate-state";
      state.textContent = checkbox.checked ? "Enabled" : "Will be disabled";
      description.appendChild(state);
      if (!checkbox.checked) row.classList.add("will-disable");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "plugin-duplicate-delete";
      remove.setAttribute("aria-label", `Delete ${plugin.filename}`);
      remove.title = `Delete ${plugin.filename}`;
      const trashIcon = document.createElement("i");
      trashIcon.className = "fa-solid fa-trash";
      trashIcon.setAttribute("aria-hidden", "true");
      remove.appendChild(trashIcon);
      remove.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!confirm(`Permanently delete ${plugin.filename}?`)) return;
        const page = document.querySelector(".plugins-page");
        const response = await fetch(`/api/web/servers/${page.dataset.serverId}/plugins/action`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "remove", filename: plugin.filename }),
        });
        const data = await response.json();
        if (!response.ok) return showToast(data.error || "Unable to delete plugin", "error");
        latestInstalledPluginFilename = null;
        await updatePluginsPage(true);
        await refreshNotifications(false);
      });
      selection.append(checkbox, description);
      row.append(selection, remove);
      section.appendChild(row);
    }
    container.appendChild(section);
  }
  updateDuplicatePluginButton();
  modal.hidden = false;
}

function updateDuplicatePluginButton() {
  const button = document.getElementById("disable-selected-duplicates");
  if (button) {
    button.disabled = !document.querySelector(
      '#plugin-duplicate-groups input[type="checkbox"]:not(:checked)',
    );
  }
}

function keepDuplicatePluginsEnabled() {
  const modal = document.getElementById("plugin-duplicates-modal");
  const acknowledged = acknowledgedPluginDuplicates();
  try {
    const signatures = JSON.parse(modal?.dataset.groupSignatures || "[]");
    for (const signature of signatures) acknowledged.add(signature);
    storeAcknowledgedPluginDuplicates(acknowledged);
  } catch {
    if (modal?.dataset.groupSignatures) {
      sessionAcknowledgedPluginDuplicates.add(modal.dataset.groupSignatures);
    }
  }
  if (modal) modal.hidden = true;
}

async function disableSelectedDuplicatePlugins() {
  const page = document.querySelector(".plugins-page");
  const button = document.getElementById("disable-selected-duplicates");
  if (!page || !button) return;
  const selected = Array.from(document.querySelectorAll(
    '#plugin-duplicate-groups input[type="checkbox"]:not(:checked)',
  )).map((checkbox) => checkbox.value);
  if (!selected.length) return;
  button.disabled = true;
  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/plugins/duplicates/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disable: selected }),
    },
  );
  const data = await response.json();
  if (!response.ok) {
    showToast(data.error || "Unable to disable duplicate plugins", "error");
    button.disabled = false;
    return;
  }
  const modal = document.getElementById("plugin-duplicates-modal");
  if (modal) modal.hidden = true;
  pluginRestartRequired = data.restart_required === true;
  showPluginRestartAlert();
  await updatePluginsPage();
  await refreshNotifications(false);
}

function openPluginFolder(path) {
  const page = document.querySelector(".plugins-page[data-server-id]");
  if (!page || !path) return;
  const url = `/servers/${page.dataset.serverId}/files?path=${encodeURIComponent(path)}`;
  htmx.ajax("GET", url, {
    target: "#page-content",
    swap: "innerHTML",
    pushUrl: url,
  });
}

function editPluginConfig(path) {
  const page = document.querySelector(".plugins-page[data-server-id]");
  if (!page) return;
  const url = `/servers/${page.dataset.serverId}/files/edit?path=${
    encodeURIComponent(path)
  }`;
  htmx.ajax("GET", url, {
    target: "#page-content",
    swap: "innerHTML",
    pushUrl: url,
  });
}

function parseConsoleLine(line) {
  const cleanedLine = String(line)
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\u009b[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\u00a7[0-9A-FK-ORX]/gi, "");
  const match = cleanedLine.match(
    /^\[?(\d{2}:\d{2}:\d{2})(?:\s+(INFO|WARN|ERROR|DEBUG))?\]?:?\s*(?:\[([^\]]+)\/(INFO|WARN|ERROR|DEBUG)\]:?\s*)?(.*)$/i,
  );

  if (!match) {
    return {
      time: "",
      level: "INFO",
      message: cleanedLine,
    };
  }

  return {
    time: match[1] || "",
    level: (match[4] || match[2] || "INFO").toUpperCase(),
    message: (match[3] ? `[${match[3]}] ` : "") +
      (match[5] || ""),
  };
}

function renderConsoleLines() {
  updateConsoleFilterButtons();
  const output = document.getElementById(
    "console-output",
  );

  if (!output) {
    return;
  }

  const filtered = consoleFilters.size
    ? consoleLines.filter((line) => consoleFilters.has(line.level))
    : consoleLines;

  if (!filtered.length) {
    output.innerHTML =
      '<div class="empty-message">No matching log entries.</div>';

    return;
  }

  output.innerHTML = filtered
    .map((line) => {
      const levelClass = line.level.toLowerCase();

      return (
        '<div class="console-line">' +
        '<span class="console-time">' +
        escapeHtml(line.time) +
        "</span>" +
        '<span class="console-level ' +
        levelClass +
        '">' +
        escapeHtml(line.level) +
        "</span>" +
        '<span class="console-message">' +
        escapeHtml(line.message) +
        "</span>" +
        "</div>"
      );
    })
    .join("");
}

function updateConsoleFilterButtons() {
  document.querySelectorAll(".console-filter").forEach((button) => {
    const level = button.dataset.level;
    const selected = level === "ALL"
      ? consoleFilters.size === 0
      : consoleFilters.has(level);
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeJsString(value) {
  return escapeHtml(String(value).replaceAll("\\", "\\\\").replaceAll("'", "\\'"));
}

async function updateConsolePage() {
  const page = document.querySelector(
    ".console-page",
  );

  if (!page) {
    return;
  }

  const serverId = page.dataset.serverId;

  const output = document.getElementById(
    "console-output",
  );

  const input = document.getElementById(
    "console-input",
  );

  const send = document.getElementById(
    "console-send",
  );

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/console-data`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    const newLines = data.lines.map(
      parseConsoleLine,
    );

    /*
     * Check whether the log has actually
     * changed since the last poll.
     */
    const newSignature = JSON.stringify(newLines);

    const logChanged = newSignature !==
      lastConsoleSignature;

    const consoleNeedsRender = logChanged ||
      output.children.length === 0;

    lastConsoleSignature = newSignature;

    consoleLines = newLines;

    /*
     * We only need to redraw the console
     * when something changed.
     */
    if (consoleNeedsRender) {
      renderConsoleLines();
    }

    if (data.running && data.console_available !== false && data.state !== "stopping") {
      input.disabled = false;
      send.disabled = false;

      input.placeholder = "Type a command and press Enter...";
    } else if (data.running) {
      input.disabled = true;
      send.disabled = true;

      input.placeholder = data.state === "stopping"
        ? "Server is shutting down"
        : data.state === "starting"
          ? "Server is starting; console is not ready"
          : "Server console unavailable";
    } else {
      input.disabled = true;
      send.disabled = true;

      input.placeholder = "Server is stopped";
    }

    /*
     * Only autoscroll when:
     *
     * 1. Autoscroll is enabled
     * 2. The log actually changed
     */
    if (
      consoleAutoScroll &&
      logChanged
    ) {
      scrollConsoleToBottom();
    }
  } catch {
    consoleLines = [];

    output.textContent = "Unable to load console.";

    input.disabled = true;
    send.disabled = true;
  }
}

async function sendConsoleCommand() {
  const page = document.querySelector(
    ".console-page",
  );

  if (!page) {
    return;
  }

  const serverId = page.dataset.serverId;

  const input = document.getElementById(
    "console-input",
  );

  const command = input.value.trim();

  if (!command) {
    return;
  }

  const response = await fetch(
    `/api/web/servers/${serverId}/command`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        command: command,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Command failed",
    );

    return;
  }

  input.value = "";

  setTimeout(
    updateConsolePage,
    200,
  );
}

document.addEventListener(
  "keydown",
  function (event) {
    if (
      event.key === "Enter" &&
      event.target?.id === "console-input"
    ) {
      sendConsoleCommand();
    }
  },
);

updateConsolePage();

setInterval(
  updateConsolePage,
  1000,
);

async function updateOverviewLogs() {
  const overview = document.querySelector(
    ".server-overview",
  );

  if (!overview) {
    return;
  }

  const serverId = overview.dataset.serverId;

  const output = document.getElementById(
    "overview-logs",
  );

  if (!output) {
    return;
  }

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/console-data`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    if (!data.lines.length) {
      output.textContent = data.running
        ? "No console output yet."
        : "No recent logs available.";

      return;
    }

    output.textContent = data.lines
      .slice(-20)
      .join("\n");

    output.scrollTop = output.scrollHeight;
  } catch {
    output.textContent = "Unable to load recent logs.";
  }
}

updateOverviewLogs();

setInterval(
  updateOverviewLogs,
  3000,
);

function scrollConsoleToBottom() {
  const output = document.getElementById(
    "console-output",
  );

  if (!output) {
    return;
  }

  output.scrollTop = output.scrollHeight;
}

document.addEventListener(
  "click",
  function (event) {
    const button = event.target.closest(
      ".console-filter",
    );

    if (!button) {
      return;
    }

    const level = button.dataset.level;
    if (level === "ALL") {
      consoleFilters.clear();
    } else {
      if (consoleFilters.has(level)) consoleFilters.delete(level);
      else consoleFilters.add(level);
      if (consoleFilters.size === consoleLevels.length) consoleFilters.clear();
    }

    renderConsoleLines();
  },
);

let consoleAutoScroll = true;

function toggleConsoleAutoScroll() {
  consoleAutoScroll = !consoleAutoScroll;

  const button = document.getElementById(
    "console-autoscroll",
  );

  if (!button) {
    return;
  }

  button.classList.toggle(
    "active",
    consoleAutoScroll,
  );

  button.title = consoleAutoScroll
    ? "Auto-scroll enabled"
    : "Auto-scroll disabled";

  if (consoleAutoScroll) {
    scrollConsoleToBottom();
  }
}

updateActiveNavigation();

async function updatePlayersPage() {
  const page = document.querySelector(
    ".players-page",
  );

  if (!page) {
    return;
  }

  const serverId = page.dataset.serverId;

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/players`,
    );

    if (!response.ok) {
      throw new Error();
    }

    playerData = await response.json();

    renderPlayerPage();
  } catch {
    const list = document.getElementById(
      "player-list",
    );

    if (list) {
      list.textContent = "Unable to load players.";
    }
  }
}

function renderPlayerPage() {
  if (!playerData) {
    return;
  }

  const sortControl = document.getElementById("player-sort");
  if (sortControl) sortControl.value = playerSort;

  setText(
    "players-online-count",
    playerData.online_count,
  );

  setText(
    "players-whitelist-count",
    playerData.whitelisted_count,
  );

  setText(
    "players-op-count",
    playerData.operator_count,
  );

  setText(
    "players-ban-count",
    playerData.banned_count,
  );

  setText(
    "filter-all-count",
    playerData.players.length,
  );

  setText(
    "filter-online-count",
    playerData.online_count,
  );

  setText(
    "filter-whitelist-count",
    playerData.whitelisted_count,
  );

  setText(
    "filter-op-count",
    playerData.operator_count,
  );

  setText(
    "filter-ban-count",
    playerData.banned_count,
  );

  setText(
    "ip-ban-count",
    playerData.ip_banned_count,
  );

  const ipList = document.getElementById("ip-ban-list");
  if (ipList) {
    ipList.innerHTML = (playerData.ip_bans || []).length
      ? playerData.ip_bans.map((item) => `
          <div class="ip-ban-row"><div><strong>${
        escapeHtml(item.ip)
      }</strong><small>${escapeHtml(item.reason || "Banned")}</small></div>
          <button class="button" ${
        !playerData.running ? "disabled" : ""
      } onclick="ipBanAction('${
        escapeJs(item.ip)
      }', 'pardon')">Unblock</button></div>`).join("")
      : '<div class="empty-message">No blocked IP addresses.</div>';
  }

  const toggle = document.getElementById(
    "whitelist-toggle",
  );

  if (toggle) {
    toggle.checked = playerData.whitelist_enabled;
  }

  const offline = document.getElementById(
    "players-offline-note",
  );

  if (offline) {
    offline.hidden = playerData.running;
  }

  renderPlayerList();
}

function setText(
  id,
  value,
) {
  const element = document.getElementById(id);

  if (element) {
    element.textContent = value;
  }
}

function formatLastOnlineDate(date) {
  const differenceMs = date.getTime() - Date.now();
  const absoluteDifferenceMs = Math.abs(differenceMs);
  const absoluteDate = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);

  if (absoluteDifferenceMs > 30 * 24 * 60 * 60 * 1000) {
    return absoluteDate;
  }
  if (absoluteDifferenceMs < 60 * 1000) {
    return "just now";
  }

  const units = [
    {name: "day", milliseconds: 24 * 60 * 60 * 1000},
    {name: "hour", milliseconds: 60 * 60 * 1000},
    {name: "minute", milliseconds: 60 * 1000},
  ];
  const unit = units.find((candidate) => absoluteDifferenceMs >= candidate.milliseconds);
  const relativeDate = new Intl.RelativeTimeFormat(undefined, {numeric: "auto"});

  return relativeDate.format(
    Math.round(differenceMs / unit.milliseconds),
    unit.name,
  );
}

function renderPlayerList() {
  const list = document.getElementById(
    "player-list",
  );

  if (
    !list ||
    !playerData
  ) {
    return;
  }

  const search = (
    document
      .getElementById(
        "player-search",
      )
      ?.value ||
    ""
  )
    .trim()
    .toLowerCase();
  const normalizedSearch = search.replace(/-/g, "");

  let players = playerData.players.filter(
    (player) => {
      if (search) {
        const playerName = String(player.name || "").toLowerCase();
        const playerUuid = String(player.uuid || "").toLowerCase();
        const uuidMatches = playerUuid.includes(search)
          || (normalizedSearch && playerUuid.replace(/-/g, "").includes(normalizedSearch));
        if (!playerName.includes(search) && !uuidMatches) {
          return false;
        }
      }

      switch (
        playerFilter
      ) {
        case "online":
          return player.online;

        case "whitelisted":
          return player.whitelisted;

        case "operator":
          return player.operator;

        case "banned":
          return player.banned;

        default:
          return true;
      }
    },
  );

  const compareName = (left, right) =>
    String(left.name || "").localeCompare(
      String(right.name || ""),
      undefined,
      {sensitivity: "base"},
    );
  const lastOnlineTime = (player) => {
    const timestamp = Date.parse(player.last_online || "");
    return Number.isFinite(timestamp) ? timestamp : null;
  };
  players.sort((left, right) => {
    if (playerSort === "name-desc") return compareName(right, left);
    if (playerSort === "online-first") {
      return Number(right.online) - Number(left.online) || compareName(left, right);
    }
    if (playerSort === "last-online-desc" || playerSort === "last-online-asc") {
      const leftTime = lastOnlineTime(left);
      const rightTime = lastOnlineTime(right);
      if (leftTime === null || rightTime === null) {
        if (leftTime === rightTime) return compareName(left, right);
        return leftTime === null ? 1 : -1;
      }
      const direction = playerSort === "last-online-desc" ? -1 : 1;
      return (leftTime - rightTime) * direction || compareName(left, right);
    }
    return compareName(left, right);
  });

  if (!players.length) {
    list.innerHTML = '<div class="empty-message">No players found.</div>';
    const pagination = document.getElementById("player-pagination");
    if (pagination) pagination.hidden = true;

    return;
  }

  const pageCount = Math.ceil(players.length / PLAYER_PAGE_SIZE);
  playerPage = Math.min(Math.max(playerPage, 1), pageCount);
  const pagination = document.getElementById("player-pagination");
  if (pagination) {
    const start = (playerPage - 1) * PLAYER_PAGE_SIZE + 1;
    const end = Math.min(playerPage * PLAYER_PAGE_SIZE, players.length);
    const summary = pagination.querySelector(".player-pagination-summary");
    const previous = pagination.querySelector('[data-page-direction="previous"]');
    const next = pagination.querySelector('[data-page-direction="next"]');
    pagination.hidden = pageCount <= 1;
    if (summary) summary.textContent = `Showing ${start}–${end} of ${players.length}`;
    if (previous) previous.disabled = playerPage <= 1;
    if (next) next.disabled = playerPage >= pageCount;
  }
  players = players.slice((playerPage - 1) * PLAYER_PAGE_SIZE, playerPage * PLAYER_PAGE_SIZE);

  list.innerHTML = players.map(
    (player) => {
      const avatar = player.uuid
        ? `https://mc-heads.net/avatar/${encodeURIComponent(player.uuid)}/40`
        : "";
      const lastOnlineDate = player.last_online ? new Date(player.last_online) : null;
      const hasLastOnlineDate = lastOnlineDate && !Number.isNaN(lastOnlineDate.getTime());
      const lastOnlineLabel = hasLastOnlineDate
        ? `Last online ${formatLastOnlineDate(lastOnlineDate)}`
        : "Last online unknown";
      const lastOnlineTitle = !hasLastOnlineDate
        ? "No saved player data was found"
        : `Last online ${new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(lastOnlineDate)}`;
      const canManagePlayers = document.querySelector(".players-page")?.dataset.canManage === "true";
      const statusBadges = [
        player.whitelisted ? '<span class="player-state-badge is-whitelisted"><i class="fa-solid fa-check" aria-hidden="true"></i>Whitelisted</span>' : "",
        player.operator ? `<span class="player-state-badge is-admin"><i class="fa-solid fa-shield-halved" aria-hidden="true"></i>OP${player.op_level == null ? "" : ` · L${escapeHtml(player.op_level)}`}</span>` : "",
        player.banned ? '<span class="player-state-badge is-blocked"><i class="fa-solid fa-ban" aria-hidden="true"></i>Blocked</span>' : "",
      ].filter(Boolean).join("") || '';

      return `
                    <div class="player-row">

                        <div class="player-identity">

                            <span class="
                                player-online-dot
                                ${player.online ? "online" : ""}
                            "></span>

                            ${
        avatar
          ? `
                                    <img
                                        class="player-avatar"
                                        src="${avatar}"
                                        alt=""
                                    >
                                `
          : `
                                    <div class="player-avatar placeholder">
                                        <i class="fa-solid fa-user"></i>
                                    </div>
                                `
      }

                            <div>

                                <strong>
                                    ${escapeHtml(player.name)}
                                </strong>
                                ${player.uuid ? `<small class="player-uuid">UUID: ${escapeHtml(player.uuid)}</small>` : ""}

                                <small>
                                    ${player.online ? "Online" : `Offline · <span class="player-last-online" title="${escapeHtml(lastOnlineTitle)}">${escapeHtml(lastOnlineLabel)}</span>`}
                                </small>

                            </div>

                        </div>


                        ${canManagePlayers && playerData.running ? `<div class="player-actions">

                            <button
                                class="
                                    player-pill
                                    ${player.whitelisted ? "positive" : ""}
                                "
                                ${!playerData.running ? "disabled" : ""}
                                onclick="
                                    playerAction(
                                        '${escapeJs(player.name)}',
                                        '${
        player.whitelisted ? "unwhitelist" : "whitelist"
      }'
                                    )
                                "
                            >
                                ${
        player.whitelisted ? "✓ Whitelisted" : "+ Whitelist"
      }
                            </button>


                            <button
                                class="
                                    player-pill
                                    ${player.operator ? "operator" : ""}
                                "
                                ${!playerData.running ? "disabled" : ""}
                                onclick="
                                    playerAction(
                                        '${escapeJs(player.name)}',
                                        '${player.operator ? "deop" : "op"}'
                                    )
                                "
                            >
                                ${player.operator ? "✓ OP" : "+ OP"}
                            </button>


                            ${
        player.operator
          ? `
                                    <span class="op-level">
                                        L${player.op_level ?? "-"}
                                    </span>
                                `
          : ""
      }


                            ${
        player.online
          ? `
                                    <button
                                        class="button secondary"
                                        onclick="
                                            playerAction(
                                                '${escapeJs(player.name)}',
                                                'kick'
                                            )
                                        "
                                    >
                                        Remove from game
                                    </button>
                                `
          : ""
      }


                            ${
        player.banned
          ? `
                                    <button
                                        ${!playerData.running ? "disabled" : ""}
                                        onclick="
                                            playerAction(
                                                '${escapeJs(player.name)}',
                                                'pardon'
                                            )
                                        "
                                    >
                                        Unblock
                                    </button>
                                `
          : `
                                    <button
                                        class="button danger"
                                        ${!playerData.running ? "disabled" : ""}
                                        onclick="
                                            playerAction(
                                                '${escapeJs(player.name)}',
                                                'ban'
                                            )
                                        "
                                    >
                                        Block
                                    </button>
                                `
      }

                        </div>` : `<div class="player-readonly-state">
                          <div class="player-state-badges">${statusBadges}</div>
                        </div>`}

                    </div>
                `;
    },
  )
    .join("");
}

function escapeJs(value) {
  return String(value)
    .replaceAll("\\", "\\\\")
    .replaceAll("'", "\\'");
}

async function playerAction(
  player,
  action,
) {
  const page = document.querySelector(
    ".players-page",
  );

  if (!page) {
    return false;
  }

  let reason;
  if (action === "ban" || action === "pardon") {
    const label = action === "ban" ? "ban" : "unban";
    reason = window.prompt(`Reason to ${label} ${player}:`);
    if (reason === null) return false;
    reason = reason.trim();
    if (!reason) {
      alert("A reason is required.");
      return false;
    }
  }

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/players/action`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        player,
        action,
        reason,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Player action failed",
    );

    return false;
  }

  setTimeout(
    updatePlayersPage,
    500,
  );
  return true;
}

function addPlayerAction(
  action,
) {
  const input = document.getElementById(
    "player-add-name",
  );

  const player = input?.value.trim();

  if (!player) {
    return;
  }

  playerAction(
    player,
    action,
  ).then((success) => {
    if (success) input.value = "";
  });
}

async function toggleWhitelist() {
  const page = document.querySelector(
    ".players-page",
  );

  const toggle = document.getElementById(
    "whitelist-toggle",
  );

  if (
    !page ||
    !toggle
  ) {
    return;
  }

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/whitelist-enabled`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        enabled: toggle.checked,
      }),
    },
  );

  if (!response.ok) {
    toggle.checked = !toggle.checked;

    const data = await response.json();

    alert(
      data.error ||
        "Unable to change whitelist",
    );
  }
}

document.addEventListener(
  "click",
  function (event) {
    const pageButton = event.target.closest(".player-page-button");
    if (pageButton && !pageButton.disabled) {
      playerPage += pageButton.dataset.pageDirection === "next" ? 1 : -1;
      renderPlayerList();
      return;
    }

    const button = event.target.closest(
      ".player-filter",
    );

    if (!button) {
      return;
    }

    document
      .querySelectorAll(
        ".player-filter",
      )
      .forEach(
        (item) =>
          item.classList.remove(
            "active",
          ),
      );

    button.classList.add(
      "active",
    );

    playerFilter = button.dataset.filter;
    playerPage = 1;

    renderPlayerList();
  },
);

document.addEventListener(
  "input",
  function (event) {
    if (
      event.target.id ===
        "player-search"
    ) {
      playerPage = 1;
      renderPlayerList();
    }
  },
);

document.addEventListener("change", (event) => {
  if (event.target.id !== "player-sort") return;
  playerSort = event.target.value;
  playerPage = 1;
  renderPlayerList();
});

updatePlayersPage();

async function ipBanAction(ip, action, reason = "") {
  const page = document.querySelector(".players-page");
  if (!page) return false;
  if (action === "pardon") {
    reason = window.prompt(`Reason to unban ${ip}:`);
    if (reason === null) return false;
  }
  reason = String(reason || "").trim();
  if (!reason) {
    alert("A reason is required.");
    return false;
  }
  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/ip-bans/action`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, action, reason }),
    },
  );
  const data = await response.json();
  if (!response.ok) {
    alert(data.error || "Unable to update IP ban");
    return false;
  }
  setTimeout(updatePlayersPage, 300);
  return true;
}

function banIpAddress(event) {
  event.preventDefault();
  const form = event.currentTarget;
  ipBanAction(form.elements.ip.value, "ban", form.elements.reason.value)
    .then((success) => { if (success) form.reset(); });
}

setInterval(
  updatePlayersPage,
  3000,
);

async function updateOverviewPlayers() {
  const overview = document.querySelector(
    ".server-overview",
  );

  if (!overview) {
    return;
  }

  try {
    const response = await fetch(
      `/api/web/servers/${overview.dataset.serverId}/players`,
    );

    if (!response.ok) {
      return;
    }

    const data = await response.json();

    setText(
      "overview-players",
      data.online_count,
    );

    setText(
      "overview-max-players",
      data.max_players,
    );
  } catch {
    // Leave existing values alone.
  }
}

updateOverviewPlayers();

setInterval(
  updateOverviewPlayers,
  3000,
);

function formatPluginModified(modifiedNs) {
  if (!modifiedNs) return "Unknown";
  const date = new Date(Number(modifiedNs) / 1_000_000);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

async function togglePlugin(
  filename,
  action,
) {
  const page = document.querySelector(
    ".plugins-page",
  );

  if (!page) {
    return;
  }

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/plugins/action`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        filename,
        action,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to change plugin",
    );

    return;
  }

  pluginRestartRequired = data.restart_required === true;

  showPluginRestartAlert();

  await updatePluginsPage();
  await refreshNotifications(false);
}

function openPluginRemoveModal(
  filename,
) {
  const plugin = pluginData.find(
    (item) => item.filename === filename,
  );

  if (!plugin) {
    return;
  }

  pluginPendingRemoval = plugin;
  pluginRemovalInProgress = false;
  setPluginRemoveBusy(false);
  const progress = document.getElementById("plugin-remove-status");
  const error = document.getElementById("plugin-remove-error");
  if (progress) progress.hidden = true;
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }

  document.getElementById(
    "remove-plugin-name",
  ).textContent = plugin.name;

  const checkbox = document.getElementById(
    "remove-plugin-config",
  );

  checkbox.checked = false;

  const option = document.getElementById(
    "remove-config-option",
  );

  const description = document.getElementById(
    "remove-config-description",
  );

  if (plugin.config_directory) {
    option.hidden = false;

    description.textContent =
      `This will also permanently delete plugins/${plugin.config_directory}/`;
  } else {
    option.hidden = true;

    description.textContent = "No matching plugin data directory was detected.";
  }

  document.getElementById(
    "plugin-remove-modal",
  ).hidden = false;
}

function closePluginRemoveModal() {
  if (pluginRemovalInProgress) return;
  setPluginRemoveBusy(false);
  document.getElementById(
    "plugin-remove-modal",
  ).hidden = true;

  pluginPendingRemoval = null;
  const progress = document.getElementById("plugin-remove-status");
  const error = document.getElementById("plugin-remove-error");
  if (progress) progress.hidden = true;
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
}

function setPluginRemoveBusy(busy) {
  const modal = document.getElementById("plugin-remove-modal");
  const card = modal?.querySelector(".modal-card");
  const removeButton = document.getElementById("confirm-plugin-remove");
  const removeButtonText = document.getElementById("plugin-remove-button-text");
  const spinner = document.getElementById("plugin-remove-spinner");
  const cancelButton = document.getElementById("cancel-plugin-remove");
  const configCheckbox = document.getElementById("remove-plugin-config");
  if (card) {
    if (busy) card.setAttribute("aria-busy", "true");
    else card.removeAttribute("aria-busy");
  }
  if (removeButton) removeButton.disabled = busy;
  if (removeButtonText) removeButtonText.textContent = busy ? "Removing…" : "Remove";
  if (spinner) spinner.hidden = !busy;
  if (cancelButton) cancelButton.disabled = busy;
  if (configCheckbox) configCheckbox.disabled = busy;
  const progress = document.getElementById("plugin-remove-status");
  if (progress) progress.hidden = !busy;
}

async function confirmPluginRemove() {
  if (!pluginPendingRemoval || pluginRemovalInProgress) {
    return;
  }

  const page = document.querySelector(
    ".plugins-page",
  );
  if (!page) return;

  const plugin = pluginPendingRemoval;
  const error = document.getElementById("plugin-remove-error");
  const progress = document.getElementById("plugin-remove-status");
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
  pluginRemovalInProgress = true;
  setPluginRemoveBusy(true);

  let removed = false;
  let data;
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/plugins/action`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          filename: plugin.filename,
          action: "remove",
          remove_config: document.getElementById("remove-plugin-config").checked,
        }),
      },
    );

    data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Unable to remove plugin");
    }
    removed = true;
  } catch (removeError) {
    if (progress) progress.hidden = true;
    if (error) {
      error.textContent = removeError.message || "Unable to remove plugin";
      error.hidden = false;
    }
  } finally {
    pluginRemovalInProgress = false;
    setPluginRemoveBusy(false);
  }

  if (!removed) return;

  pluginRestartRequired = data.restart_required === true;

  closePluginRemoveModal();

  showPluginRestartAlert();

  await updatePluginsPage();
  await refreshNotifications(false);
}

function showPluginRestartAlert() {
  const alert = document.getElementById(
    "plugin-restart-alert",
  );
  const icon = document.getElementById("plugin-change-alert-icon");
  const message = document.getElementById("plugin-change-alert-message");
  const label = document.getElementById("plugin-change-alert-label");

  if (!alert) return;
  alert.hidden = !pluginRestartRequired;
  if (!pluginRestartRequired || !icon || !message || !label) return;

  icon.className = pluginServerRunning
    ? "fa-solid fa-rotate"
    : "fa-solid fa-circle-info";
  message.textContent = pluginServerRunning
    ? "Plugin changes are pending. Restart the server to apply them."
    : "Plugin changes will apply when the server is next started.";
  label.textContent = pluginServerRunning
    ? "Restart required"
    : "Applies on next start";
}

document.addEventListener(
  "input",
  function (event) {
    if (
      event.target.id === "plugin-search"
    ) {
      renderPlugins();
    }
  },
);

updatePluginsPage();

async function updateOverviewPlugins() {
  const overview = document.querySelector(
    ".server-overview",
  );

  if (!overview) {
    return;
  }

  const serverId = overview.dataset.serverId;

  const active = document.getElementById("overview-plugin-active");
  const total = document.getElementById("overview-plugin-total");

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/plugins`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    const plugins = data.plugins || [];

    const enabledPlugins = plugins.filter(
      (plugin) => plugin.enabled,
    );

    if (active) active.textContent = enabledPlugins.length;
    if (total) total.textContent = plugins.length;

    const status = document.getElementById("overview-plugin-status");
    if (status) {
      const disabled = plugins.length - enabledPlugins.length;
      status.textContent = `${disabled} disabled`;
    }

    const bedrockPort = document.getElementById("overview-bedrock-port");
    const bedrockStatus = document.getElementById("overview-bedrock-status");
    if (bedrockPort && bedrockStatus) {
      bedrockPort.textContent = data.geyser?.port || "";
      bedrockStatus.textContent = !data.geyser?.installed
        ? "Geyser is not installed"
        : !data.geyser.enabled
        ? "Geyser is disabled"
        : data.geyser.port
        ? ""
        : "Geyser installed; port not detected";
    }
  } catch (error) {
    console.error(
      "Overview plugins error:",
      error,
    );

    if (active) active.textContent = "-";
    if (total) total.textContent = "-";
  }
}

updateOverviewPlugins();

function updateDocumentTitle() {
  const page = document.querySelector(
    "#page-content [data-page-title]",
  );

  if (!page) return;

  const pageTitle = page.dataset.pageTitle;
  let pageHasHeading = Boolean(page.querySelector("h1, .page-heading"));
  if (!pageHasHeading && pageTitle) {
    const heading = document.createElement("h1");
    heading.className = "page-title-heading";
    heading.textContent = pageTitle;
    page.prepend(heading);
    pageHasHeading = true;
  }
  document.title = `${pageTitle} | Craftarr`;
}

updateDocumentTitle();

function initializeCodeEditors(root = document) {
  if (!window.CraftarrCodeEditor) return;
  root.querySelectorAll("textarea.file-editor").forEach((textarea) => {
    const page = textarea.closest(".file-editor-page");
    const path = textarea.form?.querySelector('[name="path"]')?.value || textarea.dataset.filename || "";
    const storageKey = `craftarr.editor.${page?.dataset.serverId || ""}.${path}`;
    const warningElement = root.querySelector("[data-editor-warning]");
    const warning = warningElement ? {
      message: warningElement.dataset.message,
      line: Number(warningElement.dataset.line),
      column: Number(warningElement.dataset.column),
    } : null;
    const view = window.CraftarrCodeEditor.create(textarea, {
      filename: textarea.dataset.filename,
      warning,
      storageKey,
    });
    const form = textarea.form;
    if (form && !form.dataset.editorPositionReady) {
      form.dataset.editorPositionReady = "true";
      form.addEventListener("submit", () => {
        window.CraftarrCodeEditor?.savePosition(view, storageKey);
      });
    }
    if (form && !form.dataset.yamlCheckReady && /\.ya?ml$/i.test(textarea.dataset.filename || "")) {
      form.dataset.yamlCheckReady = "true";
      form.addEventListener("submit", (event) => validateFileYamlBeforeSave(event, textarea));
    }
  });
}

function showEditorYamlWarning(textarea, warning) {
  window.CraftarrCodeEditor?.showWarning(textarea._codeEditor, warning);
  let banner = document.querySelector("[data-editor-warning]");
  if (!warning) {
    if (banner) banner.remove();
    return;
  }
  if (!banner) {
    banner = document.createElement("div");
    banner.className = "editor-warning";
    banner.dataset.editorWarning = "true";
    document.querySelector(".file-editor-page")?.prepend(banner);
  }
  banner.textContent = `${warning.message} at line ${warning.line}, column ${warning.column}.`;
}

async function validateFileYamlBeforeSave(event, textarea) {
  const form = event.currentTarget;
  if (form.dataset.yamlCheckBypass === "true") {
    delete form.dataset.yamlCheckBypass;
    return;
  }
  event.preventDefault();
  const page = document.querySelector(".file-editor-page");
  const submitter = event.submitter;
  try {
    const response = await fetch(`/api/web/servers/${page.dataset.serverId}/files/yaml-check`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({content: textarea.value}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to check YAML");
    showEditorYamlWarning(textarea, data.warning);
    if (data.warning && !window.confirm(
      `${data.warning.message}\nLine ${data.warning.line}, column ${data.warning.column}.\n\nSave anyway?`,
    )) return;
  } catch (error) {
    if (!window.confirm(`${error.message}\n\nThe YAML check could not be completed. Save anyway?`)) return;
  }
  form.dataset.yamlCheckBypass = "true";
  form.requestSubmit(submitter || undefined);
}

initializeCodeEditors();
window.addEventListener("craftarr:editor-ready", () => initializeCodeEditors());
document.body.addEventListener("htmx:afterSwap", (event) => {
  initializeCodeEditors(event.detail.target || document);
});

let fileConflictResolver = null;

function askFileConflict(title, message, folders = true) {
  const modal = document.getElementById("file-conflict-modal");
  if (!modal) return Promise.resolve("cancel");
  document.getElementById("file-conflict-title").textContent = title;
  document.getElementById("file-conflict-message").textContent = message;
  const merge = document.getElementById("file-conflict-merge");
  const replace = document.getElementById("file-conflict-replace");
  merge.textContent = folders ? "Merge & Replace" : "Replace";
  replace.hidden = !folders;
  modal.hidden = false;
  return new Promise((resolve) => {
    fileConflictResolver = resolve;
  });
}

function resolveFileConflict(mode) {
  const modal = document.getElementById("file-conflict-modal");
  if (modal) modal.hidden = true;
  if (fileConflictResolver) fileConflictResolver(mode);
  fileConflictResolver = null;
}

function filesWithPaths(files, folderUpload = false) {
  return Array.from(files).map((file) => ({
    file,
    path: folderUpload && file.webkitRelativePath
      ? file.webkitRelativePath
      : file.name,
  }));
}

function currentFileNames() {
  return new Set(Array.from(document.querySelectorAll("#file-browser .file-row[data-name]"))
    .map((row) => row.dataset.name));
}

function showFileOperationProgress(title, message, percent = null, detail = "", allowClose = false) {
  const modal = document.getElementById("file-operation-progress");
  const bar = document.getElementById("file-operation-progress-bar");
  const label = document.getElementById("file-operation-progress-percent");
  const detailLabel = document.getElementById("file-operation-progress-detail");
  const actions = document.getElementById("file-operation-progress-actions");
  const track = modal?.querySelector('[role="progressbar"]');
  if (!modal || !bar || !label) return;
  document.getElementById("file-operation-progress-title").textContent = title;
  document.getElementById("file-operation-progress-message").textContent = message;
  if (detailLabel) {
    detailLabel.textContent = detail;
    detailLabel.hidden = !detail;
  }
  if (actions) actions.hidden = !allowClose;
  bar.classList.toggle("upload-progress-failed", allowClose);
  bar.classList.toggle("indeterminate", percent === null);
  if (percent === null) {
    bar.style.width = "35%";
    label.textContent = "In progress";
    track?.removeAttribute("aria-valuenow");
    track?.removeAttribute("aria-valuetext");
  } else {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    bar.style.width = `${value}%`;
    label.textContent = `${Math.round(value)}%`;
    track?.setAttribute("aria-valuenow", String(Math.round(value)));
    track?.setAttribute("aria-valuetext", detail || `${Math.round(value)}% complete`);
  }
  modal.hidden = false;
}

function hideFileOperationProgress() {
  const modal = document.getElementById("file-operation-progress");
  if (modal) modal.hidden = true;
  const actions = document.getElementById("file-operation-progress-actions");
  if (actions) actions.hidden = true;
  document.getElementById("file-operation-progress-bar")?.classList.remove("upload-progress-failed");
}

function closeFileOperationProgress() {
  hideFileOperationProgress();
  if (serverZipImportLocked) unlockServerZipImportNavigation();
}

function uploadFileWithProgress(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", url);
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    });
    request.addEventListener("load", () => {
      let responsePath = "";
      try {
        responsePath = new URL(request.responseURL, window.location.href).pathname;
      } catch {
        // Use the status code when the response URL cannot be parsed.
      }
      if (request.status === 401 || ["/login", "/login/tfa"].includes(responsePath)) {
        redirectToLogin();
        reject(new Error("Your session expired. Sign in again to continue."));
        return;
      }
      if (responsePath === "/change-password") {
        window.location.replace(request.responseURL);
      }
      resolve(request);
    });
    request.addEventListener("error", () => reject(new Error("Upload failed. Check the connection and try again.")));
    request.addEventListener("abort", () => reject(new Error("Upload was cancelled.")));
    request.send(form);
  });
}

function joinFilePath(parent, child) {
  return parent ? `${parent}/${child}` : child;
}

async function deleteUploadConflicts(page, names) {
  for (const name of names) {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/files/delete`,
      {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path: joinFilePath(page.dataset.currentPath || "", name)}),
      },
    );
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || `Unable to replace ${name}`);
    }
  }
}

async function uploadFiles(files, folderUpload = false) {
  const page = currentFilesPage();

  const uploads = Array.isArray(files) && files.length && files[0].file
    ? files
    : filesWithPaths(files, folderUpload);

  if (
    !page ||
    !uploads.length
  ) {
    return;
  }

  const roots = new Set(uploads.map((upload) => upload.path.split("/")[0]));
  const conflicts = [...roots].filter((name) => currentFileNames().has(name));
  const containsFolders = uploads.some((upload) => upload.path.includes("/"));
  let mode = "new";

  if (conflicts.length) {
    mode = await askFileConflict(
      containsFolders ? "Folder already exists" : "File already exists",
      containsFolders
        ? "Merge and replace matching files, or fully replace the existing folder?"
        : "Replace the existing file?",
      containsFolders,
    );
    if (mode === "cancel") return;
  }

  try {
    if (mode === "replace") {
      await deleteUploadConflicts(page, conflicts);
    }

    const totalBytes = uploads.reduce((total, upload) => total + upload.file.size, 0);
    let completedBytes = 0;
    showFileOperationProgress(
      folderUpload ? "Uploading folder" : "Uploading files",
      `Uploading 0 of ${uploads.length} files…`,
      0,
    );

    for (let index = 0; index < uploads.length; index += 1) {
      const upload = uploads[index];
      const form = new FormData();
      form.append("path", page.dataset.currentPath || "");
      form.append("file", upload.file);
      form.append("relative_path", upload.path);
      form.append("replace", mode === "merge" ? "true" : "false");

      await uploadFileWithProgress(
        `/servers/${page.dataset.serverId}/files/upload`,
        form,
        (loaded) => {
          const percent = totalBytes ? ((completedBytes + loaded) / totalBytes) * 100 : 100;
          showFileOperationProgress(
            folderUpload ? "Uploading folder" : "Uploading files",
            `Uploading ${index + 1} of ${uploads.length}: ${upload.file.name}`,
            percent,
          );
        },
      );

      completedBytes += upload.file.size;
    }
    hideFileOperationProgress();

  } catch (error) {
    hideFileOperationProgress();
    alert(error.message || "Unable to upload files");
    return;
  }

  reloadFilesPage();
}

function openUnknownTextFile(event, path) {
  event.preventDefault();
  if (!window.confirm("This file type is not recognised. Open it as text anyway?")) {
    return false;
  }
  const page = currentFilesPage();
  const url = `/servers/${page.dataset.serverId}/files/edit?path=${encodeURIComponent(path)}&confirm_unknown=true`;
  htmx.ajax("GET", url, {target: "#page-content", swap: "innerHTML"});
  history.pushState({}, "", url);
  return false;
}

function openFileImagePreview(path, name) {
  const page = currentFilesPage();
  const modal = document.getElementById("file-image-preview-modal");
  const image = document.getElementById("file-image-preview-image");
  const title = document.getElementById("file-image-preview-title");
  const status = document.getElementById("file-image-preview-status");
  const error = document.getElementById("file-image-preview-error");
  if (!page || !modal || !image) return;

  fileImagePreviewPreviousFocus = document.activeElement;
  title.textContent = name;
  image.alt = name;
  image.hidden = false;
  image.onload = () => { status.hidden = true; };
  image.onerror = () => {
    image.hidden = true;
    status.hidden = true;
    error.hidden = false;
  };
  status.hidden = false;
  error.hidden = true;
  modal.hidden = false;
  document.getElementById("file-image-preview-close")?.focus({preventScroll: true});
  image.src = `/servers/${page.dataset.serverId}/files/preview?path=${encodeURIComponent(path)}`;
}

function closeFileImagePreview() {
  const modal = document.getElementById("file-image-preview-modal");
  const image = document.getElementById("file-image-preview-image");
  const status = document.getElementById("file-image-preview-status");
  const error = document.getElementById("file-image-preview-error");
  if (!modal) return;
  modal.hidden = true;
  if (image) {
    image.onload = null;
    image.onerror = null;
    image.removeAttribute("src");
  }
  if (status) status.hidden = false;
  if (error) error.hidden = true;
  if (fileImagePreviewPreviousFocus?.isConnected) fileImagePreviewPreviousFocus.focus();
  fileImagePreviewPreviousFocus = null;
}

document.addEventListener("click", (event) => {
  const previewButton = event.target.closest(".file-image-preview-button");
  if (previewButton) openFileImagePreview(previewButton.dataset.previewPath, previewButton.dataset.previewName);
});

async function zipFileEntry(path) {
  const page = currentFilesPage();
  showFileOperationProgress("Creating ZIP", `Compressing ${path}…`);
  try {
    const response = await fetch(`/api/web/servers/${page.dataset.serverId}/files/zip`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to create ZIP");
    reloadFilesPage();
  } catch (error) {
    alert(error.message || "Unable to create ZIP");
  } finally {
    hideFileOperationProgress();
  }
}

async function extractZipEntry(path) {
  const page = currentFilesPage();
  const endpoint = `/api/web/servers/${page.dataset.serverId}/files/extract`;
  let response = await fetch(endpoint, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path, mode: "check"}),
  });
  let data = await response.json();
  if (!response.ok) return alert(data.error || "Unable to inspect ZIP");

  let mode = "merge";
  if (data.conflicts.length) {
    mode = await askFileConflict(
      "Items already exist",
      "Merge and replace matching files, or fully replace the conflicting folders and files?",
      true,
    );
    if (mode === "cancel") return;
  }
  response = await fetch(endpoint, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path, mode}),
  });
  data = await response.json();
  if (!response.ok) return alert(data.error || "Unable to extract ZIP");
  reloadFilesPage();
}

let internalDraggedPath = null;

function handleInternalDragStart(event) {
  const row = event.currentTarget;

  internalDraggedPath = row.dataset.path;

  event.dataTransfer.effectAllowed = "move";

  event.dataTransfer.setData(
    "text/plain",
    internalDraggedPath,
  );
}

function handleFolderDragOver(event) {
  if (!internalDraggedPath) {
    return;
  }

  event.preventDefault();
  event.stopPropagation();

  event.currentTarget.classList.add(
    "drag-target",
  );

  event.dataTransfer.dropEffect = "move";
}

function handleFolderDragLeave(event) {
  event.currentTarget.classList.remove(
    "drag-target",
  );
}

async function handleFolderDrop(event) {
  if (!internalDraggedPath) {
    return;
  }

  event.preventDefault();
  event.stopPropagation();

  event.currentTarget.classList.remove(
    "drag-target",
  );

  const page = currentFilesPage();

  const destination = event.currentTarget.dataset.folderPath;

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/files/move`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        source: internalDraggedPath,

        destination,
      }),
    },
  );

  const data = await response.json();

  internalDraggedPath = null;

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to move file",
    );

    return;
  }

  reloadFilesPage();
}

function handleFileBrowserDragOver(
  event,
) {
  const hasFiles = Array.from(
    event.dataTransfer.types,
  ).includes("Files");

  if (!hasFiles) {
    return;
  }

  event.preventDefault();

  event.currentTarget.classList.add(
    "external-drag",
  );

  event.dataTransfer.dropEffect = "copy";
}

function handleFileBrowserDragLeave(
  event,
) {
  if (
    !event.currentTarget.contains(
      event.relatedTarget,
    )
  ) {
    event.currentTarget.classList.remove(
      "external-drag",
    );
  }
}

async function handleFileBrowserDrop(
  event,
) {
  event.preventDefault();

  event.currentTarget.classList.remove(
    "external-drag",
  );

  if (internalDraggedPath) {
    return;
  }

  const items = Array.from(event.dataTransfer.items || []);
  const entries = items.map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);

  if (entries.some((entry) => entry.isDirectory)) {
    const uploads = [];
    for (const entry of entries) {
      await collectDroppedFiles(entry, "", uploads);
    }
    uploadFiles(uploads, true);
    return;
  }

  const files = event.dataTransfer.files;

  if (
    files &&
    files.length
  ) {
    uploadFiles(
      files,
    );
  }
}

async function collectDroppedFiles(entry, parent, uploads) {
  const path = parent ? `${parent}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    uploads.push({file, path});
    return;
  }
  const reader = entry.createReader();
  while (true) {
    const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!children.length) break;
    for (const child of children) {
      await collectDroppedFiles(child, path, uploads);
    }
  }
}

function handleFolderRowDragOver(event) {
  if (!internalDraggedPath) {
    return;
  }

  const row = event.currentTarget;

  const destination = row.dataset.folderPath;

  if (
    !destination ||
    destination === internalDraggedPath
  ) {
    return;
  }

  event.preventDefault();
  event.stopPropagation();

  row.classList.add(
    "drag-target",
  );

  const icon = row.querySelector(
    "[data-folder-icon]",
  );

  if (icon) {
    icon.classList.remove(
      "fa-folder",
    );

    icon.classList.add(
      "fa-folder-open",
    );
  }

  event.dataTransfer.dropEffect = "move";
}

function handleFolderRowDragLeave(event) {
  const row = event.currentTarget;

  if (
    row.contains(
      event.relatedTarget,
    )
  ) {
    return;
  }

  clearFolderDropTarget(
    row,
  );
}

function clearFolderDropTarget(row) {
  row.classList.remove(
    "drag-target",
  );

  const icon = row.querySelector(
    "[data-folder-icon]",
  );

  if (icon) {
    icon.classList.remove(
      "fa-folder-open",
    );

    icon.classList.add(
      "fa-folder",
    );
  }
}

async function handleFolderRowDrop(event) {
  if (!internalDraggedPath) {
    return;
  }

  event.preventDefault();
  event.stopPropagation();

  const row = event.currentTarget;

  const destination = row.dataset.folderPath;

  clearFolderDropTarget(
    row,
  );

  if (
    !destination ||
    destination === internalDraggedPath
  ) {
    internalDraggedPath = null;
    return;
  }

  const page = currentFilesPage();

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/files/move`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        source: internalDraggedPath,

        destination,
      }),
    },
  );

  const data = await response.json();

  internalDraggedPath = null;

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to move file",
    );

    return;
  }

  reloadFilesPage();
}

document.addEventListener(
  "dragend",
  function () {
    internalDraggedPath = null;

    document
      .querySelectorAll(
        ".file-folder-row.drag-target",
      )
      .forEach(
        clearFolderDropTarget,
      );
  },
);

let backupData = [];
let backupPendingRestore = null;
let backupPendingDelete = null;

async function updateBackupsPage() {
  const page = document.querySelector(
    ".backups-page",
  );

  if (!page) {
    return;
  }

  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/backups`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    backupData = data.backups || [];

    const warning = document.getElementById(
      "backup-running-warning",
    );

    if (warning) {
      warning.hidden = !data.running;
    }

    renderBackups(
      data.running,
    );
  } catch {
    const list = document.getElementById(
      "backup-list",
    );

    if (list) {
      list.textContent = "Unable to load backups.";
    }
  }
}

function renderBackups(
  serverRunning,
) {
  const list = document.getElementById(
    "backup-list",
  );

  if (!list) {
    return;
  }

  const count = document.getElementById(
    "backup-count",
  );

  if (count) {
    count.textContent = `${backupData.length} ${
      backupData.length === 1 ? "backup" : "backups"
    }`;
  }

  if (!backupData.length) {
    list.innerHTML = `
            <div class="empty-message backup-empty">
                No backups created yet.
            </div>
            `;

    return;
  }

  list.innerHTML = backupData.map(
    (backup) => `
                <div class="backup-row">

                    <div class="backup-main">

                        <i class="fa-solid fa-box-archive"></i>

                        <div>

                            <strong>
                                ${escapeHtml(backup.filename)}
                            </strong>

                            <small>
                                ${escapeHtml(backup.created_display)}
                                ·
                                ${escapeHtml(backup.size_display)}
                            </small>

                        </div>

                    </div>


                    <div class="backup-actions">

                        <a
                            class="button backup-action-button"
                            href="/servers/${
      document.querySelector(".backups-page").dataset.serverId
    }/backups/download?filename=${encodeURIComponent(backup.filename)}"
                            aria-label="Download backup ${escapeHtml(backup.filename)}"
                        >
                            <i class="fa-solid fa-download" aria-hidden="true"></i><span>Download</span>
                        </a>

                        <button
                            class="button backup-action-button"
                            ${serverRunning ? "disabled" : ""}
                            onclick="openRestoreBackupModal(
                                '${escapeJs(backup.filename)}'
                            )"
                            aria-label="Restore backup ${escapeHtml(backup.filename)}"
                        >
                            <i class="fa-solid fa-clock-rotate-left" aria-hidden="true"></i><span>Restore</span>
                        </button>

                        <details class="backup-more-actions">
                          <summary aria-label="More actions for ${escapeHtml(backup.filename)}" title="More actions"><i class="fa-solid fa-ellipsis" aria-hidden="true"></i></summary>
                          <div class="backup-more-menu"><button type="button" onclick="this.closest('details').open=false; openDeleteBackupModal('${escapeJs(backup.filename)}')"><i class="fa-solid fa-trash" aria-hidden="true"></i> Delete backup</button></div>
                        </details>

                    </div>

                </div>
            `,
  )
    .join("");
}

function openCreateBackupModal() {
  document.getElementById(
    "backup-label",
  ).value = "";

  document.getElementById(
    "backup-create-form",
  ).hidden = false;

  document.getElementById(
    "backup-create-progress",
  ).hidden = true;

  document.getElementById(
    "create-backup-modal",
  ).hidden = false;
}

function closeCreateBackupModal() {
  document.getElementById(
    "create-backup-modal",
  ).hidden = true;
}

let activeBackupJobId = null;

async function createBackup() {
  const page = document.querySelector(
    ".backups-page",
  );

  if (!page) {
    return;
  }

  const label = document
    .getElementById(
      "backup-label",
    )
    .value
    .trim();

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/backups/create`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        label,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to create backup",
    );

    return;
  }

  activeBackupJobId = data.job_id;

  document.getElementById(
    "backup-create-form",
  ).hidden = true;

  document.getElementById(
    "backup-create-progress",
  ).hidden = false;

  updateBackupJobs();
}

function openRestoreBackupModal(
  filename,
) {
  backupPendingRestore = filename;

  document.getElementById(
    "restore-backup-name",
  ).textContent = filename;

  document.getElementById(
    "restore-backup-modal",
  ).hidden = false;
}

function closeRestoreBackupModal() {
  document.getElementById(
    "restore-backup-modal",
  ).hidden = true;

  backupPendingRestore = null;
}

async function confirmRestoreBackup() {
  if (!backupPendingRestore) {
    return;
  }

  const page = document.querySelector(
    ".backups-page",
  );

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/backups/restore`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        filename: backupPendingRestore,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to restore backup",
    );

    return;
  }

  closeRestoreBackupModal();

  updateBackupsPage();
}

function openDeleteBackupModal(
  filename,
) {
  backupPendingDelete = filename;

  document.getElementById(
    "delete-backup-name",
  ).textContent = filename;

  document.getElementById(
    "delete-backup-modal",
  ).hidden = false;
}

function closeDeleteBackupModal() {
  document.getElementById(
    "delete-backup-modal",
  ).hidden = true;

  backupPendingDelete = null;
}

async function confirmDeleteBackup() {
  if (!backupPendingDelete) {
    return;
  }

  const page = document.querySelector(
    ".backups-page",
  );

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/backups/delete`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        filename: backupPendingDelete,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to delete backup",
    );

    return;
  }

  closeDeleteBackupModal();

  updateBackupsPage();
}

updateBackupsPage();

async function updatePropertiesPage() {
  const page = document.querySelector(
    ".properties-page",
  );

  if (!page) {
    return;
  }

  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/properties`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    const p = data.properties;
    const startup = data.startup || {};
    const management = data.management || {};

    setValue("property-server-name", management.name || "");
    const instanceDirectory = document.getElementById("property-instance-directory");
    if (instanceDirectory) instanceDirectory.textContent = management.directory || "";
    setValue("property-process-backend", management.process_backend || "subprocess");
    const backendSelect = document.getElementById("property-process-backend");
    if (backendSelect) {
      backendSelect.dataset.systemdAvailable = String(management.systemd_available !== false);
    }
    const systemdOption = backendSelect?.querySelector('option[value="systemd"]');
    if (systemdOption) {
      systemdOption.disabled = management.systemd_available === false;
    }
    const systemdNotice = document.getElementById("property-systemd-unavailable");
    if (systemdNotice) {
      systemdNotice.hidden = management.systemd_available !== false;
    }
    const serviceName = document.getElementById("property-service-name");
    if (serviceName) {
      serviceName.textContent = management.unit_name || management.service_name || "—";
    }
    setChecked("property-systemd-enabled", management.enabled_at_boot);
    const serviceState = document.getElementById("property-service-state");
    if (serviceState) {
      const runtimeState = management.running ? "Running" : "Stopped";
      const bootState = management.enabled_at_boot === true
        ? "enabled at boot"
        : management.enabled_at_boot === false
          ? "disabled at boot"
          : "boot status unavailable";
      serviceState.textContent = `${runtimeState} · ${bootState}`;
    }
    updateProcessManagementFields();

    setValue("property-min-memory", startup.min_memory || "2G");
    setValue("property-max-memory", startup.max_memory || "2G");
    setValue("property-jar-name", startup.jar_name || "paper.jar");
    setValue("property-java-args", startup.java_args || "");
    setValue("property-stop-commands", startup.stop_commands || "");
    const javaSelect = document.getElementById("property-java-path");
    if (javaSelect) {
      javaSelect.replaceChildren(...(startup.java_runtimes || []).map((runtime) => {
        const option = document.createElement("option");
        option.value = runtime.major;
        option.textContent = runtime.label || `Java ${runtime.major}`;
        option.selected = runtime.major === startup.java_major;
        return option;
      }));
    }

    const jarList = document.getElementById("server-jar-files");
    if (jarList) {
      jarList.replaceChildren(
        ...(startup.jar_files || []).map((name) => {
          const option = document.createElement("option");
          option.value = name;
          return option;
        }),
      );
    }

    const commandPreview = document.getElementById("startup-command-preview");
    if (commandPreview && Array.isArray(startup.command)) {
      commandPreview.textContent = startup.command.join(" ");
    }

    setValue(
      "property-motd",
      p.motd,
    );

    setValue(
      "property-server-port",
      p.server_port,
    );
    checkServerPortWarning(p.server_port, page.dataset.serverId);

    setValue(
      "property-max-players",
      p.max_players,
    );

    setValue(
      "property-difficulty",
      p.difficulty,
    );

    setValue(
      "property-gamemode",
      p.gamemode,
    );

    setChecked(
      "property-online-mode",
      p.online_mode,
    );

    setChecked(
      "property-enforce-secure-profile",
      p.enforce_secure_profile,
    );

    setValue(
      "property-level-name",
      p.level_name,
    );

    setValue(
      "property-level-seed",
      p.level_seed,
    );

    setValue(
      "property-view-distance",
      p.view_distance,
    );

    setValue(
      "property-simulation-distance",
      p.simulation_distance,
    );

    setValue(
      "property-spawn-protection",
      p.spawn_protection,
    );

    setChecked(
      "property-allow-nether",
      p.allow_nether,
    );

    setChecked(
      "property-pvp",
      p.pvp,
    );

    setChecked(
      "property-hardcore",
      p.hardcore,
    );

    setChecked(
      "property-command-block",
      p.enable_command_block,
    );

    setChecked(
      "property-allow-flight",
      p.allow_flight,
    );

    setChecked(
      "property-whitelist",
      p.white_list,
    );

    setChecked(
      "property-enable-query",
      p.enable_query,
    );

    setChecked(
      "property-enable-rcon",
      p.enable_rcon,
    );

    setValue(
      "property-resource-pack",
      p.resource_pack,
    );
  } catch {
    const status = document.getElementById(
      "properties-save-status",
    );

    if (status) {
      status.textContent = "Unable to load properties.";
    }
  }
}

async function loadAdvancedProperties() {
  const page = document.querySelector(".advanced-properties-page");
  const container = document.getElementById("advanced-properties-groups");
  if (!page || !container) return;
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/advanced-properties`,
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load advanced properties.");
    container.replaceChildren();
    if (!(data.groups || []).length) {
      container.innerHTML = '<div class="empty-message">No supported YAML configuration files have been generated yet. Start the server once, then return here.</div>';
      return;
    }
    (data.groups || []).forEach((group, groupIndex) => {
      const section = document.createElement("details");
      section.className = "advanced-property-group";
      section.open = groupIndex === 0;
      const summary = document.createElement("summary");
      summary.textContent = `${group.name} (${group.files.length})`;
      section.appendChild(summary);
      group.files.forEach((file) => {
        const editor = document.createElement("div");
        editor.className = "advanced-property-editor";
        editor.dataset.path = file.path;
        const heading = document.createElement("div");
        heading.className = "advanced-property-heading";
        const title = document.createElement("div");
        const strong = document.createElement("strong");
        strong.textContent = file.label;
        const path = document.createElement("small");
        path.textContent = file.path;
        const language = document.createElement("small");
        language.className = "code-editor-language";
        title.append(strong, path, language);
        const status = document.createElement("span");
        status.className = "muted-small advanced-property-status";
        heading.append(title, status);
        const textarea = document.createElement("textarea");
        textarea.className = "advanced-property-content";
        textarea.value = file.content;
        textarea.spellcheck = false;
        const actions = document.createElement("div");
        actions.className = "advanced-property-actions";
        const save = document.createElement("button");
        save.type = "button";
        save.className = "button";
        save.textContent = "Save file";
        save.addEventListener("click", () => saveAdvancedProperty(editor, save));
        actions.appendChild(save);
        editor.append(heading, textarea, actions);
        section.appendChild(editor);
        if (window.CraftarrCodeEditor) {
          window.CraftarrCodeEditor.create(textarea, {
            filename: file.path,
            languageLabel: language,
            onSave: () => saveAdvancedProperty(editor, save),
          });
        }
      });
      container.appendChild(section);
    });
  } catch (error) {
    container.innerHTML = `<div class="empty-message">${escapeHtml(error.message)}</div>`;
  }
}

async function saveAdvancedProperty(editor, button) {
  const page = document.querySelector(".advanced-properties-page");
  const textarea = editor.querySelector(".advanced-property-content");
  const status = editor.querySelector(".advanced-property-status");
  if (!page || !textarea || !status) return;
  button.disabled = true;
  status.textContent = "Checking YAML...";
  try {
    let response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/advanced-properties`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: editor.dataset.path,
          content: textarea.value,
          validate_only: true,
        }),
      },
    );
    let data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to check YAML.");
    window.CraftarrCodeEditor?.showWarning(textarea._codeEditor, data.warning);
    if (data.warning) {
      status.textContent = `Potential issue · line ${data.warning.line}, column ${data.warning.column}`;
      status.classList.add("warning-text");
      if (!window.confirm(
        `${data.warning.message}\nLine ${data.warning.line}, column ${data.warning.column}.\n\nSave anyway?`,
      )) return;
    }
    status.textContent = "Saving...";
    response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/advanced-properties`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: editor.dataset.path, content: textarea.value }),
      },
    );
    data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to save configuration.");
    if (data.warning) {
      status.textContent = `Saved with warning · line ${data.warning.line}, column ${data.warning.column}`;
      status.classList.add("warning-text");
    } else {
      status.textContent = data.running ? "Saved · restart required" : "Saved";
      status.classList.remove("warning-text");
    }
    window.CraftarrCodeEditor?.showWarning(textarea._codeEditor, data.warning);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function setValue(
  id,
  value,
) {
  const element = document.getElementById(id);

  if (element) {
    element.value = value ?? "";
  }
}

function setChecked(
  id,
  value,
) {
  const element = document.getElementById(id);

  if (element) {
    element.checked = value === true;
  }
}

let portWarningTimer = null;

function checkServerPortWarning(port, excludeServerId = null) {
  clearTimeout(portWarningTimer);
  portWarningTimer = setTimeout(async () => {
    const notice = document.getElementById("server-port-warning");
    if (!notice) return;
    const numericPort = Number(port);
    if (!Number.isInteger(numericPort) || numericPort < 1 || numericPort > 65535) {
      notice.hidden = true;
      return;
    }
    const params = new URLSearchParams({ port: String(numericPort) });
    if (excludeServerId) params.set("exclude_server_id", String(excludeServerId));
    try {
      const response = await fetch(`/api/web/servers/port-warning?${params}`);
      const data = await response.json();
      notice.textContent = data.warning || "";
      notice.hidden = !data.warning;
    } catch {
      notice.hidden = true;
    }
  }, 250);
}

async function saveServerProperties(
  event,
  restartIfRunning = false,
) {
  event?.preventDefault();

  const page = document.querySelector(
    ".properties-page",
  );

  if (!page) {
    return;
  }

  const payload = {
    restart_if_running: restartIfRunning,

    process_backend: valueOf("property-process-backend"),

    enabled_at_boot: checkedOf("property-systemd-enabled"),

    min_memory: normalizedStartupMemory("property-min-memory"),

    max_memory: normalizedStartupMemory("property-max-memory"),

    jar_name: valueOf("property-jar-name"),

    java_args: valueOf("property-java-args"),

    stop_commands: valueOf("property-stop-commands"),

    java_major: numberOf("property-java-path"),

    motd: valueOf(
      "property-motd",
    ),

    server_port: numberOf(
      "property-server-port",
    ),

    max_players: numberOf(
      "property-max-players",
    ),

    difficulty: valueOf(
      "property-difficulty",
    ),

    gamemode: valueOf(
      "property-gamemode",
    ),

    online_mode: checkedOf(
      "property-online-mode",
    ),

    enforce_secure_profile: checkedOf(
      "property-enforce-secure-profile",
    ),

    level_name: valueOf(
      "property-level-name",
    ),

    level_seed: valueOf(
      "property-level-seed",
    ),

    view_distance: numberOf(
      "property-view-distance",
    ),

    simulation_distance: numberOf(
      "property-simulation-distance",
    ),

    spawn_protection: numberOf(
      "property-spawn-protection",
    ),

    allow_nether: checkedOf(
      "property-allow-nether",
    ),

    pvp: checkedOf(
      "property-pvp",
    ),

    hardcore: checkedOf(
      "property-hardcore",
    ),

    enable_command_block: checkedOf(
      "property-command-block",
    ),

    allow_flight: checkedOf(
      "property-allow-flight",
    ),

    white_list: checkedOf(
      "property-whitelist",
    ),

    enable_query: checkedOf(
      "property-enable-query",
    ),

    enable_rcon: checkedOf(
      "property-enable-rcon",
    ),

    resource_pack: valueOf(
      "property-resource-pack",
    ),
  };

  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/properties`,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify(
        payload,
      ),
    },
  );

  const data = await response.json();

  const status = document.getElementById(
    "properties-save-status",
  );

  if (!response.ok) {
    if (data.restart_confirmation_required) {
      openPropertiesRestartModal();
      return;
    }
    showFormError(
      document.getElementById("properties-form"),
      data.error || "Save failed.",
      data.field,
    );
    if (status) {
      status.textContent = data.error ||
        "Save failed.";
    }

    return;
  }

  if (status) {
    status.textContent = "Saved";
  }
  if (data.warning) showToast(data.warning, "warning");
  clearFormErrors(document.getElementById("properties-form"));

  const restartAlert = document.getElementById(
    "properties-restart-alert",
  );

  if (restartAlert) {
    const message = document.getElementById("properties-pending-message");
    const label = document.getElementById("properties-pending-label");
    if (data.restarted) {
      restartAlert.hidden = true;
      if (status) status.textContent = "Saved · server restarted";
    } else if (data.running) {
      restartAlert.hidden = false;
      if (message) message.textContent = "Property changes are pending. Restart the server to apply them.";
      if (label) label.textContent = "Restart required";
    } else {
      restartAlert.hidden = true;
    }
  }
}

async function renameServer() {
  await submitServerRename(false);
}

async function submitServerRename(confirm) {
  const page = document.querySelector(".properties-page");
  const input = document.getElementById("property-server-name");
  const button = document.getElementById("rename-server-button");
  if (!page || !input || !button || !input.reportValidity()) return;
  button.disabled = true;
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/name`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: input.value, confirm }),
      },
    );
    const data = await response.json();
    if (!response.ok) {
      if (data.rename_confirmation_required) {
        openRenameServerModal(data);
        return;
      }
      showFormError(
        document.getElementById("properties-form"),
        data.error || "Unable to rename server.",
        "server_name",
      );
      return;
    }
    input.value = data.server_name;
    const activeName = document.querySelector(
      "#server-selector-toggle .sidebar-server-copy strong",
    );
    if (activeName) activeName.textContent = data.server_name;
    document.querySelectorAll(`#server-menu a[href="/servers/${page.dataset.serverId}"] strong`)
      .forEach((name) => { name.textContent = data.server_name; });
    const instanceDirectory = document.getElementById("property-instance-directory");
    if (instanceDirectory) instanceDirectory.textContent = data.directory;
    const serviceName = document.getElementById("property-service-name");
    if (serviceName) serviceName.textContent = data.service_name;
    if (data.warning) showToast(data.warning, "warning");
    clearFieldError(input);
  } finally {
    button.disabled = false;
  }
}

function openRenameServerModal(data) {
  const modal = document.getElementById("rename-server-modal");
  const directory = document.getElementById("rename-server-directory");
  const service = document.getElementById("rename-server-service");
  const runningNote = document.getElementById("rename-server-running-note");
  if (!modal || !directory || !service || !runningNote) return;
  directory.textContent = data.directory;
  service.textContent = data.service_name;
  runningNote.textContent = data.running
    ? "The running server will be stopped safely and restarted after the rename. Players will be disconnected."
    : "The server is stopped and will remain stopped after the rename.";
  modal.hidden = false;
}

function closeRenameServerModal() {
  const modal = document.getElementById("rename-server-modal");
  if (modal) modal.hidden = true;
}

async function confirmServerRename() {
  const button = document.getElementById("confirm-server-rename");
  if (!button) return;
  button.disabled = true;
  closeRenameServerModal();
  try {
    await submitServerRename(true);
  } finally {
    button.disabled = false;
  }
}

function openPropertiesRestartModal() {
  const modal = document.getElementById("properties-restart-modal");
  if (modal) modal.hidden = false;
}

function closePropertiesRestartModal() {
  const modal = document.getElementById("properties-restart-modal");
  if (modal) modal.hidden = true;
}

async function confirmPropertiesRestart() {
  const button = document.getElementById("confirm-properties-restart");
  if (button) button.disabled = true;
  closePropertiesRestartModal();
  try {
    await saveServerProperties(null, true);
  } finally {
    if (button) button.disabled = false;
  }
}

function updateProcessManagementFields() {
  const backend = document.getElementById("property-process-backend");
  const service = document.getElementById("property-systemd-service");
  const enabled = document.getElementById("property-systemd-enabled-row");
  const enabledToggle = document.getElementById("property-systemd-enabled");
  const isSystemd = backend?.value === "systemd";
  const systemdAvailable = backend?.dataset.systemdAvailable !== "false";
  if (service) service.hidden = !isSystemd;
  if (enabled) enabled.hidden = !isSystemd || !systemdAvailable;
  if (!isSystemd && enabledToggle) enabledToggle.checked = false;
}

function updateStartupCommandPreview() {
  const preview = document.getElementById("startup-command-preview");
  if (!preview) {
    return;
  }

  const initial = valueOf("property-min-memory") || "2G";
  const maximum = valueOf("property-max-memory") || "2G";
  const jar = valueOf("property-jar-name") || "paper.jar";
  const options = valueOf("property-java-args").trim();
  preview.textContent = [
    `java`,
    `-Xms${initial}`,
    `-Xmx${maximum}`,
    options,
    "-jar",
    jar,
    "--nogui",
  ].filter(Boolean).join(" ");
}

async function updateLatestLog() {
  const viewer = document.querySelector('.server-log-content[data-live="true"]');
  if (!viewer || viewer.dataset.refreshing === "true") return;
  viewer.dataset.refreshing = "true";
  const params = new URLSearchParams();
  if (viewer.dataset.logSize) params.set("size", viewer.dataset.logSize);
  if (viewer.dataset.logModifiedNs) params.set("modified_ns", viewer.dataset.logModifiedNs);
  try {
    const response = await fetch(
      `/api/web/servers/${viewer.dataset.serverId}/logs/latest?${params}`,
    );
    if (!response.ok) return;
    const data = await response.json();
    viewer.dataset.logSize = String(data.size ?? "");
    viewer.dataset.logModifiedNs = String(data.modified_ns ?? "");
    if (!data.changed) return;
    const truncatedNotice = document.getElementById("server-log-truncated-notice");
    if (truncatedNotice) truncatedNotice.hidden = data.truncated !== true;
    const nearBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 48;
    viewer.textContent = data.content || "";
    if (nearBottom) viewer.scrollTop = viewer.scrollHeight;
  } catch (error) {
    console.debug("Unable to refresh latest.log", error);
  } finally {
    viewer.dataset.refreshing = "false";
  }
}

setInterval(updateLatestLog, 3000);

function serverLogPageUrl(serverId, page, selectedLog = "") {
  const params = new URLSearchParams({ page: String(page) });
  if (selectedLog) params.set("file", selectedLog);
  return `/servers/${serverId}/logs?${params}`;
}

async function updateServerLogList() {
  const pageElement = document.querySelector(".server-logs-page");
  const rows = document.getElementById("server-log-rows");
  if (!pageElement || !rows || pageElement.dataset.listRefreshing === "true") return;
  pageElement.dataset.listRefreshing = "true";
  try {
    const response = await fetch(
      `/api/web/servers/${pageElement.dataset.serverId}/logs?page=${pageElement.dataset.logsPage}`,
    );
    if (!response.ok) return;
    const data = await response.json();
    pageElement.dataset.logsPage = String(data.page);
    rows.replaceChildren();
    for (const log of data.logs || []) {
      const link = document.createElement("a");
      link.className = `server-log-row${log.name === pageElement.dataset.selectedLog ? " active" : ""}`;
      link.href = serverLogPageUrl(pageElement.dataset.serverId, data.page, log.name);
      link.setAttribute("hx-get", link.href);
      link.setAttribute("hx-target", "#page-content");
      link.setAttribute("hx-push-url", "true");
      const name = document.createElement("span");
      name.className = "server-log-name";
      const icon = document.createElement("i");
      icon.className = "fa-regular fa-file-lines";
      name.append(icon, document.createTextNode(log.name));
      const modified = document.createElement("span");
      modified.textContent = log.modified_display;
      const size = document.createElement("span");
      size.textContent = log.size_display;
      link.append(name, modified, size);
      rows.appendChild(link);
    }
    if (!(data.logs || []).length) {
      const empty = document.createElement("div");
      empty.className = "empty-message";
      empty.textContent = "No Minecraft log files were found in this server's logs directory.";
      rows.appendChild(empty);
    }
    if (window.htmx) window.htmx.process(rows);

    const count = document.getElementById("server-log-count");
    if (count) count.textContent = `${data.total_logs} files`;
    const pagination = document.getElementById("server-log-pagination");
    if (pagination) pagination.hidden = data.total_pages <= 1;
    const label = document.getElementById("server-log-page-label");
    if (label) label.textContent = `Page ${data.page} of ${data.total_pages}`;
    const previous = pagination?.querySelector('[data-log-page="previous"]');
    const next = pagination?.querySelector('[data-log-page="next"]');
    const selected = pageElement.dataset.selectedLog;
    if (previous) {
      previous.href = serverLogPageUrl(pageElement.dataset.serverId, Math.max(1, data.page - 1), selected);
      previous.setAttribute("hx-get", previous.href);
      previous.setAttribute("aria-disabled", String(data.page <= 1));
    }
    if (next) {
      next.href = serverLogPageUrl(pageElement.dataset.serverId, Math.min(data.total_pages, data.page + 1), selected);
      next.setAttribute("hx-get", next.href);
      next.setAttribute("aria-disabled", String(data.page >= data.total_pages));
    }
  } catch (error) {
    console.debug("Unable to refresh server log list", error);
  } finally {
    pageElement.dataset.listRefreshing = "false";
  }
}

setInterval(updateServerLogList, 5000);

function normalizedStartupMemory(id) {
  const input = document.getElementById(id);
  const value = input?.value.trim() || "";
  const match = value.match(/^([1-9][0-9]*)\s*(K|KB|M|MB|G|GB)$/i);
  if (!match) {
    return value;
  }
  const normalized = `${match[1]}${match[2][0].toUpperCase()}`;
  if (input) {
    input.value = normalized;
  }
  return normalized;
}

function normalizeStartupMemoryInput(input) {
  normalizedStartupMemory(input.id);
  updateStartupCommandPreview();
}

function valueOf(id) {
  return (
    document
      .getElementById(id)
      ?.value ??
      ""
  );
}

function numberOf(id) {
  return Number(
    valueOf(id),
  );
}

function checkedOf(id) {
  return (
    document
      .getElementById(id)
      ?.checked ===
      true
  );
}

updatePropertiesPage();
loadAdvancedProperties();

async function saveOwnProfile(event) {
  event?.preventDefault();
  const username = document
    .getElementById(
      "settings-profile-username",
    )
    ?.value
    .trim();

  const email = document
    .getElementById(
      "settings-profile-email",
    )
    ?.value
    .trim() ||
    "";

  const password = document
    .getElementById(
      "settings-profile-password",
    )
    ?.value ||
    "";

  const status = document.getElementById(
    "profile-save-status",
  );

  const response = await fetch(
    "/api/web/settings/profile",
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        username,
        email,
        password,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    if (status) {
      status.textContent = data.error ||
        "Unable to save.";
    }

    return;
  }

  if (status) {
    status.textContent = "Saved";
  }

  const passwordInput = document.getElementById(
    "settings-profile-password",
  );

  if (passwordInput) {
    passwordInput.value = "";
  }
}

function selectedRolePermissions() {
  return Array.from(document.querySelectorAll(".settings-role-permission:checked"))
    .map((checkbox) => checkbox.value);
}

function setRolePermissions(keys) {
  const selected = new Set(keys || []);
  document.querySelectorAll(".settings-role-permission").forEach((checkbox) => {
    checkbox.checked = selected.has(checkbox.value);
  });
}

function clearRoleError() {
  const error = document.getElementById("settings-role-error");
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
}

function openAddRoleModal() {
  document.getElementById("role-modal-title").textContent = "Add Role";
  document.getElementById("settings-role-id").value = "";
  document.getElementById("settings-role-name").value = "";
  document.getElementById("settings-role-description").value = "";
  document.getElementById("delete-role-button").hidden = true;
  setRolePermissions([]);
  clearRoleError();
  document.getElementById("role-modal").hidden = false;
  document.getElementById("settings-role-name").focus();
}

async function openEditRoleModal(roleId) {
  const response = await fetch(`/api/web/settings/roles/${roleId}`);
  const data = await response.json();
  if (!response.ok) {
    alert(data.error || "Unable to load role");
    return;
  }
  document.getElementById("role-modal-title").textContent = "Edit Role";
  document.getElementById("settings-role-id").value = data.id;
  document.getElementById("settings-role-name").value = data.name;
  document.getElementById("settings-role-description").value = data.description;
  document.getElementById("delete-role-button").hidden = data.system;
  setRolePermissions(data.permissions);
  clearRoleError();
  document.getElementById("role-modal").hidden = false;
}

function closeRoleModal() {
  document.getElementById("role-modal").hidden = true;
}

async function saveSettingsRole() {
  const id = document.getElementById("settings-role-id").value;
  const response = await fetch(
    id ? `/api/web/settings/roles/${id}` : "/api/web/settings/roles",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.getElementById("settings-role-name").value.trim(),
        description: document.getElementById("settings-role-description").value.trim(),
        permissions: selectedRolePermissions(),
      }),
    },
  );
  const data = await response.json();
  if (!response.ok) {
    const error = document.getElementById("settings-role-error");
    error.textContent = data.error || "Unable to save role";
    error.hidden = false;
    return;
  }
  window.location.reload();
}

async function deleteSettingsRole() {
  const id = document.getElementById("settings-role-id").value;
  if (!id || !window.confirm("Delete this role?")) return;
  const response = await fetch(`/api/web/settings/roles/${id}`, { method: "DELETE" });
  const data = await response.json();
  if (!response.ok) {
    const error = document.getElementById("settings-role-error");
    error.textContent = data.error || "Unable to delete role";
    error.hidden = false;
    return;
  }
  window.location.reload();
}

function openAddUserModal() {
  document.getElementById(
    "user-modal-title",
  ).textContent = "Add User";

  document.getElementById(
    "settings-user-id",
  ).value = "";

  document.getElementById(
    "settings-user-username",
  ).value = "";

  document.getElementById(
    "settings-user-password",
  ).value = "";

  const roleSelect = document.getElementById("settings-user-role");
  const defaultRole = Array.from(roleSelect.options).find(
    (option) => option.dataset.roleName === "User",
  );
  roleSelect.value = defaultRole?.value || roleSelect.options[0]?.value || "";

  document.getElementById(
    "settings-user-enabled",
  ).checked = true;

  document.getElementById(
    "settings-user-must-change-password",
  ).checked = true;

  document
    .querySelectorAll(
      ".settings-server-checkbox",
    )
    .forEach(
      (checkbox) => {
        checkbox.checked = false;
      },
    );

  document.getElementById(
    "settings-password-help",
  ).textContent = "Minimum 8 characters";

  document.getElementById(
    "delete-user-button",
  ).hidden = true;

  clearSettingsUserError();

  updateUserServerAccessVisibility();

  const modal = document.getElementById(
    "user-modal",
  );

  modal.hidden = false;

  document.getElementById(
    "settings-user-username",
  ).focus();
}

async function openEditUserModal(
  userId,
) {
  const response = await fetch(
    `/api/web/settings/users/${userId}`,
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to load user",
    );

    return;
  }

  document.getElementById(
    "user-modal-title",
  ).textContent = "Edit User";

  document.getElementById(
    "settings-user-id",
  ).value = data.id;

  document.getElementById(
    "settings-user-username",
  ).value = data.username;

  document.getElementById(
    "settings-user-password",
  ).value = "";

  document.getElementById(
    "settings-user-role",
  ).value = String(data.role_id);

  document.getElementById(
    "settings-user-enabled",
  ).checked = data.enabled;

  document.getElementById(
    "settings-user-must-change-password",
  ).checked = data.must_change_password;

  document.getElementById(
    "settings-password-help",
  ).textContent = "Leave blank to keep current password";

  const allowed = new Set(
    data.servers.map(
      String,
    ),
  );

  document
    .querySelectorAll(
      ".settings-server-checkbox",
    )
    .forEach(
      (checkbox) => {
        checkbox.checked = allowed.has(
          checkbox.value,
        );
      },
    );

  document.getElementById(
    "delete-user-button",
  ).hidden = false;

  clearSettingsUserError();

  updateUserServerAccessVisibility();

  document.getElementById(
    "user-modal",
  ).hidden = false;
}

function closeUserModal() {
  document.getElementById(
    "user-modal",
  ).hidden = true;
}

function updateUserServerAccessVisibility() {
  const select = document.getElementById("settings-user-role");
  const selected = select?.selectedOptions[0];

  const access = document.getElementById(
    "settings-server-access",
  );

  if (!access) {
    return;
  }

  access.hidden = selected?.dataset.viewAll === "true";
}

function selectedSettingsServers() {
  return Array
    .from(
      document.querySelectorAll(
        ".settings-server-checkbox:checked",
      ),
    )
    .map(
      (checkbox) =>
        Number(
          checkbox.value,
        ),
    );
}

async function saveSettingsUser() {
  const id = document
    .getElementById(
      "settings-user-id",
    )
    .value;

  const payload = {
    username: document
      .getElementById(
        "settings-user-username",
      )
      .value
      .trim(),

    password: document
      .getElementById(
        "settings-user-password",
      )
      .value,

    role_id: Number(document
      .getElementById(
        "settings-user-role",
      )
      .value),

    enabled: document
      .getElementById(
        "settings-user-enabled",
      )
      .checked,

    must_change_password: document.getElementById(
      "settings-user-must-change-password",
    ).checked,

    servers: selectedSettingsServers(),
  };

  const url = id ? `/api/web/settings/users/${id}` : "/api/web/settings/users";

  const response = await fetch(
    url,
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify(
        payload,
      ),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    showSettingsUserError(
      data.error ||
        "Unable to save user.",
    );

    return;
  }

  closeUserModal();

  window.location.reload();
}

async function deleteSettingsUser() {
  const id = document
    .getElementById(
      "settings-user-id",
    )
    .value;

  if (!id) {
    return;
  }

  if (
    !confirm(
      "Delete this user?",
    )
  ) {
    return;
  }

  const response = await fetch(
    `/api/web/settings/users/${id}`,
    {
      method: "DELETE",
    },
  );

  const data = await response.json();

  if (!response.ok) {
    showSettingsUserError(
      data.error ||
        "Unable to delete user.",
    );

    return;
  }

  window.location.reload();
}

function showSettingsUserError(
  message,
) {
  const error = document.getElementById(
    "settings-user-error",
  );

  error.textContent = message;

  error.hidden = false;
}

function clearSettingsUserError() {
  const error = document.getElementById(
    "settings-user-error",
  );

  if (!error) {
    return;
  }

  error.textContent = "";
  error.hidden = true;
}

async function updateSMTPSettings() {
  const host = document.getElementById(
    "smtp-host",
  );

  if (!host) {
    return;
  }

  try {
    const response = await fetch(
      "/api/web/settings/smtp",
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    setValue(
      "smtp-host",
      data.smtp_host,
    );

    setValue(
      "smtp-port",
      data.smtp_port,
    );

    setValue(
      "smtp-username",
      data.smtp_username,
    );

    setValue(
      "smtp-password",
      "",
    );

    setValue(
      "smtp-security",
      data.smtp_security,
    );

    setValue(
      "smtp-from-name",
      data.smtp_from_name,
    );

    setValue(
      "smtp-from-address",
      data.smtp_from_address,
    );
  } catch {
    const status = document.getElementById(
      "smtp-save-status",
    );

    if (status) {
      status.textContent = "Unable to load SMTP settings.";
    }
  }
}

async function saveLoginMessage() {
  const status = document.getElementById(
    "login-message-save-status",
  );
  const input = document.getElementById(
    "login-message",
  );

  if (!input) {
    return;
  }

  const response = await fetch(
    "/api/web/settings/login-message",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login_message: input.value }),
    },
  );
  const data = await response.json();

  if (!response.ok) {
    if (status) {
      status.textContent = data.error || "Unable to save login message.";
    }
    return;
  }

  input.value = data.login_message;
  if (status) {
    status.textContent = "Saved";
  }
}

async function saveSMTPSettings() {
  const status = document.getElementById(
    "smtp-save-status",
  );

  const payload = {
    smtp_host: valueOf(
      "smtp-host",
    ),

    smtp_port: valueOf(
      "smtp-port",
    ),

    smtp_username: valueOf(
      "smtp-username",
    ),

    smtp_password: valueOf(
      "smtp-password",
    ),

    smtp_security: valueOf(
      "smtp-security",
    ),

    smtp_from_name: valueOf(
      "smtp-from-name",
    ),

    smtp_from_address: valueOf(
      "smtp-from-address",
    ),
  };

  const response = await fetch(
    "/api/web/settings/smtp",
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify(
        payload,
      ),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    if (status) {
      status.textContent = data.error ||
        "Unable to save SMTP settings.";
    }

    return;
  }

  if (status) {
    status.textContent = "Saved";
  }

  const password = document.getElementById(
    "smtp-password",
  );

  if (password) {
    password.value = "";
  }
}

async function sendSMTPTest() {
  const status = document.getElementById(
    "smtp-save-status",
  );

  if (status) {
    status.textContent = "Sending...";
  }

  const response = await fetch(
    "/api/web/settings/smtp/test",
    {
      method: "POST",
    },
  );

  const data = await response.json();

  if (!response.ok) {
    if (status) {
      status.textContent = data.error ||
        "Test failed.";
    }

    return;
  }

  if (status) {
    status.textContent = "Test email sent.";
  }
}

updateSMTPSettings();

let offsiteRemoteState = [];

async function loadOffsiteBackupSettings() {
  const status = document.getElementById("offsite-settings-status");
  const list = document.getElementById("offsite-remote-list");
  if (!status) return;
  try {
    const response = await fetch("/api/web/settings/offsite-backups");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Off-site backups could not be checked.");
    status.textContent = data.available
      ? `${data.remotes.length} destination${data.remotes.length === 1 ? "" : "s"} configured`
      : data.reason === "not_installed"
        ? "rclone is not installed. Install it to enable off-site backups."
        : `Off-site backups are unavailable: ${data.error}`;
    const options = document.getElementById("offsite-test-remote");
    const selectedRemote = options.value;
    options.innerHTML = '<option value="">Choose a destination</option>' + (data.remotes || []).map((remote) => `<option value="${escapeHtml(remote)}">${escapeHtml(remote)}</option>`).join("");
    if ((data.remotes || []).includes(selectedRemote)) options.value = selectedRemote;
    offsiteRemoteState = data.destinations || [];
    list.innerHTML = offsiteRemoteState.length ? offsiteRemoteState.map((remote) => `
      <div class="offsite-remote-row">
        <div><strong>${escapeHtml(remote.name)}</strong><small>${escapeHtml(offsiteProviderName(remote.backend))}${remote.host ? ` · ${escapeHtml(remote.user || "")}@${escapeHtml(remote.host)}` : ""}</small></div>
        <div class="offsite-remote-actions">${["b2", "storj", "sftp"].includes(remote.backend) ? `<button class="button" type="button" onclick="openOffsiteRemoteModal('${escapeJsString(remote.name)}')">Edit</button>` : ""}<button class="button danger" type="button" onclick="deleteOffsiteRemote('${escapeJsString(remote.name)}')">Remove</button></div>
      </div>`).join("") : `<div class="empty-message">${data.reason === "not_installed" ? "Install rclone, then add your first destination here." : "No off-site destinations configured yet."}</div>`;
  } catch (error) {
    status.textContent = "Off-site backups could not be checked. Try refreshing after restarting the panel.";
    if (list) list.innerHTML = '<div class="empty-message">No destination information is available.</div>';
  }
}

async function loadPluginMonitoringRepository() {
  const editor = document.getElementById("plugin-monitoring-repository-editor");
  const status = document.getElementById("plugin-monitoring-repository-status");
  if (!editor || !status) return;
  status.textContent = "Loading shared settings…";
  try {
    const response = await fetch("/api/web/settings/plugin-monitoring-repository");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load shared plugin settings.");
    editor.value = data.content || "";
    status.textContent = data.using_bundled_defaults
      ? "Loaded the bundled defaults. Saving creates your shared repository."
      : "Shared repository loaded.";
  } catch (error) {
    status.textContent = error.message || "Unable to load shared plugin settings.";
  }
}

async function savePluginMonitoringRepository() {
  const editor = document.getElementById("plugin-monitoring-repository-editor");
  const status = document.getElementById("plugin-monitoring-repository-status");
  if (!editor || !status) return;
  status.textContent = "Saving shared settings…";
  try {
    const response = await fetch("/api/web/settings/plugin-monitoring-repository", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: editor.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to save shared plugin settings.");
    status.textContent = `${data.plugins} shared plugin setting${data.plugins === 1 ? "" : "s"} saved.`;
  } catch (error) {
    status.textContent = error.message || "Unable to save shared plugin settings.";
  }
}

async function uploadPluginMonitoringRepository(input) {
  const file = input.files?.[0];
  const editor = document.getElementById("plugin-monitoring-repository-editor");
  const status = document.getElementById("plugin-monitoring-repository-status");
  if (!file || !editor || !status) return;
  input.value = "";
  if (file.size > 262144) {
    status.textContent = "The repository file must be 256 KiB or smaller.";
    return;
  }
  if (!confirm("Replace the shared plugin update settings with this file?")) return;
  status.textContent = "Uploading shared settings…";
  try {
    const content = await file.text();
    const response = await fetch("/api/web/settings/plugin-monitoring-repository/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to upload shared plugin settings.");
    editor.value = content;
    status.textContent = `${data.plugins} shared plugin setting${data.plugins === 1 ? "" : "s"} uploaded.`;
  } catch (error) {
    status.textContent = error.message || "Unable to upload shared plugin settings.";
  }
}

function offsiteProviderName(backend) {
  return { b2: "Backblaze B2", storj: "Storj", sftp: "SFTP server" }[backend] || backend;
}

function updateOffsiteRemoteFields() {
  const backend = document.getElementById("offsite-remote-backend")?.value;
  document.querySelectorAll("[data-offsite-provider]").forEach((fields) => {
    const active = fields.dataset.offsiteProvider === backend;
    fields.hidden = !active;
    fields.style.display = active ? "grid" : "none";
    fields.querySelectorAll("input, select").forEach((control) => {
      control.disabled = !active;
    });
  });
}

function openOffsiteRemoteModal(name = "") {
  const remote = offsiteRemoteState.find((item) => item.name === name);
  document.getElementById("offsite-remote-name").value = remote?.name || "";
  document.getElementById("offsite-remote-name").readOnly = Boolean(remote);
  document.getElementById("offsite-remote-backend").value = remote?.backend || "b2";
  document.getElementById("offsite-b2-account").value = remote?.account || "";
  document.getElementById("offsite-b2-secret").value = "";
  document.getElementById("offsite-storj-access").value = remote?.access_key_id || "";
  document.getElementById("offsite-storj-secret").value = "";
  document.getElementById("offsite-storj-endpoint").value = remote?.endpoint || "https://gateway.storjshare.io";
  document.getElementById("offsite-sftp-host").value = remote?.host || "";
  document.getElementById("offsite-sftp-port").value = remote?.port || "22";
  document.getElementById("offsite-sftp-user").value = remote?.user || "";
  document.getElementById("offsite-sftp-secret").value = "";
  document.getElementById("offsite-remote-save-status").textContent = remote ? "Leave the secret blank to keep the saved value." : "";
  updateOffsiteRemoteFields();
  document.getElementById("offsite-remote-modal").hidden = false;
}

function closeOffsiteRemoteModal() {
  document.getElementById("offsite-remote-modal").hidden = true;
}

async function saveOffsiteRemote() {
  const backend = document.getElementById("offsite-remote-backend").value;
  const payload = { name: document.getElementById("offsite-remote-name").value.trim(), backend };
  if (backend === "b2") Object.assign(payload, { account: document.getElementById("offsite-b2-account").value.trim(), secret: document.getElementById("offsite-b2-secret").value });
  if (backend === "storj") Object.assign(payload, { access_key: document.getElementById("offsite-storj-access").value.trim(), secret: document.getElementById("offsite-storj-secret").value, endpoint: document.getElementById("offsite-storj-endpoint").value.trim() });
  if (backend === "sftp") Object.assign(payload, { host: document.getElementById("offsite-sftp-host").value.trim(), port: document.getElementById("offsite-sftp-port").value, user: document.getElementById("offsite-sftp-user").value.trim(), secret: document.getElementById("offsite-sftp-secret").value });
  const status = document.getElementById("offsite-remote-save-status");
  status.textContent = "Saving...";
  const response = await fetch("/api/web/settings/offsite-backups/remotes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const data = await response.json();
  if (!response.ok) return status.textContent = data.error || "Unable to save destination.";
  closeOffsiteRemoteModal();
  await loadOffsiteBackupSettings();
}

async function deleteOffsiteRemote(name) {
  if (!confirm(`Remove the ${name} destination? Existing remote files will not be deleted.`)) return;
  const response = await fetch(`/api/web/settings/offsite-backups/remotes/${encodeURIComponent(name)}`, { method: "DELETE" });
  const data = await response.json();
  if (!response.ok) return alert(data.error || "Unable to remove destination.");
  loadOffsiteBackupSettings();
}

function testNamedOffsiteRemote(name) {
  document.getElementById("offsite-test-remote").value = name;
  document.getElementById("offsite-test-path").value = "";
  testOffsiteBackupDestination();
}

function setOffsiteTestStatus(message, error = false) {
  const status = document.getElementById("offsite-test-status");
  if (!status) return;
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle("error", error);
}

async function testOffsiteBackupDestination() {
  const remote = document.getElementById("offsite-test-remote")?.value;
  const path = document.getElementById("offsite-test-path")?.value.trim() || "";
  if (!remote) {
    setOffsiteTestStatus("Choose a destination before testing the connection.", true);
    document.getElementById("offsite-test-remote")?.focus();
    return;
  }
  setOffsiteTestStatus("Testing connection...");
  try {
    const response = await fetch("/api/web/settings/offsite-backups/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote, path }),
    });
    const data = await response.json();
    setOffsiteTestStatus(
      response.ok ? "Connection successful." : (data.error || "Connection failed."),
      !response.ok,
    );
  } catch (error) {
    setOffsiteTestStatus("Connection test could not be completed.", true);
  }
}

loadOffsiteBackupSettings();
loadPluginMonitoringRepository();

async function updateTFASettings() {
  const disabled = document.getElementById(
    "tfa-disabled-controls",
  );

  const enabled = document.getElementById(
    "tfa-enabled-controls",
  );

  if (
    !disabled ||
    !enabled
  ) {
    return;
  }

  try {
    const response = await fetch(
      "/api/web/settings/tfa",
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    disabled.hidden = data.enabled;

    enabled.hidden = !data.enabled;
  } catch {
    disabled.hidden = true;
    enabled.hidden = true;
  }
}

async function beginTFASetup() {
  const response = await fetch(
    "/api/web/settings/tfa/setup",
    {
      method: "POST",
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to start 2FA setup",
    );

    return;
  }

  document.getElementById(
    "tfa-qr-code",
  ).src = data.qr_code;

  document.getElementById(
    "tfa-secret",
  ).textContent = data.secret;

  document.getElementById(
    "tfa-confirm-code",
  ).value = "";

  const error = document.getElementById(
    "tfa-setup-error",
  );

  error.hidden = true;

  document.getElementById(
    "tfa-setup-modal",
  ).hidden = false;

  document.getElementById(
    "tfa-confirm-code",
  ).focus();
}

function closeTFASetupModal() {
  document.getElementById(
    "tfa-setup-modal",
  ).hidden = true;
}

async function confirmTFASetup() {
  const code = document
    .getElementById(
      "tfa-confirm-code",
    )
    .value
    .trim();

  const response = await fetch(
    "/api/web/settings/tfa/confirm",
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        code,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    const error = document.getElementById(
      "tfa-setup-error",
    );

    error.textContent = data.error ||
      "Invalid code";

    error.hidden = false;

    return;
  }

  closeTFASetupModal();

  showRecoveryCodes(
    data.recovery_codes,
  );

  updateTFASettings();
}

function showRecoveryCodes(
  codes,
) {
  const container = document.getElementById(
    "tfa-recovery-codes",
  );

  container.innerHTML = codes.map(
    (code) => `
                <code>
                    ${escapeHtml(code)}
                </code>
            `,
  ).join("");

  document.getElementById(
    "tfa-recovery-modal",
  ).hidden = false;
}

function closeRecoveryCodesModal() {
  document.getElementById(
    "tfa-recovery-modal",
  ).hidden = true;
}

async function regenerateRecoveryCodes() {
  if (
    !confirm(
      "Generate new recovery codes? Existing unused codes will stop working.",
    )
  ) {
    return;
  }

  const response = await fetch(
    "/api/web/settings/tfa/recovery-codes",
    {
      method: "POST",
    },
  );

  const data = await response.json();

  if (!response.ok) {
    alert(
      data.error ||
        "Unable to generate recovery codes",
    );

    return;
  }

  showRecoveryCodes(
    data.recovery_codes,
  );
}

function openDisableTFAModal() {
  document.getElementById(
    "tfa-disable-password",
  ).value = "";

  document.getElementById(
    "tfa-disable-error",
  ).hidden = true;

  document.getElementById(
    "tfa-disable-modal",
  ).hidden = false;

  document.getElementById(
    "tfa-disable-password",
  ).focus();
}

function closeDisableTFAModal() {
  document.getElementById(
    "tfa-disable-modal",
  ).hidden = true;
}

async function disableTFA() {
  const password = document
    .getElementById(
      "tfa-disable-password",
    )
    .value;

  const response = await fetch(
    "/api/web/settings/tfa/disable",
    {
      method: "POST",

      headers: {
        "Content-Type": "application/json",
      },

      body: JSON.stringify({
        password,
      }),
    },
  );

  const data = await response.json();

  if (!response.ok) {
    const error = document.getElementById(
      "tfa-disable-error",
    );

    error.textContent = data.error ||
      "Unable to disable 2FA";

    error.hidden = false;

    return;
  }

  closeDisableTFAModal();

  updateTFASettings();
}

updateTFASettings();

async function loadSystemAlertSettings() {
  if (!document.getElementById("system-alerts-enabled")) return;
  try {
    const response = await fetch("/api/web/settings/system-alerts");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load alerts");
    setChecked("system-alerts-enabled", data.enabled);
    setValue("system-alert-memory", data.memory_percent);
    setValue("system-alert-storage", data.storage_percent);
    setValue("system-alert-cooldown", data.cooldown_minutes);
  } catch (error) {
    document.getElementById("system-alert-save-status").textContent = error.message;
  }
}

async function saveSystemAlertSettings() {
  const status = document.getElementById("system-alert-save-status");
  const response = await fetch("/api/web/settings/system-alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      enabled: checkedOf("system-alerts-enabled"),
      memory_percent: numberOf("system-alert-memory"),
      storage_percent: numberOf("system-alert-storage"),
      cooldown_minutes: numberOf("system-alert-cooldown"),
    }),
  });
  const data = await response.json();
  status.textContent = response.ok ? "Saved" : (data.error || "Unable to save alerts");
}

loadSystemAlertSettings();

async function updateBackupJobs() {
  const page = document.querySelector(
    ".backups-page",
  );

  if (!page) {
    return;
  }

  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/backups/jobs`,
    );

    if (!response.ok) {
      return;
    }

    const data = await response.json();

    renderBackupJobs(
      data.jobs || [],
    );

    if (activeBackupJobId) {
      const job = (data.jobs || []).find(
        (item) =>
          item.id ===
            activeBackupJobId,
      );

      if (job) {
        updateBackupModalProgress(
          job,
        );
      }
    }
  } catch {
    // leave existing UI alone
  }
}

function renderBackupJobs(
  jobs,
) {
  const container = document.getElementById(
    "backup-jobs",
  );

  if (!container) {
    return;
  }

  const active = jobs.filter(
    (job) =>
      [
        "queued",
        "saving",
        "archiving",
        "uploading",
      ].includes(
        job.status,
      ),
  );

  if (!active.length) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = active.map(
    (job) => `
                <div class="overview-card backup-job-card">

                    <div class="backup-job-header">

                        <div>
                            <strong>
                                Backup in progress
                            </strong>

                            <small>
                                ${
      escapeHtml(
        job.message ||
          job.status,
      )
    }
                            </small>
                        </div>

                        <strong>
                            ${Number.isFinite(Number(job.progress)) ? `${Number(job.progress)}%` : "In progress"}
                        </strong>

                    </div>

                    <div class="upload-progress-track">

                        <div
                            class="upload-progress-bar"
                            style="width: ${Number(job.progress || 0)}%"
                        ></div>

                    </div>

                    ${document.querySelector('.backups-page')?.dataset.canManage === "true" ? `
                      <div class="backup-job-actions">
                        <button class="button danger" onclick="cancelBackup(${job.id})">Cancel backup</button>
                      </div>` : ""}

                </div>
            `,
  )
    .join("");
}

async function cancelBackup(jobId) {
  const page = document.querySelector(".backups-page");
  if (!page || !confirm("Cancel this backup? The incomplete archive will be removed.")) return;
  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/backups/jobs/${jobId}/cancel`,
    { method: "POST" },
  );
  const data = await response.json();
  if (!response.ok) return showToast(data.error || "Unable to cancel backup", "error");
  showToast("Backup cancellation requested.", "warning");
  updateBackupJobs();
}

function cancelActiveBackup() {
  if (activeBackupJobId) cancelBackup(activeBackupJobId);
}

function updateBackupModalProgress(
  job,
) {
  const message = document.getElementById(
    "backup-create-message",
  );

  const bar = document.getElementById(
    "backup-create-progress-bar",
  );

  const percent = document.getElementById(
    "backup-create-progress-percent",
  );

  if (message) {
    message.textContent = job.message ||
      job.status;
  }

  if (bar) {
    bar.style.width = `${job.progress}%`;
  }

  if (percent) {
    percent.textContent = `${job.progress}%`;
  }

  if (
    job.status === "complete" ||
    job.status === "failed"
  ) {
    activeBackupJobId = null;

    if (
      job.status === "complete"
    ) {
      updateBackupsPage();
    }
  }
}

setInterval(
  updateBackupJobs,
  1500,
);

// Refresh the directory-backed list too, so a page left open across a console
// restart repopulates when the service returns.
setInterval(updateBackupsPage, 10000);

updateBackupJobs();

async function serverAction(
  action,
) {
  const topbar = document.querySelector(
    ".topbar",
  );

  if (!topbar) {
    return;
  }

  const serverId = topbar.dataset.serverId ||
    document.querySelector(".server-overview[data-server-id]")?.dataset.serverId;

  if (!serverId) {
    return;
  }

  const controls = [
    "dashboard-start",
    "dashboard-stop",
    "dashboard-restart",
  ].map((id) => document.getElementById(id)).filter(Boolean);
  controls.forEach((button) => { button.disabled = true; });

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/${action}`,
      {
        method: "POST",
      },
    );

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      showToast(data.error || `Unable to ${action} server`, "error");
      await updateServerStatus();
      return;
    }

    // Give the process a moment to change state.
    setTimeout(
      async () => {
        await updateServerStatus();
        await updatePluginsPage();
      },
      500,
    );
  } catch (error) {
    console.error(
      "Server action failed:",
      error,
    );
    showToast("Could not reach the server. Try again.", "error");
    await updateServerStatus();
  }
}

async function updateServerProcessStats() {
  const page = document.querySelector(
    ".server-overview",
  );

  if (!page) {
    return;
  }

  const serverId = page.dataset.serverId;

  try {
    const response = await fetch(
      `/api/web/servers/${serverId}/process-stats`,
    );

    if (!response.ok) {
      throw new Error();
    }

    const data = await response.json();

    const cpuValue = document.getElementById(
      "overview-cpu-value",
    );

    const memoryValue = document.getElementById(
      "overview-memory-value",
    );

    const uptimeValue = document.getElementById(
      "overview-uptime",
    );

    if (!data.running) {
      if (cpuValue) {
        cpuValue.textContent = "0%";
      }

      if (memoryValue) {
        memoryValue.textContent = "0 MB";
      }

      if (uptimeValue) {
        uptimeValue.textContent = "-";
      }

      return;
    }

    if (cpuValue) {
      cpuValue.textContent = `${data.cpu_percent}%`;
    }

    if (memoryValue) {
      const configuredMemory = memoryValue.dataset.memory;

      memoryValue.textContent = `${formatMemoryBytes(data.memory_used)} / ${
        formatConfiguredMemory(configuredMemory)
      }`;
    }

    if (uptimeValue) {
      uptimeValue.textContent = formatUptime(data.uptime_seconds);
    }
  } catch (error) {
    console.error(
      "Process stats error:",
      error,
    );
  }
}

setInterval(
  updateServerProcessStats,
  2000,
);

updateServerProcessStats();

function formatUptime(value) {
  const totalSeconds = Math.max(0, Math.floor(Number(value) || 0));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);

  if (days) return `${days}d ${hours}h ${minutes}m`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${totalSeconds}s`;
}

function formatMemoryBytes(bytes) {
  const mb = bytes / 1024 / 1024;

  if (mb >= 1024) {
    const gb = mb / 1024;

    return `${gb.toFixed(2)} GB`;
  }

  return `${mb.toFixed(0)} MB`;
}

function formatConfiguredMemory(memory) {
  if (!memory) {
    return "";
  }

  return memory
    .replace(/(\d+)G$/i, "$1 GB")
    .replace(/(\d+)M$/i, "$1 MB");
}

let consoleUpdateTag = null;
const SYSTEM_OPERATION_KEY = "craftarrSystemOperation";
let systemOperationActive = false;
let localSystemOperation = false;
let sharedSystemOperationSeen = false;

function setSystemOperationState(title, message, phase = "working") {
  const overlay = document.getElementById("system-operation-overlay");
  if (!overlay) return;
  document.getElementById("system-operation-title").textContent = title;
  document.getElementById("system-operation-message").textContent = message;
  overlay.dataset.phase = phase;
  sessionStorage.setItem(SYSTEM_OPERATION_KEY, JSON.stringify({
    title,
    message,
    phase,
    startedAt: Date.now(),
  }));
}

function beginSystemOperation(title, message, phase = "working", local = true) {
  const overlay = document.getElementById("system-operation-overlay");
  if (!overlay) return;
  systemOperationActive = true;
  localSystemOperation = local;
  overlay.hidden = false;
  overlay.classList.remove("failed");
  document.getElementById("system-operation-spinner").hidden = false;
  document.getElementById("system-operation-failed-icon").hidden = true;
  document.getElementById("system-operation-note").hidden = false;
  document.getElementById("system-operation-close").hidden = true;
  document.body.classList.add("system-operation-active");
  const shell = document.querySelector(".app-shell");
  if (shell) shell.inert = true;
  setSystemOperationState(title, message, phase);
}

function failSystemOperation(message) {
  const overlay = document.getElementById("system-operation-overlay");
  if (!overlay) return;
  systemOperationActive = false;
  localSystemOperation = false;
  sharedSystemOperationSeen = false;
  sessionStorage.removeItem(SYSTEM_OPERATION_KEY);
  overlay.classList.add("failed");
  document.getElementById("system-operation-title").textContent = "Something went wrong";
  document.getElementById("system-operation-message").textContent = message;
  document.getElementById("system-operation-spinner").hidden = true;
  document.getElementById("system-operation-failed-icon").hidden = false;
  document.getElementById("system-operation-note").hidden = true;
  document.getElementById("system-operation-close").hidden = false;
  document.getElementById("system-operation-close").focus();
}

function closeSystemOperation() {
  const overlay = document.getElementById("system-operation-overlay");
  if (!overlay?.classList.contains("failed")) return;
  overlay.hidden = true;
  overlay.classList.remove("failed");
  localSystemOperation = false;
  document.body.classList.remove("system-operation-active");
  const shell = document.querySelector(".app-shell");
  if (shell) shell.inert = false;
}

window.addEventListener("beforeunload", (event) => {
  if (!systemOperationActive && !serverZipImportInProgress) return;
  event.preventDefault();
  event.returnValue = serverZipImportInProgress
    ? "A server ZIP import is still in progress."
    : "A system operation is still in progress.";
});

function resumeSystemOperation() {
  const stored = sessionStorage.getItem(SYSTEM_OPERATION_KEY);
  if (!stored) return;
  try {
    const operation = JSON.parse(stored);
    beginSystemOperation(operation.title, operation.message, operation.phase, false);
    if (operation.phase === "restarting") {
      waitForConsoleRestart();
    } else {
      failSystemOperation(
        "The page was reloaded before the update completed. Check the installed version and service logs before trying again.",
      );
    }
  } catch {
    sessionStorage.removeItem(SYSTEM_OPERATION_KEY);
  }
}

async function pollSharedSystemOperation() {
  try {
    const response = await nativeFetch("/api/web/settings/system-operation", {
      cache: "no-store",
    });
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) return;
    if (!response.ok) return;
    const operation = await response.json();
    if (operation.active) {
      sharedSystemOperationSeen = true;
      if (!systemOperationActive) {
        beginSystemOperation(
          operation.title || "Getting things ready…",
          operation.message || "Please wait while this finishes.",
          operation.phase || "working",
          false,
        );
      } else if (!localSystemOperation) {
        setSystemOperationState(
          operation.title || "Getting things ready…",
          operation.message || "Please wait while this finishes.",
          operation.phase || "working",
        );
      }
      return;
    }
    if (sharedSystemOperationSeen && !localSystemOperation) {
      sharedSystemOperationSeen = false;
      systemOperationActive = false;
      sessionStorage.removeItem(SYSTEM_OPERATION_KEY);
      window.location.reload();
    }
  } catch {
    // Connection failures are expected while the console service restarts.
  }
}

window.setInterval(pollSharedSystemOperation, 1000);
pollSharedSystemOperation();

function setConsoleUpdateStatus(stateClass, iconClass, label, title = label) {
  const status = document.getElementById("console-update-status");
  if (!status) return;
  status.classList.remove("is-current", "is-update", "is-warning", "is-error", "is-pending");
  status.classList.add(stateClass);
  status.title = title || "";
  const icon = document.createElement("i");
  icon.className = `fa-solid ${iconClass}`;
  icon.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  text.textContent = label;
  status.replaceChildren(icon, text);
}

async function updateConsoleVersionStatus() {
  const status = document.getElementById(
    "console-update-status",
  );

  if (!status) {
    return;
  }

  const button = document.getElementById(
    "console-update-button",
  );

  try {
    const response = await fetch(
      "/api/web/settings/update",
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(
        data.error ||
          "Unable to check",
      );
    }

    if (
      data.release_available ===
        false
    ) {
      setConsoleUpdateStatus("is-pending", "fa-circle-question", "No published releases");
      if (button) button.hidden = true;

      return;
    }

    if (data.update_available) {
      consoleUpdateTag = data.tag;
      setConsoleUpdateStatus("is-update", "fa-circle-up", `v${data.latest_version} available`);
      if (button) button.hidden = false;
    } else {
      consoleUpdateTag = null;
      setConsoleUpdateStatus("is-current", "fa-circle-check", "Up to date");
      if (button) button.hidden = true;
    }
  } catch (error) {
    consoleUpdateTag = null;
    setConsoleUpdateStatus(
      "is-error",
      "fa-circle-exclamation",
      "Unable to check for updates",
      error.message || "Unable to check for updates",
    );
    if (button) button.hidden = true;
  }
}

updateConsoleVersionStatus();
resumeSystemOperation();

async function upgradeConsole() {
  if (
    !consoleUpdateTag ||
    !confirm(
      `Install ${consoleUpdateTag}? A verified backup will be created first.`,
    )
  ) return;
  beginSystemOperation(
    "Updating Craftarr",
    `Downloading and verifying ${consoleUpdateTag}. Do not close this page.`,
    "installing",
  );
  const button = document.getElementById("console-update-button");
  if (button) button.disabled = true;
  setConsoleUpdateStatus("is-pending", "fa-spinner fa-spin", "Downloading and verifying…");
  try {
    const response = await fetch("/api/web/settings/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: consoleUpdateTag }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Update failed");
    setConsoleUpdateStatus("is-pending", "fa-spinner fa-spin", "Installed; restarting console…");
    setSystemOperationState(
      "Restarting Craftarr",
      "The update is installed. Waiting for the console service to return...",
      "restarting",
    );
    if (data.rollback_id) {
      sessionStorage.setItem("consoleRollbackId", data.rollback_id);
      document.getElementById("console-rollback-button").hidden = false;
    }
    button.hidden = true;
    await waitForConsoleRestart();
  } catch (error) {
    if (button) button.disabled = false;
    failSystemOperation(error.message);
    updateConsoleVersionStatus();
  }
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForConsoleRestart() {
  const deadline = Date.now() + 90000;

  await delay(2500);
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`/health?restart=${Date.now()}`, {
        cache: "no-store",
      });
      if (response.ok) {
        systemOperationActive = false;
        sessionStorage.removeItem(SYSTEM_OPERATION_KEY);
        window.location.reload();
        return;
      }
    } catch {
      // The service is expected to be briefly unavailable while systemd restarts it.
    }
    await delay(1000);
  }

  const message = "The console service did not return within 90 seconds. Check the service logs before trying again.";
  failSystemOperation(message);
  updateConsoleVersionStatus();
}

async function restartConsoleService() {
  if (!confirm("Restart Craftarr now?")) return;

  beginSystemOperation(
    "Restarting Craftarr",
    "Requesting a service restart. This page will reconnect automatically...",
    "restarting",
  );

  const button = document.getElementById("console-restart-button");
  button.disabled = true;
  setConsoleUpdateStatus("is-pending", "fa-spinner fa-spin", "Restarting console…");

  try {
    const response = await fetch("/api/web/settings/restart", {
      method: "POST",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Restart failed");
    await waitForConsoleRestart();
  } catch (error) {
    button.disabled = false;
    failSystemOperation(error.message);
    updateConsoleVersionStatus();
  }
}

async function systemServerAction(serverId, action, button = null) {
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/web/servers/${serverId}/${action}`, {
      method: "POST",
    });
    const data = await response.json();
    if (!response.ok) {
      alert(data.error || `Unable to ${action} server`);
      return;
    }
  } catch {
    // The shared fetch wrapper already reports connection failures.
  } finally {
    window.setTimeout(updateSystemStats, 500);
  }
}

function setPaperUpdateIndicator(update) {
  const indicator = document.getElementById("paper-version-detail");
  if (!indicator) return;
  indicator.classList.remove("is-current", "is-update", "is-warning", "is-error", "is-pending");
  const installed = update?.installed_version || update?.current_version;
  const latest = update?.latest_version;
  indicator.title = installed
    ? `Installed: ${installed}${update?.update_available && latest ? ` · Available: ${latest}` : ""}`
    : update?.error || "";

  if (update?.update_available) {
    indicator.classList.add("is-update");
    indicator.innerHTML = `<i class="fa-solid fa-circle-up" aria-hidden="true"></i><span>Update available${latest ? ` · ${escapeHtml(latest)}` : ""}</span>`;
  } else if (update?.status === "Current") {
    indicator.classList.add("is-current");
    indicator.innerHTML = '<i class="fa-solid fa-circle-check" aria-hidden="true"></i><strong>Up to date</strong>';
  } else if (update?.status === "Incompatible") {
    indicator.classList.add("is-warning");
    indicator.innerHTML = `<i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i><span>Compatibility needs review${latest ? ` · ${escapeHtml(latest)}` : ""}</span>`;
  } else if (update?.status === "Check failed") {
    indicator.classList.add("is-error");
    indicator.title = update.error || "Could not check for updates";
    indicator.innerHTML = '<i class="fa-solid fa-circle-exclamation" aria-hidden="true"></i><span>Could not check</span>';
  } else {
    indicator.classList.add("is-pending");
    indicator.innerHTML = '<i class="fa-solid fa-circle-question" aria-hidden="true"></i><span>Status not checked</span>';
  }
}

async function loadPaperVersionStatus() {
  const page = document.querySelector(
    ".paper-management[data-server-id], .server-overview[data-server-id]",
  );
  const detail = document.getElementById("paper-version-detail");
  if (!page || !detail) return;
  if (page.classList.contains("server-overview")) {
    try {
      const response = await fetch(`/api/web/servers/${page.dataset.serverId}/paper/update-status`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Unable to load Paper status");
      if (!page.isConnected) return;
      const installed = document.getElementById("paper-installed-version");
      if (installed && data.installed_version) installed.textContent = data.installed_version;
      setPaperUpdateIndicator(data);
      return data;
    } catch (error) {
      const result = {status: "Check failed", error: error.message};
      setPaperUpdateIndicator(result);
      return result;
    }
  }
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/paper`,
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to check Paper");
    const installed = document.getElementById("paper-installed-version");
    if (installed) {
      installed.innerHTML = `${escapeHtml(data.current_version || "Unknown")}${
        data.current_build
          ? ` <small>(build ${escapeHtml(data.current_build)})</small>`
          : ""
      }`;
    }
    const sortedVersions = sortMinecraftVersions(data.versions || []);
    const newestVersion = sortedVersions[0] || data.latest_version;
    const versionUpdate = data.jar_inspected === true
      && Boolean(newestVersion && data.current_version && newestVersion !== data.current_version);
    const buildUpdate = data.jar_inspected === true && Number(data.builds_behind) > 0;
    const updateStatus = {
      installed_version: `Paper ${data.current_version || "Unknown"}${data.current_build ? ` build ${data.current_build}` : ""}`,
      latest_version: versionUpdate
        ? `Minecraft ${newestVersion}`
        : buildUpdate
          ? `build ${data.latest_build}`
          : null,
      update_available: versionUpdate || buildUpdate,
      status: versionUpdate || buildUpdate
        ? "Update available"
        : data.jar_inspected === true && data.builds_behind === 0
          ? "Current"
          : "Not checked",
      current_version: data.current_version,
      current_build: data.current_build,
      latest_build: data.latest_build,
    };
    setPaperUpdateIndicator(updateStatus);
    const button = document.getElementById("paper-update-button");
    const select = document.getElementById("paper-version-select");
    if (select) {
      select.innerHTML = sortedVersions.map((version) =>
        `<option value="${escapeHtml(version)}" ${
          version === data.current_version ? "selected" : ""
        }>${escapeHtml(version)}</option>`
      ).join("");
    }
    populatePaperBuilds(data.builds || [], data.current_build);
    if (button) {
      setPaperUpdateAvailability(
        button,
        data.running
          ? "Stop the server before installing a Paper update."
          : !(data.builds || []).length
            ? "No Paper updates are available for this version."
            : "",
      );
    }
    return updateStatus;
  } catch (error) {
    const result = {status: "Check failed", error: error.message};
    setPaperUpdateIndicator(result);
    return result;
  }
}

function setPaperUpdateAvailability(button, reason = "") {
  if (!button) return;
  button.dataset.unavailableReason = reason;
  button.setAttribute("aria-disabled", reason ? "true" : "false");
  button.classList.toggle("is-unavailable", Boolean(reason));
  button.title = reason;
}

function sortMinecraftVersions(versions) {
  return [...new Set(versions)].sort((left, right) => {
    const leftParts = String(left).match(/\d+/g)?.map(Number) || [];
    const rightParts = String(right).match(/\d+/g)?.map(Number) || [];
    const length = Math.max(leftParts.length, rightParts.length);
    for (let index = 0; index < length; index += 1) {
      const difference = (rightParts[index] || 0) - (leftParts[index] || 0);
      if (difference) return difference;
    }
    return String(right).localeCompare(String(left));
  });
}

function populatePaperBuilds(builds, installedBuild = null) {
  const select = document.getElementById("paper-build-select");
  if (!select) return;
  if (!builds.length) {
    select.innerHTML = "<option>No build data — restart the panel</option>";
    return;
  }
  select.innerHTML = builds.map((build, index) => {
    const labels = [];
    if (index === 0) labels.push("latest");
    if (String(build.id) === String(installedBuild)) labels.push("installed");
    if (build.channel && build.channel !== "STABLE") {
      labels.push(build.channel.toLowerCase());
    }
    return `<option value="${escapeHtml(build.id)}">Build ${
      escapeHtml(build.id)
    }${labels.length ? ` — ${labels.join(", ")}` : ""}</option>`;
  }).join("");
  const button = document.getElementById("paper-update-button");
  if (button && builds.length) {
    button.innerHTML = '<i class="fa-solid fa-download" aria-hidden="true"></i> Download and install';
  }
}

function openDeleteServerModal() {
  document.getElementById("delete-server-files").checked = false;
  document.getElementById("delete-server-error").textContent = "";
  document.getElementById("delete-server-modal").hidden = false;
  document.getElementById("confirm-delete-server").focus();
}

function closeDeleteServerModal() {
  document.getElementById("delete-server-modal").hidden = true;
}

async function confirmDeleteServer() {
  const page = document.querySelector(".properties-page[data-server-id]");
  const button = document.getElementById("confirm-delete-server");
  const error = document.getElementById("delete-server-error");
  button.disabled = true;
  error.textContent = "Deleting server...";

  try {
    const response = await fetch(`/api/web/servers/${page.dataset.serverId}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirmed: true,
        delete_files: document.getElementById("delete-server-files").checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to delete server");
    if (data.warning) alert(data.warning);
    window.location.href = "/servers";
  } catch (requestError) {
    error.textContent = requestError.message;
    button.disabled = false;
  }
}

async function loadPaperBuilds(version) {
  const page = document.querySelector(".paper-management[data-server-id]");
  const select = document.getElementById("paper-build-select");
  const button = document.getElementById("paper-update-button");
  if (!page || !select) return;
  select.innerHTML = "<option>Loading builds...</option>";
  setPaperUpdateAvailability(button, "Paper builds are still loading.");
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/paper?version=${
        encodeURIComponent(version)
      }`,
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load builds");
    populatePaperBuilds(data.builds || [], data.current_build);
    setPaperUpdateAvailability(
      button,
      data.running
        ? "Stop the server before installing a Paper update."
        : !data.builds?.length
          ? "No Paper updates are available for this version."
          : "",
    );
  } catch (error) {
    select.innerHTML = `<option>${escapeHtml(error.message)}</option>`;
    setPaperUpdateAvailability(button, "Paper builds could not be loaded.");
  }
}

async function installPaperVersion() {
  const page = document.querySelector(".paper-management[data-server-id]");
  const message = document.getElementById("paper-update-message");
  const button = document.getElementById("paper-update-button");
  const unavailableReason = button?.dataset.unavailableReason;
  if (unavailableReason) {
    message.textContent = unavailableReason;
    message.classList.add("error");
    return;
  }
  if (button?.disabled) return;
  if (button) button.disabled = true;
  message.classList.remove("error");
  message.textContent = "Downloading and checking the new server files…";
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/paper`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: document.getElementById("paper-version-select").value,
          build: document.getElementById("paper-build-select").value,
        }),
      },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The server update did not finish.");
    const updateStatus = await loadPaperVersionStatus();
    const installedLabel = `Paper ${data.version} build ${data.build}`;
    let notification;
    if (updateStatus?.status === "Current") {
      notification = `${installedLabel} installed. The server is up to date.`;
      message.classList.remove("error");
      message.textContent = notification;
      showToast(notification, "success", 6500);
    } else if (updateStatus?.update_available) {
      notification = `${installedLabel} installed. A newer Paper version or build is available.`;
      message.classList.remove("error");
      message.textContent = notification;
      showToast(notification, "warning", 7000);
    } else {
      notification = `${installedLabel} installed. The latest update status could not be confirmed.`;
      message.classList.remove("error");
      message.textContent = notification;
      showToast(notification, "info", 7000);
    }
    const serverId = page.dataset.serverId;
    recordInAppNotification({
      id: `paper-install:${serverId}:${Date.now()}`,
      kind: "paper-install",
      title: updateStatus?.status === "Current"
        ? "Paper update installed · server is up to date"
        : "Paper update installed",
      message: notification,
      url: `/servers/${encodeURIComponent(serverId)}/properties`,
      checked_at: new Date().toISOString(),
    });
    if (button) button.disabled = false;
    await refreshNotifications(false);
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
    if (button) button.disabled = false;
    if (String(error.message).includes("Stop the server")) {
      setPaperUpdateAvailability(button, error.message);
    }
  }
}

loadPaperVersionStatus();

async function rollbackConsoleUpdate() {
  const rollbackId = sessionStorage.getItem("consoleRollbackId");
  if (
    !rollbackId ||
    !confirm("Restore the application files from before this update?")
  ) return;
  beginSystemOperation(
    "Rolling back Craftarr",
    "Restoring the verified application backup. Do not close this page.",
    "installing",
  );
  const button = document.getElementById("console-rollback-button");
  button.disabled = true;
  setConsoleUpdateStatus("is-pending", "fa-spinner fa-spin", "Restoring previous version…");
  try {
    const response = await fetch("/api/web/settings/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "rollback", rollback_id: rollbackId }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Rollback failed");
    setConsoleUpdateStatus("is-pending", "fa-spinner fa-spin", "Previous version restored; restarting console…");
    setSystemOperationState(
      "Restarting Craftarr",
      "The previous version is restored. Waiting for the console service to return...",
      "restarting",
    );
    sessionStorage.removeItem("consoleRollbackId");
    button.hidden = true;
    await waitForConsoleRestart();
  } catch (error) {
    button.disabled = false;
    failSystemOperation(error.message);
    updateConsoleVersionStatus();
  }
}

function automationPage() {
  return document.querySelector(
    ".server-overview[data-server-id], .automation-page[data-server-id], .server-performance-page[data-server-id]",
  );
}

function localScheduleTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || automationPage()?.dataset.timezone || "local time";
}

function parseUtcTimestamp(value) {
  const timestamp = String(value || "");
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp);
  return new Date(hasTimezone ? timestamp : `${timestamp}Z`);
}

function drawMetricChart(
  canvasId,
  rows,
  value,
  label,
  formatValue = (point) => point.toLocaleString(),
) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(120, canvas.clientWidth);
  const height = Math.max(72, canvas.clientHeight || 120);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.width = "100%";
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  const values = rows.map((row) => Number(value(row) || 0));
  const rootStyle = getComputedStyle(document.documentElement);
  if (!values.length) {
    context.fillStyle = rootStyle.getPropertyValue("--sm-muted").trim() || "#64748b";
    context.font = "13px Poppins, sans-serif";
    context.fillText("No history yet", 8, 22);
    canvas.setAttribute("aria-label", `${label}: no historical data yet`);
    canvas.title = `${label}: no historical data yet`;
    return;
  }
  const maximum = Math.max(...values);
  const scaleMaximum = Math.max(1, maximum);
  const peakIndex = values.reduce(
    (peak, point, index) => point > values[peak] ? index : peak,
    0,
  );
  const latestText = formatValue(values.at(-1), rows.at(-1));
  const peakText = formatValue(maximum, rows[peakIndex]);
  const chartToken = {
    "metric-cpu": "--sm-green-dark",
    "metric-memory": "--sm-blue-dark",
    "metric-players": "--sm-orange-dark",
    "metric-uptime": "--sm-purple-dark",
  }[canvasId] || "--sm-blue-dark";
  const chartColor = rootStyle.getPropertyValue(chartToken).trim() || "#1866c5";
  const xInset = 4;
  const yTop = 9;
  const yBottom = height - 24;
  context.strokeStyle = "rgba(100, 116, 139, .15)";
  context.lineWidth = 1;
  [0.25, 0.55, 0.85].forEach((fraction) => {
    const y = yTop + (yBottom - yTop) * fraction;
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(width, y);
    context.stroke();
  });
  context.strokeStyle = chartColor;
  context.lineWidth = 2.5;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.beginPath();
  values.forEach((point, index) => {
    const x = values.length === 1
      ? xInset
      : xInset + index * (width - xInset * 2) / (values.length - 1);
    const y = yBottom - (point / scaleMaximum) * (yBottom - yTop);
    index ? context.lineTo(x, y) : context.moveTo(x, y);
  });
  context.stroke();
  context.fillStyle = rootStyle.getPropertyValue("--sm-muted").trim() || "#64748b";
  context.font = "12px Poppins, sans-serif";
  context.fillText(`Latest ${latestText} · Peak ${peakText}`, 8, height - 5);

  const summary = `${label}: latest ${latestText}, peak ${peakText}, ${values.length} readings`;
  canvas.setAttribute("aria-label", summary);
  canvas.title = summary;
  const describePoint = (index) => {
    const row = rows[index];
    const recorded = row?.recorded_at ? new Date(row.recorded_at).toLocaleString() : `Reading ${index + 1}`;
    return `${label}: ${formatValue(values[index], row)} · ${recorded}`;
  };
  canvas.onmousemove = (event) => {
    const bounds = canvas.getBoundingClientRect();
    const position = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    canvas.title = describePoint(Math.round(position * (values.length - 1)));
  };
  canvas.onmouseleave = () => { canvas.title = summary; };
}

async function loadServerMetrics() {
  const page = automationPage();
  if (!page || !document.getElementById("metric-cpu")) return;
  const hours = document.getElementById("metrics-range")?.value || 24;
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/metrics?hours=${hours}`,
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load metrics");
    const rows = data.metrics || [];
    drawMetricChart(
      "metric-cpu",
      rows,
      (row) => row.cpu_percent,
      "CPU use",
      (point) => `${Math.round(point).toLocaleString()}%`,
    );
    drawMetricChart(
      "metric-memory",
      rows,
      (row) => row.memory_bytes / 1048576,
      "Memory",
      (_point, row) => formatMemoryBytes(row.memory_bytes || 0),
    );
    drawMetricChart(
      "metric-players",
      rows,
      (row) => row.player_count,
      "Players online",
      (point) => `${point.toLocaleString()} ${point === 1 ? "player" : "players"}`,
    );
    drawMetricChart(
      "metric-uptime",
      rows,
      (row) => row.uptime_seconds || 0,
      "Time online",
      (point, row) => row.uptime_seconds == null ? "Offline" : formatUptime(point),
    );
  } catch (error) {
    ["metric-cpu", "metric-memory", "metric-players", "metric-uptime"].forEach(
      (id) => drawMetricChart(id, [], () => 0, error.message),
    );
  }
}

let metricResizeTimeout = null;
window.addEventListener("resize", () => {
  window.clearTimeout(metricResizeTimeout);
  metricResizeTimeout = window.setTimeout(() => {
    if (document.getElementById("metric-cpu")) loadServerMetrics();
  }, 180);
});

let serverScheduleState = [];
let scheduleRunsPage = 1;

async function loadServerSchedules() {
  if (document.hidden) return;
  const page = automationPage();
  const commandList = document.getElementById("command-schedule-list");
  const backupList = document.getElementById("backup-schedule-list");
  if (!page || (!commandList && !backupList)) return;
  try {
    const response = await fetch(
      `/api/web/servers/${page.dataset.serverId}/schedules?runs_page=${scheduleRunsPage}&runs_per_page=10`,
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load schedules");
    const tasks = (data.tasks || []).filter((task) => task.enabled);
    serverScheduleState = tasks;
    const taskNames = new Map((data.tasks || []).map((task) => [Number(task.id), task.name]));
    const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
    const remoteOptions = document.getElementById("rclone-remotes");
    if (remoteOptions) {
      remoteOptions.innerHTML = (data.offsite_remotes || []).map((remote) => `<option value="${escapeHtml(remote)}:"></option>`).join("");
    }
    const offsiteAvailability = document.getElementById("offsite-availability");
    if (offsiteAvailability) {
      offsiteAvailability.textContent = data.offsite_error
        ? `Off-site copies unavailable: ${data.offsite_error}`
        : (data.offsite_remotes || []).length
          ? `Available remotes: ${data.offsite_remotes.join(", ")}`
          : "No rclone remotes are configured.";
    }
    const describeWhen = (task) => {
      if (task.frequency === "hourly") return "Every hour";
      const timezone = task.schedule_timezone || localScheduleTimezone();
      const time = `${String(task.run_hour ?? 0).padStart(2, "0")}:00 ${timezone}`;
      if (task.frequency === "daily") return `Every day at ${time}`;
      if (task.frequency === "weekly") return `Every ${weekdays[task.run_weekday] || "week"} at ${time}`;
      if (task.frequency === "monthly") return `The first day of every month at ${time}`;
      if (task.frequency === "custom") return `Custom (${escapeHtml(task.cron_expression || "")}) · ${timezone}`;
      return `Every ${task.interval_minutes} minutes`;
    };
    const canManage = page.dataset.canManage === "true";
    const activeJobs = data.backup_jobs || [];
    const backupRunning = activeJobs.length > 0;
    const renderTasks = (type) => {
      const matches = tasks.filter((task) => task.task_type === type);
      return matches.length ? matches.map((task) => `
            <div class="schedule-row"><div><strong>${
        escapeHtml(task.name)
      }</strong><br>
            <small>${task.task_type === "backup" ? `${describeWhen(task)} · ` : ""}${
        task.task_type === "backup"
          ? `Keeps ${task.retention_count || "all"} backup${Number(task.retention_count) === 1 ? "" : "s"} on this server${
            task.remote_destination
              ? ` and copies each one to ${escapeHtml(task.remote_destination)}, keeping ${task.remote_retention_count || "all"} there`
              : ""
          }`
          : escapeHtml(task.command)
      }${task.task_type === "command" ? ` · ${describeWhen(task)}` : ""}</small></div>
            ${canManage ? `<div class="schedule-row-actions">
              ${type === "backup" ? `<button class="button" onclick="runServerScheduleNow(${Number(task.id)}, this)" ${backupRunning ? "disabled" : ""}>${backupRunning ? "Backup running" : "Run now"}</button>` : ""}
              <button class="button" onclick="editServerSchedule(${Number(task.id)})">Edit</button>
              <button class="button" onclick="deleteServerSchedule(${Number(task.id)})">Delete</button>
            </div>` : ""}</div>`).join("")
        : `<div class="empty-message">No ${type} jobs yet.</div>`;
    };
    if (commandList) commandList.innerHTML = renderTasks("command");
    if (backupList) backupList.innerHTML = renderTasks("backup");
    const progress = document.getElementById("scheduled-backup-progress");
    if (progress) progress.innerHTML = activeJobs.map((job) => {
      const uploading = job.status === "uploading";
      const percent = Number(job.progress || 0);
      return `<div class="scheduled-backup-job"><div class="backup-job-header"><div><strong>${uploading ? "Copying backup off-site" : "Backup in progress"}</strong><small>${escapeHtml(job.message || job.status)}</small></div><strong>${Number.isFinite(percent) ? `${percent}%` : "In progress"}</strong></div><div class="upload-progress-track"><div class="upload-progress-bar" style="width: ${Number.isFinite(percent) ? percent : 0}%"></div></div></div>`;
    }).join("");
    const runs = document.getElementById("schedule-runs");
    const recentRuns = [...(data.runs || [])].sort(
      (left, right) => parseUtcTimestamp(right.started_at) - parseUtcTimestamp(left.started_at),
    );
    const activityTitle = (run) => {
      const name = taskNames.get(Number(run.task_id)) || run.task_type;
      if (run.task_type === "command" && run.status === "complete") {
        return `Sent command “${name}”`;
      }
      const actions = {
        complete: "Completed",
        failed: "Could not complete",
        running: "Started",
      };
      const action = actions[run.status] || `${run.status.charAt(0).toUpperCase()}${run.status.slice(1)}`;
      return `${action} ${run.task_type} “${name}”`;
    };
    runs.innerHTML = recentRuns.length ? recentRuns.map((run) => {
      const startedAt = parseUtcTimestamp(run.started_at);
      const activityTimezone = localScheduleTimezone();
      return `<div class="schedule-run">
        <time datetime="${escapeHtml(run.started_at)}">
          <strong>${escapeHtml(startedAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZone: activityTimezone }))}</strong>
          <span>${escapeHtml(startedAt.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric", timeZone: activityTimezone }))}</span>
        </time>
        <div class="schedule-run-detail">
          <strong>${escapeHtml(activityTitle(run))}</strong>
          ${run.detail ? `<small>${escapeHtml(run.detail)}</small>` : ""}
        </div>
      </div>`;
    }).join("") : '<div class="empty-message">No recent activity yet.</div>';
    const pagination = data.runs_pagination || { page: 1, pages: 1, total: recentRuns.length };
    scheduleRunsPage = Number(pagination.page || 1);
    const paginationElement = document.getElementById("schedule-runs-pagination");
    if (paginationElement) {
      paginationElement.hidden = Number(pagination.pages || 1) <= 1;
      paginationElement.querySelector("button:first-child").disabled = scheduleRunsPage <= 1;
      paginationElement.querySelector("button:last-child").disabled = scheduleRunsPage >= Number(pagination.pages || 1);
      document.getElementById("schedule-runs-page-label").textContent = `Page ${scheduleRunsPage} of ${Number(pagination.pages || 1)} · ${Number(pagination.total || 0)} entries`;
    }
  } catch (error) {
    if (commandList) commandList.textContent = error.message;
    if (backupList) backupList.textContent = error.message;
  }
}

function changeScheduleRunsPage(direction) {
  scheduleRunsPage = Math.max(1, scheduleRunsPage + Number(direction));
  loadServerSchedules();
}

function friendlyScheduleSummary(frequency, runHour = 0, runWeekday = 6, cronExpression = "", timezone = localScheduleTimezone()) {
  const time = `${String(runHour ?? 0).padStart(2, "0")}:00`;
  const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  if (frequency === "hourly") return `At the start of every hour · ${timezone}`;
  if (frequency === "daily") return `Every day at ${time} · ${timezone}`;
  if (frequency === "weekly") return `Every ${weekdays[runWeekday] || "Sunday"} at ${time} · ${timezone}`;
  if (frequency === "monthly") return `On the first day of every month at ${time} · ${timezone}`;
  if (frequency === "custom") return `Custom ${cronExpression || "schedule"} · ${timezone}`;
  return `Scheduled time · ${timezone}`;
}

function updateScheduleWhen(select) {
  const fields = select.closest(".schedule-when-fields");
  if (!fields) return;
  fields.querySelector("[name=run_hour]").value = "0";
  fields.querySelector("[name=run_weekday]").value = "6";
  const summary = fields.querySelector(".selected-schedule-summary");
  if (select.value === "custom") {
    openCustomSchedule(select);
  } else {
    fields.querySelector("[name=cron_expression]").value = "";
    summary.textContent = friendlyScheduleSummary(select.value, 0, 6);
  }
}

let customScheduleSelect = null;

function customCronValue(id) {
  return document.getElementById(id).value.trim();
}

function customWeekdayValue() {
  const checked = [...document.querySelectorAll("#custom-cron-weekday input:checked")].map((item) => item.value);
  return checked.length === 7 ? "*" : checked.join(",");
}

function updateCustomSchedulePreview() {
  const expression = [customCronValue("custom-cron-minute"), customCronValue("custom-cron-hour"), customCronValue("custom-cron-monthday"), customCronValue("custom-cron-month"), customWeekdayValue()].join(" ");
  document.getElementById("custom-cron-preview").textContent = expression;
  document.getElementById("custom-cron-description").textContent = `Custom schedule in ${localScheduleTimezone()}`;
  return expression;
}

function openCustomSchedule(select) {
  customScheduleSelect = select;
  const saved = select.closest(".schedule-when-fields").querySelector("[name=cron_expression]").value;
  if (saved) {
    const [minute, hour, monthday, month, weekday] = saved.split(" ");
    document.getElementById("custom-cron-minute").value = minute;
    document.getElementById("custom-cron-hour").value = hour;
    document.getElementById("custom-cron-monthday").value = monthday;
    document.getElementById("custom-cron-month").value = month;
    const selected = weekday === "*" ? null : new Set(weekday.split(","));
    document.querySelectorAll("#custom-cron-weekday input").forEach((item) => item.checked = !selected || selected.has(item.value));
  }
  updateCustomSchedulePreview();
  document.getElementById("custom-schedule-modal").hidden = false;
}

function closeCustomSchedule(keepCustom) {
  document.getElementById("custom-schedule-modal").hidden = true;
  if (!keepCustom && customScheduleSelect) {
    const hidden = customScheduleSelect.closest(".schedule-when-fields").querySelector("[name=cron_expression]");
    if (!hidden.value) {
      const form = customScheduleSelect.closest("form");
      customScheduleSelect.value = form?.elements.task_type.value === "backup" ? "daily" : "hourly";
      updateScheduleWhen(customScheduleSelect);
    }
  }
  customScheduleSelect = null;
}

function saveCustomSchedule() {
  if (!customScheduleSelect) return;
  const expression = updateCustomSchedulePreview();
  if (expression.split(" ").some((part) => !part)) return alert("Choose at least one day and complete every field.");
  const fields = customScheduleSelect.closest(".schedule-when-fields");
  fields.querySelector("[name=cron_expression]").value = expression;
  fields.querySelector(".selected-schedule-summary").innerHTML = `Custom <code>${escapeHtml(expression)}</code> · ${escapeHtml(localScheduleTimezone())}`;
  closeCustomSchedule(true);
}

document.querySelectorAll("#custom-schedule-modal input").forEach((input) => input.addEventListener("input", updateCustomSchedulePreview));

async function createServerSchedule(event) {
  event.preventDefault();
  const page = automationPage();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  const scheduleId = payload.schedule_id;
  delete payload.schedule_id;
  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/schedules${scheduleId ? `/${scheduleId}` : ""}`,
    {
      method: scheduleId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const data = await response.json();
  if (!response.ok) return alert(data.error || "Unable to add schedule");
  closeServerScheduleModal();
  await loadServerSchedules();
}

function openServerScheduleModal(taskType) {
  const modal = document.getElementById(`${taskType}-schedule-modal`);
  const form = modal?.querySelector(".friendly-schedule-form");
  if (!modal || !form) return;
  resetServerScheduleForm(form);
  modal.querySelector("h2").textContent = `Add ${taskType}`;
  modal.hidden = false;
  form.elements.name.focus();
}

function closeServerScheduleModal(event) {
  if (event && event.target !== event.currentTarget) return;
  document.querySelectorAll("#command-schedule-modal, #backup-schedule-modal").forEach((modal) => {
    modal.hidden = true;
    const form = modal.querySelector(".friendly-schedule-form");
    if (form) resetServerScheduleForm(form);
  });
}

function editServerSchedule(taskId) {
  const task = serverScheduleState.find((item) => Number(item.id) === Number(taskId));
  if (!task) return;
  const form = [...document.querySelectorAll(".friendly-schedule-form")].find((item) => item.elements.task_type.value === task.task_type);
  if (!form) return;
  form.elements.schedule_id.value = task.id;
  form.elements.name.value = task.name || "";
  if (form.elements.command) form.elements.command.value = task.command || "";
  form.elements.frequency.value = task.frequency || "hourly";
  form.elements.run_hour.value = task.run_hour ?? 0;
  form.elements.run_weekday.value = task.run_weekday ?? 6;
  form.elements.cron_expression.value = task.cron_expression || "";
  form.elements.schedule_timezone.value = localScheduleTimezone();
  if (form.elements.retention_count) form.elements.retention_count.value = task.retention_count || 7;
  if (form.elements.remote_destination) form.elements.remote_destination.value = task.remote_destination || "";
  if (form.elements.remote_retention_count) form.elements.remote_retention_count.value = task.remote_retention_count || 30;
  const summary = form.querySelector(".selected-schedule-summary");
  if (summary) summary.textContent = friendlyScheduleSummary(
    task.frequency || "hourly",
    task.run_hour ?? 0,
    task.run_weekday ?? 6,
    task.cron_expression || "",
    localScheduleTimezone(),
  );
  form.querySelector(".schedule-submit-button").textContent = `Save ${task.task_type}`;
  const modal = form.closest(".modal-backdrop");
  modal.querySelector("h2").textContent = `Edit ${task.task_type}`;
  modal.hidden = false;
  form.elements.name.focus();
}

function resetServerScheduleForm(form) {
  form.reset();
  form.elements.schedule_id.value = "";
  form.elements.schedule_timezone.value = localScheduleTimezone();
  form.elements.frequency.value = form.elements.task_type.value === "backup" ? "daily" : "hourly";
  const submit = form.querySelector(".schedule-submit-button");
  submit.textContent = submit.dataset.createLabel;
  updateScheduleWhen(form.elements.frequency);
}

function cancelServerScheduleEdit(form) {
  resetServerScheduleForm(form);
  closeServerScheduleModal();
}

async function runServerScheduleNow(taskId, button) {
  const page = automationPage();
  button.disabled = true;
  button.textContent = "Starting…";
  const response = await fetch(`/api/web/servers/${page.dataset.serverId}/schedules/${taskId}/run`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    button.disabled = false;
    button.textContent = "Run now";
    return alert(data.error || "Unable to start backup");
  }
  const progress = document.getElementById("scheduled-backup-progress");
  if (progress) progress.innerHTML = '<div class="scheduled-backup-job"><strong>Starting backup…</strong></div>';
  window.setTimeout(loadServerSchedules, 300);
}

async function deleteServerSchedule(taskId) {
  const page = automationPage();
  const response = await fetch(
    `/api/web/servers/${page.dataset.serverId}/schedules/${taskId}`,
    { method: "DELETE" },
  );
  const data = await response.json();
  if (!response.ok) return alert(data.error || "Unable to disable schedule");
  loadServerSchedules();
}

loadServerMetrics();
loadServerSchedules();

setInterval(loadServerSchedules, 2500);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadServerSchedules();
});


function renderPluginMonitoringVersionPreview(detection) {
  if (!detection) return "";
  const sourceValue = detection.source_value || (detection.source_label === "Source metadata"
    ? "Expression searched the source response"
    : "Not found");
  const captureStatus = detection.pattern_status === "matched" ? "Expression matched"
    : detection.pattern_status === "not_set" ? "No expression applied"
    : detection.pattern_status === "no_match" ? "No match found"
    : detection.pattern_status === "invalid" ? "Expression is invalid"
    : detection.pattern_status === "uncomparable" ? "Captured value is not comparable"
    : "Expression did not produce a usable value";
  return `<div class="monitoring-preview-version">
    <strong>Version detection</strong>
    <dl>
      <div><dt>${escapeHtml(detection.source_label || "Source value")}</dt><dd><code>${escapeHtml(sourceValue)}</code></dd></div>
      <div><dt>Captured version</dt><dd><code>${escapeHtml(detection.captured_value || "No value captured")}</code></dd><small>${captureStatus}</small></div>
      <div><dt>Comparable version</dt><dd>${detection.comparable ? "Yes" : "No"}</dd></div>
    </dl>
    ${detection.error ? `<span class="monitoring-preview-error">${escapeHtml(detection.error)}</span>` : ""}
  </div>`;
}

function renderPluginMonitoringPreview(data) {
  const isUpdate = data.update_available === true;
  const isCurrent = data.status === "Current";
  const hasError = data.status === "Check failed";
  const statusClass = isUpdate ? "is-update" : isCurrent ? "is-current" : hasError ? "is-error" : "is-pending";
  const statusIcon = isUpdate ? "fa-circle-up" : isCurrent ? "fa-circle-check" : hasError ? "fa-triangle-exclamation" : "fa-circle-question";
  const statusLabel = isUpdate
    ? `New version ${data.latest_version || "available"}`
    : isCurrent ? "Up to date" : hasError ? "Could not check" : data.status || "Check complete";
  const installed = data.installed_version || data.installed_comparison || "Unknown";
  const latest = data.latest_version || "Unknown";
  const release = data.release_url?.startsWith("https://")
    ? `<a href="${escapeHtml(data.release_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(latest)}</a>`
    : escapeHtml(latest);
  const source = data.source_url?.startsWith("https://")
    ? `<a href="${escapeHtml(data.source_url)}" target="_blank" rel="noopener noreferrer">Open source page</a>`
    : "Not available";
  const download = data.download_url?.startsWith("https://")
    ? `<a class="monitoring-preview-action" href="${escapeHtml(data.download_url)}" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i> Open download</a>`
    : `<span class="monitoring-preview-no-link">No direct download link found</span>`;
  const assetPreview = data.asset_preview;
  const assetList = Array.isArray(assetPreview?.available_assets) ? assetPreview.available_assets : [];
  const assetNames = assetList.length
    ? `${assetList.slice(0, 6).map((name) => escapeHtml(name)).join(", ")}${assetList.length > 6 ? `, and ${assetList.length - 6} more` : ""}`
    : "";
  const assetDetail = assetPreview
    ? `<div><dt>GitHub JAR selection</dt><dd>${escapeHtml(assetPreview.selected_asset || assetPreview.message || "No single JAR selected")}</dd>${assetNames ? `<small>Release JARs: ${assetNames}</small>` : ""}</div>`
    : "";
  const versionDetection = renderPluginMonitoringVersionPreview(data.version_preview);

  return `<div class="monitoring-preview-card ${statusClass}">
    <div class="monitoring-preview-heading">
      <strong><i class="fa-solid ${statusIcon}" aria-hidden="true"></i>${escapeHtml(statusLabel)}</strong>
      <span class="monitoring-preview-only">Preview only</span>
    </div>
    <dl class="monitoring-preview-details">
      <div><dt>Installed</dt><dd>${escapeHtml(installed)}</dd><small>${escapeHtml(data.installed_comparison_source || "JAR metadata")}</small></div>
      <div><dt>Latest</dt><dd>${release}</dd></div>
      <div><dt>Compatibility</dt><dd>${escapeHtml(data.compatibility || "Unknown")}</dd></div>
      <div><dt>Source</dt><dd>${source}</dd></div>
      ${assetDetail}
    </dl>
    ${versionDetection}
    <div class="monitoring-preview-footer">${download}${data.error ? `<span class="monitoring-preview-error">${escapeHtml(data.error)}</span>` : ""}${renderPluginComparisonDetails(data.comparison_details)}</div>
  </div>`;
}

function updateCheckFeedback(scope, updates, serverId) {
  const results = Array.isArray(updates) ? updates : [];
  const pluginsLink = serverId
    ? { label: "Open Plugins", href: `/servers/${serverId}/plugins` }
    : null;
  if (scope === "paper") {
    const paper = results.find((item) => item.component === "@paper") || results[0];
    if (!paper) {
      return { message: "Paper update check finished, but no version details were returned.", type: "warning" };
    }
    const installed = paper.installed_version;
    const latest = paper.latest_version;
    if (paper.update_available) {
      return {
        message: latest
          ? `Paper update available: ${latest}${installed ? ` (installed: ${installed})` : ""}.`
          : "A Paper update is available. Open Server settings to review it.",
        type: "info",
      };
    }
    if (paper.status === "Current") {
      return { message: installed ? `Paper is up to date (${installed}).` : "Paper is up to date.", type: "success" };
    }
    if (paper.status === "Incompatible") {
      return {
        message: latest
          ? `Paper ${latest} is not marked compatible with this Minecraft version.`
          : "A newer Paper build may not be compatible with this Minecraft version.",
        type: "warning",
      };
    }
    if (paper.status === "Check failed") {
      return { message: `Couldn't check Paper updates: ${paper.error || "Try again shortly."}`, type: "error" };
    }
    return { message: `Paper update check: ${paper.status || "no result"}.`, type: "warning" };
  }

  const found = results.filter((item) => item.update_available);
  if (found.length) {
    return {
      message: `${found.length} plugin update${found.length === 1 ? "" : "s"} found. Open Plugins to review${found.length === 1 ? " it" : " them"}.`,
      type: "info",
      link: pluginsLink,
    };
  }
  const incompatible = results.filter((item) => item.status === "Incompatible");
  if (incompatible.length) {
    return {
      message: `${incompatible.length} plugin version${incompatible.length === 1 ? "" : "s"} may not match this Minecraft version. Open Plugins to review.`,
      type: "warning",
      link: pluginsLink,
    };
  }
  const failures = results.filter((item) => item.status === "Check failed");
  const checked = results.filter((item) => ["Current", "Update available", "Incompatible", "Compatibility unknown", "Check failed"].includes(item.status));
  const skipped = results.length - checked.length;
  if (!results.length) return { message: "No plugins were found to check.", type: "info" };
  if (!checked.length) {
    return { message: "No plugins are set up for update checks yet. Open Plugins to configure them.", type: "warning", link: pluginsLink };
  }
  if (failures.length) {
    return {
      message: `${checked.length} plugin${checked.length === 1 ? " was" : "s were"} checked; ${failures.length} couldn't be checked. Open Plugins for details.`,
      type: "warning",
      link: pluginsLink,
    };
  }
  return {
    message: skipped
      ? `No updates found among ${checked.length} checked plugin${checked.length === 1 ? "" : "s"}; ${skipped} skipped.`
      : `No updates found for ${checked.length} checked plugin${checked.length === 1 ? "" : "s"}.`,
    type: "success",
  };
}

async function checkPluginUpdate(filename, button) {
  const page = document.querySelector(".plugins-page[data-server-id]");
  const plugin = pluginData.find((item) => item.filename === filename);
  if (!page || !plugin || plugin.enabled !== true || plugin.previous_version === true) return;

  const icon = button?.querySelector("i");
  const label = button?.querySelector("span");
  const originalIconClass = icon?.className;
  const originalLabel = label?.textContent;
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  if (icon) icon.className = "fa-solid fa-spinner fa-spin";
  if (label) label.textContent = "Checking";

  const progressToast = showToast(`Checking updates for ${plugin.name}…`, "info", 0, { persistent: true });
  try {
    const response = await nativeFetch(`/api/web/servers/${page.dataset.serverId}/plugins/check-update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    });
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) {
      progressToast?.remove();
      return;
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to check plugin updates.");
    if (data.skipped) {
      progressToast?.remove();
      return;
    }

    const update = data.update;
    if (update) plugin.update = update;
    plugin.suggested_filename = update?.suggested_filename || null;
    renderPlugins();
    await refreshNotifications(false);

    let message;
    let type;
    if (update?.update_available) {
      message = update.latest_version
        ? `Update available for ${plugin.name}: ${update.latest_version}.`
        : `An update is available for ${plugin.name}.`;
      type = "info";
    } else if (update?.status === "Current") {
      message = update.installed_version
        ? `${plugin.name} is up to date (${update.installed_version}).`
        : `${plugin.name} is up to date.`;
      type = "success";
    } else if (update?.status === "Incompatible") {
      message = update.latest_version
        ? `${plugin.name} has a newer version (${update.latest_version}) that may not match this Minecraft version.`
        : `A newer version of ${plugin.name} may not match this Minecraft version.`;
      type = "warning";
    } else if (update?.status === "Check failed") {
      message = `Couldn't check ${plugin.name}: ${update.error || "Try again shortly."}`;
      type = "error";
    } else if (update?.status === "Monitoring disabled") {
      message = `Update checks are disabled for ${plugin.name}.`;
      type = "info";
    } else {
      message = `No update source is configured for ${plugin.name}. Open Update settings to configure it.`;
      type = "warning";
    }
    updateToast(progressToast, message, type, { timeout: 6500 });
  } catch (error) {
    updateToast(progressToast, error.message || "Unable to check plugin updates.", "error", { timeout: 6500 });
  } finally {
    if (icon && originalIconClass) icon.className = originalIconClass;
    if (label && originalLabel !== undefined) label.textContent = originalLabel;
    button?.removeAttribute("aria-busy");
    if (button) button.disabled = false;
  }
}

async function correctPluginFilename(filename, button) {
  const page = document.querySelector(".plugins-page[data-server-id]");
  const plugin = pluginData.find((item) => item.filename === filename);
  if (!page || !plugin || plugin.enabled !== true || !plugin.suggested_filename) return;

  const icon = button?.querySelector("i");
  const label = button?.querySelector("span");
  const originalIconClass = icon?.className;
  const originalLabel = label?.textContent;
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  if (icon) icon.className = "fa-solid fa-spinner fa-spin";
  if (label) label.textContent = "Correcting";

  const progressToast = showToast(`Correcting ${plugin.name}'s filename…`, "info", 0, { persistent: true });
  try {
    const response = await nativeFetch(`/api/web/servers/${page.dataset.serverId}/plugins/correct-filename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    });
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) {
      progressToast?.remove();
      return;
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to correct the plugin filename.");
    await updatePluginsPage();
    await refreshNotifications(false);
    updateToast(progressToast, `${plugin.name} filename corrected to ${data.filename}.`, "success", { timeout: 6500 });
  } catch (error) {
    updateToast(progressToast, error.message || "Unable to correct the plugin filename.", "error", { timeout: 6500 });
  } finally {
    if (icon && originalIconClass) icon.className = originalIconClass;
    if (label && originalLabel !== undefined) label.textContent = originalLabel;
    button?.removeAttribute("aria-busy");
    if (button) button.disabled = false;
  }
}

async function checkMonitoredUpdates(button, scope) {
  const page = button.closest("[data-server-id]");
  if (!page) return;
  const icon = button.querySelector("i");
  const label = button.querySelector("span");
  const originalIconClass = icon?.className;
  const originalLabel = label?.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (icon) icon.className = "fa-solid fa-spinner fa-spin";
  if (label) label.textContent = "Checking";
  let progressToast = null;
  try {
    // Plugin checks run in the background so the toast can show per-plugin progress.
    const response = await nativeFetch(`/api/web/servers/${page.dataset.serverId}/${scope}/check-updates`, {method: "POST"});
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) return;
    let data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to check for updates");
    if (scope === "plugins") {
      progressToast = showToast(
        `Checking plugin updates · 0 of ${data.progress?.total || 0} checked`,
        "info",
        0,
        { persistent: true, progress: data.progress },
      );
      data = await waitForPluginUpdateCheck(page.dataset.serverId, data.job_id, progressToast);
      if (!data) return;
      if (document.querySelector(`.plugins-page[data-server-id="${CSS.escape(page.dataset.serverId)}"]`)) {
        await updatePluginsPage();
      }
      await refreshNotifications(false);
      const feedbackMessage = updateCheckFeedback(scope, data.updates, page.dataset.serverId);
      updateToast(progressToast, feedbackMessage.message, feedbackMessage.type, {
        link: feedbackMessage.link,
        timeout: 6000,
      });
    } else {
      if (page.isConnected) await loadPaperVersionStatus();
      await refreshNotifications(false);
      const feedbackMessage = updateCheckFeedback(scope, data.updates, page.dataset.serverId);
      showToast(feedbackMessage.message, feedbackMessage.type, 4500, { link: feedbackMessage.link });
    }
  } catch (error) {
    const message = error.message || "Unable to check for updates.";
    if (progressToast) updateToast(progressToast, message, "error", { timeout: 6500 });
    else showToast(message, "error");
  } finally {
    if (icon && originalIconClass) icon.className = originalIconClass;
    if (label && originalLabel !== undefined) label.textContent = originalLabel;
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

async function waitForPluginUpdateCheck(serverId, jobId, toast) {
  const startedAt = Date.now();
  let lastProgressMessage = toast?.querySelector(".toast-message")?.textContent || "";
  while (Date.now() - startedAt < 30 * 60 * 1000) {
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const response = await nativeFetch(`/api/web/servers/${serverId}/plugins/update-progress`, { cache: "no-store" });
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) return null;
    const progress = await response.json();
    if (!response.ok) throw new Error(progress.error || "Unable to read update check progress.");
    if (progress.job_id !== jobId) throw new Error("The plugin update check is no longer available.");
    if (progress.status === "complete") return progress;
    if (progress.status === "failed") throw new Error(progress.error || "Unable to check plugin updates.");

    const completed = Number(progress.completed) || 0;
    const total = Number(progress.total) || 0;
    const current = progress.current_plugin ? `Checking ${progress.current_plugin} · ` : "";
    const message = total
      ? `${current}${completed} of ${total} plugins checked`
      : "Checking plugin updates…";
    if (message !== lastProgressMessage) {
      updateToast(toast, message, "info", { progress, timeout: 0 });
      lastProgressMessage = message;
    }
  }
  throw new Error("The plugin update check is taking longer than expected. You can review its results on the Plugins page.");
}

function openPluginMonitoring(index) {
  const plugin = pluginData[index];
  const modal = document.getElementById("plugin-monitoring-modal");
  const page = document.querySelector(".plugins-page");
  if (!plugin || !modal || !page) return;
  const config = plugin.update?.monitoring || {};
  const selectedMode = config.selection || config.mode || "disabled";
  pluginMonitoringPreviousFocus = document.activeElement;
  modal.dataset.filename = plugin.filename;
  modal.dataset.serverId = page.dataset.serverId;
  modal.dataset.globalSettings = JSON.stringify(config.global_settings || {});
  modal.dataset.globalAvailable = String(Boolean(config.global_available));
  modal.dataset.globalError = config.error || "";
  modal.dataset.monitoringMode = selectedMode;
  modal.dataset.manualSettings = "";
  document.getElementById("plugin-monitoring-name").textContent = plugin.name;
  document.getElementById("plugin-monitoring-mode").value = selectedMode;
  document.getElementById("plugin-monitoring-provider").value = config.provider || "github";
  document.getElementById("plugin-monitoring-project").value = config.project || "";
  for (const field of ["version", "link", "installed", "asset"]) {
    document.getElementById(`plugin-monitoring-${field}-pattern`).value = config[`${field}_pattern`] || "";
  }
  document.getElementById("plugin-monitoring-expressions").open = false;
  updatePluginMonitoringFields();
  if (config.error) setPluginMonitoringFeedback(config.error, "error");
  modal.hidden = false;
  document.getElementById("plugin-monitoring-mode").focus();
}

function closePluginMonitoring() {
  const modal = document.getElementById("plugin-monitoring-modal");
  if (modal) modal.hidden = true;
  const previousFocus = pluginMonitoringPreviousFocus;
  pluginMonitoringPreviousFocus = null;
  if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
}

function clearMonitoringPreview() {
  window.clearTimeout(pluginMonitoringExpressionTimer);
  const modal = document.getElementById("plugin-monitoring-modal");
  if (modal) {
    modal._monitoringPreviewData = null;
    modal._previewEvaluationSequence = (modal._previewEvaluationSequence || 0) + 1;
  }
  const output = document.getElementById("plugin-monitoring-preview");
  if (output) { output.hidden = true; output.innerHTML = ""; }
  const expressionPreview = document.getElementById("plugin-monitoring-expression-preview");
  if (expressionPreview) { expressionPreview.hidden = true; expressionPreview.innerHTML = ""; }
  setPluginMonitoringFeedback("");
}

let pluginMonitoringExpressionTimer = null;

function setPluginMonitoringFeedback(message, state = "error") {
  const feedback = document.getElementById("plugin-monitoring-error");
  if (!feedback) return;
  feedback.textContent = message || "";
  feedback.hidden = !message;
  feedback.dataset.state = state;
  feedback.setAttribute("role", state === "error" ? "alert" : "status");
  feedback.setAttribute("aria-live", state === "error" ? "assertive" : "polite");
}

function updatePluginMonitoringFields(providerChanged = false) {
  const modal = document.getElementById("plugin-monitoring-modal");
  const modeSelect = document.getElementById("plugin-monitoring-mode");
  const mode = modeSelect.value;
  const previousMode = modal.dataset.monitoringMode || mode;
  const enabled = mode === "custom";
  const editable = enabled;
  const usesGlobal = mode === "global";
  const globalAvailable = modal.dataset.globalAvailable === "true";
  const globalOption = modeSelect.querySelector('option[value="global"]');
  globalOption.hidden = !globalAvailable && !usesGlobal;
  globalOption.disabled = !globalAvailable;
  globalOption.textContent = globalAvailable ? "Use shared settings" : "Shared settings unavailable";

  const fieldNames = ["provider", "project", "version_pattern", "link_pattern", "installed_pattern", "asset_pattern"];
  const fieldIds = ["provider", "project", "version-pattern", "link-pattern", "installed-pattern", "asset-pattern"];
  const readFields = () => Object.fromEntries(fieldNames.map((name, index) => [name, document.getElementById(`plugin-monitoring-${fieldIds[index]}`).value]));
  const fillFields = (fields) => fieldIds.forEach((field, index) => {
    document.getElementById(`plugin-monitoring-${field}`).value = fields[fieldNames[index]] || "";
  });
  if (mode === "global" && previousMode !== "global") {
    if (previousMode === "custom") modal.dataset.manualSettings = JSON.stringify(readFields());
    try { fillFields(JSON.parse(modal.dataset.globalSettings || "{}")); } catch { /* Empty global settings remain blank. */ }
  } else if (mode === "custom" && previousMode !== "custom" && modal.dataset.manualSettings) {
    try { fillFields(JSON.parse(modal.dataset.manualSettings)); } catch { /* Keep current fields if saved values are malformed. */ }
  }
  modal.dataset.monitoringMode = mode;
  document.getElementById("plugin-monitoring-custom").hidden = !enabled;
  document.getElementById("plugin-monitoring-global").hidden = !usesGlobal;
  const globalSettings = (() => {
    try { return JSON.parse(modal.dataset.globalSettings || "{}"); } catch { return {}; }
  })();
  const globalProviderNames = { github: "GitHub Releases", modrinth: "Modrinth", jenkins: "Jenkins", custom: "Custom URL" };
  const globalSummary = document.getElementById("plugin-monitoring-global-source");
  const globalNotes = document.getElementById("plugin-monitoring-global-notes");
  if (globalSummary) globalSummary.textContent = globalAvailable
    ? `${globalProviderNames[globalSettings.provider] || globalSettings.provider} · ${globalSettings.project || ""}`
    : modal.dataset.globalError || "No shared update settings are available for this plugin.";
  if (globalNotes) {
    globalNotes.textContent = globalSettings.notes || "";
    globalNotes.hidden = !globalAvailable || !globalSettings.notes;
  }
  const promoteButton = document.getElementById("plugin-monitoring-promote-button");
  if (promoteButton) {
    promoteButton.hidden = !enabled;
    const promoteLabel = document.getElementById("plugin-monitoring-promote-label");
    if (promoteLabel) promoteLabel.textContent = globalAvailable ? "Update global" : "Make global";
  }
  document.getElementById("plugin-monitoring-provider").disabled = !editable;
  const provider = document.getElementById("plugin-monitoring-provider").value;
  const documentSource = ["jenkins", "custom"].includes(provider);
  const project = document.getElementById("plugin-monitoring-project");
  const previewButton = document.getElementById("plugin-monitoring-preview-button");
  if (providerChanged) {
    project.value = "";
    for (const field of ["version", "link", "installed", "asset"]) document.getElementById(`plugin-monitoring-${field}-pattern`).value = "";
  }
  project.required = editable;
  project.disabled = !editable;
  previewButton.hidden = !(enabled || (usesGlobal && globalAvailable));
  previewButton.disabled = !(enabled || (usesGlobal && globalAvailable));
  const hints = {
    github: "Use the plugin's GitHub project page. We check its latest stable release.",
    modrinth: "Use the plugin's Modrinth project page. We prefer stable Paper releases and use beta only when no stable release is available.",
    jenkins: "Use the plugin's build page. We compare successful builds automatically.",
    custom: "Use a public plugin page that shows its latest version.",
  };
  project.placeholder = documentSource ? "https://example.org/releases" : "Project identifier or URL";
  document.getElementById("plugin-monitoring-hint").textContent = hints[provider];
  const version = document.getElementById("plugin-monitoring-version-pattern");
  version.required = editable && provider === "custom";
  document.getElementById("plugin-monitoring-version-hint").textContent = provider === "jenkins"
    ? "Usually leave this blank. Build pages are compared automatically."
    : documentSource
    ? 'Required. Match the metadata response and capture the version, for example "version"\\s*:\\s*"([^"]+)". Test shows the captured value.'
    : "Optional. Extract a comparable version from the release tag/version number, for example ^v?([0-9.]+). Test shows the tag and captured value; edits preview against the same fetched release.";
  document.getElementById("plugin-monitoring-link-fields").hidden = !documentSource;
  document.getElementById("plugin-monitoring-asset-fields").hidden = provider !== "github";
  for (const field of ["version", "link", "installed", "asset"]) {
    document.getElementById(`plugin-monitoring-${field}-pattern`).disabled = !editable || (field === "link" && !documentSource);
  }
  document.getElementById("plugin-monitoring-asset-pattern").disabled = !editable || provider !== "github";
  clearMonitoringPreview();
}

function pluginMonitoringPayload() {
  const modal = document.getElementById("plugin-monitoring-modal");
  const mode = document.getElementById("plugin-monitoring-mode").value;
  let fields = {};
  if (mode === "global") {
    try { fields = JSON.parse(modal.dataset.globalSettings || "{}"); } catch { fields = {}; }
  }
  const fieldValue = (name, inputId) => Object.hasOwn(fields, name)
    ? fields[name]
    : document.getElementById(inputId).value;
  const provider = fields.provider || document.getElementById("plugin-monitoring-provider").value;
  return {
    filename: modal.dataset.filename,
    mode,
    provider,
    project: String(fieldValue("project", "plugin-monitoring-project")).trim(),
    version_pattern: fieldValue("version_pattern", "plugin-monitoring-version-pattern"),
    link_pattern: ["jenkins", "custom"].includes(provider) ? fieldValue("link_pattern", "plugin-monitoring-link-pattern") : "",
    installed_pattern: fieldValue("installed_pattern", "plugin-monitoring-installed-pattern"),
    asset_pattern: provider === "github" ? fieldValue("asset_pattern", "plugin-monitoring-asset-pattern") : "",
  };
}

function previewPluginMonitoringExpressionChanged() {
  const modal = document.getElementById("plugin-monitoring-modal");
  const cached = modal?._monitoringPreviewData;
  if (!cached || typeof cached.preview_input !== "string") {
    clearMonitoringPreview();
    return;
  }
  window.clearTimeout(pluginMonitoringExpressionTimer);
  const sequence = modal._previewEvaluationSequence = (modal._previewEvaluationSequence || 0) + 1;
  pluginMonitoringExpressionTimer = window.setTimeout(async () => {
    const settings = pluginMonitoringPayload();
    const fingerprint = JSON.stringify(settings);
    const request = {
      ...settings,
      evaluate_only: true,
      preview_input: cached.preview_input,
      default_version: cached.default_version,
      release_url: cached.release_url,
      asset_data: cached.asset_preview,
    };
    setPluginMonitoringFeedback("Updating the preview against the tested source…", "pending");
    try {
      const response = await nativeFetch(`/api/web/servers/${modal.dataset.serverId}/plugins/monitoring/preview`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(request),
      });
      connectionFailureLatched = false;
      if (handleAuthenticationResponse(response)) return;
      const data = await response.json();
      if (!modal.isConnected || modal.hidden || modal._previewEvaluationSequence !== sequence
          || JSON.stringify(pluginMonitoringPayload()) !== fingerprint) return;
      if (!response.ok) throw new Error(data.error || "Unable to preview this expression.");
      modal._monitoringPreviewData = {...cached, ...data};
      const output = document.getElementById("plugin-monitoring-preview");
      output.innerHTML = renderPluginMonitoringPreview(modal._monitoringPreviewData);
      output.hidden = false;
      const expressionPreview = document.getElementById("plugin-monitoring-expression-preview");
      expressionPreview.innerHTML = renderPluginMonitoringVersionPreview(data.version_preview);
      expressionPreview.hidden = !data.version_preview;
      setPluginMonitoringFeedback(data.status === "Check failed" ? data.error || "The settings need attention." : "Preview updated.", data.status === "Check failed" ? "error" : "success");
    } catch (failure) {
      if (modal.isConnected && modal._previewEvaluationSequence === sequence
          && JSON.stringify(pluginMonitoringPayload()) === fingerprint) {
        setPluginMonitoringFeedback(failure.message || "Unable to preview this expression.", "error");
      }
    }
  }, 180);
}

async function previewPluginMonitoring() {
  const modal = document.getElementById("plugin-monitoring-modal");
  const output = document.getElementById("plugin-monitoring-preview");
  const button = document.getElementById("plugin-monitoring-preview-button");
  const settings = pluginMonitoringPayload();
  const fingerprint = JSON.stringify(settings);
  modal._previewEvaluationSequence = (modal._previewEvaluationSequence || 0) + 1;
  button.disabled = true;
  output.hidden = true;
  output.innerHTML = "";
  setPluginMonitoringFeedback("Checking the release source…", "pending");
  try {
    const response = await nativeFetch(`/api/web/servers/${modal.dataset.serverId}/plugins/monitoring/preview`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: fingerprint,
    });
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) return;
    let data = {};
    try { data = await response.json(); } catch { /* Use the HTTP status below. */ }
    if (!modal.isConnected || modal.hidden || JSON.stringify(pluginMonitoringPayload()) !== fingerprint) return;
    if (!response.ok && !data.version_preview) throw new Error(data.error || "Preview failed");
    modal._monitoringPreviewData = data;
    if (data.status === "Check failed") {
      const message = data.error || "The release was found, but its version could not be compared.";
      setPluginMonitoringFeedback(message, "error");
      showToast(message, "error");
    } else {
      const message = data.update_available
        ? `Settings test found a newer version: ${data.latest_version || "available"}.`
        : `Settings test complete: ${data.status || "source checked"}.`;
      setPluginMonitoringFeedback(message, "success");
      showToast(message, "success");
    }
    output.innerHTML = renderPluginMonitoringPreview(data);
    output.hidden = false;
    const expressionPreview = document.getElementById("plugin-monitoring-expression-preview");
    expressionPreview.innerHTML = renderPluginMonitoringVersionPreview(data.version_preview);
    expressionPreview.hidden = !data.version_preview;
  } catch (failure) {
    if (modal.isConnected && JSON.stringify(pluginMonitoringPayload()) === fingerprint) {
      const message = failure instanceof TypeError
        ? "Unable to connect to the server."
        : failure.message || "Unable to test these settings.";
      setPluginMonitoringFeedback(message, "error");
      showToast(message, "error");
    }
  } finally {
    button.disabled = false;
  }
}

async function savePluginMonitoring(event) {
  event.preventDefault();
  const modal = document.getElementById("plugin-monitoring-modal");
  const button = event.target.querySelector('[type="submit"]');
  button.disabled = true;
  setPluginMonitoringFeedback("");
  const settings = pluginMonitoringPayload();
  try {
    const response = await fetch(`/api/web/servers/${modal.dataset.serverId}/plugins/monitoring`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(settings),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to save monitoring settings");
    if (!modal.isConnected) return;
    closePluginMonitoring();
    await updatePluginsPage();
  } catch (failure) {
    setPluginMonitoringFeedback(failure.message || "Unable to save monitoring settings.", "error");
  } finally {
    button.disabled = false;
  }
}

async function promotePluginMonitoring() {
  const modal = document.getElementById("plugin-monitoring-modal");
  const button = document.getElementById("plugin-monitoring-promote-button");
  if (!modal || !button || document.getElementById("plugin-monitoring-mode").value !== "custom") return;
  const pluginName = document.getElementById("plugin-monitoring-name").textContent;
  const replacingGlobal = modal.dataset.globalAvailable === "true";
  const prompt = replacingGlobal
    ? `Replace the shared update settings for ${pluginName}? This changes the source for every server using the shared setting.`
    : `Share the update settings for ${pluginName} with all servers?`;
  if (!confirm(prompt)) return;
  button.disabled = true;
  setPluginMonitoringFeedback("");
  try {
    const response = await nativeFetch(`/api/web/servers/${modal.dataset.serverId}/plugins/monitoring/global`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pluginMonitoringPayload()),
    });
    connectionFailureLatched = false;
    if (handleAuthenticationResponse(response)) return;
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to add shared plugin settings.");
    closePluginMonitoring();
    await updatePluginsPage();
    showToast(data.replaced || replacingGlobal
      ? `Shared update settings updated for ${pluginName}.`
      : `Shared update settings added for ${pluginName}.`, "success");
  } catch (failure) {
    const message = failure.message || "Unable to add shared plugin settings.";
    setPluginMonitoringFeedback(message, "error");
    showToast(message, "error");
  } finally {
    button.disabled = false;
  }
}
