(() => {
  const root = document.body.getAttribute("data-root") || "";
  const focus = document.body.getAttribute("data-focus") || "";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const video = document.querySelector(".hero-video");
  if (reduced && video) {
    video.removeAttribute("autoplay");
    video.pause();
  }
  if (focus === "download") {
    const target = document.getElementById("download");
    if (target) target.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  }

  document.querySelectorAll("[data-count]").forEach((el) => {
    const end = Number(el.getAttribute("data-count") || "0");
    if (reduced || !Number.isFinite(end)) {
      el.textContent = String(end);
      return;
    }
    const started = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - started) / 700);
      el.textContent = String(Math.round(end * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

  const table = document.getElementById("pricing-table");
  const body = document.getElementById("pricing-body");
  const fallback = document.getElementById("pricing-fallback");
  if (!table || !body || !fallback) return;

  const cell = (value) => {
    const td = document.createElement("td");
    td.textContent = value == null || value === "" ? "—" : String(value);
    return td;
  };
  const money = (value, unit) => (value == null || value === "" ? "" : `$${value} / ${unit}`);
  const sellText = (plan) => {
    const sell = plan.sell && typeof plan.sell === "object" ? plan.sell : plan;
    if (plan.modality === "image") return money(sell.usd_per_image, "张") || "—";
    if (plan.modality === "video") return money(sell.usd_per_second, "秒") || "—";
    const input = sell.input_usd_per_1m;
    const output = sell.output_usd_per_1m;
    if (input == null && output == null) return plan.price || plan.usd || "—";
    return `输入 ${input == null ? "—" : `$${input}`} · 输出 ${output == null ? "—" : `$${output}`}`;
  };

  fetch(`${root}/portal/api/pricing`, { credentials: "omit" })
    .then((response) => (response.ok ? response.json() : Promise.reject()))
    .then((payload) => {
      const plans = Array.isArray(payload.models) ? payload.models : [];
      if (!plans.length) return;
      body.replaceChildren();
      for (const plan of plans) {
        const row = document.createElement("tr");
        row.append(cell(plan.model || plan.name || "模型"), cell(plan.provider || ""), cell(sellText(plan)));
        body.append(row);
      }
      table.classList.remove("hidden");
      fallback.classList.add("hidden");
    })
    .catch(() => {
      table.classList.add("hidden");
      fallback.classList.remove("hidden");
    });
})();
