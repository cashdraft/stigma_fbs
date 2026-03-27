document.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-orders-page]");
  if (!root) return;

  const bar = document.getElementById("orders-bulk-bar");
  const tbody = document.getElementById("orders-tbody");
  const cbs = () => Array.from(root.querySelectorAll(".order-cb"));
  const master = root.querySelector(".order-cb-master");
  const bulkLabel = document.getElementById("bulk-selected-label");
  const shipmentForm = document.getElementById("shipment-create-form");
  const shipmentOrderInputs = document.getElementById("shipment-order-inputs");
  const shipmentNameHidden = document.getElementById("shipment-name-field");
  const shipmentOpenBtn = document.getElementById("shipment-open-btn");
  const selectPageBtn = document.getElementById("bulk-select-page");
  const clearBtn = document.getElementById("bulk-clear");
  const loadBtn = document.getElementById("btn-load-more");
  const loadWrap = document.getElementById("load-more-wrap");

  const shipmentModal = document.getElementById("shipment-modal");
  const shipmentNameInput = document.getElementById("shipment-name-input");
  const shipmentModalCreate = document.getElementById("shipment-modal-create");
  const shipmentModalClose = document.getElementById("shipment-modal-close");
  const shipmentModalX = document.getElementById("shipment-modal-x");

  let nextPage = parseInt(root.dataset.nextPage || "0", 10) || 0;

  function orderWordRu(n) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return "заказ";
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "заказа";
    return "заказов";
  }

  function bulkSelectionText(n) {
    if (n <= 0) return "";
    if (n === 1) return "Выбран 1 заказ";
    return `Выбрано ${n} ${orderWordRu(n)}`;
  }

  function openShipmentModal() {
    if (!shipmentModal || !shipmentNameInput) return;
    shipmentModal.classList.remove("modal-overlay--hidden");
    shipmentModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    shipmentNameInput.focus();
    shipmentNameInput.select();
  }

  function closeShipmentModal() {
    if (!shipmentModal) return;
    shipmentModal.classList.add("modal-overlay--hidden");
    shipmentModal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

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

    if (bulkLabel) bulkLabel.textContent = bulkSelectionText(n);

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

    if (shipmentOrderInputs) {
      shipmentOrderInputs.innerHTML = "";
      sel.forEach((cb) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "order_ids";
        input.value = cb.value;
        shipmentOrderInputs.appendChild(input);
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

  async function fetchSuggestedShipmentName() {
    const res = await fetch("/orders/shipment/next-name");
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    return (data && data.name) || "";
  }

  if (shipmentOpenBtn) {
    shipmentOpenBtn.addEventListener("click", async () => {
      const n = selected().length;
      if (n === 0 || !shipmentForm || !shipmentNameInput) return;
      sync();
      shipmentNameInput.value = "";
      try {
        shipmentNameInput.value = await fetchSuggestedShipmentName();
      } catch {
        shipmentNameInput.value = "";
      }
      openShipmentModal();
    });
  }

  function submitShipmentForm() {
    if (!shipmentForm || !shipmentNameInput || !shipmentNameHidden) return;
    const name = shipmentNameInput.value.trim();
    if (!name) {
      shipmentNameInput.focus();
      return;
    }
    shipmentNameHidden.value = name;
    sync();
    shipmentForm.submit();
  }

  if (shipmentModalCreate && shipmentForm) {
    shipmentModalCreate.addEventListener("click", () => submitShipmentForm());
  }

  if (shipmentNameInput) {
    shipmentNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitShipmentForm();
      }
    });
  }

  if (shipmentModalClose) {
    shipmentModalClose.addEventListener("click", () => closeShipmentModal());
  }

  if (shipmentModalX) {
    shipmentModalX.addEventListener("click", () => closeShipmentModal());
  }

  if (shipmentModal) {
    shipmentModal.addEventListener("click", (e) => {
      if (e.target === shipmentModal) closeShipmentModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && shipmentModal && !shipmentModal.classList.contains("modal-overlay--hidden")) {
        closeShipmentModal();
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
