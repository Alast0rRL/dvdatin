/* Разметка внешности (/rate): каждое фото анкеты оценивается ОТДЕЛЬНО —
   одно фото на экране, сверху «фото 2 из 3». Оценка с клавиатуры, кнопками
   и свайпом, отмена. Оценка уходит сразу (оптимистично) — следующее фото
   показывается без ожидания сервера; фото следующих анкет грузятся заранее.

   Телефон: свайп вправо — нравится, влево — нет (как в Tinder).
   Компьютер: клавиши ← → ↓ ↑ Backspace. */
(function () {
    'use strict';

    var root = document.getElementById('rate');
    if (!root) return;

    var csrf = root.getAttribute('data-csrf') || '';
    var card = document.getElementById('rate-card');
    var photosBox = document.getElementById('rate-photos');
    var dotsBox = document.getElementById('rate-dots');
    var posBox = document.getElementById('rate-pos');
    var emptyBox = document.getElementById('rate-empty');
    var undoBtn = document.getElementById('rate-undo');
    var badge = document.getElementById('rate-badge');

    var QUEUE_MIN = 8;      // держим в очереди не меньше фото (грузятся заранее)
    var QUEUE_FETCH = 10;   // сколько анкет просить за раз (= RATE_BATCH_MAX)
    var SWIPE_SHARE = 0.25; // доля ширины карточки, после которой свайп = оценка
    var TAP_PX = 10;        // сдвиг меньше — это тап, а не свайп

    // Очередь ФОТО: {profile_id, message_id, src, pos, count, uncertain, img}
    var queue = [];
    var current = null;
    var history = [];       // оценённые фото (для «Назад»)
    var loading = false;
    var exhausted = false;

    function setStats(s) {
        if (!s) return;
        var map = {'rate-rated': s.rated, 'rate-left': s.left, 'rate-old': s.profiles_rated,
                   'rate-like': s.LIKE, 'rate-dislike': s.DISLIKE, 'rate-skip': s.SKIP,
                   'rate-vice': s.VICE};
        Object.keys(map).forEach(function (id) {
            var el = document.getElementById(id);
            if (el && map[id] !== undefined) el.textContent = String(map[id]);
        });
    }

    // Предзагрузка: держим ссылку на Image, чтобы браузер не выбросил её из кэша.
    function preload(photo) {
        var img = new Image();
        img.decoding = 'async';
        img.src = photo.src;
        photo.img = img;
    }

    // Анкеты, которые уже были в этой вкладке, повторно в очередь не попадают
    // (раньше обрезанный список исключений возвращал анкеты по второму кругу).
    var seen = {};

    function profileIds() {
        var ids = {};
        queue.forEach(function (p) { ids[p.profile_id] = 1; });
        if (current) ids[current.profile_id] = 1;
        history.slice(-40).forEach(function (h) { ids[h.photo.profile_id] = 1; });
        return Object.keys(ids);
    }

    function refill() {
        if (loading || exhausted || queue.length >= QUEUE_MIN) return;
        loading = true;
        var url = '/rate/next?limit=' + QUEUE_FETCH + '&exclude=' + profileIds().join(',');
        fetch(url, {headers: {'X-Requested-With': 'fetch'}})
            .then(function (r) { return r.json(); })
            .then(function (data) {
                loading = false;
                setStats(data.stats);
                var items = data.items || [];
                var fresh = items.filter(function (it) { return !seen[it.profile_id]; });
                if (!items.length) exhausted = true;
                fresh.forEach(function (it) {
                    seen[it.profile_id] = true;
                    var n = it.photos.length;
                    it.photos.forEach(function (ph, i) {
                        var photo = {profile_id: it.profile_id, message_id: ph.message_id,
                                     src: ph.src, pos: i + 1, count: n,
                                     uncertain: !!it.uncertain};
                        preload(photo);
                        queue.push(photo);
                    });
                });
                if (items.length && !fresh.length) {
                    // Пришли только уже виденные — не долбим сервер по кругу.
                    setTimeout(refill, 2000);
                    return;
                }
                if (!current) showNext();
                else refill();
            })
            .catch(function () {
                loading = false;
                if (!current) showEmpty('Не удалось загрузить фото. Обнови страницу.');
            });
    }

    function showEmpty(text) {
        resetCard();
        badge.hidden = true;
        photosBox.textContent = '';
        dotsBox.textContent = '';
        posBox.textContent = '';
        emptyBox.textContent = text;
        emptyBox.hidden = false;
    }

    function render(photo) {
        photosBox.textContent = '';
        emptyBox.hidden = true;
        // Анкета из очереди «модель сомневается» — её оценка учит модель сильнее всего.
        badge.hidden = !photo.uncertain;
        resetCard();
        if (!photo.img) preload(photo);
        var img = document.createElement('img');
        img.className = 'rate__photo is-current';
        img.alt = '';
        img.draggable = false;
        img.decoding = 'async';
        img.addEventListener('error', function () { img.classList.add('is-broken'); });
        img.src = photo.img.src;  // уже в кэше браузера, если предзагрузка успела
        photosBox.appendChild(img);

        // Какое фото анкеты на экране: полоски + «фото 2 из 3».
        dotsBox.textContent = '';
        posBox.textContent = photo.count > 1
            ? 'Фото ' + photo.pos + ' из ' + photo.count : 'Одно фото';
        if (photo.count > 1) {
            for (var i = 1; i <= photo.count; i++) {
                var d = document.createElement('span');
                d.className = 'rate__dot' + (i === photo.pos ? ' is-current' : '');
                dotsBox.appendChild(d);
            }
        }
    }

    function showNext() {
        current = queue.shift() || null;
        if (current) {
            render(current);
        } else if (exhausted) {
            showEmpty('Все фото оценены. Спасибо!');
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

    function photoUrl(photo) {
        return '/rate/' + photo.profile_id + '/' + photo.message_id;
    }

    function rate(rating) {
        if (!current) return;
        var photo = current;
        history.push({photo: photo, rating: rating});
        undoBtn.disabled = false;
        showNext();
        post(photoUrl(photo), {rating: rating})
            .then(function (data) { setStats(data.stats); })
            .catch(function () {
                // Не сохранилось — вернуть фото в начало очереди.
                history = history.filter(function (h) { return h.photo !== photo; });
                if (current) queue.unshift(current);
                current = null;
                queue.unshift(photo);
                showNext();
            });
    }

    function undo() {
        var last = history.pop();
        undoBtn.disabled = history.length === 0;
        if (!last) return;
        if (current) queue.unshift(current);
        current = last.photo;
        render(current);
        post(photoUrl(last.photo) + '/undo', {})
            .then(function (data) { setStats(data.stats); })
            .catch(function () { /* оценка останется — повторная её перезапишет */ });
    }

    // ── Свайп (pointer events: палец и мышь) ──────────────────────────
    var drag = null;  // {id, x0, dx, w}

    function resetCard() {
        card.style.transition = '';
        card.style.transform = '';
        card.classList.remove('is-swipe-like', 'is-swipe-dislike', 'is-dragging');
    }

    function onDown(e) {
        if (!current || e.button > 0) return;
        drag = {id: e.pointerId, x0: e.clientX, dx: 0,
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
        var w = drag.w;
        drag = null;
        card.classList.remove('is-dragging');

        if (Math.abs(dx) < TAP_PX || Math.abs(share) < SWIPE_SHARE) {
            // Тап или не дотянул — карточка возвращается на место.
            card.style.transition = 'transform .18s ease-out';
            card.style.transform = '';
            card.classList.remove('is-swipe-like', 'is-swipe-dislike');
            return;
        }
        // Улетает в сторону свайпа, затем оценка и следующее фото.
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
    // Кнопки не забирают фокус: иначе после клика мышью стрелки в части
    // браузеров ходят фокусом по кнопкам, а Enter/пробел жмёт выделенную.
    var actionBtns = root.querySelectorAll('[data-rate], #rate-undo');
    for (var b = 0; b < actionBtns.length; b++) {
        actionBtns[b].addEventListener('mousedown', function (e) { e.preventDefault(); });
    }
    root.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-rate]');
        if (btn) {
            btn.blur();
            rate(btn.getAttribute('data-rate'));
        }
    });
    undoBtn.addEventListener('click', function () { undoBtn.blur(); undo(); });

    var KEY_RATE = {ArrowLeft: 'DISLIKE', a: 'DISLIKE', 'ф': 'DISLIKE',
                    ArrowRight: 'LIKE', d: 'LIKE', 'в': 'LIKE',
                    ArrowDown: 'SKIP', s: 'SKIP', 'ы': 'SKIP',
                    ArrowUp: 'VICE', w: 'VICE', 'ц': 'VICE'};
    var KEY_UNDO = {Backspace: 1, z: 1, 'я': 1};

    // Перехват на window в фазе захвата — раньше браузерной навигации
    // фокусом и чужих обработчиков.
    window.addEventListener('keydown', function (e) {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        var tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        var k = e.key;
        if (!KEY_RATE[k] && !KEY_UNDO[k]) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.repeat) return;  // зажатая клавиша не оценивает пачку фото
        if (document.activeElement && document.activeElement !== document.body) {
            document.activeElement.blur();
        }
        if (KEY_RATE[k]) rate(KEY_RATE[k]);
        else undo();
    }, true);

    showEmpty('Загружаю…');
    refill();
})();
