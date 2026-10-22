# Переобучение модели вкуса по фото на разметке /rate.
#
#   venv/bin/python scripts/train_photo_taste.py            # отчёт + замена, если не хуже
#   venv/bin/python scripts/train_photo_taste.py --dry-run  # только отчёт
#   venv/bin/python scripts/train_photo_taste.py --force    # заменить в любом случае
#
# Данные: photo_embeddings (отпечатки фото, пишет бот — CLIP здесь не нужен)
# × photo_ratings (LIKE=1, DISLIKE/VICE=0, SKIP — исключён). Качество —
# честное: out-of-fold AUC (5 повторов × 5 фолдов, модель не видит анкету,
# которую угадывает). Новая модель заменяет текущую, только если её AUC не
# хуже записанного в текущем JSON (минус допуск --tolerance). Старый JSON
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
from sklearn.model_selection import StratifiedKFold

CS = [0.03, 0.1, 0.3, 1, 3, 10]
LIKE_SHARE = 0.15      # верхние 15% анкет → зона LIKE
DISLIKE_SHARE = 0.50   # нижние 50% → зона DISLIKE


def load_data(db_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(X размеченных, y, X всех анкет с отпечатком — для порогов зон)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT e.profile_id, e.emb, r.rating FROM photo_embeddings e "
        "LEFT JOIN photo_ratings r ON r.profile_id = e.profile_id "
        "WHERE e.emb IS NOT NULL"
    ).fetchall()
    con.close()
    X_all, X, y = [], [], []
    for _pid, blob, rating in rows:
        v = np.frombuffer(blob, dtype="<f4")
        X_all.append(v)
        if rating in ("LIKE", "DISLIKE", "VICE"):
            X.append(v)
            y.append(1 if rating == "LIKE" else 0)
    return np.array(X), np.array(y, dtype=int), np.array(X_all)


def oof_auc(X: np.ndarray, y: np.ndarray, repeats: int = 5) -> tuple[float, float]:
    aucs = []
    for r in range(repeats):
        p = np.zeros(len(y))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=r).split(X, y):
            m = LogisticRegressionCV(Cs=CS, cv=4, scoring="roc_auc",
                                     class_weight="balanced", max_iter=5000).fit(X[tr], y[tr])
            p[te] = m.predict_proba(X[te])[:, 1]
        aucs.append(roc_auc_score(y, p))
    return float(np.mean(aucs)), float(np.std(aucs))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/database.db")
    ap.add_argument("--model", default="data/ml/taste_model.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()

    X, y, X_all = load_data(Path(args.db))
    if len(y) < 100 or y.sum() < 20:
        print(f"Мало данных: {len(y)} анкет, {y.sum()} ❤️ — нужно ≥100 и ≥20 ❤️")
        return 1
    print(f"Размечено с отпечатком: {len(y)} анкет, ❤️ {y.sum()} ({y.mean() * 100:.1f}%); "
          f"всего отпечатков: {len(X_all)}")

    auc, std = oof_auc(X, y)
    print(f"Новая модель: AUC {auc:.3f} ± {std:.3f} (на анкетах, которых модель не видела)")

    model_path = Path(args.model)
    old = json.loads(model_path.read_text(encoding="utf-8")) if model_path.exists() else {}
    old_auc = float(old.get("oof_auc") or 0.0)
    if old:
        print(f"Текущая модель {old.get('version')}: AUC {old_auc:.3f}, "
              f"обучена на {old.get('trained_on')}")

    C = float(LogisticRegressionCV(Cs=CS, cv=5, scoring="roc_auc", class_weight="balanced",
                                   max_iter=5000).fit(X, y).C_[0])
    final = LogisticRegression(C=C, class_weight="balanced", max_iter=5000).fit(X, y)
    s_all = final.predict_proba(X_all)[:, 1]
    like_thr = float(np.quantile(s_all, 1 - LIKE_SHARE))
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
        "trained_on": {"profiles": int(len(y)), "likes": int(y.sum())},
        "oof_auc": round(auc, 3),
    }
    print(f"Пороги: LIKE ≥ {like_thr:.3f} (верхние {LIKE_SHARE:.0%}), "
          f"DISLIKE ≤ {dislike_thr:.3f} (нижние {DISLIKE_SHARE:.0%}), C={C}")

    if args.dry_run:
        print("--dry-run: модель не заменена")
        return 0
    if old and auc < old_auc - args.tolerance and not args.force:
        print(f"Новая модель хуже текущей ({auc:.3f} < {old_auc:.3f}) — не заменяю "
              "(--force, чтобы заменить)")
        return 2
    if old:
        backup = model_path.with_name(f"taste_model.{old.get('version', 'old')}.json")
        backup.write_text(json.dumps(old), encoding="utf-8")
        print(f"Старая модель сохранена: {backup}")
    tmp = model_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new), encoding="utf-8")
    os.replace(tmp, model_path)
    print(f"Новая модель записана: {model_path} ({version}); бот подхватит её за ~30 с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
