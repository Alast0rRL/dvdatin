// DvAI Web UI — client-side interactions

(function () {
    "use strict";

    // ── Toasts ──
    function toast(message, kind) {
        const wrap = document.getElementById("toast-wrap");
        if (!wrap) return;
        const el = document.createElement("div");
        el.className = "toast" + (kind ? " toast-" + kind : "");
        el.textContent = message;
        wrap.appendChild(el);
        requestAnimationFrame(() => el.classList.add("show"));
        setTimeout(() => {
            el.classList.remove("show");
            setTimeout(() => el.remove(), 300);
        }, 2600);
    }

    window.showToast = toast;

    // ── Авто-обновление ленты (in-place, без перезагрузки страницы) ──
    // Каждые ~8с спрашиваем лёгкую сигнатуру (/dashboard/new-count).
    // Сигнатура учитывает не только total/pending, но и MAX(last_seen_at)
    // и MAX(raw_messages.id) — поэтому подхватываются и повторы анкет.
    // При изменении сигнатуры тянем HTML-фрагменты (/dashboard/feed)
    // и подменяем DOM, минуя полную перезагрузку (F5 не нужен).
    const header = document.querySelector(".dashboard-header");
    const feedEl = document.getElementById("profile-feed");
    const paginationEl = document.getElementById("pagination");

    function currentParams() {
        return new URLSearchParams(window.location.search);
    }

    function updateStats(data) {
        if (!data || typeof data !== "object") return;
        const map = {
            total: "total",
            pending: "pending",
            like: "like",
            review: "review",
            dislike: "dislike",
        };
        for (const [key, stat] of Object.entries(map)) {
            if (data[key] === undefined) continue;
            const el = document.querySelector(`[data-stat="${stat}"]`);
            if (el && String(el.textContent) !== String(data[key])) {
                el.textContent = data[key];
            }
        }
    }

    if (header && feedEl) {
        let lastSignature = "";
        let lastFeedHtml = feedEl.innerHTML;
        let lastPaginationHtml = paginationEl
            ? paginationEl.innerHTML
            : "";

        setInterval(() => {
            if (Object.keys(busy).length > 0) return;
            const params = currentParams();
            const url = `/dashboard/new-count?decision=${encodeURIComponent(
                params.get("decision") || ""
            )}`;

            fetch(url, { credentials: "same-origin" })
                .then((resp) => (resp.ok ? resp.json() : null))
                .then((data) => {
                    if (!data || !data.signature) return;
                    if (data.signature === lastSignature) return;

                    const feedUrl =
                        `/dashboard/feed` +
                        (params.toString() ? `?${params.toString()}` : "");
                    return fetch(feedUrl, { credentials: "same-origin" })
                        .then((r) => (r.ok ? r.json() : null))
                        .then((frag) => {
                            if (!frag) return;

                            updateStats(data);
                            if (header) {
                                header.dataset.total = data.total ?? "";
                                header.dataset.pending = data.pending ?? "";
                            }
                            lastSignature = data.signature;

                            if (
                                frag.feed &&
                                frag.feed !== lastFeedHtml
                            ) {
                                feedEl.innerHTML = frag.feed;
                                lastFeedHtml = frag.feed;
                            }
                            if (
                                paginationEl &&
                                frag.pagination &&
                                frag.pagination !== lastPaginationHtml
                            ) {
                                paginationEl.innerHTML = frag.pagination;
                                lastPaginationHtml = frag.pagination;
                            }
                        });
                })
                .catch(() => {});
        }, 8000);
    }

    // ── Быстрое действие (LIKE / DISLIKE) из ленты ──
    const busy = {};

    window.quickAction = function quickAction(profileId, action, btnEl) {
        if (busy[profileId]) return;
        busy[profileId] = true;

        const buttonsEl = document.querySelector(
            `[data-profile-id="${profileId}"]`
        );
        const resultEl = document.getElementById(
            `action-result-${profileId}`
        );
        const buttons = buttonsEl
            ? Array.from(buttonsEl.querySelectorAll("button"))
            : btnEl
            ? [btnEl]
            : [];
        buttons.forEach((b) => {
            b.disabled = true;
            b.classList.add("is-loading");
        });

        fetch(`/dashboard/action/${profileId}/${action}`, {
            method: "GET",
            credentials: "same-origin",
        })
            .then((resp) => {
                if (resp.ok) {
                    buttons.forEach((b) => (b.style.display = "none"));
                    const msg =
                        action === "LIKE"
                            ? "LIKE отправлен"
                            : "DISLIKE отправлен";
                    if (resultEl) resultEl.textContent = "✅ " + msg;
                    toast(msg, action === "LIKE" ? "like" : "dislike");
                } else if (resp.status === 409) {
                    if (resultEl) resultEl.textContent = "Уже обработано";
                    toast("Анкета уже обработана", "error");
                } else {
                    if (resultEl) resultEl.textContent = "Ошибка";
                    toast("Ошибка при отправке", "error");
                }
            })
            .catch(() => {
                if (resultEl) resultEl.textContent = "Ошибка сети";
                toast("Ошибка сети", "error");
            })
            .finally(() => {
                delete busy[profileId];
                buttons.forEach((b) => b.classList.remove("is-loading"));
            });
    };
})();