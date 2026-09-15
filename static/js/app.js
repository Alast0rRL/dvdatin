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

    // ── Авто-обновление ленты ──
    // Каждые 10с проверяем число анкет в БД. Если появились новые
    // (total вырос) или изменился pending — молча перезагружаем страницу.
    const header = document.querySelector(".dashboard-header");
    if (header) {
        const known = {
            total: parseInt(header?.dataset.total || "0", 10),
            pending: parseInt(header?.dataset.pending || "0", 10),
        };

        setInterval(() => {
            fetch("/dashboard/new-count", { credentials: "same-origin" })
                .then((resp) => (resp.ok ? resp.json() : null))
                .then((data) => {
                    if (!data) return;
                    const totalChanged =
                        !Number.isNaN(data.total) && data.total !== known.total;
                    const pendingChanged =
                        !Number.isNaN(data.pending) &&
                        data.pending !== known.pending;
                    if (totalChanged || pendingChanged) {
                        const cur = window.location.href;
                        window.location.href = cur.includes("?")
                            ? cur + "&t=" + Date.now()
                            : cur + "?t=" + Date.now();
                    }
                })
                .catch(() => {});
        }, 10000);
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