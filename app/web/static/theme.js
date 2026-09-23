(() => {
  const key = "minking-theme";
  const legacy = "lyxh-theme";
  const allowed = ["light", "dark", "blue"];
  const root = document.documentElement;
  const toggle = document.querySelector(".theme-toggle");
  const menu = document.querySelector(".theme-menu");

  function paint(theme) {
    const next = allowed.includes(theme) ? theme : "light";
    root.dataset.theme = next;
    localStorage.setItem(key, next);
    document.querySelectorAll(".theme-menu button[data-theme]").forEach((button) => {
      const on = button.dataset.theme === next;
      button.classList.toggle("active", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function closeMenu() {
    if (!menu || !toggle) return;
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  }

  paint(root.dataset.theme || localStorage.getItem(key) || localStorage.getItem(legacy) || "light");

  if (toggle && menu) {
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    menu.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-theme]");
      if (!button) return;
      paint(button.dataset.theme);
      closeMenu();
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".theme-switch")) closeMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenu();
    });
  }
})();
