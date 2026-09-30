"""Scalp Motoru (A) - EMA + VWAP + Destek/Direnç ile hızlı, kısa vadeli işlemler."""
from backend.engines import base_engine

ENGINE_NAME = "scalp"


def run(client):
    base_engine.run_cycle(ENGINE_NAME, client)
