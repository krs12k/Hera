import os
import secrets
from datetime import timedelta

# Render définit toujours RENDER=true sur ses instances → signal de production.
IS_PRODUCTION = bool(os.environ.get('RENDER'))


def _require(name, value, dev_fallback):
    """En production : la variable est obligatoire (aucune valeur par défaut connue).
    En local : on retombe sur une valeur de dev pour rester pratique."""
    if value:
        return value
    if IS_PRODUCTION:
        raise RuntimeError(
            f"{name} doit être défini comme variable d'environnement en production. "
            f"Configure-le dans Render → Settings → Environment."
        )
    return dev_fallback


class Config:
    # Clé de signature des sessions : jamais de valeur connue en production.
    SECRET_KEY = _require('SECRET_KEY', os.environ.get('SECRET_KEY'),
                          'local-dev-only-not-for-production')

    database_url = os.environ.get('DATABASE_URL') or 'sqlite:///fidelite.db'
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    if database_url.startswith('postgresql://') and 'sslmode' not in database_url:
        database_url += '?sslmode=require'
    SQLALCHEMY_DATABASE_URI = database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}

    # ── Sécurité des cookies de session ──────────────────────────
    SESSION_COOKIE_HTTPONLY = True            # inaccessible au JavaScript (anti-XSS)
    SESSION_COOKIE_SAMESITE = 'Lax'           # limite l'envoi cross-site (anti-CSRF)
    SESSION_COOKIE_SECURE = IS_PRODUCTION     # cookie envoyé uniquement en HTTPS en prod
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)

    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = os.environ.get('MAIL_PORT') != '465'
    MAIL_USE_SSL = os.environ.get('MAIL_PORT') == '465'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')

    STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY')
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
    STRIPE_PRICE = os.environ.get('STRIPE_PRICE')
    STRIPE_PRICE_ANNUAL = os.environ.get('STRIPE_PRICE_ANNUAL')

    # Mot de passe admin : jamais de valeur connue en production.
    ADMIN_PASSWORD = _require('ADMIN_PASSWORD', os.environ.get('ADMIN_PASSWORD'),
                              'hera-admin-local')

    # Secret protégeant l'endpoint cron (récap hebdo). Si absent, l'endpoint
    # est désactivé (403) — aucun envoi automatique tant qu'il n'est pas défini.
    CRON_SECRET = os.environ.get('CRON_SECRET')
