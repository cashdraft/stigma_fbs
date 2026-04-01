document.addEventListener("DOMContentLoaded", () => {
  let tapeInFlight = false;
  let tapeAbortController = null;
  let tapeOpGeneration = 0;

  function getTapeEls() {
    return {
      modal: document.getElementById("tape-progress-modal"),
      stage: document.getElementById("tape-progress-stage"),
      list: document.getElementById("tape-progress-list"),
      closeBtn: document.getElementById("tape-progress-close"),
    };
  }

  function openTapeProgressModal(steps) {
    const { modal, list, stage, closeBtn } = getTapeEls();
    if (!modal || !list || !stage) return;
    modal.classList.remove("modal-overlay--hidden");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    stage.classList.remove("status-success", "status-error");
    stage.textContent = "Подготовка...";
    list.innerHTML = "";
    steps.forEach((s, idx) => {
      const line = document.createElement("div");
      line.className = `shipment-progress-item${idx === 0 ? " is-active" : ""}`;
      line.textContent = `• ${s}`;
      list.appendChild(line);
    });
    if (closeBtn) closeBtn.disabled = false;
  }

  function setTapeProgress(stepIdx, state, stageText) {
    const { list, stage } = getTapeEls();
    if (!list) return;
    if (stage && stageText) {
      stage.textContent = stageText;
      stage.classList.remove("status-success", "status-error");
      if (state === "done") stage.classList.add("status-success");
      if (state === "error") stage.classList.add("status-error");
    }
    const lines = Array.from(list.querySelectorAll(".shipment-progress-item"));
    lines.forEach((line, idx) => {
      line.classList.remove("is-active", "is-done", "is-error");
      if (idx < stepIdx) line.classList.add("is-done");
      if (idx === stepIdx) {
        if (state === "error") line.classList.add("is-error");
        else if (state === "done") line.classList.add("is-done");
        else line.classList.add("is-active");
      }
    });
  }

  function closeTapeProgressModal() {
    const { modal } = getTapeEls();
    if (!modal) return;
    modal.classList.add("modal-overlay--hidden");
    modal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function abortTapeDownload() {
    if (tapeAbortController && tapeInFlight) {
      try {
        tapeAbortController.abort();
      } catch (_) {}
    }
    tapeInFlight = false;
    closeTapeProgressModal();
  }

  function extractFilenameFromHeader(headerValue) {
    if (!headerValue) return "";
    const mUtf = headerValue.match(/filename\*=UTF-8''([^;]+)/i);
    if (mUtf && mUtf[1]) return decodeURIComponent(mUtf[1]);
    const mPlain = headerValue.match(/filename=\"?([^\";]+)\"?/i);
    return (mPlain && mPlain[1]) || "";
  }

  async function downloadTapeWithProgress(url) {
    if (!url) return;
    const myGen = ++tapeOpGeneration;
    if (tapeAbortController) {
      try {
        tapeAbortController.abort();
      } catch (_) {}
      tapeAbortController = null;
    }
    tapeInFlight = true;
    const steps = ["Отправляем запрос", "Формируем PDF на сервере", "Скачиваем файл"];
    openTapeProgressModal(steps);
    setTapeProgress(0, "active", "Отправляем запрос...");
    const startedAt = Date.now();
    const tick = setInterval(() => {
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      setTapeProgress(1, "active", `Формируем PDF на сервере... Прошло: ${sec} сек`);
    }, 1000);
    try {
      tapeAbortController = new AbortController();
      const res = await fetch(url, {
        method: "GET",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: tapeAbortController.signal,
      });
      const contentType = (res.headers.get("content-type") || "").toLowerCase();
      if (!res.ok || !contentType.includes("application/pdf")) {
        throw new Error("Не удалось сформировать PDF. Проверьте доступность всех этикеток.");
      }
      setTapeProgress(1, "done", "PDF сформирован. Начинаем скачивание...");
      setTapeProgress(2, "active", "Скачиваем файл...");
      const blob = await res.blob();
      const filename =
        extractFilenameFromHeader(res.headers.get("content-disposition") || "") || "lenta_zakazov.pdf";
      const link = document.createElement("a");
      const blobUrl = URL.createObjectURL(blob);
      link.href = blobUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      setTapeProgress(2, "done", `Готово. Файл скачан за ${sec} сек.`);
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      setTapeProgress(1, "error", `Ошибка: ${err.message || "неизвестно"}`);
    } finally {
      clearInterval(tick);
      if (myGen === tapeOpGeneration) {
        tapeInFlight = false;
        tapeAbortController = null;
      }
      const { closeBtn } = getTapeEls();
      if (closeBtn) closeBtn.disabled = false;
    }
  }

  document.addEventListener("click", (e) => {
    const tapeLink = e.target.closest("#shipment-tape-download-btn");
    if (tapeLink) {
      e.preventDefault();
      downloadTapeWithProgress(tapeLink.href);
      return;
    }
    if (e.target.closest("#tape-progress-close")) {
      e.preventDefault();
      abortTapeDownload();
      return;
    }
    const { modal } = getTapeEls();
    if (modal && e.target === modal) {
      abortTapeDownload();
    }
  });

  const updateOzonProgressModal = document.getElementById("update-ozon-progress-modal");
  const updateOzonProgressStage = document.getElementById("update-ozon-progress-stage");
  const updateOzonProgressClose = document.getElementById("update-ozon-progress-close");
  let updateOzonInFlight = false;
  let updateOzonAbortController = null;

  function openUpdateOzonModal() {
    if (!updateOzonProgressModal) return;
    updateOzonProgressModal.classList.remove("modal-overlay--hidden");
    updateOzonProgressModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeUpdateOzonModal() {
    if (!updateOzonProgressModal) return;
    updateOzonProgressModal.classList.add("modal-overlay--hidden");
    updateOzonProgressModal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function setUpdateOzonStage(text, kind) {
    if (!updateOzonProgressStage) return;
    updateOzonProgressStage.textContent = text;
    updateOzonProgressStage.classList.remove(
      "status-progress",
      "status-success",
      "status-error",
      "status-cancelled",
    );
    if (kind === "progress") updateOzonProgressStage.classList.add("status-progress");
    if (kind === "success") updateOzonProgressStage.classList.add("status-success");
    if (kind === "error") updateOzonProgressStage.classList.add("status-error");
    if (kind === "cancelled") updateOzonProgressStage.classList.add("status-cancelled");
  }

  function onUpdateOzonModalAction() {
    if (updateOzonInFlight && updateOzonAbortController) {
      try {
        updateOzonAbortController.abort();
      } catch (_) {}
      return;
    }
    closeUpdateOzonModal();
  }

  if (updateOzonProgressModal && updateOzonProgressClose) {
    updateOzonProgressClose.addEventListener("click", () => onUpdateOzonModalAction());
    updateOzonProgressModal.addEventListener("click", (e) => {
      if (e.target === updateOzonProgressModal) onUpdateOzonModalAction();
    });
  }

  async function runOzonJsonSync(form, scope, disableEls) {
    if (!form || !updateOzonProgressModal || !updateOzonProgressStage) return;
    const els = (disableEls || []).filter(Boolean);
    if (els.some((el) => el.disabled)) return;

    const scopeInput = form.querySelector('input[name="sync_scope"]');
    if (scopeInput) scopeInput.value = scope === "all" ? "all" : "active";

    const scopeLabel =
      scope === "all"
        ? "все 3 статуса (сборка, отгрузка, доставляются)"
        : "2 статуса (сборка, отгрузка)";

    els.forEach((el) => {
      el.disabled = true;
    });

    openUpdateOzonModal();
    setUpdateOzonStage(`Запрос в Ozon отправлен (${scopeLabel}). Прошло: 0 сек...`, "progress");
    if (updateOzonProgressClose) {
      updateOzonProgressClose.textContent = "Отменить";
      updateOzonProgressClose.disabled = false;
    }

    const startedAt = Date.now();
    const tick = setInterval(() => {
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      setUpdateOzonStage(`Запрос в Ozon отправлен (${scopeLabel}). Прошло: ${sec} сек...`, "progress");
    }, 1000);

    const formData = new FormData(form);
    updateOzonAbortController = new AbortController();
    updateOzonInFlight = true;

    try {
      const res = await fetch("/orders/update-json", {
        method: "POST",
        body: formData,
        signal: updateOzonAbortController.signal,
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.message) || `Ошибка ${res.status}`);
      }
      clearInterval(tick);
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      const result = (data && data.result) || {};
      const total = Number(result.total || 0);
      const created = Number(result.created || 0);
      const updated = Number(result.updated || 0);
      const deleted = Number(result.deleted || 0);
      setUpdateOzonStage(
        `Ответ Ozon получен за ${sec} сек (${scopeLabel}). Получено: ${total}. ` +
          `Создано: ${created}, обновлено: ${updated}, удалено: ${deleted}.`,
        "success",
      );
    } catch (err) {
      clearInterval(tick);
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      if (err && err.name === "AbortError") {
        setUpdateOzonStage("Запрос отменён.", "cancelled");
      } else {
        setUpdateOzonStage(
          `Ошибка обновления через ${sec} сек: ${err.message || "неизвестно"}`,
          "error",
        );
      }
    } finally {
      clearInterval(tick);
      updateOzonInFlight = false;
      updateOzonAbortController = null;
      els.forEach((el) => {
        el.disabled = false;
      });
      if (updateOzonProgressClose) updateOzonProgressClose.textContent = "Закрыть";
    }
  }

  const updateWbProgressModal = document.getElementById("update-wb-progress-modal");
  const updateWbProgressStage = document.getElementById("update-wb-progress-stage");
  const updateWbProgressClose = document.getElementById("update-wb-progress-close");
  let updateWbInFlight = false;
  let updateWbAbortController = null;

  function openUpdateWbModal() {
    if (!updateWbProgressModal) return;
    updateWbProgressModal.classList.remove("modal-overlay--hidden");
    updateWbProgressModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeUpdateWbModal() {
    if (!updateWbProgressModal) return;
    updateWbProgressModal.classList.add("modal-overlay--hidden");
    updateWbProgressModal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function setUpdateWbStage(text, kind) {
    if (!updateWbProgressStage) return;
    updateWbProgressStage.textContent = text;
    updateWbProgressStage.classList.remove(
      "status-progress",
      "status-success",
      "status-error",
      "status-cancelled",
    );
    if (kind === "progress") updateWbProgressStage.classList.add("status-progress");
    if (kind === "success") updateWbProgressStage.classList.add("status-success");
    if (kind === "error") updateWbProgressStage.classList.add("status-error");
    if (kind === "cancelled") updateWbProgressStage.classList.add("status-cancelled");
  }

  function onUpdateWbModalAction() {
    if (updateWbInFlight && updateWbAbortController) {
      try {
        updateWbAbortController.abort();
      } catch (_) {}
      return;
    }
    closeUpdateWbModal();
  }

  if (updateWbProgressModal && updateWbProgressClose) {
    updateWbProgressClose.addEventListener("click", () => onUpdateWbModalAction());
    updateWbProgressModal.addEventListener("click", (e) => {
      if (e.target === updateWbProgressModal) onUpdateWbModalAction();
    });
  }

  function wbSyncProgressText(ev, sec) {
    const head = `Прошло: ${sec} сек`;
    const step = ev && ev.step;
    if (step === "new_orders_start") {
      return `${head}\nНовые задания: запрос /api/v3/orders/new…`;
    }
    if (step === "new_orders_done") {
      const feed = ev.in_new_feed != null ? ` · в ленте /new: ${ev.in_new_feed}` : "";
      return `${head}\nПосле «новых»: уникальных id ${ev.known}${feed}`;
    }
    if (step === "orders_page") {
      return `${head}\nСтраница списка: ${ev.page} · в ответе ${ev.batch} шт. · всего id ${ev.known}`;
    }
    if (step === "orders_pages_done") {
      return `${head}\nСписок по датам готов: страниц ${ev.pages}, id ${ev.known}`;
    }
    if (step === "status_chunk") {
      return `${head}\nСтатусы: чанк ${ev.chunk}/${ev.chunks_total} · ${ev.ids} id`;
    }
    if (step === "statuses_done") {
      return `${head}\nСтатусы загружены (чанков: ${ev.chunks_total})`;
    }
    if (step === "content_cards_start") {
      return `${head}\nКарточки WB (название, фото, размер): уникальных nmId ${ev.total}…`;
    }
    if (step === "content_cards") {
      if (ev.mode === "local_db") {
        return `${head}\nЛокальный каталог WB: найдено ${ev.found} / ${ev.needed} nmId`;
      }
      if (ev.mode === "catalog") {
        return `${head}\nКаталог карточек WB: стр. ${ev.page} · собрано ${ev.found} / ${ev.needed} (в ответе ${ev.batch} шт.)`;
      }
      if (ev.mode === "per_nm") {
        return `${head}\nДобор по nmId: ${ev.done} / ${ev.total} · всего в кэше ${ev.found} / ${ev.needed}`;
      }
      return `${head}\nContent API: ${ev.done != null ? `${ev.done} / ${ev.total}` : "загрузка…"}`;
    }
    if (step === "content_cards_done") {
      const stopped = ev.auth_stopped ? " · остановка (нет доступа «Контент»)" : "";
      return `${head}\nКарточки: получено ${ev.loaded} из ${ev.requested}${stopped}`;
    }
    if (step === "status_retry") {
      return `${head}\nПовтор статусов: нет ответа по ${ev.missing} id (попытка ${ev.round}/2)…`;
    }
    if (step === "database_start") {
      return `${head}\nСохранение в БД: ${ev.total} заказов…`;
    }
    if (step === "database_save") {
      return `${head}\nБД: записано ${ev.current} / ${ev.total}`;
    }
    return `${head}\nОжидание ответа сервера…`;
  }

  async function runWbJsonSync(form, disableEls) {
    if (!form || !updateWbProgressModal || !updateWbProgressStage) return;
    const els = (disableEls || []).filter(Boolean);
    if (els.some((el) => el.disabled)) return;

    els.forEach((el) => {
      el.disabled = true;
    });

    openUpdateWbModal();
    setUpdateWbStage("Прошло: 0 сек\nЗапуск синхронизации…", "progress");
    if (updateWbProgressClose) {
      updateWbProgressClose.textContent = "Отменить";
      updateWbProgressClose.disabled = false;
    }

    const startedAt = Date.now();
    let lastProgressEv = null;
    const tick = setInterval(() => {
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      setUpdateWbStage(wbSyncProgressText(lastProgressEv, sec), "progress");
    }, 400);

    const formData = new FormData(form);
    updateWbAbortController = new AbortController();
    updateWbInFlight = true;

    try {
      const res = await fetch("/orders_wb/update-stream", {
        method: "POST",
        body: formData,
        signal: updateWbAbortController.signal,
      });
      if (!res.ok) {
        let msg = `Ошибка ${res.status}`;
        try {
          const errJson = await res.json();
          if (errJson && errJson.message) msg = errJson.message;
        } catch (_) {
          /* ignore */
        }
        throw new Error(msg);
      }
      const reader = res.body && res.body.getReader ? res.body.getReader() : null;
      if (!reader) {
        throw new Error("Браузер не поддерживает потоковый ответ");
      }
      const decoder = new TextDecoder();
      let buffer = "";
      let result = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (let i = 0; i < lines.length; i += 1) {
          const line = lines[i].trim();
          if (!line) continue;
          let obj;
          try {
            obj = JSON.parse(line);
          } catch (_) {
            continue;
          }
          if (obj.type === "progress") {
            lastProgressEv = obj;
            const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
            setUpdateWbStage(wbSyncProgressText(obj, sec), "progress");
          } else if (obj.type === "done") {
            result = obj.result || {};
          } else if (obj.type === "error") {
            throw new Error((obj && obj.message) || "Ошибка синхронизации");
          }
        }
      }
      clearInterval(tick);
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      if (result == null) {
        throw new Error("Соединение оборвалось до завершения синхронизации");
      }
      const total = Number((result && result.total) || 0);
      const created = Number((result && result.created) || 0);
      const updated = Number((result && result.updated) || 0);
      const deleted = Number((result && result.deleted) || 0);
      const inFeed = Number((result && result.wb_new_feed) || 0);
      const feedLine =
        inFeed > 0 ? `\nВ ленте «Новые» WB сейчас: ${inFeed} id (см. вкладку «Новые»).` : "";
      setUpdateWbStage(
        `Готово за ${sec} сек.\nЗаписей в выгрузке: ${total}. Создано: ${created}, обновлено: ${updated}, удалено устаревших: ${deleted}.${feedLine}`,
        "success",
      );
    } catch (err) {
      clearInterval(tick);
      const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      if (err && err.name === "AbortError") {
        setUpdateWbStage("Запрос отменён.", "cancelled");
      } else {
        setUpdateWbStage(
          `Ошибка через ${sec} сек:\n${err.message || "неизвестно"}`,
          "error",
        );
      }
    } finally {
      clearInterval(tick);
      updateWbInFlight = false;
      updateWbAbortController = null;
      els.forEach((el) => {
        el.disabled = false;
      });
      if (updateWbProgressClose) updateWbProgressClose.textContent = "Закрыть";
    }
  }

  const updateWbForm = document.getElementById("update-wb-form");
  const updateWbBtn = document.getElementById("update-wb-btn");
  if (updateWbForm && updateWbBtn && updateWbProgressModal && updateWbProgressStage && updateWbProgressClose) {
    updateWbForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await runWbJsonSync(updateWbForm, [updateWbBtn]);
    });
  }

  const homeSyncForm = document.getElementById("home-sync-ozon-form");
  const homeSyncBtn = document.getElementById("home-sync-ozon-btn");
  if (homeSyncForm && homeSyncBtn && updateOzonProgressModal && updateOzonProgressStage && updateOzonProgressClose) {
    homeSyncForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await runOzonJsonSync(homeSyncForm, "all", [homeSyncBtn]);
    });
  }

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
  const shipmentProgressModal = document.getElementById("shipment-progress-modal");
  const shipmentProgressStage = document.getElementById("shipment-progress-stage");
  const shipmentProgressList = document.getElementById("shipment-progress-list");
  const shipmentProgressClose = document.getElementById("shipment-progress-close");
  const updateOrdersForm = document.getElementById("update-orders-form");
  const updateOrdersBtn = document.getElementById("update-orders-btn");
  const updateAllOrdersBtn = document.getElementById("update-all-orders-btn");
  const updateOrdersScope = document.getElementById("update-orders-scope");
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

  if (
    updateOrdersForm &&
    updateOrdersBtn &&
    updateOzonProgressModal &&
    updateOzonProgressStage &&
    updateOzonProgressClose
  ) {
    async function runOrdersPageSync(scope) {
      if (updateOrdersScope) updateOrdersScope.value = scope;
      await runOzonJsonSync(updateOrdersForm, scope, [updateOrdersBtn, updateAllOrdersBtn].filter(Boolean));
    }

    updateOrdersBtn.addEventListener("click", (e) => {
      e.preventDefault();
      runOrdersPageSync("active");
    });

    if (updateAllOrdersBtn) {
      updateAllOrdersBtn.addEventListener("click", (e) => {
        e.preventDefault();
        runOrdersPageSync("all");
      });
    }

    updateOrdersForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitter = e.submitter;
      const scope = submitter && submitter.id === "update-all-orders-btn" ? "all" : "active";
      runOrdersPageSync(scope);
    });
  }

  let pendingSplitAction = null;
  let shipmentActionInFlight = false;

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
    if (shipmentModalCreate) shipmentModalCreate.disabled = false;
    if (shipmentModalClose) shipmentModalClose.disabled = false;
    shipmentNameInput.focus();
    shipmentNameInput.select();
  }

  function closeShipmentModal() {
    if (!shipmentModal) return;
    if (shipmentActionInFlight) return;
    shipmentModal.classList.add("modal-overlay--hidden");
    shipmentModal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function openExistingModal() {
    if (!shipmentExistingModal) return;
    shipmentExistingModal.classList.remove("modal-overlay--hidden");
    shipmentExistingModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    if (shipmentExistingModalAddBtn) shipmentExistingModalAddBtn.disabled = false;
    if (shipmentExistingModalClose) shipmentExistingModalClose.disabled = false;
  }

  function closeExistingModal() {
    if (!shipmentExistingModal) return;
    if (shipmentActionInFlight) return;
    shipmentExistingModal.classList.add("modal-overlay--hidden");
    shipmentExistingModal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function openShipmentProgressModal(title, steps) {
    if (!shipmentProgressModal || !shipmentProgressList || !shipmentProgressStage) return;
    shipmentProgressModal.classList.remove("modal-overlay--hidden");
    shipmentProgressModal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    shipmentProgressStage.textContent = title || "Обработка...";
    shipmentProgressList.innerHTML = "";
    steps.forEach((s, idx) => {
      const line = document.createElement("div");
      line.className = `shipment-progress-item${idx === 0 ? " is-active" : ""}`;
      line.dataset.step = String(idx);
      line.textContent = `• ${s}`;
      shipmentProgressList.appendChild(line);
    });
    if (shipmentProgressClose) shipmentProgressClose.disabled = true;
  }

  function setShipmentProgress(stepIdx, state, stageText) {
    if (shipmentProgressStage && stageText) shipmentProgressStage.textContent = stageText;
    if (!shipmentProgressList) return;
    const lines = Array.from(shipmentProgressList.querySelectorAll(".shipment-progress-item"));
    lines.forEach((line, idx) => {
      line.classList.remove("is-active", "is-done", "is-error");
      if (idx < stepIdx) line.classList.add("is-done");
      if (idx === stepIdx) {
        if (state === "error") line.classList.add("is-error");
        else if (state === "done") line.classList.add("is-done");
        else line.classList.add("is-active");
      }
    });
  }

  function closeShipmentProgressModal() {
    if (!shipmentProgressModal) return;
    shipmentProgressModal.classList.add("modal-overlay--hidden");
    shipmentProgressModal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  async function submitShipmentActionAjax(formEl, progressTitle, hasSplit) {
    const steps = hasSplit
      ? [
          "Передаем выбранные заказы",
          "Разбиваем на отправления и отгружаем в Ozon",
          "Обновляем статусы: Ожидают сборки/Ожидают отгрузки",
        ]
      : [
          "Передаем выбранные заказы",
          "Отгружаем в Ozon",
          "Обновляем статусы: Ожидают сборки/Ожидают отгрузки",
        ];
    openShipmentProgressModal(progressTitle, steps);
    setShipmentProgress(0, "active", "Передаем данные...");

    const timers = [
      setTimeout(() => setShipmentProgress(0, "done", "Передача выполнена"), 350),
      setTimeout(() => setShipmentProgress(1, "active", hasSplit ? "Идет разбивка и отгрузка..." : "Идет отгрузка..."), 500),
      setTimeout(() => setShipmentProgress(2, "active", "Идет обновление заказов..."), 2200),
    ];

    try {
      const res = await fetch(formEl.action, {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        body: new FormData(formEl),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error((data && data.message) || `Ошибка ${res.status}`);
      let doneText = data.message || "Готово";
      if (Number(data.failed_count || 0) > 0) {
        doneText += ` Завершилось с ошибкой: ${Number(data.failed_count)}.`;
      }
      setShipmentProgress(2, "done", doneText);
      setShipmentProgress(3, "done", "Готово");
      if (shipmentProgressClose) shipmentProgressClose.disabled = false;
      setTimeout(() => {
        const next = (data && data.next_url) || window.location.pathname + window.location.search;
        if (next === window.location.pathname + window.location.search) {
          window.location.reload();
          return;
        }
        window.location.assign(next);
      }, 700);
    } catch (err) {
      setShipmentProgress(1, "error", `Ошибка: ${err.message || "неизвестно"}`);
      if (shipmentProgressClose) shipmentProgressClose.disabled = false;
      throw err;
    } finally {
      timers.forEach((t) => clearTimeout(t));
    }
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
    shipmentExistingModalAddBtn.addEventListener("click", async () => {
      withSplitConfirm(async () => {
        if (shipmentActionInFlight) return;
        const id = shipmentExistingSelect.value || "";
        if (!id) return;
        if (shipmentExistingHidden) shipmentExistingHidden.value = id;
        sync();
        const hasSplit = splitOrdersCount() > 0;
        if (shipmentExistingModalAddBtn) shipmentExistingModalAddBtn.disabled = true;
        if (shipmentExistingModalClose) shipmentExistingModalClose.disabled = true;
        closeExistingModal();
        shipmentActionInFlight = true;
        try {
          await submitShipmentActionAjax(shipmentAddForm, "Добавляем в существующую поставку", hasSplit);
        } catch (_) {
          shipmentActionInFlight = false;
          if (shipmentExistingModalAddBtn) shipmentExistingModalAddBtn.disabled = false;
          if (shipmentExistingModalClose) shipmentExistingModalClose.disabled = false;
          openExistingModal();
        }
      });
    });
  }

  async function submitShipmentForm() {
    if (!shipmentForm || !shipmentNameInput || !shipmentNameHidden) return;
    if (shipmentActionInFlight) return;
    const name = shipmentNameInput.value.trim();
    if (!name) {
      shipmentNameInput.focus();
      return;
    }
    shipmentNameHidden.value = name;
    sync();
    const hasSplit = splitOrdersCount() > 0;
    if (shipmentModalCreate) shipmentModalCreate.disabled = true;
    if (shipmentModalClose) shipmentModalClose.disabled = true;
    closeShipmentModal();
    shipmentActionInFlight = true;
    try {
      await submitShipmentActionAjax(shipmentForm, "Создаем поставку", hasSplit);
    } catch (_) {
      shipmentActionInFlight = false;
      if (shipmentModalCreate) shipmentModalCreate.disabled = false;
      if (shipmentModalClose) shipmentModalClose.disabled = false;
      openShipmentModal();
    }
  }

  if (shipmentModalCreate && shipmentForm) {
    shipmentModalCreate.addEventListener("click", async () => {
      withSplitConfirm(async () => submitShipmentForm());
    });
  }

  if (shipmentNameInput) {
    shipmentNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        withSplitConfirm(async () => submitShipmentForm());
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

  if (shipmentProgressClose) {
    shipmentProgressClose.addEventListener("click", () => closeShipmentProgressModal());
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

  const loadMoreBase =
    (root.dataset.loadMoreUrl && String(root.dataset.loadMoreUrl).trim()) || "/orders/load-more";

  if (loadBtn && tbody && nextPage >= 2) {
    loadBtn.addEventListener("click", async () => {
      if (!nextPage || loadBtn.disabled) return;
      loadBtn.disabled = true;
      loadBtn.textContent = "Загрузка…";
      try {
        const res = await fetch(`${loadMoreBase}?${loadMoreQuery(nextPage)}`);
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
