document.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-orders-page]");
  if (!root) return;

  const bar = document.getElementById("orders-bulk-bar");
  const tbody = document.getElementById("orders-tbody");
  const cbs = () => Array.from(root.querySelectorAll(".order-cb"));
  const master = root.querySelector(".order-cb-master");
  const countEl = document.getElementById("bulk-selected-count");
  const assembleForm = document.getElementById("assemble-form");
  const assembleInputs = document.getElementById("assemble-inputs");
  const assembleBtn = document.getElementById("assemble-submit");
  const selectPageBtn = document.getElementById("bulk-select-page");
  const clearBtn = document.getElementById("bulk-clear");
  const loadBtn = document.getElementById("btn-load-more");
  const loadWrap = document.getElementById("load-more-wrap");

  let nextPage = parseInt(root.dataset.nextPage || "0", 10) || 0;

  function selected() {
    return cbs().filter((cb) => cb.checked);
  }

  function pageTotal() {
    return cbs().length;
  }

  function sync() {
    const list = cbs();
    const sel = selected();
    const n = sel.length;
    const total = list.length;

    if (countEl) countEl.textContent = String(n);
    if (assembleBtn) assembleBtn.textContent = `Собрать (${n})`;
    if (selectPageBtn) {
      const t = pageTotal();
      selectPageBtn.textContent = t ? `Выбрать все (${t})` : "Выбрать все на странице";
    }

    if (bar) {
      const show = n > 0;
      bar.classList.toggle("bulk-bar--hidden", !show);
      bar.setAttribute("aria-hidden", show ? "false" : "true");
      document.body.classList.toggle("has-bulk-bar", show);
    }

    if (master && total > 0) {
      master.checked = n === total;
      master.indeterminate = n > 0 && n < total;
    }

    if (assembleInputs) {
      assembleInputs.innerHTML = "";
      sel.forEach((cb) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "order_ids";
        input.value = cb.value;
        assembleInputs.appendChild(input);
      });
    }
  }

  root.addEventListener("change", (e) => {
    const t = e.target;
    if (t && t.classList && t.classList.contains("order-cb")) {
      sync();
    }
  });

  if (master) {
    master.addEventListener("change", () => {
      const on = master.checked;
      cbs().forEach((cb) => {
        cb.checked = on;
      });
      sync();
    });
  }

  if (selectPageBtn) {
    selectPageBtn.addEventListener("click", () => {
      cbs().forEach((cb) => {
        cb.checked = true;
      });
      sync();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      cbs().forEach((cb) => {
        cb.checked = false;
      });
      if (master) master.checked = false;
      sync();
    });
  }

  if (assembleForm) {
    assembleForm.addEventListener("submit", (e) => {
      const n = selected().length;
      if (n === 0) {
        e.preventDefault();
        return;
      }
      const mod10 = n % 10;
      const mod100 = n % 100;
      const word =
        mod10 === 1 && mod100 !== 11
          ? "заказ"
          : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)
            ? "заказа"
            : "заказов";
      const msg = `Будут собраны ${n} ${word}. Вы уверены?`;
      if (!window.confirm(msg)) {
        e.preventDefault();
      }
    });
  }

  function loadMoreQuery(page) {
    const p = new URLSearchParams();
    p.set("status", root.dataset.filterStatus || "all");
    p.set("date_from", root.dataset.filterDateFrom || "");
    p.set("date_to", root.dataset.filterDateTo || "");
    p.set("q", root.dataset.filterQ || "");
    p.set("page", String(page));
    return p.toString();
  }

  if (loadBtn && tbody && nextPage >= 2) {
    loadBtn.addEventListener("click", async () => {
      if (!nextPage || loadBtn.disabled) return;
      loadBtn.disabled = true;
      loadBtn.textContent = "Загрузка…";
      try {
        const res = await fetch(`/orders/load-more?${loadMoreQuery(nextPage)}`);
        if (!res.ok) throw new Error(String(res.status));
        const payload = await res.json();
        if (payload.html) {
          tbody.insertAdjacentHTML("beforeend", payload.html);
        }
        if (payload.has_more && payload.next_page) {
          nextPage = payload.next_page;
          root.dataset.nextPage = String(nextPage);
          root.dataset.hasMore = "true";
          if (loadWrap) loadWrap.style.display = "";
        } else {
          nextPage = 0;
          root.dataset.nextPage = "0";
          root.dataset.hasMore = "false";
          if (loadWrap) loadWrap.style.display = "none";
        }
        loadBtn.textContent = "Показать еще";
      } catch (err) {
        loadBtn.textContent = "Ошибка — повторить";
      } finally {
        loadBtn.disabled = false;
      }
      sync();
    });
  }

  sync();
});
