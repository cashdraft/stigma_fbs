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

  const shipmentAddForm = document.getElementById("shipment-add-form");
  const shipmentOrderInputsAdd = document.getElementById("shipment-order-inputs-add");
  const shipmentExistingHidden = document.getElementById("shipment-existing-id-field");
  const shipmentAddOpenBtn = document.getElementById("shipment-add-open-btn");
  const shipmentExistingSelect = document.getElementById("shipment-existing-select");
  const selectPageBtn = document.getElementById("bulk-select-page");
  const clearBtn = document.getElementById("bulk-clear");
  const loadBtn = document.getElementById("btn-load-more");
  const loadWrap = document.getElementById("load-more-wrap");

  const shipmentModal = document.getElementById("shipment-modal");
  const shipmentNameInput = document.getElementById("shipment-name-input");
  const shipmentModalCreate = document.getElementById("shipment-modal-create");
  const shipmentModalClose = document.getElementById("shipment-modal-close");
  const shipmentModalX = document.getElementById("shipment-modal-x");

  const shipmentExistingModal = document.getElementById("shipment-existing-modal");
  const shipmentExistingModalAddBtn = document.getElementById("shipment-existing-modal-add-btn");
  const shipmentExistingModalClose = document.getElementById("shipment-existing-modal-close");
  const shipmentExistingModalX = document.getElementById("shipment-existing-modal-x");

  const splitConfirmModal = document.getElementById("split-confirm-modal");
  const splitConfirmText = document.getElementById("split-confirm-text");
  const splitConfirmOk = document.getElementById("split-confirm-ok");
  const splitConfirmCancel = document.getElementById("split-confirm-cancel");
  const splitConfirmModalX = document.getElementById("split-confirm-modal-x");
  const updateOrdersForm = document.getElementById("update-orders-form");
  const updateOrdersBtn = document.getElementById("update-orders-btn");
  const updateAllOrdersBtn = document.getElementById("update-all-orders-btn");
  const updateOrdersScope = document.getElementById("update-orders-scope");
  const updateOrdersStatus = document.getElementById("update-orders-status");
  const periodBtn = document.getElementById("period-btn");
  const periodBtnDates = document.getElementById("period-btn-dates");
  const periodBtnSep = document.getElementById("period-btn-sep");
  const periodBtnReset = document.getElementById("period-btn-reset");
  const periodPopover = document.getElementById("period-popover");
  const periodFrom = document.getElementById("period-from");
  const periodTo = document.getElementById("period-to");
  const periodApply = document.getElementById("period-apply");
  const periodCancel = document.getElementById("period-cancel");
  const searchDateFrom = document.getElementById("search-date-from");
  const searchDateTo = document.getElementById("search-date-to");

  let nextPage = parseInt(root.dataset.nextPage || "0", 10) || 0;

  const ORDERS_FOCUS_Q_KEY = "stigma_fbs_orders_focus_q";
  let ordersSearchInput = null;

  const searchForm = root.querySelector("#orders-search-form");
  const filtersForm = root.querySelector("#orders-filters");
  const qInput = (searchForm && searchForm.querySelector('input[name="q"]')) || null;
  ordersSearchInput = qInput || null;
  let qDebounce = null;

  if (filtersForm) {
    const submitFilters = (opts = {}) => {
      if (opts.focusSearch) {
        try {
          sessionStorage.setItem(ORDERS_FOCUS_Q_KEY, "1");
        } catch (_) {
          /* игнорируем private mode и т.п. */
        }
      }
      filtersForm.submit();
    };

    filtersForm.addEventListener("change", (e) => {
      const el = e.target;
      if (!el || !el.name) return;
      if (el.name === "status" || el.name === "date_from" || el.name === "date_to") {
        submitFilters();
      }
    });
  }

  if (searchForm && qInput) {
    const submitSearch = (opts = {}) => {
      if (opts.focusSearch) {
        try {
          sessionStorage.setItem(ORDERS_FOCUS_Q_KEY, "1");
        } catch (_) {}
      }
      searchForm.submit();
    };
    qInput.addEventListener("input", () => {
      clearTimeout(qDebounce);
      qDebounce = setTimeout(() => submitSearch({ focusSearch: true }), 400);
    });
    qInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        clearTimeout(qDebounce);
        submitSearch({ focusSearch: true });
      }
    });
  }

  function periodLabel(from, to) {
    return `Период: ${from || "..."} – ${to || "..."}`;
  }

  function openPeriodPopover() {
    if (!periodPopover) return;
    periodPopover.classList.remove("period-popover--hidden");
  }

  function closePeriodPopover() {
    if (!periodPopover) return;
    periodPopover.classList.add("period-popover--hidden");
  }

  function applyPeriod(from, to) {
    if (searchDateFrom) searchDateFrom.value = from || "";
    if (searchDateTo) searchDateTo.value = to || "";
    if (periodBtn && periodBtnDates) {
      if (from || to) {
        periodBtn.classList.add("period-btn--selected");
        periodBtnDates.classList.remove("period-btn-dates--hidden");
        periodBtnDates.textContent = `: ${from || "..."} – ${to || "..."}`;
        if (periodBtnSep) periodBtnSep.classList.remove("period-btn-sep--hidden");
        if (periodBtnReset) periodBtnReset.classList.remove("period-btn-reset--hidden");
      } else {
        periodBtn.classList.remove("period-btn--selected");
        periodBtnDates.classList.add("period-btn-dates--hidden");
        periodBtnDates.textContent = "";
        if (periodBtnSep) periodBtnSep.classList.add("period-btn-sep--hidden");
        if (periodBtnReset) periodBtnReset.classList.add("period-btn-reset--hidden");
      }
    }
    if (searchForm) searchForm.submit();
  }

  if (periodBtn) {
    periodBtn.addEventListener("click", (e) => {
      if (periodBtnReset && periodBtnReset.contains(e.target)) {
        e.preventDefault();
        e.stopPropagation();
        if (periodFrom) periodFrom.value = "";
        if (periodTo) periodTo.value = "";
        applyPeriod("", "");
        return;
      }
      if (!periodPopover) return;
      const hidden = periodPopover.classList.contains("period-popover--hidden");
      if (hidden) openPeriodPopover();
      else closePeriodPopover();
    });
  }

  if (periodCancel) {
    periodCancel.addEventListener("click", () => closePeriodPopover());
  }

  if (periodApply) {
    periodApply.addEventListener("click", () => {
      const from = periodFrom ? periodFrom.value : "";
      const to = periodTo ? periodTo.value : "";
      closePeriodPopover();
      applyPeriod(from, to);
    });
  }

  document.addEventListener("click", (e) => {
    if (!periodPopover || periodPopover.classList.contains("period-popover--hidden")) return;
    const t = e.target;
    if (
      periodPopover.contains(t) ||
      (periodBtn && periodBtn.contains(t))
    ) {
      return;
    }
    closePeriodPopover();
  });

  if (updateOrdersForm && updateOrdersBtn && updateOrdersStatus) {
    updateOrdersForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (updateOrdersBtn.disabled || (updateAllOrdersBtn && updateAllOrdersBtn.disabled)) return;

      const submitter = e.submitter;
      const scope = submitter && submitter.id === "update-all-orders-btn" ? "all" : "active";
      if (updateOrdersScope) updateOrdersScope.value = scope;
      const scopeLabel =
        scope === "all"
          ? "все 3 статуса (сборка, отгрузка, доставляются)"
          : "2 статуса (сборка, отгрузка)";

      updateOrdersBtn.disabled = true;
      if (updateAllOrdersBtn) updateAllOrdersBtn.disabled = true;
      const startedAt = Date.now();
      updateOrdersStatus.classList.remove("status-success", "status-error");
      updateOrdersStatus.classList.add("status-progress");
      updateOrdersStatus.textContent = `Запрос в Ozon отправлен (${scopeLabel}). Прошло: 0 сек...`;
      const tick = setInterval(() => {
        const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
        updateOrdersStatus.textContent = `Запрос в Ozon отправлен (${scopeLabel}). Прошло: ${sec} сек...`;
      }, 1000);

      const formData = new FormData(updateOrdersForm);
      try {
        const res = await fetch("/orders/update-json", {
          method: "POST",
          body: formData,
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          throw new Error((data && data.message) || `Ошибка ${res.status}`);
        }
        const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
        const result = (data && data.result) || {};
        const total = Number(result.total || 0);
        const created = Number(result.created || 0);
        const updated = Number(result.updated || 0);
        const deleted = Number(result.deleted || 0);
        updateOrdersStatus.classList.remove("status-progress", "status-error");
        updateOrdersStatus.classList.add("status-success");
        updateOrdersStatus.textContent =
          `Ответ Ozon получен за ${sec} сек (${scopeLabel}). Получено: ${total}. ` +
          `Создано: ${created}, обновлено: ${updated}, удалено: ${deleted}.`;
      } catch (err) {
        const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
        updateOrdersStatus.classList.remove("status-progress", "status-success");
        updateOrdersStatus.classList.add("status-error");
        updateOrdersStatus.textContent =
          `Ошибка обновления через ${sec} сек: ${err.message || "неизвестно"}`;
      } finally {
        clearInterval(tick);
        updateOrdersBtn.disabled = false;
        if (updateAllOrdersBtn) updateAllOrdersBtn.disabled = false;
      }
    });
  }

  let pendingSplitAction = null;

  function splitOrdersCount() {
    return selected().filter((cb) => {
      const v = parseInt(cb.dataset.unitCount || "0", 10);
      return v > 1;
    }).length;
  }

  function openSplitConfirm(n) {
    if (!splitConfirmModal || !splitConfirmText) return;
    splitConfirmText.textContent = `${n} ${n === 1 ? "заказ" : "заказов"} будут разбиты на отправления в ОЗОН. После этого продолжим и поменяем статусы.`;
    splitConfirmModal.classList.remove("modal-overlay--hidden");
    splitConfirmModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeSplitConfirm() {
    if (!splitConfirmModal) return;
    splitConfirmModal.classList.add("modal-overlay--hidden");
    splitConfirmModal.setAttribute("aria-hidden", "true");
    // Don't force restore overflow here; underlying shipment modal already manages it.
    // We'll keep it simple: clear only when no other modal is open.
  }

  function withSplitConfirm(actionFn) {
    const n = splitOrdersCount();
    if (!n) {
      actionFn();
      return;
    }
    pendingSplitAction = actionFn;
    openSplitConfirm(n);
  }

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

  function openExistingModal() {
    if (!shipmentExistingModal) return;
    shipmentExistingModal.classList.remove("modal-overlay--hidden");
    shipmentExistingModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeExistingModal() {
    if (!shipmentExistingModal) return;
    shipmentExistingModal.classList.add("modal-overlay--hidden");
    shipmentExistingModal.setAttribute("aria-hidden", "true");
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

    if (shipmentOrderInputsAdd) {
      shipmentOrderInputsAdd.innerHTML = "";
      sel.forEach((cb) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "order_ids";
        input.value = cb.value;
        shipmentOrderInputsAdd.appendChild(input);
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

  async function fetchAvailableShipmentsForAwaitingDeliver() {
    const res = await fetch("/orders/shipment/available");
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    return (data && data.shipments) || [];
  }

  if (shipmentAddOpenBtn) {
    shipmentAddOpenBtn.addEventListener("click", async () => {
      const n = selected().length;
      if (n === 0 || !shipmentAddForm || !shipmentExistingModal) return;

      sync();
      if (shipmentExistingHidden) shipmentExistingHidden.value = "";

      openExistingModal();

      if (!shipmentExistingSelect) return;
      shipmentExistingSelect.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Загрузка...";
      shipmentExistingSelect.appendChild(placeholder);

      let shipments = [];
      try {
        shipments = await fetchAvailableShipmentsForAwaitingDeliver();
      } catch {
        shipments = [];
      }

      shipmentExistingSelect.innerHTML = "";
      if (!shipments.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "Нет подходящих поставок";
        opt.disabled = true;
        shipmentExistingSelect.appendChild(opt);
        if (shipmentExistingModalAddBtn) shipmentExistingModalAddBtn.disabled = true;
        return;
      }

      shipmentExistingSelect.appendChild(placeholder);
      shipments.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = String(s.id);
        opt.textContent = s.name || String(s.id);
        shipmentExistingSelect.appendChild(opt);
      });

      if (shipmentExistingModalAddBtn) shipmentExistingModalAddBtn.disabled = false;
    });
  }

  if (shipmentExistingModalAddBtn && shipmentExistingSelect && shipmentAddForm) {
    shipmentExistingModalAddBtn.addEventListener("click", () => {
      withSplitConfirm(() => {
        const id = shipmentExistingSelect.value || "";
        if (!id) return;
        if (shipmentExistingHidden) shipmentExistingHidden.value = id;
        sync();
        shipmentAddForm.submit();
      });
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
    shipmentModalCreate.addEventListener("click", () => {
      withSplitConfirm(() => submitShipmentForm());
    });
  }

  if (shipmentNameInput) {
    shipmentNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        withSplitConfirm(() => submitShipmentForm());
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

  if (shipmentExistingModal) {
    shipmentExistingModal.addEventListener("click", (e) => {
      if (e.target === shipmentExistingModal) closeExistingModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && shipmentExistingModal && !shipmentExistingModal.classList.contains("modal-overlay--hidden")) {
        closeExistingModal();
      }
    });
  }

  if (shipmentExistingModalClose) {
    shipmentExistingModalClose.addEventListener("click", () => closeExistingModal());
  }

  if (shipmentExistingModalX) {
    shipmentExistingModalX.addEventListener("click", () => closeExistingModal());
  }

  if (splitConfirmCancel) {
    splitConfirmCancel.addEventListener("click", () => {
      pendingSplitAction = null;
      closeSplitConfirm();
    });
  }

  if (splitConfirmModalX) {
    splitConfirmModalX.addEventListener("click", () => {
      pendingSplitAction = null;
      closeSplitConfirm();
    });
  }

  if (splitConfirmOk) {
    splitConfirmOk.addEventListener("click", () => {
      if (pendingSplitAction) {
        const fn = pendingSplitAction;
        pendingSplitAction = null;
        closeSplitConfirm();
        fn();
      } else {
        closeSplitConfirm();
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

  if (ordersSearchInput) {
    try {
      if (sessionStorage.getItem(ORDERS_FOCUS_Q_KEY) === "1") {
        sessionStorage.removeItem(ORDERS_FOCUS_Q_KEY);
        setTimeout(() => {
          ordersSearchInput.focus();
          const n = ordersSearchInput.value.length;
          if (typeof ordersSearchInput.setSelectionRange === "function") {
            ordersSearchInput.setSelectionRange(n, n);
          }
        }, 0);
      }
    } catch (_) {}
  }

  sync();
});
