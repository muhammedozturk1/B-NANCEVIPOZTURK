"""Canlı botta AI filtresi: eğitilmiş modeli yükler, her aday sinyale olasılık verir."""
import logging
import os
import joblib
import numpy as np
import pandas as pd
from backend import config

logger = logging.getLogger("ai_model")
_cache = {}  # engine -> (mtime, bundle)


def _path(engine):
    return os.path.join(config.AI_MODEL_DIR, f"{engine}.joblib")


def load(engine: str):
    path = _path(engine)
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    cached = _cache.get(engine)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        bundle = joblib.load(path)
        _cache[engine] = (mtime, bundle)
        logger.info(f"[{engine}] AI modeli yüklendi (onaylı: {bundle.get('approved')}, eşik: {bundle.get('threshold')})")
        return bundle
    except Exception as e:
        logger.error(f"[{engine}] AI modeli yüklenemedi: {e}")
        return None


def engine_ready(engine: str):
    """(hazır_mı, açıklama)"""
    if config.AI_MODE == "off":
        return True, "AI kapalı (sadece kurallar)"
    bundle = load(engine)
    if bundle is None:
        if config.AI_MODE == "required":
            return False, "model yok - önce `python -m backend.ai.train --engine %s` çalıştır" % engine
        return True, "model yok, kurallarla devam (advisory)"
    if not bundle.get("approved"):
        if config.AI_MODE == "required":
            return False, "model backtest'i geçemedi (onaysız)"
        return True, "model onaysız, kurallarla devam (advisory)"
    return True, f"AI aktif (eşik {bundle['threshold']})"


def evaluate(engine: str, vector: dict):
    """(izin_var_mı, olasılık)"""
    if config.AI_MODE == "off":
        return True, None
    bundle = load(engine)
    if bundle is None or not bundle.get("approved"):
        return config.AI_MODE != "required", None
    X = pd.DataFrame([vector])[bundle["features"]]
    if X.isna().any(axis=None) or not np.isfinite(X.values).all():
        return False, None
    prob = float(bundle["model"].predict_proba(X)[0, 1])
    threshold = max(bundle["threshold"], config.AI_MIN_PROBABILITY)
    return prob >= threshold, prob


def setups_for(engine: str) -> list:
    """Onaylı model varsa, modelin doğrulama döneminde kârlı bulduğu kurulumlar;
    yoksa config.ENGINE_SETUPS."""
    if config.AI_MODE != "off":
        bundle = load(engine)
        if bundle and bundle.get("approved") and bundle.get("setups"):
            return list(bundle["setups"])
    return list(config.ENGINE_SETUPS[engine])
