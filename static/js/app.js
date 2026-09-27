/* DvAI — единый экран: polling ленты, drawer настроек, подсказки капч. */
(function () {
    'use strict';

    var POLL_MS = 3000;
    var app = document.getElementById('app');
    var feed = document.getElementById('feed');
    var feedMore = document.getElementById('feed-more');
    var drawer = document.getElementById('drawer');
    var backdrop = document.getElementById('drawer-backdrop');

    /* ── Drawer настроек ─────────────────────────────────────────── */
    function openDrawer() {
        if (!drawer) return;
        drawer.hidden = false;
        if (backdrop) backdrop.hidden = false;
        document.body.style.overflow = 'hidden';
    }

    function closeDrawer() {
        if (!drawer) return;
        drawer.hidden = true;
        if (backdrop) backdrop.hidden = true;
        document.body.style.overflow = '';
    }

    var openBtn = document.getElementById('settings-open');
    if (openBtn) openBtn.addEventListener('click', openDrawer);
    var closeBtn = document.getElementById('drawer-close');
    if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
    if (backdrop) backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeDrawer();
    });

    /* ── Тосты живут 5 секунд ────────────────────────────────────── */
    document.querySelectorAll('.toast').forEach(function (t) {
        setTimeout(function () { t.remove(); }, 5000);
    });

    /* ── Подсказки капч подставляют текст в поле ответа ──────────── */
    function bindSuggestions(root) {
        (root || document).querySelectorAll('.chip[data-suggest]').forEach(function (chip) {
            if (chip.dataset.bound) return;
            chip.dataset.bound = '1';
            chip.addEventListener('click', function () {
                var box = chip.closest('.pending__item, .card--captcha, .bubble');
                var input = box && box.querySelector('input[name="answer"]');
                if (input) {
                    input.value = chip.dataset.suggest || chip.textContent.trim();
                    input.focus();
                }
            });
        });
    }
    bindSuggestions(document);

    /* ── Лента ───────────────────────────────────────────────────── */
    function atBottom() {
        return window.innerHeight + window.scrollY >= document.body.offsetHeight - 260;
    }

    function scrollToBottom() {
        window.scrollTo({top: document.body.offsetHeight, behavior: 'smooth'});
    }

    function setLoadMoreState(page, totalPages) {
        if (!feedMore) return;
        var btn = feedMore.querySelector('#load-older');
        if (!btn) {
            if (page > 1 && totalPages > 1) {
                feedMore.innerHTML = '<button type="button" class="btn btn--ghost" id="load-older" ' +
                    'data-page="' + (page + 1) + '">Показать более ранние</button>';
                bindLoadMore();
            }
            return;
        }
        btn.dataset.page = page + 1;
        btn.hidden = page >= totalPages;
    }

    function bindLoadMore() {
        var btn = feedMore && feedMore.querySelector('#load-older');
        if (!btn || btn.dataset.bound) return;
        btn.dataset.bound = '1';
        btn.addEventListener('click', function () {
            var next = parseInt(btn.dataset.page || '2', 10);
            if (!next || isNaN(next)) return;
            var before = document.body.offsetHeight;
            fetch('/chat/feed?page=' + next)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    // Страница 1 = свежие, дальше — более ранние → вставляем сверху.
                    feed.insertAdjacentHTML('afterbegin', data.feed);
                    setLoadMoreState(next, data.total_pages);
                    bindSuggestions(feed);
                    window.scrollTo(0, window.scrollY + (document.body.offsetHeight - before));
                })
                .catch(function () { /* следующая попытка */ });
        });
    }
    bindLoadMore();

    function refreshFeed(page, keepScroll) {
        var stay = keepScroll === true;
        var before = document.body.offsetHeight;
        return fetch('/chat/feed?page=' + page)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var oldPending = document.getElementById('pending');
                if (oldPending) oldPending.remove();
                if (data.pending) {
                    feed.insertAdjacentHTML('beforebegin', data.pending);
                }
                feed.innerHTML = data.feed;
                if (app) {
                    app.dataset.page = data.page;
                    app.dataset.totalPages = data.total_pages;
                }
                setLoadMoreState(data.page, data.total_pages);
                bindSuggestions(document);
                if (stay) {
                    window.scrollTo(0, window.scrollY + (document.body.offsetHeight - before));
                } else if (atBottom()) {
                    scrollToBottom();
                }
            })
            .catch(function () { /* не сеть — ждём следующий тик */ });
    }

    function poll() {
        if (!app) return;
        fetch('/chat/new-count', {headers: {'X-Requested-With': 'fetch'}})
            .then(function (r) { return r.json(); })
            .then(function (sig) {
                if (sig && sig.signature && sig.signature !== app.dataset.sig) {
                    app.dataset.sig = sig.signature;
                    return refreshFeed(1, false);
                }
            })
            .catch(function () { /* тихо */ });
    }

    if (app) {
        setInterval(poll, POLL_MS);
        window.dvai = {refreshFeed: refreshFeed, openDrawer: openDrawer, closeDrawer: closeDrawer};
    }
})();
