(function () {
  const recent = new Map();

  window.showToast = window.showToast || function (message, type = "info", timeout = 4500) {
    const text = String(message || "").trim();
    if (!text) return;
    const now = Date.now();
    if (now - (recent.get(`${type}:${text}`) || 0) < 1200) return;
    recent.set(`${type}:${text}`, now);
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
    toast.innerHTML = `<i class="fa-solid fa-circle-${type === "success" ? "check" : type === "error" ? "exclamation" : "info"}"></i><span></span><button type="button" class="toast-close" aria-label="Dismiss notification">&times;</button>`;
    toast.querySelector("span").textContent = text;
    toast.querySelector("button").addEventListener("click", () => toast.remove());
    region.append(toast);
    requestAnimationFrame(() => toast.classList.add("visible"));
    window.setTimeout(() => {
      toast.classList.remove("visible");
      window.setTimeout(() => toast.remove(), 200);
    }, timeout);
  };

  function clear(field) {
    field?.classList.remove("field-invalid");
    field?.removeAttribute("aria-invalid");
    if (field?.nextElementSibling?.classList.contains("field-error-message")) {
      field.nextElementSibling.remove();
    }
  }

  document.addEventListener("invalid", (event) => {
    const field = event.target;
    if (!field?.matches?.("input, select, textarea")) return;
    const validationMessage = field.validationMessage || "Check this value";
    clear(field);
    field.classList.add("field-invalid");
    field.setAttribute("aria-invalid", "true");
    const error = document.createElement("small");
    error.className = "field-error-message";
    error.textContent = validationMessage;
    field.insertAdjacentElement("afterend", error);
    if (field.type === "password") field.value = "";
    window.showToast("Please correct the highlighted fields.", "error");
  }, true);

  document.addEventListener("input", (event) => clear(event.target));
  document.addEventListener("change", (event) => clear(event.target));

  document.addEventListener("DOMContentLoaded", () => {
    const error = Array.from(document.querySelectorAll(".login-error, .form-error"))
      .find((item) => !item.hidden && item.textContent.trim());
    if (error) {
      document.querySelectorAll("form input[type='password']").forEach((field) => {
        field.value = "";
      });
      window.showToast(error.textContent.trim(), "error");
    }
  });
}());
