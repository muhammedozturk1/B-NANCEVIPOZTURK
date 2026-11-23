"""Önceden belirlenmiş varyantlar (veriye bakmadan yazıldı; ayar taraması yok)."""
BASE = dict(fng_filter=False)  # F&G geçmişi bu ortamdan indirilemedi: iki sistem de F&G'siz karşılaştırılır
USER_LOCK = "1.0:0, 1.5:0.5, 2.0:1.0"   # kullanıcının şartnamesindeki örnek

V = {
    "orig": ({}, {}),
    # eski kurallar, yeni motor: 4s mumlar, funding yok (motorun eskiyle aynı sonucu verdiğini doğrulamak için)
    "legacy_4h_nofund": ({}, {"legacy": True, "c1": False, "funding": False}),
    # eski kurallar + doğru muhasebe (1s yol, gap açılışı, funding)
    "legacy": ({}, {"legacy": True}),
    # A: sadece kademeli kâr kilidi (şartnamedeki örnek), swing trailing eskisi gibi
    "A_lock": (dict(lock_tiers=USER_LOCK, trail_mode="swing", trail_activate_r=0.0,
                    exit_trigger_source="mark_price", manage_tf="4h"), {}),
    # B: kilit + ATR trailing +1.5R'den sonra (3×ATR = başlangıç stop mesafesi), saatlik güncelleme
    "B_lock_atr": (dict(lock_tiers=USER_LOCK, trail_mode="atr", trail_activate_r=1.5, trail_atr_mult=3.0,
                        exit_trigger_source="mark_price", manage_tf="1h"), {}),
    # C: B + kısmi TP (+2R'de %33)
    "C_lock_atr_tp": (dict(lock_tiers=USER_LOCK, trail_mode="atr", trail_activate_r=1.5, trail_atr_mult=3.0,
                           partial_tp_tiers="2.0:0.33", exit_trigger_source="mark_price", manage_tf="1h"), {}),
    # D: kilit + swing ve ATR birlikte (en koruyucu), 4s kapanış tetikli (canlıda en az emir)
    "D_lock_both_close": (dict(lock_tiers=USER_LOCK, trail_mode="both", trail_activate_r=1.0, trail_atr_mult=3.0,
                               exit_trigger_source="candle_close", manage_tf="4h"), {}),
}


def get(name, defaults):
    st, kw = V[name]
    return dict(defaults, **BASE, **st), kw

# ---- İkinci aşama: küçük, önceden yazılmış sağlamlık seti (seçim yalnız IS 2020-2023 ile; OOS sadece doğrulama)
_LOCKS = {"L2_0": "2.0:0", "L3_1": "3.0:1.0", "Lstep": "2.0:0, 3.0:1.0, 4.0:2.0", "Luser": USER_LOCK}
for _ln, _lt in _LOCKS.items():
    for _src, _tag in (("mark_price", "mark"), ("candle_close", "close")):
        V[f"G_{_ln}_{_tag}"] = (dict(lock_tiers=_lt, trail_mode="swing", trail_activate_r=-1.0,
                                     exit_trigger_source=_src, manage_tf="4h"), {})
V["G_tp3_25"] = (dict(partial_tp_tiers="3.0:0.25", trail_mode="swing", trail_activate_r=-1.0,
                      exit_trigger_source="mark_price", manage_tf="4h"), {})
V["G_both_atr3"] = (dict(trail_mode="both", trail_activate_r=-1.0, trail_atr_mult=3.0,
                         exit_trigger_source="candle_close", manage_tf="4h"), {})
V["G_both_atr3_1h"] = (dict(trail_mode="both", trail_activate_r=-1.0, trail_atr_mult=3.0,
                            exit_trigger_source="candle_close", manage_tf="1h"), {})

# ---- Üçüncü aşama: gönderilen VARSAYILAN ayarlar (config.py) ve tek tek açılan filtreler
V["NEW_default"] = ({}, {"use_defaults": True})
V["F_volume"] = (dict(filter_volume=True), {"use_defaults": True})
V["F_body"] = (dict(filter_body=True), {"use_defaults": True})
V["F_close"] = (dict(filter_close_strength=True), {"use_defaults": True})
V["F_corr"] = (dict(filter_correlation=True), {"use_defaults": True})
V["F_all3"] = (dict(filter_volume=True, filter_body=True, filter_close_strength=True), {"use_defaults": True})
