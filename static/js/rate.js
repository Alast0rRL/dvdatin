/* Разметка внешности (/rate): очередь анкет, оценка с клавиатуры, отмена.
   Оценка уходит сразу (оптимистично) — следующая анкета показывается без
   ожидания ответа сервера; фото следующих анкет грузятся заранее. */
(function () {
    'use strict';

    var root = document.getElementById('rate');
    if (!root) return;

    var csrf = root.getAttribute('data-csrf') || '';
    var photosBox = document.getElementById('rate-photos');
    var emptyBox = document.getElementById('rate-empty');
    var undoBtn = document.getElementById('rate-undo');

    var QUEUE_MIN = 3;      // держим в очереди не меньше
    var QUEUE_FETCH = 6;    // сколько просить за раз

    var queue = [];         // [{profile_id, photos: [url]}]
    var current = null;
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

    function preload(item) {
        item.photos.forEach(function (src) { var img = new Image(); img.src = src; });
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
                if (!data.items || !data.items.length) exhausted = true;
                (data.items || []).forEach(function (it) { queue.push(it); preload(it); });
                if (!current) showNext();
            })
            .catch(function () {
                loading = false;
                if (!current) showEmpty('Не удалось загрузить анкеты. Обнови страницу.');
            });
    }

    function showEmpty(text) {
        photosBox.textContent = '';
        emptyBox.textContent = text;
        emptyBox.hidden = false;
    }

    function render(item) {
        photosBox.textContent = '';
        emptyBox.hidden = true;
        item.photos.forEach(function (src) {
            var img = document.createElement('img');
            img.className = 'rate__photo';
            img.src = src;
            img.alt = '';
            img.decoding = 'async';
            img.addEventListener('error', function () { img.classList.add('is-broken'); });
            photosBox.appendChild(img);
        });
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
