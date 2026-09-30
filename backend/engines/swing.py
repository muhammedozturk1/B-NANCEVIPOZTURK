"""Swing Motoru (B) - SMC + Likidite Bölgeleri + Destek/Direnç + Korku-Açgözlülük ile geniş vadeli işlemler."""
from backend.engines import base_engine

ENGINE_NAME = "swing"


def run(client):
    base_engine.run_cycle(ENGINE_NAME, client)
