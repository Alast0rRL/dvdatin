// DvAI Web UI — Client-side interactions

// Авто-обновление ленты: каждые 10с проверяем число анкет в БД.
// Если появились новые (total вырос) или изменился pending — молча
// перезагружаем страницу, чтобы новые анкеты появлялись без ручного F5.
(function autoRefresh() {
    if (typeof window._refreshTimer !== 'undefined') return;
    window._refreshTimer = true;

    const header = document.querySelector('.dashboard-header');
    if (!header) return; // авто-обновление только для дашборда
    const known = {
        total: parseInt(header?.dataset.total || '0', 10),
        pending: parseInt(header?.dataset.pending || '0', 10),
    };

    setInterval(() => {
        fetch('/dashboard/new-count', { credentials: 'same-origin' })
            .then(resp => {
                if (!resp.ok) return null;
                return resp.json();
            })
            .then(data => {
                if (!data) return;
                const totalChanged = data.total !== known.total
                    && !Number.isNaN(data.total);
                const pendingChanged = data.pending !== known.pending
                    && !Number.isNaN(data.pending);
                if (totalChanged || pendingChanged) {
                    const cur = window.location.href;
                    window.location.href = cur.includes('?')
                        ? cur + '&t=' + Date.now()
                        : cur + '?t=' + Date.now();
                }
            })
            .catch(() => { /* сетевые ошибки игнорируем */ });
    }, 10000);
})();

function quickAction(profileId, action) {
    const resultEl = document.getElementById(`action-result-${profileId}`);
    const buttonsEl = document.querySelector(`[data-profile-id="${profileId}"]`);

    fetch(`/dashboard/action/${profileId}/${action}`, {
        method: 'GET',
        credentials: 'same-origin',
    })
    .then(resp => {
        if (resp.ok) {
            if (buttonsEl) buttonsEl.style.display = 'none';
            if (resultEl) {
                resultEl.textContent = action === 'LIKE' ? '✅ LIKE отправлен' : '✅ DISLIKE отправлен';
            }
        } else if (resp.status === 409) {
            if (resultEl) resultEl.textContent = 'Уже обработано';
        } else {
            if (resultEl) resultEl.textContent = 'Ошибка';
        }
    })
    .catch(() => {
        if (resultEl) resultEl.textContent = 'Ошибка сети';
    });
}
