# Переобучение модели вкуса по фото на разметке /rate.
#
#   venv/bin/python scripts/train_photo_taste.py            # отчёт + замена, если не хуже
#   venv/bin/python scripts/train_photo_taste.py --dry-run  # только отчёт
#   venv/bin/python scripts/train_photo_taste.py --force    # заменить в любом случае
#
# После замены модели вердикты photo_scores пересчитываются новой моделью по
# сохранённым отпечаткам (CLIP не нужен) — очередь /rate «модель сомневается»
# сразу строится по свежей модели (--no-rescore, чтобы не трогать).
#
# Данные (CLIP здесь не нужен — отпечатки пишет бот):
#   * оценки ОТДЕЛЬНЫХ фото (photo_item_ratings × photo_item_embeddings, с
#     2026-10-29) — основной источник;
#   * старые оценки анкеты целиком (photo_ratings × photo_embeddings — средний
#     отпечаток её фото), кроме анкет, у которых уже есть оценки фото.
# LIKE=1, DISLIKE/VICE=0, SKIP — исключён. Качество — честное: out-of-fold AUC
# (5 повторов × 5 фолдов, фолды по анкетам — фото одной девушки не попадают
# одновременно в обучение и в проверку).
#
# Модель пишется с ``aggregate: mean_prob``: бот оценивает каждое фото анкеты
# и усредняет score (services/photo_taste.py); пороги «верхние N% анкет»
# считаются так же — по среднему score фото каждой анкеты.
#
# Сравнение со старой моделью — на ОДНИХ И ТЕХ ЖЕ новых оценках (поставленных
# после её обучения, она их не видела): старая модель против out-of-fold
# прогнозов новой. Общий AUC тут не годится: очередь /rate отдаёт сначала
# «пограничные» анкеты, на них любая модель путается, и общий AUC падает от
# того, что в разметку добавились трудные примеры, а не от того, что модель
# стала хуже. Новых оценок мало (<30 или нет обоих классов) — сравнение по
# общему AUC. Замена, если новая не хуже (минус допуск --tolerance). Старый JSON
# сохраняется рядом (taste_model.<версия>.json), запись атомарная — бот
# подхватывает новый файл сам (PhotoTasteModel проверяет mtime раз в 30 с).
#
# Нужны numpy + scikit-learn (requirements-ml.txt).

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold

CS = [0.03, 0.1, 0.3, 1, 3, 10]
#: Сколько фото анкеты смотрит бот (config photo_taste.max_photos) — для порогов.
MAX_PHOTOS = 3
#: Альбом одного показа — номера сообщений подряд (как Database.PHOTO_ALBUM_GAP).
ALBUM_GAP = 2
LIKE_SHARE = 0.15      # верхние 15% анкет → зона LIKE
DISLIKE_SHARE = 0.50   # нижние 50% → зона DISLIKE


def _vec(blob: bytes) -> np.ndarray:
    v = np.frombuffer(blob, dtype="<f4").astype(np.float64)
    return v / (np.linalg.norm(v) or 1.0)


def profile_photo_vectors(con: sqlite3.Connection) -> dict[int, list[np.ndarray]]:
    """Отпечатки, по которым бот оценивает анкету: первые MAX_PHOTOS фото
    последнего показа; нет отпечатков фото — средний отпечаток анкеты."""
    out: dict[int, list[np.ndarray]] = {}
    has_items = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_item_embeddings'"
    ).fetchone()
    rows = con.execute(
        "SELECT profile_id, telegram_message_id, emb FROM photo_item_embeddings "
        "WHERE emb IS NOT NULL ORDER BY profile_id, telegram_message_id"
    ).fetchall() if has_items else []
    by_pid: dict[int, list[tuple[int, bytes]]] = {}
    for pid, mid, blob in rows:
        by_pid.setdefault(pid, []).append((mid, blob))
    for pid, items in by_pid.items():
        start = len(items) - 1
        while start > 0 and items[start][0] - items[start - 1][0] <= ALBUM_GAP:
            start -= 1
        out[pid] = [_vec(b) for _m, b in items[start:][:MAX_PHOTOS]]
    for pid, blob in con.execute(
        "SELECT profile_id, emb FROM photo_embeddings WHERE emb IS NOT NULL"
    ):
        out.setdefault(pid, [_vec(blob)])
    return out


def profile_score(vecs: list[np.ndarray], w: np.ndarray, bias: float) -> float:
    """Как бот (aggregate=mean_prob): среднее score отдельных фото."""
    z = np.array([v @ w + bias for v in vecs])
    return float(np.mean(1.0 / (1.0 + np.exp(-z))))


def rescore(db_path: Path, model: dict) -> int:
    """Пересчитывает последний вердикт каждой анкеты новой моделью."""
    w = np.asarray(model["weights"], dtype=np.float64)
    con = sqlite3.connect(str(db_path), timeout=30)
    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_scores'"
    ).fetchone()
    if not has_table:
        con.close()
        return 0
    vectors = profile_photo_vectors(con)
    rows = con.execute(
        "SELECT id, profile_id FROM photo_scores WHERE id IN "
        "(SELECT MAX(id) FROM photo_scores GROUP BY profile_id)"
    ).fetchall()
    n = 0
    for row_id, pid in rows:
        vecs = vectors.get(pid)
        if not vecs:
            continue
        score = profile_score(vecs, w, float(model["bias"]))
        if score >= model["like_threshold"]:
            verdict = "LIKE"
        elif score <= model["dislike_threshold"]:
            verdict = "DISLIKE"
        else:
            verdict = "REVIEW"
        con.execute(
            "UPDATE photo_scores SET score = ?, verdict = ?, model_version = ? WHERE id = ?",
            (score, verdict, model["version"], row_id),
        )
        n += 1
    con.commit()
    con.close()
    return n


def load_data(db_path: Path) -> dict:
    """Обучающие примеры (фото и старые анкеты целиком) + отпечатки всех анкет.

    ``X, y, groups`` (анкета), ``rated_at``, ``kind`` ('photo'/'profile'),
    ``profiles`` — {profile_id: [векторы фото]} для порогов.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    has_items = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_item_ratings'"
    ).fetchone()
    X, y, groups, rated_at, kind = [], [], [], [], []
    item_pids: set[int] = set()
    if has_items:
        for pid, blob, rating, created_at in con.execute(
            "SELECT r.profile_id, e.emb, r.rating, r.created_at FROM photo_item_ratings r "
            "JOIN photo_item_embeddings e ON e.profile_id = r.profile_id "
            " AND e.telegram_message_id = r.telegram_message_id "
            "WHERE e.emb IS NOT NULL"
        ):
            item_pids.add(pid)
            if rating not in ("LIKE", "DISLIKE", "VICE"):
                continue
            X.append(np.frombuffer(blob, dtype="<f4"))
            y.append(1 if rating == "LIKE" else 0)
            groups.append(pid)
            rated_at.append(created_at or "")
            kind.append("photo")
    for pid, blob, rating, created_at in con.execute(
        "SELECT r.profile_id, e.emb, r.rating, r.created_at FROM photo_ratings r "
        "JOIN photo_embeddings e ON e.profile_id = r.profile_id WHERE e.emb IS NOT NULL"
    ):
        if pid in item_pids or rating not in ("LIKE", "DISLIKE", "VICE"):
            continue
        X.append(np.frombuffer(blob, dtype="<f4"))
        y.append(1 if rating == "LIKE" else 0)
        groups.append(pid)
        rated_at.append(created_at or "")
        kind.append("profile")
    profiles = profile_photo_vectors(con)
    con.close()
    return {
        "X": np.array(X), "y": np.array(y, dtype=int), "groups": np.array(groups),
        "rated_at": rated_at, "kind": np.array(kind), "profiles": profiles,
    }


def oof_predictions(
    X: np.ndarray, y: np.ndarray, groups: np.ndarray, repeats: int = 5,
) -> tuple[np.ndarray, list[float]]:
    """Out-of-fold вероятности (среднее по повторам) и AUC каждого повтора.
    Фолды по анкетам: фото одной девушки — целиком в обучении или в проверке."""
    mean = np.zeros(len(y))
    aucs = []
    for r in range(repeats):
        p = np.zeros(len(y))
        folds = StratifiedGroupKFold(5, shuffle=True, random_state=r)
        for tr, te in folds.split(X, y, groups):
            m = LogisticRegressionCV(Cs=CS, cv=4, scoring="roc_auc",
                                     class_weight="balanced", max_iter=5000).fit(X[tr], y[tr])
            p[te] = m.predict_proba(X[te])[:, 1]
        aucs.append(roc_auc_score(y, p))
        mean += p / repeats
    return mean, aucs


def model_scores(model: dict, X: np.ndarray) -> np.ndarray:
    """Вероятности сохранённой модели (как PhotoTasteModel в боте)."""
    w = np.asarray(model["weights"], dtype=np.float64)
    Xn = X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)
    return 1.0 / (1.0 + np.exp(-(Xn @ w + float(model["bias"]))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/database.db")
    ap.add_argument("--model", default="data/ml/taste_model.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.01)
    ap.add_argument("--no-rescore", action="store_true",
                    help="не пересчитывать вердикты photo_scores новой моделью")
    args = ap.parse_args()

    data = load_data(Path(args.db))
    X, y, groups, rated_at = data["X"], data["y"], data["groups"], data["rated_at"]
    kind, profiles = data["kind"], data["profiles"]
    if len(y) < 100 or y.sum() < 20:
        print(f"Мало данных: {len(y)} примеров, {y.sum()} ❤️ — нужно ≥100 и ≥20 ❤️")
        return 1
    n_photo = int((kind == "photo").sum())
    print(f"Размечено с отпечатком: {len(y)} примеров, ❤️ {y.sum()} ({y.mean() * 100:.1f}%) — "
          f"фото по одному: {n_photo}, анкет целиком: {len(y) - n_photo}; "
          f"анкет с отпечатками: {len(profiles)}")

    oof, aucs = oof_predictions(X, y, groups)
    auc, std = float(np.mean(aucs)), float(np.std(aucs))
    print(f"Новая модель: AUC {auc:.3f} ± {std:.3f} по всей разметке "
          "(на анкетах, которых модель не видела)")

    model_path = Path(args.model)
    old = json.loads(model_path.read_text(encoding="utf-8")) if model_path.exists() else {}
    old_auc = float(old.get("oof_auc") or 0.0)
    # Честное сравнение — на оценках, поставленных после обучения текущей модели.
    new_auc_cmp, old_auc_cmp, cmp_note = auc, old_auc, "по всей разметке"
    if old:
        print(f"Текущая модель {old.get('version')}: AUC {old_auc:.3f}, "
              f"обучена на {old.get('trained_on')}")
        trained_at = old.get("trained_at") or datetime.fromtimestamp(
            model_path.stat().st_mtime, tz=timezone.utc).isoformat()
        fresh = np.array([t > trained_at for t in rated_at])
        if fresh.sum() >= 30 and 0 < y[fresh].sum() < fresh.sum():
            old_auc_cmp = float(roc_auc_score(y[fresh], model_scores(old, X[fresh])))
            new_auc_cmp = float(roc_auc_score(y[fresh], oof[fresh]))
            cmp_note = f"на {int(fresh.sum())} новых оценках (❤️ {int(y[fresh].sum())})"
            print(f"Сравнение {cmp_note}, которых текущая модель не видела: "
                  f"текущая {old_auc_cmp:.3f}, новая {new_auc_cmp:.3f}")

    C = float(LogisticRegressionCV(Cs=CS, cv=5, scoring="roc_auc", class_weight="balanced",
                                   max_iter=5000).fit(X, y).C_[0])
    final = LogisticRegression(C=C, class_weight="balanced", max_iter=5000).fit(X, y)
    w_final, b_final = final.coef_[0].astype(np.float64), float(final.intercept_[0])
    # Пороги — по оценке АНКЕТЫ так, как её считает бот (среднее score фото).
    s_all = np.array([profile_score(v, w_final, b_final) for v in profiles.values()])
    like_thr = float(np.quantile(s_all, 1 - LIKE_SHARE))
    # Пороги «верхние N% анкет» — для режима decide (photo_taste.like_share).
    like_thresholds = {
        f"{share:.2f}": float(np.quantile(s_all, 1 - share))
        for share in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)
    }
    dislike_thr = float(np.quantile(s_all, DISLIKE_SHARE))
    version = "clip-l14-lr-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    new = {
        "version": version,
        "model": "ViT-L-14/laion2b_s32b_b82k",
        "C": C,
        "weights": [float(w) for w in final.coef_[0]],
        "bias": float(final.intercept_[0]),
        "like_threshold": like_thr,
        "dislike_threshold": dislike_thr,
        "like_thresholds": like_thresholds,
        # Бот: score анкеты = среднее score её фото (services/photo_taste.py).
        "aggregate": "mean_prob",
        "trained_on": {"examples": int(len(y)), "photos": n_photo,
                       "profiles": int(len(set(groups.tolist()))), "likes": int(y.sum())},
        "oof_auc": round(auc, 3),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"Пороги: LIKE ≥ {like_thr:.3f} (верхние {LIKE_SHARE:.0%}), "
          f"DISLIKE ≤ {dislike_thr:.3f} (нижние {DISLIKE_SHARE:.0%}), C={C}")

    if args.dry_run:
        print("--dry-run: модель не заменена")
        return 0
    if old and new_auc_cmp < old_auc_cmp - args.tolerance and not args.force:
        print(f"Новая модель хуже текущей {cmp_note} ({new_auc_cmp:.3f} < "
              f"{old_auc_cmp:.3f}) — не заменяю (--force, чтобы заменить)")
        return 2
    if old:
        backup = model_path.with_name(f"taste_model.{old.get('version', 'old')}.json")
        backup.write_text(json.dumps(old), encoding="utf-8")
        print(f"Старая модель сохранена: {backup}")
    tmp = model_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new), encoding="utf-8")
    os.replace(tmp, model_path)
    print(f"Новая модель записана: {model_path} ({version}); бот подхватит её за ~30 с")
    if not args.no_rescore:
        n = rescore(Path(args.db), new)
        print(f"Вердикты photo_scores пересчитаны новой моделью: {n} анкет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
