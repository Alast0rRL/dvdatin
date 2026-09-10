// DvAI Web UI — Client-side interactions

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
