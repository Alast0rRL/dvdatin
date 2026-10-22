/* Разметка внешности (/rate): очередь анкет, оценка с клавиатуры и свайпом,
   отмена. Оценка уходит сразу (оптимистично) — следующая анкета показывается
   без ожидания сервера; фото следующих анкет грузятся заранее.

   Телефон (узкий экран): одно большое фото, тап по левой/правой половине
   листает фото анкеты, свайп вправо — нравится, влево — нет (как в Tinder).
   Компьютер: сетка фото, клавиши ← → ↓ Backspace. */
(function () {
    'use strict';

    var root = document.getElementById('rate');
    if (!root) return;

    var csrf = root.getAttribute('data-csrf') || '';
    var card = document.getElementById('rate-card');
    var photosBox = document.getElementById('rate-photos');
    var dotsBox = document.getElementById('rate-dots');
    var emptyBox = document.getElementById('rate-empty');
    var undoBtn = document.getElementById('rate-undo');

    var QUEUE_MIN = 6;      // держим в очереди не меньше (фото грузятся заранее)
    var QUEUE_FETCH = 10;   // сколько просить за раз (= RATE_BATCH_MAX)
    var SWIPE_SHARE = 0.25; // доля ширины карточки, после которой свайп = оценка
    var TAP_PX = 10;        // сдвиг меньше — это тап, а не свайп

    var narrow = window.matchMedia('(max-width: 640px)');

    var queue = [];         // [{profile_id, photos: [url]}]
    var current = null;
    var photoIdx = 0;       // текущее фото в карусели (телефон)
    var history = [];       // оценённые элементы (для «Назад»)
    var loading = false;
    var exhausted = false;

    function setStats(s) {
        if (!s) return;
        var map = {'rate-rated': s.rated, 'rate-total': s.total, 'rate-like': s.LIKE,
                   'rate-dislike': s.DISLIKE, 'rate-skip': s.SKIP};
        Object.keys(map).forEach(function (id) {
            var el = document.getElementById(id);
            if (el && map[id] !== undefined) el.textContent = String(map[id]);
        });
    }

    // Предзагрузка: держим ссылки на Image, чтобы браузер не выбросил их из кэша.
    function preload(item) {
        item.imgs = item.photos.map(function (src) {
            var img = new Image();
            img.decoding = 'async';
            img.src = src;
            return img;
        });
    }

    function knownIds() {
        var ids = queue.map(function (i) { return i.profile_id; });
        if (current) ids.push(current.profile_id);
        history.forEach(function (h) { ids.push(h.item.profile_id); });
        return ids.slice(-150);
    }

    function refill() {
        if (loading || exhausted || queue.length >= QUEUE_MIN) return;
        loading = true;
        var url = '/rate/next?limit=' + QUEUE_FETCH + '&exclude=' + knownIds().join(',');
        fetch(url, {headers: {'X-Requested-With': 'fetch'}})
            .then(function (r) { return r.json(); })
            .then(function (data) {
                loading = false;
                setStats(data.stats);
                var known = knownIds();
                var fresh = (data.items || []).filter(function (it) {
                    return known.indexOf(it.profile_id) < 0;
                });
                if (!fresh.length) exhausted = true;
                fresh.forEach(function (it) { preload(it); queue.push(it); });
                if (!current) showNext();
                else refill();
            })
            .catch(function () {
                loading = false;
                if (!current) showEmpty('Не удалось загрузить анкеты. Обнови страницу.');
            });
    }

    function showEmpty(text) {
        resetCard();
        photosBox.textContent = '';
        dotsBox.textContent = '';
        emptyBox.textContent = text;
        emptyBox.hidden = false;
    }

    function renderDots() {
        dotsBox.textContent = '';
        if (!current || current.photos.length < 2) return;
        current.photos.forEach(function (_, i) {
            var d = document.createElement('span');
            d.className = 'rate__dot' + (i === photoIdx ? ' is-current' : '');
            dotsBox.appendChild(d);
        });
    }

    function setPhoto(i) {
        if (!current) return;
        var n = current.photos.length;
        photoIdx = (i + n) % n;
        var imgs = photosBox.querySelectorAll('.rate__photo');
        for (var k = 0; k < imgs.length; k++) {
            imgs[k].classList.toggle('is-current', k === photoIdx);
        }
        renderDots();
    }

    function render(item) {
        photosBox.textContent = '';
        emptyBox.hidden = true;
        resetCard();
        (item.imgs || []).length || preload(item);
        item.imgs.forEach(function (pre) {
            var img = document.createElement('img');
            img.className = 'rate__photo';
            img.alt = '';
            img.draggable = false;
            img.decoding = 'async';
            img.addEventListener('error', function () { img.classList.add('is-broken'); });
            img.src = pre.src;  // уже в кэше браузера, если предзагрузка успела
            photosBox.appendChild(img);
        });
        setPhoto(0);
    }

    function showNext() {
        current = queue.shift() || null;
        if (current) {
            render(current);
        } else if (exhausted) {
            showEmpty('Все анкеты с фото оценены. Спасибо!');
        } else {
            showEmpty('Загружаю…');
        }
        refill();
    }

    function post(url, data) {
        var body = new FormData();
        body.append('csrf_token', csrf);
        Object.keys(data || {}).forEach(function (k) { body.append(k, data[k]); });
        return fetch(url, {method: 'POST', body: body, headers: {'X-Requested-With': 'fetch'}})
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            });
    }

    function rate(rating) {
        if (!current) return;
        var item = current;
        history.push({item: item, rating: rating});
        undoBtn.disabled = false;
        showNext();
        post('/rate/' + item.profile_id, {rating: rating})
            .then(function (data) { setStats(data.stats); })
            .catch(function () {
                // Не сохранилось — вернуть анкету в начало очереди.
                history = history.filter(function (h) { return h.item !== item; });
                if (current) queue.unshift(current);
                current = null;
                queue.unshift(item);
                showNext();
            });
    }

    function undo() {
        var last = history.pop();
        undoBtn.disabled = history.length === 0;
        if (!last) return;
        if (current) queue.unshift(current);
        current = last.item;
        render(current);
        post('/rate/' + last.item.profile_id + '/undo', {})
            .then(function (data) { setStats(data.stats); })
            .catch(function () { /* оценка останется — повторная её перезапишет */ });
    }

    // ── Свайп (pointer events: палец и мышь) ──────────────────────────
    var drag = null;  // {id, x0, y0, dx, w}

    function resetCard() {
        card.style.transition = '';
        card.style.transform = '';
        card.classList.remove('is-swipe-like', 'is-swipe-dislike', 'is-dragging');
    }

    function onDown(e) {
        if (!current || e.button > 0) return;
        drag = {id: e.pointerId, x0: e.clientX, y0: e.clientY, dx: 0,
                w: card.getBoundingClientRect().width || 1};
        card.classList.add('is-dragging');
        try { card.setPointerCapture(e.pointerId); } catch (err) { /* старые браузеры */ }
    }

    function onMove(e) {
        if (!drag || e.pointerId !== drag.id) return;
        drag.dx = e.clientX - drag.x0;
        var share = drag.dx / drag.w;
        card.style.transform = 'translateX(' + drag.dx + 'px) rotate(' + (share * 12) + 'deg)';
        card.classList.toggle('is-swipe-like', share > SWIPE_SHARE / 2);
        card.classList.toggle('is-swipe-dislike', share < -SWIPE_SHARE / 2);
    }

    function onUp(e) {
        if (!drag || e.pointerId !== drag.id) return;
        var dx = drag.dx;
        var share = dx / drag.w;
        var x = e.clientX - card.getBoundingClientRect().left;
        var w = drag.w;
        drag = null;
        card.classList.remove('is-dragging');

        if (Math.abs(dx) < TAP_PX) {
            resetCard();
            // Тап: на телефоне листает фото анкеты (левая треть — назад).
            if (narrow.matches && current && current.photos.length > 1) {
                setPhoto(photoIdx + (x < w / 3 ? -1 : 1));
            }
            return;
        }
        if (Math.abs(share) < SWIPE_SHARE) {
            // Не дотянул — карточка возвращается на место.
            card.style.transition = 'transform .18s ease-out';
            card.style.transform = '';
            card.classList.remove('is-swipe-like', 'is-swipe-dislike');
            return;
        }
        // Улетает в сторону свайпа, затем оценка и следующая анкета.
        var dir = share > 0 ? 1 : -1;
        card.style.transition = 'transform .16s ease-in';
        card.style.transform = 'translateX(' + (dir * w * 1.4) + 'px) rotate(' + (dir * 18) + 'deg)';
        setTimeout(function () { rate(dir > 0 ? 'LIKE' : 'DISLIKE'); }, 150);
    }

    function onCancel() {
        if (!drag) return;
        drag = null;
        resetCard();
    }

    card.addEventListener('pointerdown', onDown);
    card.addEventListener('pointermove', onMove);
    card.addEventListener('pointerup', onUp);
    card.addEventListener('pointercancel', onCancel);

    // ── Кнопки и клавиатура ──────────────────────────────────────────
    root.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-rate]');
        if (btn) rate(btn.getAttribute('data-rate'));
    });
    undoBtn.addEventListener('click', undo);

    document.addEventListener('keydown', function (e) {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        var tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        var k = e.key;
        var map = {ArrowLeft: 'DISLIKE', a: 'DISLIKE', 'ф': 'DISLIKE',
                   ArrowRight: 'LIKE', d: 'LIKE', 'в': 'LIKE',
                   ArrowDown: 'SKIP', s: 'SKIP', 'ы': 'SKIP'};
        if (map[k]) {
            e.preventDefault();
            rate(map[k]);
        } else if (k === 'Backspace' || k === 'z' || k === 'я') {
            e.preventDefault();
            undo();
        }
    });

    showEmpty('Загружаю…');
    refill();
})();
