"""Dashboard şifresi için hash üretir. Çıktıyı Render'da DASHBOARD_PASSWORD_HASH olarak girin.

    python -m scripts.smc.hash_password
"""
import getpass

from backend.smc.api.auth import hash_password

if __name__ == "__main__":
    pw = getpass.getpass("Dashboard şifresi: ")
    if len(pw) < 12:
        raise SystemExit("Şifre en az 12 karakter olmalı")
    if pw != getpass.getpass("Tekrar: "):
        raise SystemExit("Şifreler eşleşmiyor")
    print(hash_password(pw))
