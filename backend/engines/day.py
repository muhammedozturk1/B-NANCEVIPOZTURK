"""Day Motoru (C) - EMA + VWAP + SMC + Destek/Direnç ile gün içi trend takibi."""
from backend.engines import base_engine

ENGINE_NAME = "day"


def run(client):
    base_engine.run_cycle(ENGINE_NAME, client)
