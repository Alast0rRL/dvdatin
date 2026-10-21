/* DvAI — единый экран: polling ленты, drawer настроек, подсказки капч. */
(function () {
    'use strict';

    var POLL_MS = 3000;
    var app = document.getElementById('app');
    var feed = document.getElementById('feed');
    var feedMore = document.getElementById('feed-more');
    var drawer = document.getElementById('drawer');
    var backdrop = document.getElementById('drawer-backdrop');

    /* ── Переключатель темы ──────────────────────────────────────── */
    /* Тема применена инлайном ДО отрисовки (partials/theme_boot.html);
       здесь только переключаем и запоминаем выбор. */
    var THEME_KEY = 'dvai.theme';

    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') === 'light'
            ? 'light' : 'dark';
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        try {
            localStorage.setItem(THEME_KEY, theme);
        } catch (e) { /* приватный режим — просто не запоминаем */ }
    }

    var themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) {
        themeBtn.addEventListener('click', function () {
            applyTheme(currentTheme() === 'light' ? 'dark' : 'light');
        });
    }

    /* ── Аккаунт-исполнитель: переключение сразу по выбору ────────── */
    document.querySelectorAll('select[data-auto-submit]').forEach(function (sel) {
        sel.addEventListener('change', function () {
            if (sel.form) sel.form.submit();
        });
    });

    /* ── Drawer настроек ─────────────────────────────────────────── */
    var lastFocused = null;

    function openDrawer() {
        if (!drawer) return;
        lastFocused = document.activeElement;
        drawer.hidden = false;
        if (backdrop) backdrop.hidden = false;
        document.body.style.overflow = 'hidden';
        if (openBtn) openBtn.setAttribute('aria-expanded', 'true');
        var close = document.getElementById('drawer-close');
        if (close) close.focus();
    }

    function closeDrawer() {
        if (!drawer) return;
        drawer.hidden = true;
        if (backdrop) backdrop.hidden = true;
        document.body.style.overflow = '';
        if (openBtn) openBtn.setAttribute('aria-expanded', 'false');
        if (lastFocused && lastFocused.focus) lastFocused.focus();
    }

    var openBtn = document.getElementById('settings-open');
    if (openBtn) openBtn.addEventListener('click', openDrawer);
    var closeBtn = document.getElementById('drawer-close');
    if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
    if (backdrop) backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            if (debugDrawer && !debugDrawer.hidden) {
                closeDebug();
            } else {
                closeDrawer();
            }
        }
    });

    /* ── Тосты живут 5 секунд ────────────────────────────────────── */
    document.querySelectorAll('.toast').forEach(function (t) {
        setTimeout(function () { t.remove(); }, 5000);
    });

    /* ── Битые фото не оставляют дыру в строке ───────────────────── */
    /* Ошибка загрузки не всплывает (error не bubble), поэтому ловим
       в фазе захвата. Пустая .thumbs при этом схлопывается (нет детей). */
    function dropBrokenImage(img) {
        if (img.dataset.broken) return;
        img.dataset.broken = '1';
        img.remove();
    }
    document.addEventListener('error', function (e) {
        if (e.target && e.target.tagName === 'IMG') dropBrokenImage(e.target);
    }, true);
    function sweepBrokenImages(root) {
        (root || document).querySelectorAll('img.thumbs__img').forEach(function (img) {
            if (img.complete && img.naturalWidth === 0) dropBrokenImage(img);
        });
    }
    sweepBrokenImages(document);

    /* ── Подсказки капч подставляют текст в поле ответа ──────────── */
    function bindSuggestions(root) {
        (root || document).querySelectorAll('.chip[data-suggest]').forEach(function (chip) {
            if (chip.dataset.bound) return;
            chip.dataset.bound = '1';
            chip.addEventListener('click', function () {
                var box = chip.closest('.pending__item, .row--captcha');
                var input = box && box.querySelector('input[name="answer"]');
                if (input) {
                    input.value = chip.dataset.suggest || chip.textContent.trim();
                    input.focus();
                }
            });
        });
    }
    bindSuggestions(document);

    /* ── Фильтр по решению + поиск по имени/городу ───────────────── */
    /* Работают по data-атрибутам строк (проставлены в app_feed.html),
       поэтому перерисовка ленты не сбрасывает выбранный фильтр. */
    var filter = 'ALL';
    var query = '';

    function rowMatches(row) {
        if (filter !== 'ALL' && row.dataset.decision !== filter) return false;
        if (query) {
            var name = (row.dataset.name || '').toLowerCase();
            if (name.indexOf(query) === -1) return false;
        }
        return true;
    }

    function applyFilter() {
        if (!feed) return;
        var shown = 0;
        var total = 0;
        feed.querySelectorAll('.row').forEach(function (row) {
            total += 1;
            var ok = rowMatches(row);
            row.hidden = !ok;
            if (ok) shown += 1;
        });
        var counter = document.getElementById('feed-count');
        if (counter) counter.textContent = (query || filter !== 'ALL')
            ? shown + ' из ' + total
            : total + ' событий';
    }

    var seg = document.getElementById('filter-seg');
    if (seg) {
        seg.addEventListener('click', function (e) {
            var btn = e.target.closest('.seg__btn');
            if (!btn) return;
            filter = btn.dataset.filter || 'ALL';
            seg.querySelectorAll('.seg__btn').forEach(function (b) {
                var on = b === btn;
                b.classList.toggle('is-active', on);
                b.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
            applyFilter();
        });
    }

    var search = document.getElementById('feed-search');
    if (search) {
        search.addEventListener('input', function () {
            query = search.value.trim().toLowerCase();
            applyFilter();
        });
    }

    /* ── Лента ───────────────────────────────────────────────────── */
    function atBottom() {
        return window.innerHeight + window.scrollY >= document.body.offsetHeight - 260;
    }

    function scrollToBottom() {
        window.scrollTo({top: document.body.offsetHeight, behavior: 'smooth'});
    }

    /* Сколько страниц ленты сейчас показано (1 = только свежие). Храним в
       dataset — перерисовка по polling НЕ должна схлопывать загруженную
       историю обратно к первой странице. */
    function loadedPages() {
        var n = parseInt((app && app.dataset.pagesLoaded) || '1', 10);
        return (!n || isNaN(n) || n < 1) ? 1 : n;
    }
    function setLoadedPages(n) {
        if (app) app.dataset.pagesLoaded = String(Math.max(1, n || 1));
    }
    setLoadedPages(loadedPages());

    /* Время сообщений хранится в UTC (message_date Telegram). Telegram
       показывает местное время — переводим, иначе лента «отстаёт» на часы. */
    function localizeTimes(root) {
        (root || document).querySelectorAll('time.row__time[datetime]').forEach(function (t) {
            var d = new Date(t.getAttribute('datetime'));
            if (isNaN(d.getTime())) return;
            t.textContent = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
            t.title = d.toLocaleString();
        });
    }
    localizeTimes(feed);

    function fetchPage(page) {
        return fetch('/chat/feed?page=' + page).then(function (r) { return r.json(); });
    }

    function setLoadMoreState(pagesLoaded, totalPages) {
        if (!feedMore) return;
        var btn = feedMore.querySelector('#load-older');
        if (!btn) {
            if (pagesLoaded >= 1 && totalPages > pagesLoaded) {
                feedMore.innerHTML = '<button type="button" class="btn btn--ghost" id="load-older" ' +
                    'data-page="' + (pagesLoaded + 1) + '">Показать более ранние</button>';
                bindLoadMore();
            }
            return;
        }
        btn.dataset.page = pagesLoaded + 1;
        btn.hidden = pagesLoaded >= totalPages;
    }

    function bindLoadMore() {
        var btn = feedMore && feedMore.querySelector('#load-older');
        if (!btn || btn.dataset.bound) return;
        btn.dataset.bound = '1';
        btn.addEventListener('click', function () {
            var next = parseInt(btn.dataset.page || '2', 10);
            if (!next || isNaN(next)) return;
            var before = document.body.offsetHeight;
            fetchPage(next)
                .then(function (data) {
                    // Страница 1 = свежие, дальше — более ранние → вставляем сверху.
                    feed.insertAdjacentHTML('afterbegin', data.feed);
                    localizeTimes(feed);
                    setLoadedPages(next);
                    setLoadMoreState(next, data.total_pages || 1);
                    bindSuggestions(feed);
                    sweepBrokenImages(feed);
                    applyFilter();
                    window.scrollTo(0, window.scrollY + (document.body.offsetHeight - before));
                })
                .catch(function () { /* следующая попытка */ });
        });
    }
    bindLoadMore();

    /* Полная перерисовка ВСЕХ загруженных страниц. Раньше polling заменял
       ленту только первой страницей — вся подгруженная история исчезала, а
       позиция скролла сбрасывалась («лента съезжает»). Теперь тянем страницы
       1..N и склеиваем: сверху — самые старые. */
    function refreshFeed() {
        var wasAtBottom = atBottom();
        var before = document.body.offsetHeight;
        var n = loadedPages();
        // Один срез на все загруженные страницы: параллельные запросы по
        // OFFSET разъезжались, если между ними приходило новое сообщение.
        return fetch('/chat/feed?page=1&pages=' + n)
            .then(function (r) { return r.json(); })
            .then(function (first) {
                var totalPages = first.total_pages || 1;
                if (n > totalPages) n = totalPages;
                setLoadedPages(n);
                var banner = document.getElementById('stream-banner');
                if (banner && typeof first.banner === 'string') {
                    banner.innerHTML = first.banner;
                }
                var oldPending = document.getElementById('pending');
                if (oldPending) oldPending.remove();
                if (first.pending) {
                    feed.insertAdjacentHTML('beforebegin', first.pending);
                }
                feed.innerHTML = first.feed || '';
                localizeTimes(feed);
                if (app) {
                    app.dataset.page = String(first.page || 1);
                    app.dataset.totalPages = String(totalPages);
                }
                setLoadMoreState(n, totalPages);
                bindSuggestions(document);
                sweepBrokenImages(feed);
                applyFilter();
                if (wasAtBottom) {
                    scrollToBottom();
                } else {
                    // Пользователь читает историю — держим позицию по дельте
                    // высоты, чтобы новые сообщения снизу не дёргали вид.
                    window.scrollTo(
                        0, window.scrollY + (document.body.offsetHeight - before)
                    );
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
                    return refreshFeed();
                }
            })
            .catch(function () { /* тихо */ });
    }

    /* ── Debug-консоль (Stage 9) ─────────────────────────────────── */
    /* Снимок состояния + последние логи из памяти (/chat/debug).
       Обновляется, пока панель открыта, — чтобы ловить «прямо сейчас». */
    var debugDrawer = document.getElementById('debug-drawer');
    var debugBackdrop = document.getElementById('debug-backdrop');
    var debugStateBox = document.getElementById('debug-state');
    var debugLogsBox = document.getElementById('debug-logs');
    var debugOpenBtn = document.getElementById('debug-open');
    var debugTimer = null;

    function openDebug() {
        if (!debugDrawer) return;
        debugDrawer.hidden = false;
        if (debugBackdrop) debugBackdrop.hidden = false;
        document.body.style.overflow = 'hidden';
        if (debugOpenBtn) debugOpenBtn.setAttribute('aria-expanded', 'true');
        refreshDebug();
        if (debugTimer) clearInterval(debugTimer);
        debugTimer = setInterval(refreshDebug, POLL_MS);
        var close = document.getElementById('debug-close');
        if (close) close.focus();
    }

    function closeDebug() {
        if (!debugDrawer) return;
        debugDrawer.hidden = true;
        if (debugBackdrop) debugBackdrop.hidden = true;
        document.body.style.overflow = '';
        if (debugOpenBtn) debugOpenBtn.setAttribute('aria-expanded', 'false');
        if (debugTimer) { clearInterval(debugTimer); debugTimer = null; }
    }

    function fmtTs(state) {
        return (state && state.generated_at) ? ' снимок ' + state.generated_at : '';
    }

    function renderDebugState(state) {
        if (!debugStateBox) return;
        state = state || {};
        var counts = state.counts || {};
        var signals = state.signals || {};
        var err = state.stream_error;
        var lines = [
            'режим: ' + (state.mode || '—') + '   аккаунт: ' + (state.account_session || '—'),
            'сбой потока: ' + (err ? (err.reason || err.message || 'да') + ' @ ' + (err.at || '') : 'нет'),
            '❤️ лимит до: ' + (state.likes_exhausted_until || '—'),
            'счётчики: raw=' + (counts.raw_messages || 0) +
                ' sent=' + (counts.sent_messages || 0) +
                ' auto=' + (counts.auto_actions || 0) +
                ' profiles=' + (counts.profiles || 0) +
                ' pm=' + (counts.profile_messages || 0) +
                ' капчи=' + (counts.pending_captchas || 0),
            'сигналы: raw=' + (signals.max_raw || '—') +
                ' sent=' + (signals.max_sent || '—') +
                ' auto=' + (signals.max_auto || '—')
        ];
        var actions = state.last_auto_actions || [];
        if (actions.length) {
            lines.push('', 'последние авто-действия:');
            actions.forEach(function (a) {
                lines.push('  ' + (a.sent_at || '') + '  ' + (a.action || '') +
                    ' / ' + (a.decision || '') + '  #' + (a.profile_id == null ? '?' : a.profile_id) +
                    '  ' + (a.message_text || ''));
            });
        }
        debugStateBox.textContent = fmtTs(state) + '\n' + lines.join('\n');
    }

    function renderDebugLogs(logs) {
        if (!debugLogsBox) return;
        debugLogsBox.textContent = '';
        if (!logs || !logs.length) {
            var empty = document.createElement('div');
            empty.className = 'debug__empty';
            empty.textContent = 'Нет записей (поднимите уровень или подождите).';
            debugLogsBox.appendChild(empty);
            return;
        }
        logs.forEach(function (entry) {
            var row = document.createElement('div');
            row.className = 'debug__log debug__log--' + (entry.level || 'info').toLowerCase();
            var meta = document.createElement('span');
            meta.className = 'debug__log-meta';
            var lvl = document.createElement('span');
            lvl.className = 'debug__log-level';
            lvl.textContent = (entry.level || '').padEnd(8);
            var ts = document.createTextNode((entry.ts || '').slice(-8) + ' ');
            meta.appendChild(ts);
            meta.appendChild(lvl);
            var msg = document.createElement('span');
            msg.className = 'debug__log-msg';
            msg.textContent = entry.message || '';
            row.appendChild(meta);
            row.appendChild(msg);
            debugLogsBox.appendChild(row);
        });
        debugLogsBox.scrollTop = debugLogsBox.scrollHeight;
    }

    function refreshDebug() {
        if (!debugDrawer || debugDrawer.hidden) return;
        var levelSel = document.getElementById('debug-level');
        var level = levelSel ? levelSel.value : '';
        var url = '/chat/debug?limit=300' + (level ? '&level=' + encodeURIComponent(level) : '');
        fetch(url, {headers: {'X-Requested-With': 'fetch'}})
            .then(function (r) { return r.json(); })
            .then(function (data) {
                renderDebugState(data.state);
                renderDebugLogs(data.logs);
            })
            .catch(function () { /* тихо: следующий тик */ });
    }

    if (debugOpenBtn) debugOpenBtn.addEventListener('click', openDebug);
    var debugCloseBtn = document.getElementById('debug-close');
    if (debugCloseBtn) debugCloseBtn.addEventListener('click', closeDebug);
    if (debugBackdrop) debugBackdrop.addEventListener('click', closeDebug);
    var debugRefreshBtn = document.getElementById('debug-refresh');
    if (debugRefreshBtn) debugRefreshBtn.addEventListener('click', refreshDebug);
    var debugLevelSel = document.getElementById('debug-level');
    if (debugLevelSel) debugLevelSel.addEventListener('change', refreshDebug);

    if (app) {
        setInterval(poll, POLL_MS);
        applyFilter();
        window.dvai = {
            refreshFeed: refreshFeed, openDrawer: openDrawer, closeDrawer: closeDrawer,
            openDebug: openDebug, closeDebug: closeDebug
        };
    }
})();
