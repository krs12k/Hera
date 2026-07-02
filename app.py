import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, make_response
from translations import TRANSLATIONS
from sqlalchemy import func, inspect as sa_inspect
from collections import defaultdict
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_migrate import Migrate, upgrade as db_upgrade, stamp as db_stamp
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from models import db, Restaurant, Client, Visit, PointRule, AdminUser, Report, DiscountCode, SubscriptionPromoCode, EmailCampaign
from config import Config, IS_PRODUCTION
from datetime import datetime, timedelta
from functools import wraps
import qrcode
import io
import base64
import time
import queue
import threading
import secrets
import string
import csv
import stripe
from itsdangerous import URLSafeSerializer, BadData

# ── Suivi des erreurs (Sentry) ──────────────────────────────────
# Actif uniquement si SENTRY_DSN est défini (donc silencieux en local,
# actif en prod une fois la variable configurée dans Render).
# Doit être initialisé AVANT la création de l'app Flask.
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        environment='production' if IS_PRODUCTION else 'development',
        # RGPD : ne jamais transmettre de données personnelles (emails,
        # IP, cookies) à Sentry. On veut la trace technique, pas le client.
        send_default_pii=False,
        # Échantillonnage des traces de performance : 0 = erreurs seulement
        # (suffisant et économe en quota). Passe à 0.1 pour un peu d'APM.
        traces_sample_rate=0.0,
    )

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
# render_as_batch : indispensable pour que les futures migrations de colonnes
# (ALTER) fonctionnent sur SQLite en local, pas seulement sur Postgres en prod.
migrate = Migrate(app, db, render_as_batch=True)
mail = Mail(app)
csrf = CSRFProtect(app)

# Limitation de débit : protège les routes sensibles contre le brute-force.
# Stockage en mémoire (suffisant pour un seul worker gunicorn).
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri='memory://',
)


@app.errorhandler(429)
def ratelimit_handler(e):
    flash('Trop de tentatives. Patiente une minute avant de réessayer.', 'danger')
    return redirect(request.referrer or url_for('home')), 429

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Connecte-toi pour accéder à cette page.'


@app.after_request
def set_security_headers(response):
    """En-têtes de sécurité appliqués à toutes les réponses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    # HSTS uniquement en production (HTTPS), pour ne pas gêner le dev local en HTTP.
    if app.config.get('SESSION_COOKIE_SECURE'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.context_processor
def inject_translations():
    lang = session.get('lang', 'fr')
    return {'t': TRANSLATIONS[lang], 'lang': lang}

@app.route('/set-language/<lang>')
def set_language(lang):
    if lang in ('fr', 'nl'):
        session['lang'] = lang
    return redirect(request.referrer or url_for('home'))

@login_manager.user_loader
def load_user(user_id):
    return Restaurant.query.get(int(user_id))

def init_database():
    """Met la base au niveau du schéma courant via Alembic (Flask-Migrate).

    Trois cas gérés automatiquement, sans intervention manuelle :
      • Base vierge (nouveau déploiement / nouvelle machine) → `upgrade` crée
        tout le schéma à partir des migrations.
      • Base déjà peuplée mais antérieure à Alembic (l'ancien système
        `ALTER TABLE` en prod) → on la « tamponne » (stamp) à la révision de
        base, sans rien recréer, puis les migrations futures s'appliqueront.
      • Base déjà suivie par Alembic → `upgrade` applique les migrations en
        attente (no-op s'il n'y en a pas).
    """
    with app.app_context():
        insp = sa_inspect(db.engine)
        schema_existant = insp.has_table('restaurants')
        deja_suivi = insp.has_table('alembic_version')
        if schema_existant and not deja_suivi:
            # Base pré-Alembic : le schéma correspond déjà aux modèles.
            db_stamp()
        else:
            db_upgrade()


# Sauté pendant les commandes `flask db …` (le dossier migrations/ peut être
# en cours de génération) ; l'app applique les migrations au démarrage normal.
if os.environ.get('SKIP_DB_INIT') != '1':
    init_database()

stripe.api_key = app.config.get('STRIPE_SECRET_KEY')

def hera_email(content_html):
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Inter,Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:32px 16px">
    <tr><td align="center">
      <table width="100%" style="max-width:520px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">
        <!-- Bannière noire -->
        <tr>
          <td style="background:#1A1A2E;padding:24px 32px;text-align:center">
            <span style="font-size:1.6rem;font-weight:700;color:#ffffff;letter-spacing:-1px">hera</span><span style="color:#1BBFB2;font-size:1.8rem;font-weight:700">.</span>
          </td>
        </tr>
        <!-- Contenu -->
        <tr>
          <td style="padding:32px;color:#1a1a2e">
            {content_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#f8f9fa;padding:16px 32px;text-align:center;border-top:1px solid #eee">
            <p style="margin:0;font-size:0.78rem;color:#999">hera. — Programme de fidélité pour restaurateurs belges</p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

# ── File d'attente d'emails ─────────────────────────────────────
# Tous les emails passent par une file en mémoire consommée par UN seul
# worker : envoi en série (fini la rafale d'un thread par email vers Brevo),
# léger throttle entre deux envois, et retries automatiques sur échec
# transitoire. Remplace l'ancien « un thread par email » qui perdait
# définitivement l'email au premier pépin réseau.
#
# Limite connue : file en mémoire → un redéploiement Render perd les emails
# encore en attente. Acceptable au volume actuel ; passer à une file en base
# (table EmailOutbox) le jour où la fiabilité doit être garantie à 100 %.
_BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_email_queue = queue.Queue()


def _brevo_post(payload, tentatives=3):
    """Envoie un payload à Brevo avec retries + backoff exponentiel.
    Ne réessaie pas sur une erreur définitive (4xx hors 429 rate-limit)."""
    api_key = os.environ.get('BREVO_API_KEY') or os.environ.get('MAIL_PASSWORD')
    if not api_key:
        app.logger.warning("Brevo API key manquante — email non envoyé")
        return
    import requests as req
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    for essai in range(1, tentatives + 1):
        try:
            resp = req.post(_BREVO_URL, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            app.logger.info(f"Email envoyé via Brevo : {resp.status_code}")
            return
        except Exception as e:
            statut = getattr(getattr(e, 'response', None), 'status_code', None)
            # 4xx (adresse invalide, payload refusé…) = définitif → inutile de réessayer.
            if statut and 400 <= statut < 500 and statut != 429:
                app.logger.error(f"Email rejeté par Brevo ({statut}) — pas de retry : {e}")
                return
            if essai == tentatives:
                app.logger.error(f"Email abandonné après {tentatives} tentatives : {e}")
                return
            time.sleep(2 ** essai)  # backoff : 2s puis 4s


def _email_worker():
    """Consomme la file d'emails en série, pour toute la vie du process."""
    while True:
        payload = _email_queue.get()
        try:
            _brevo_post(payload)
        except Exception:
            app.logger.exception("Erreur inattendue dans le worker email")
        finally:
            _email_queue.task_done()
            time.sleep(0.35)  # throttle léger vers Brevo entre deux envois


threading.Thread(target=_email_worker, daemon=True, name='email-worker').start()


def enfiler_email(payload):
    """Place un payload Brevo (déjà construit) dans la file d'envoi."""
    _email_queue.put(payload)


def send_email(subject, recipients, body_text, body_html=None):
    """Construit un email simple et le place dans la file d'envoi."""
    api_key = os.environ.get('BREVO_API_KEY') or os.environ.get('MAIL_PASSWORD')
    sender = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')
    if not api_key or not sender:
        app.logger.warning("Brevo API key ou sender manquant — email non envoyé")
        return
    to_list = recipients if isinstance(recipients, list) else [recipients]
    payload = {
        "sender": {"email": sender},
        "to": [{"email": r} for r in to_list],
        "subject": subject,
        "textContent": body_text,
    }
    if body_html:
        payload["htmlContent"] = body_html
    enfiler_email(payload)


def _unsub_serializer():
    return URLSafeSerializer(app.config['SECRET_KEY'], salt='desinscription-client')


def lien_desinscription(client):
    """URL absolue de désinscription pour un client (token signé, sans stockage)."""
    token = _unsub_serializer().dumps(client.id)
    return url_for('desinscription', token=token, _external=True)


def pied_desinscription(client):
    """Bloc « se désinscrire » (HTML + texte) à ajouter aux emails clients."""
    url = lien_desinscription(client)
    html = (f'<p style="margin:18px 0 0;font-size:0.72rem;color:#bbb;text-align:center">'
            f'Vous ne souhaitez plus recevoir ces emails ? '
            f'<a href="{url}" style="color:#999;text-decoration:underline">Se désinscrire</a></p>')
    text = f"\n\n—\nPour ne plus recevoir ces emails : {url}"
    return html, text


def envoyer_email_client(client, resto, subject, body_text, content_html):
    """Envoi d'un email à un client : saute les désinscrits et ajoute
    automatiquement le pied de désinscription (RGPD)."""
    if getattr(client, 'email_opt_out', False) or not client.email:
        return
    foot_html, foot_text = pied_desinscription(client)
    send_email(
        subject=subject,
        recipients=[client.email],
        body_text=body_text + foot_text,
        body_html=hera_email(content_html + foot_html),
    )


def notifier_points(client, resto, points_ajoutes):
    """Envoie au client un email après un ajout de points :
    soit « récompense débloquée » si le seuil vient d'être franchi,
    soit « points gagnés + progression » sinon. Sans effet si le resto
    a désactivé les notifications ou si le client n'a pas d'email."""
    if not getattr(resto, 'notify_clients', True):
        return
    if not client.email or points_ajoutes <= 0:
        return

    total = client.total_points
    seuil = resto.reward_threshold or 0
    avant = total - points_ajoutes
    vient_de_franchir = bool(seuil) and avant < seuil <= total

    logo_html = (
        f'<div style="text-align:center;margin-bottom:20px"><img src="{resto.logo_data}" alt="{resto.name}" style="height:56px;object-fit:contain"></div>'
        if resto.logo_data else
        f'<div style="text-align:center;font-size:1.3rem;font-weight:700;margin-bottom:16px">{resto.name}</div>'
    )

    if vient_de_franchir:
        subject = f'🎁 Ta récompense chez {resto.name} t\'attend !'
        body_text = (
            f"Bonjour {client.first_name},\n\n"
            f"Bravo ! Tu as atteint {seuil} points chez {resto.name}.\n"
            f"Tu peux maintenant profiter de : {resto.reward_description}.\n\n"
            f"Présente ton email à la caisse lors de ta prochaine visite pour en profiter.\n\n"
            f"À bientôt !"
        )
        content_html = (f"""
            {logo_html}
            <h2 style="font-size:1.25rem;margin:0 0 8px;text-align:center">🎁 Récompense débloquée !</h2>
            <p style="color:#555;line-height:1.6;text-align:center">Bravo <strong>{client.first_name}</strong>, tu as atteint <strong>{seuil} points</strong> chez {resto.name}.</p>
            <div style="background:#e8f8f7;border:1px solid #1BBFB2;border-radius:12px;padding:22px;margin:20px 0;text-align:center">
                <div style="font-size:0.85rem;color:#0a8a80;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Ta récompense</div>
                <div style="font-size:1.15rem;font-weight:700;color:#1a1a2e">{resto.reward_description}</div>
            </div>
            <p style="color:#555;line-height:1.6;text-align:center">Présente ton email à la caisse lors de ta prochaine visite pour en profiter.</p>
        """)
    else:
        if seuil and total < seuil:
            restant = seuil - total
            pct = int(min(100, total * 100 / seuil)) if seuil else 0
            progression = f"""
            <div style="background:#eee;border-radius:99px;height:12px;overflow:hidden;margin:6px 0 10px">
                <div style="background:#1BBFB2;height:12px;width:{pct}%"></div>
            </div>
            <div style="font-size:0.95rem;color:#1a1a2e;text-align:center">
                Plus que <strong>{restant} point{'s' if restant > 1 else ''}</strong> avant <strong>{resto.reward_description}</strong> !
            </div>"""
            ligne_text = f"Plus que {restant} point(s) avant {resto.reward_description} !"
        else:
            progression = f"""
            <div style="font-size:0.95rem;color:#0a8a80;text-align:center">
                🎁 Ta récompense <strong>{resto.reward_description}</strong> est disponible !
            </div>"""
            ligne_text = f"Ta récompense {resto.reward_description} est disponible !"

        subject = f'+{points_ajoutes} points chez {resto.name} 🎉'
        body_text = (
            f"Bonjour {client.first_name},\n\n"
            f"Ta visite chez {resto.name} vient d'être validée : +{points_ajoutes} points.\n"
            f"Tu as maintenant {total} points.\n"
            f"{ligne_text}\n\n"
            f"À bientôt !"
        )
        content_html = (f"""
            {logo_html}
            <h2 style="font-size:1.25rem;margin:0 0 8px;text-align:center">+{points_ajoutes} points 🎉</h2>
            <p style="color:#555;line-height:1.6;text-align:center">Ta visite chez <strong>{resto.name}</strong> vient d'être validée.</p>
            <div style="background:#f8f9fa;border-radius:12px;padding:22px;margin:20px 0">
                <div style="text-align:center;font-size:1.6rem;font-weight:700;color:#1a1a2e;margin-bottom:4px">{total} points</div>
                {progression}
            </div>
        """)

    envoyer_email_client(client, resto, subject, body_text, content_html)


def clients_a_relancer(resto_id):
    """Clients déjà venus mais pas revenus depuis plus de 30 jours, ayant un
    email, et qui n'ont pas déjà été relancés dans les 30 derniers jours."""
    now = datetime.utcnow()
    inactif = now - timedelta(days=30)
    agg = db.session.query(
        Visit.client_id.label('cid'),
        func.max(Visit.created_at).label('last_seen'),
    ).filter(Visit.restaurant_id == resto_id).group_by(Visit.client_id).subquery()
    rows = db.session.query(Client, agg.c.last_seen)\
        .join(agg, Client.id == agg.c.cid)\
        .filter(Client.restaurant_id == resto_id, agg.c.last_seen < inactif)\
        .order_by(agg.c.last_seen.asc()).all()
    eligibles = []
    for client, _ in rows:
        if not client.email or client.email_opt_out:
            continue
        if client.last_relance_at and client.last_relance_at > inactif:
            continue
        eligibles.append(client)
    return eligibles


# Limites anti-spam : envois manuels groupés vers les clients (messages + relances).
CAMPAGNE_MAX_JOUR = 1       # campagnes par 24 h
CAMPAGNE_MAX_SEMAINE = 3    # campagnes par 7 jours


def campagnes_recentes(resto_id, jours):
    depuis = datetime.utcnow() - timedelta(days=jours)
    return EmailCampaign.query.filter(
        EmailCampaign.restaurant_id == resto_id,
        EmailCampaign.created_at >= depuis,
    ).count()


def quota_campagne(resto_id):
    """Renvoie (autorisé: bool, message_si_refus: str|None)."""
    if campagnes_recentes(resto_id, 1) >= CAMPAGNE_MAX_JOUR:
        return False, ("Vous avez déjà contacté vos clients aujourd'hui. "
                       "Patientez 24 h pour ne pas les solliciter trop souvent.")
    if campagnes_recentes(resto_id, 7) >= CAMPAGNE_MAX_SEMAINE:
        return False, (f"Limite atteinte : {CAMPAGNE_MAX_SEMAINE} envois groupés maximum "
                       f"par semaine, pour protéger vos clients du spam.")
    return True, None


def envois_restants_semaine(resto_id):
    return max(0, CAMPAGNE_MAX_SEMAINE - campagnes_recentes(resto_id, 7))


def enregistrer_campagne(resto_id, kind, recipients):
    db.session.add(EmailCampaign(restaurant_id=resto_id, kind=kind, recipients=recipients))
    db.session.commit()


def envoyer_relance(client, resto):
    """Email « vous nous manquez » à un client inactif."""
    seuil = resto.reward_threshold or 0
    if seuil and client.total_points >= seuil:
        ligne_text = f"Bonne nouvelle : ta récompense {resto.reward_description} t'attend déjà !"
        encart = f"""
            <div style="background:#e8f8f7;border:1px solid #1BBFB2;border-radius:12px;padding:18px;margin:18px 0;text-align:center">
                <div style="font-size:0.82rem;color:#0a8a80;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Ta récompense t'attend</div>
                <div style="font-size:1.1rem;font-weight:700;color:#1a1a2e">{resto.reward_description}</div>
            </div>"""
    elif seuil:
        restant = seuil - client.total_points
        ligne_text = f"Tu as {client.total_points} points — plus que {restant} avant {resto.reward_description} !"
        encart = f"""
            <div style="background:#f8f9fa;border-radius:12px;padding:18px;margin:18px 0;text-align:center">
                <div style="font-size:1.5rem;font-weight:700;color:#1a1a2e">{client.total_points} points</div>
                <div style="font-size:0.92rem;color:#555;margin-top:4px">Plus que <strong>{restant} point{'s' if restant > 1 else ''}</strong> avant <strong>{resto.reward_description}</strong></div>
            </div>"""
    else:
        ligne_text = f"Tu as {client.total_points} points chez nous."
        encart = ""

    logo_html = (
        f'<div style="text-align:center;margin-bottom:20px"><img src="{resto.logo_data}" alt="{resto.name}" style="height:56px;object-fit:contain"></div>'
        if resto.logo_data else
        f'<div style="text-align:center;font-size:1.3rem;font-weight:700;margin-bottom:16px">{resto.name}</div>'
    )

    envoyer_email_client(
        client, resto,
        subject=f'{resto.name} — vous nous manquez ! 🍽️',
        body_text=(
            f"Bonjour {client.first_name},\n\n"
            f"Ça fait un moment qu'on ne vous a pas vu chez {resto.name} !\n"
            f"{ligne_text}\n\n"
            f"On serait ravis de vous revoir très bientôt.\n\nÀ très vite !"
        ),
        content_html=f"""
            {logo_html}
            <h2 style="font-size:1.25rem;margin:0 0 8px;text-align:center">Vous nous manquez ! 🍽️</h2>
            <p style="color:#555;line-height:1.6;text-align:center">Bonjour <strong>{client.first_name}</strong>, ça fait un moment qu'on ne vous a pas vu chez {resto.name}.</p>
            {encart}
            <p style="color:#555;line-height:1.6;text-align:center">On serait ravis de vous revoir très bientôt. À très vite !</p>
        """,
    )


def envoyer_recap_hebdo(resto):
    """Email récapitulatif des 7 derniers jours au restaurateur.
    Renvoie True si un email a été envoyé."""
    if not getattr(resto, 'weekly_digest', True):
        return False
    if not resto.can_access or not resto.email:
        return False
    total_clients = Client.query.filter_by(restaurant_id=resto.id).count()
    if total_clients == 0:
        return False  # compte vide : rien à raconter

    now = datetime.utcnow()
    semaine = now - timedelta(days=7)
    nouveaux = Client.query.filter_by(restaurant_id=resto.id)\
        .filter(Client.created_at >= semaine).count()
    visites = Visit.query.filter_by(restaurant_id=resto.id)\
        .filter(Visit.created_at >= semaine).count()
    points = db.session.query(func.sum(Visit.points_earned))\
        .filter(Visit.restaurant_id == resto.id, Visit.created_at >= semaine).scalar() or 0
    a_relancer = len(clients_a_relancer(resto.id))

    dashboard_url = url_for('dashboard', _external=True)
    relance_html = ''
    relance_text = ''
    if a_relancer:
        relance_html = (
            f'<div style="background:#fff4f7;border:1px solid #f2c4d4;border-radius:12px;padding:16px;margin:8px 0 4px;text-align:center">'
            f'<strong style="color:#c43c66">{a_relancer} client{"s" if a_relancer > 1 else ""} à relancer</strong>'
            f'<div style="color:#a06; font-size:0.85rem">Pas revenus depuis plus de 30 jours — un email peut les faire revenir.</div></div>'
        )
        relance_text = f"\n⚠️ {a_relancer} client(s) à relancer (pas revenus depuis 30 j+)."

    def stat_html(valeur, libelle):
        return (f'<td style="text-align:center;padding:8px">'
                f'<div style="font-size:1.8rem;font-weight:800;color:#1a1a2e">{valeur}</div>'
                f'<div style="font-size:0.78rem;color:#888">{libelle}</div></td>')

    send_email(
        subject=f'📊 Votre semaine chez {resto.name}',
        recipients=[resto.email],
        body_text=(
            f"Bonjour,\n\nVoici votre récap des 7 derniers jours sur hera. :\n\n"
            f"• {nouveaux} nouveau(x) client(s)\n"
            f"• {visites} visite(s) validée(s)\n"
            f"• {points} points distribués\n"
            f"• {total_clients} clients au total"
            f"{relance_text}\n\n"
            f"Voir mon dashboard : {dashboard_url}\n\n— L'équipe hera."
        ),
        body_html=hera_email(f"""
            <h2 style="font-size:1.2rem;margin:0 0 4px;text-align:center">Votre semaine en bref 📊</h2>
            <p style="color:#888;text-align:center;margin:0 0 18px;font-size:0.9rem">{resto.name} — 7 derniers jours</p>
            <table width="100%" style="background:#f8f9fa;border-radius:12px;margin-bottom:8px"><tr>
                {stat_html(nouveaux, 'Nouveaux clients')}
                {stat_html(visites, 'Visites')}
                {stat_html(points, 'Points')}
            </tr></table>
            {relance_html}
            <div style="text-align:center;margin-top:22px">
                <a href="{dashboard_url}" style="display:inline-block;padding:12px 26px;background:#1BBFB2;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Voir mon dashboard</a>
            </div>
        """)
    )
    return True


def subscription_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.subscription_status == 'blocked':
            flash('Votre compte a été suspendu. Contactez-nous pour régulariser votre situation.', 'danger')
            return redirect(url_for('abonnement'))
        if not current_user.can_access:
            flash('Ton essai gratuit est terminé. Abonne-toi pour continuer.', 'warning')
            return redirect(url_for('abonnement'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        if not session.get('admin_is_super'):
            flash('Accès réservé au super administrateur.', 'danger')
            return redirect(url_for('admin_dashboard'))
        return f(*args, **kwargs)
    return decorated


# ── Pages publiques ─────────────────────────────────────────────
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/comment-ca-marche')
def comment_ca_marche():
    return render_template('comment_ca_marche.html')

@app.route('/pour-les-restaurateurs')
def pour_restaurateurs():
    return render_template('pour_restaurateurs.html')

@app.route('/tarifs')
def tarifs():
    return render_template('tarifs.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')


# ── SEO : robots.txt & sitemap.xml ──────────────────────────────
# Pages publiques indexables (celles derrière login/token sont exclues).
_PAGES_PUBLIQUES = [
    'home', 'comment_ca_marche', 'pour_restaurateurs',
    'tarifs', 'contact', 'mentions_legales', 'confidentialite',
]


@app.route('/robots.txt')
def robots_txt():
    lignes = [
        'User-agent: *',
        'Allow: /$',
        # Espaces privés / à usage unique : pas d'indexation.
        'Disallow: /dashboard',
        'Disallow: /hera-admin',
        'Disallow: /abonnement',
        'Disallow: /rejoindre/',
        'Disallow: /desinscription/',
        'Disallow: /reinitialiser-mdp/',
        '',
        f'Sitemap: {url_for("sitemap_xml", _external=True)}',
    ]
    return app.response_class('\n'.join(lignes), mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    urls = ''.join(
        f'  <url><loc>{url_for(ep, _external=True)}</loc></url>\n'
        for ep in _PAGES_PUBLIQUES
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f'{urls}</urlset>\n')
    return app.response_class(xml, mimetype='application/xml')


# ── Inscription restaurateur ────────────────────────────────────
@app.route('/inscription', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per hour', methods=['POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        address = request.form.get('address', '')
        phone = request.form.get('phone', '')

        if Restaurant.query.filter_by(email=email).first():
            flash('Cet email est déjà utilisé.', 'danger')
            return redirect(url_for('register'))

        resto = Restaurant(name=name, email=email, address=address, phone=phone)
        resto.set_password(password)
        db.session.add(resto)
        db.session.commit()
        login_user(resto)
        send_email(
            subject='Bienvenue sur hera. 🎉',
            recipients=[email],
            body_text=f"Bonjour {name},\n\nVotre compte hera a bien été créé.\nVous pouvez maintenant choisir votre abonnement et commencer à fidéliser vos clients.\n\nÀ bientôt,\nL'équipe hera.",
            body_html=hera_email(f"""
                <h2 style="font-size:1.2rem;margin-bottom:8px">Bienvenue, {name} 👋</h2>
                <p style="color:#555;line-height:1.6">Votre compte a bien été créé. Choisissez votre abonnement pour commencer à fidéliser vos clients dès aujourd'hui.</p>
                <p style="color:#555;line-height:1.6">Votre essai de 14 jours démarre dès que vous renseignez votre carte — aucun prélèvement avant la fin de la période.</p>
                <a href="{url_for('login', _external=True)}" style="display:inline-block;margin-top:16px;padding:12px 24px;background:#1BBFB2;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Accéder à mon compte</a>
            """)
        )
        return redirect(url_for('choisir_plan'))

    return render_template('auth/register.html')


# ── Connexion restaurateur ──────────────────────────────────────
@app.route('/connexion', methods=['GET', 'POST'])
@limiter.limit('10 per minute; 50 per hour', methods=['POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        resto = Restaurant.query.filter_by(email=email).first()

        if resto and resto.check_password(password):
            login_user(resto)
            return redirect(url_for('dashboard'))

        flash('Email ou mot de passe incorrect.', 'danger')

    return render_template('auth/login.html')


# ── Déconnexion ─────────────────────────────────────────────────
@app.route('/deconnexion')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))


# ── Mot de passe oublié ──────────────────────────────────────────
@app.route('/mot-de-passe-oublie', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 15 per hour', methods=['POST'])
def forgot_password():
    if request.method == 'POST':
        try:
            email = request.form['email'].strip()
            resto = Restaurant.query.filter_by(email=email).first()
            if resto:
                token = secrets.token_urlsafe(32)
                resto.reset_token = token
                resto.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
                db.session.commit()
                reset_url = url_for('reset_password', token=token, _external=True)
                send_email(
                    subject='Réinitialisation de votre mot de passe — hera.',
                    recipients=[email],
                    body_text=f"Bonjour,\n\nCliquez sur ce lien pour réinitialiser votre mot de passe (valable 1 heure) :\n{reset_url}\n\nSi vous n'êtes pas à l'origine de cette demande, ignorez cet email.\n\n— L'équipe hera.",
                    body_html=hera_email(f"""
                        <h2 style="font-size:1.1rem;margin-bottom:16px">Réinitialisation de mot de passe</h2>
                        <p style="color:#555;line-height:1.6">Vous avez demandé à réinitialiser votre mot de passe sur hera.</p>
                        <a href="{reset_url}" style="display:inline-block;margin:20px 0;padding:12px 24px;background:#1BBFB2;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Réinitialiser mon mot de passe</a>
                        <p style="color:#999;font-size:0.85rem">Ce lien est valable 1 heure. Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.</p>
                    """)
                )
        except Exception:
            db.session.rollback()
        flash('Si cet email est enregistré, un lien de réinitialisation a été envoyé.', 'info')
        return redirect(url_for('forgot_password'))
    return render_template('auth/forgot.html')


# ── Réinitialisation du mot de passe ────────────────────────────
@app.route('/reinitialiser-mdp/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def reset_password(token):
    resto = Restaurant.query.filter_by(reset_token=token).first()
    if not resto or not resto.reset_token_expires or resto.reset_token_expires < datetime.utcnow():
        flash('Ce lien est invalide ou expiré.', 'danger')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form['password']
        confirm = request.form['confirm']
        if password != confirm:
            flash('Les mots de passe ne correspondent pas.', 'danger')
            return redirect(url_for('reset_password', token=token))
        if len(password) < 6:
            flash('Le mot de passe doit faire au moins 6 caractères.', 'danger')
            return redirect(url_for('reset_password', token=token))
        resto.set_password(password)
        resto.reset_token = None
        resto.reset_token_expires = None
        db.session.commit()
        flash('Mot de passe mis à jour ! Vous pouvez vous connecter.', 'success')
        return redirect(url_for('login'))
    return render_template('auth/reset.html', token=token)


# ── Dashboard restaurateur ──────────────────────────────────────
@app.route('/dashboard')
@login_required
@subscription_required
def dashboard():
    clients = Client.query.filter_by(restaurant_id=current_user.id).order_by(Client.total_points.desc()).all()
    total_visits = Visit.query.filter_by(restaurant_id=current_user.id).count()
    rules = PointRule.query.filter_by(restaurant_id=current_user.id).all()
    return render_template('dashboard/index.html', clients=clients, total_visits=total_visits, rules=rules)


# ── Statistiques ────────────────────────────────────────────────
@app.route('/dashboard/statistiques')
@login_required
@subscription_required
def statistiques():
    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=29)        # pour les 30 colonnes du graphe
    inactif_seuil = now - timedelta(days=30)          # limite "client à risque"

    # Visites + inscriptions par jour sur 30 jours
    all_visits = Visit.query.filter_by(restaurant_id=current_user.id)\
        .filter(Visit.created_at >= thirty_days_ago).all()
    visits_by_day = defaultdict(int)
    for v in all_visits:
        visits_by_day[v.created_at.strftime('%d/%m')] += 1

    new_clients = Client.query.filter_by(restaurant_id=current_user.id)\
        .filter(Client.created_at >= thirty_days_ago).all()
    new_by_day = defaultdict(int)
    for c in new_clients:
        if c.created_at:
            new_by_day[c.created_at.strftime('%d/%m')] += 1

    labels, data, data_inscriptions = [], [], []
    for i in range(30):
        day = (thirty_days_ago + timedelta(days=i)).strftime('%d/%m')
        labels.append(day)
        data.append(visits_by_day.get(day, 0))
        data_inscriptions.append(new_by_day.get(day, 0))

    # Stats globales
    total_visits = Visit.query.filter_by(restaurant_id=current_user.id).count()
    total_clients = Client.query.filter_by(restaurant_id=current_user.id).count()
    total_points = db.session.query(func.sum(Visit.points_earned))\
        .filter(Visit.restaurant_id == current_user.id).scalar() or 0

    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    visits_this_month = Visit.query.filter_by(restaurant_id=current_user.id)\
        .filter(Visit.created_at >= first_of_month).count()
    new_clients_month = Client.query.filter_by(restaurant_id=current_user.id)\
        .filter(Client.created_at >= first_of_month).count()

    clients_rewarded = Client.query.filter(
        Client.restaurant_id == current_user.id,
        Client.total_points >= current_user.reward_threshold
    ).count()

    # Dernier passage + nombre de visites par client (une requête agrégée)
    agg = db.session.query(
        Visit.client_id.label('cid'),
        func.max(Visit.created_at).label('last_seen'),
        func.count(Visit.id).label('nb'),
    ).filter(Visit.restaurant_id == current_user.id)\
     .group_by(Visit.client_id).subquery()

    rows = db.session.query(Client, agg.c.last_seen, agg.c.nb)\
        .outerjoin(agg, Client.id == agg.c.cid)\
        .filter(Client.restaurant_id == current_user.id).all()

    eligibles_set = {c.id for c in clients_a_relancer(current_user.id)}

    clients_actifs = 0          # vus dans les 30 derniers jours
    avec_visite = 0             # ont au moins 1 visite
    recurrents = 0              # ont au moins 2 visites
    a_risque = []               # ont déjà visité mais pas depuis 30 j+
    for client, last_seen, nb in rows:
        nb = nb or 0
        if nb >= 1:
            avec_visite += 1
        if nb >= 2:
            recurrents += 1
        if last_seen and last_seen >= inactif_seuil:
            clients_actifs += 1
        elif last_seen:
            a_risque.append({'client': client, 'last_seen': last_seen,
                             'days': (now - last_seen).days,
                             'eligible': client.id in eligibles_set})

    a_risque.sort(key=lambda x: x['last_seen'])   # les plus anciens d'abord
    clients_a_risque_count = len(a_risque)
    a_risque = a_risque[:50]
    taux_retour = round(recurrents * 100 / avec_visite) if avec_visite else 0

    top_clients = Client.query.filter_by(restaurant_id=current_user.id)\
        .order_by(Client.total_points.desc()).limit(5).all()

    return render_template('dashboard/stats.html',
        labels=labels, data=data, data_inscriptions=data_inscriptions,
        total_visits=total_visits, total_clients=total_clients,
        total_points=total_points, visits_this_month=visits_this_month,
        new_clients_month=new_clients_month, clients_actifs=clients_actifs,
        taux_retour=taux_retour, clients_a_risque=a_risque,
        clients_a_risque_count=clients_a_risque_count,
        clients_rewarded=clients_rewarded, top_clients=top_clients,
        relance_eligibles=len(eligibles_set),
    )


# ── Relancer les clients inactifs ───────────────────────────────
@app.route('/dashboard/relancer-inactifs', methods=['POST'])
@login_required
@subscription_required
@limiter.limit('4 per hour', methods=['POST'])
def relancer_inactifs():
    eligibles = clients_a_relancer(current_user.id)
    if not eligibles:
        flash('Aucun client à relancer pour le moment.', 'info')
        return redirect(url_for('statistiques'))

    # On ne garde que les clients cochés (et toujours éligibles, par sécurité).
    ids = set(request.form.getlist('client_ids'))
    cibles = [c for c in eligibles if str(c.id) in ids]
    if not cibles:
        flash('Sélectionne au moins un client à relancer.', 'warning')
        return redirect(url_for('statistiques'))

    autorise, msg = quota_campagne(current_user.id)
    if not autorise:
        flash(msg, 'warning')
        return redirect(url_for('statistiques'))

    now = datetime.utcnow()
    for client in cibles:
        envoyer_relance(client, current_user)
        client.last_relance_at = now
    db.session.commit()
    enregistrer_campagne(current_user.id, 'relance', len(cibles))

    flash(f'Relance envoyée à {len(cibles)} client(s) inactif(s) ! 📨', 'success')
    return redirect(url_for('statistiques'))


# ── Valider une visite client ───────────────────────────────────
@app.route('/dashboard/valider/<int:client_id>', methods=['POST'])
@login_required
def valider_visite(client_id):
    client = Client.query.filter_by(id=client_id, restaurant_id=current_user.id).first_or_404()

    amount_str = request.form.get('amount', '').strip()
    note = request.form.get('note', '').strip()

    amount = None
    if amount_str:
        try:
            amount = float(amount_str.replace(',', '.'))
        except ValueError:
            flash('Montant invalide.', 'danger')
            return redirect(url_for('dashboard'))

    minimum = current_user.minimum_amount
    if minimum > 0 and (amount is None or amount < minimum):
        flash(
            f'Minimum non atteint ({minimum:.2f} €) — aucun point accordé à {client.first_name}.',
            'warning'
        )
        return redirect(url_for('dashboard'))

    # Calcul des points via les règles cochées, sinon valeur par défaut
    rule_ids = request.form.getlist('rule_ids')
    if rule_ids:
        rules = PointRule.query.filter(
            PointRule.id.in_([int(r) for r in rule_ids]),
            PointRule.restaurant_id == current_user.id
        ).all()
        points = sum(r.points for r in rules)
        note = note or ', '.join(r.label for r in rules)
    else:
        points = current_user.points_per_visit

    client.total_points += points
    visit = Visit(
        client_id=client.id,
        restaurant_id=current_user.id,
        points_earned=points,
        amount_spent=amount,
        note=note or None
    )
    db.session.add(visit)
    db.session.commit()
    notifier_points(client, current_user, points)
    flash(f'+{points} points ajoutés pour {client.first_name} !', 'success')
    return redirect(url_for('dashboard'))


# ── Réinitialiser les points d'un client ───────────────────────
@app.route('/dashboard/reinitialiser/<int:client_id>', methods=['POST'])
@login_required
def reinitialiser_points(client_id):
    client = Client.query.filter_by(id=client_id, restaurant_id=current_user.id).first_or_404()
    client.total_points = 0
    db.session.commit()
    flash(f'Points de {client.first_name} remis à zéro (récompense utilisée).', 'info')
    return redirect(url_for('dashboard'))


# ── Paramètres du restaurant ────────────────────────────────────
@app.route('/dashboard/parametres', methods=['GET', 'POST'])
@login_required
def parametres():
    if request.method == 'POST':
        current_user.name = request.form['name']
        new_email = request.form.get('email', '').strip()
        if new_email and new_email != current_user.email:
            existing = Restaurant.query.filter_by(email=new_email).first()
            if existing:
                flash('Cet email est déjà utilisé par un autre compte.', 'danger')
                return redirect(url_for('parametres'))
            current_user.email = new_email
        current_user.address = request.form.get('address', '')
        current_user.phone = request.form.get('phone', '')
        current_user.point_mode = request.form.get('point_mode', 'simple')
        current_user.notify_clients = request.form.get('notify_clients') == 'on'
        current_user.weekly_digest = request.form.get('weekly_digest') == 'on'
        current_user.points_per_visit = int(request.form['points_per_visit'])
        current_user.reward_threshold = int(request.form['reward_threshold'])
        current_user.reward_description = request.form['reward_description']
        try:
            current_user.minimum_amount = float(request.form['minimum_amount'].replace(',', '.'))
        except ValueError:
            current_user.minimum_amount = 0.0
        current_user.onboarding_configured = True
        db.session.commit()
        flash('Paramètres mis à jour !', 'success')
        return redirect(url_for('parametres'))

    rules = PointRule.query.filter_by(restaurant_id=current_user.id).all()
    codes = DiscountCode.query.filter_by(restaurant_id=current_user.id).all()
    return render_template('dashboard/parametres.html', rules=rules, codes=codes)


@app.route('/dashboard/logo', methods=['POST'])
@login_required
def upload_logo():
    f = request.files.get('logo')
    if not f or f.filename == '':
        flash('Aucun fichier sélectionné.', 'danger')
        return redirect(url_for('parametres'))
    if not f.content_type.startswith('image/'):
        flash('Le fichier doit être une image (JPG, PNG, etc.).', 'danger')
        return redirect(url_for('parametres'))
    data = f.read()
    if len(data) > 1 * 1024 * 1024:
        flash('Le logo ne doit pas dépasser 1 Mo.', 'danger')
        return redirect(url_for('parametres'))
    import base64
    current_user.logo_data = f'data:{f.content_type};base64,{base64.b64encode(data).decode()}'
    db.session.commit()
    flash('Logo mis à jour !', 'success')
    return redirect(url_for('parametres'))


@app.route('/dashboard/logo/supprimer', methods=['POST'])
@login_required
def supprimer_logo():
    current_user.logo_data = None
    db.session.commit()
    flash('Logo supprimé.', 'success')
    return redirect(url_for('parametres'))


# ── Envoyer un message aux clients ─────────────────────────────
@app.route('/dashboard/message', methods=['GET', 'POST'])
@login_required
def envoyer_message():
    clients = Client.query.filter_by(restaurant_id=current_user.id).all()

    abonnes = [c for c in clients if not c.email_opt_out]

    if request.method == 'POST':
        sujet = request.form['sujet']
        contenu = request.form['contenu']

        if not clients:
            flash('Aucun client inscrit pour le moment.', 'warning')
            return redirect(url_for('envoyer_message'))

        autorise, msg = quota_campagne(current_user.id)
        if not autorise:
            flash(msg, 'warning')
            return redirect(url_for('envoyer_message'))

        api_key = os.environ.get('BREVO_API_KEY') or os.environ.get('MAIL_PASSWORD')
        sender = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')
        if not api_key or not sender:
            flash('Email non configuré. Vérifie BREVO_API_KEY sur Render.', 'danger')
            return redirect(url_for('envoyer_message'))

        # On n'écrit jamais aux clients désinscrits (RGPD).
        destinataires = [c.email for c in abonnes]
        if not destinataires:
            flash('Aucun client abonné aux emails pour le moment.', 'warning')
            return redirect(url_for('envoyer_message'))
        resto_email = current_user.email
        resto_name = current_user.name
        resto_logo = current_user.logo_data

        logo_html = f'<img src="{resto_logo}" alt="{resto_name}" style="height:48px;object-fit:contain;margin-bottom:16px">' if resto_logo else f'<strong>{resto_name}</strong>'

        html = f"""<div style="font-family:Inter,sans-serif;max-width:520px;margin:auto;padding:32px 24px;color:#1a1a2e">
            <div style="text-align:center;margin-bottom:24px">{logo_html}</div>
            <p style="line-height:1.7;white-space:pre-line">{contenu}</p>
            <hr style="margin:24px 0;border:none;border-top:1px solid #eee">
            <p style="font-size:0.85rem;color:#999">— {resto_name}</p>
        </div>"""
        # Un seul envoi groupé en BCC, placé dans la file (avec retries).
        enfiler_email({
            "sender": {"email": sender},
            "to": [{"email": resto_email}],
            "bcc": [{"email": e} for e in destinataires],
            "subject": f'[{resto_name}] {sujet}',
            "textContent": f'{contenu}\n\n— {resto_name}',
            "htmlContent": html,
        })
        enregistrer_campagne(current_user.id, 'message', len(destinataires))
        flash(f'Message en cours d\'envoi à {len(destinataires)} client(s) !', 'success')

        return redirect(url_for('envoyer_message'))

    return render_template('dashboard/message.html', clients=clients, abonnes=abonnes,
                           envois_restants=envois_restants_semaine(current_user.id),
                           max_semaine=CAMPAGNE_MAX_SEMAINE)


# ── Règles de points ────────────────────────────────────────────
@app.route('/dashboard/regles/ajouter', methods=['POST'])
@login_required
def ajouter_regle():
    label = request.form.get('label', '').strip()
    points_str = request.form.get('points', '').strip()
    if label and points_str.isdigit() and int(points_str) > 0:
        rule = PointRule(restaurant_id=current_user.id, label=label, points=int(points_str))
        db.session.add(rule)
        db.session.commit()
        flash(f'Règle "{label}" ajoutée.', 'success')
    else:
        flash('Nom et points valides requis.', 'danger')
    return redirect(url_for('parametres'))


@app.route('/dashboard/regles/supprimer/<int:rule_id>', methods=['POST'])
@login_required
def supprimer_regle(rule_id):
    rule = PointRule.query.filter_by(id=rule_id, restaurant_id=current_user.id).first_or_404()
    db.session.delete(rule)
    db.session.commit()
    flash('Règle supprimée.', 'info')
    return redirect(url_for('parametres'))


# ── Politique de confidentialité ────────────────────────────────
@app.route('/confidentialite')
def confidentialite():
    return render_template('confidentialite.html')

@app.route('/mentions-legales')
def mentions_legales():
    return render_template('mentions_legales.html')


# ── Désinscription des emails (RGPD) ────────────────────────────
def _client_depuis_token(token):
    try:
        cid = _unsub_serializer().loads(token)
    except BadData:
        return None
    return Client.query.get(cid)


@app.route('/desinscription/<token>')
def desinscription(token):
    client = _client_depuis_token(token)
    if not client:
        return render_template('desinscription.html', etat='invalide')
    if not client.email_opt_out:
        client.email_opt_out = True
        db.session.commit()
    return render_template('desinscription.html', etat='out', token=token)


@app.route('/reabonnement/<token>')
def reabonnement(token):
    client = _client_depuis_token(token)
    if not client:
        return render_template('desinscription.html', etat='invalide')
    if client.email_opt_out:
        client.email_opt_out = False
        db.session.commit()
    return render_template('desinscription.html', etat='in', token=token)


# ── Codes de réduction ──────────────────────────────────────────
@app.route('/dashboard/codes/ajouter', methods=['POST'])
@login_required
def ajouter_code():
    code = request.form.get('code', '').strip().upper()
    description = request.form.get('description', '').strip()
    min_points_str = request.form.get('min_points', '0').strip()
    if not code or not description:
        flash('Code et description requis.', 'danger')
        return redirect(url_for('parametres'))
    try:
        min_points = int(min_points_str)
    except ValueError:
        min_points = 0
    dc = DiscountCode(restaurant_id=current_user.id, code=code, description=description, min_points=min_points)
    db.session.add(dc)
    db.session.commit()
    flash(f'Code "{code}" ajouté.', 'success')
    return redirect(url_for('parametres'))


@app.route('/dashboard/codes/supprimer/<int:code_id>', methods=['POST'])
@login_required
def supprimer_code(code_id):
    dc = DiscountCode.query.filter_by(id=code_id, restaurant_id=current_user.id).first_or_404()
    db.session.delete(dc)
    db.session.commit()
    flash('Code supprimé.', 'info')
    return redirect(url_for('parametres'))


@app.route('/dashboard/codes/toggle/<int:code_id>', methods=['POST'])
@login_required
def toggle_code(code_id):
    dc = DiscountCode.query.filter_by(id=code_id, restaurant_id=current_user.id).first_or_404()
    dc.is_active = not dc.is_active
    db.session.commit()
    return redirect(url_for('parametres'))


# ── Signaler un problème ─────────────────────────────────────────
@app.route('/signaler', methods=['GET', 'POST'])
def signaler():
    if request.method == 'POST':
        type_ = request.form.get('type', 'bug')
        message = request.form.get('message', '').strip()
        email = request.form.get('email', '').strip() or None
        if not message:
            flash('Le message ne peut pas être vide.', 'danger')
            return redirect(url_for('signaler'))
        report = Report(type=type_, message=message, email=email)
        db.session.add(report)
        db.session.commit()
        admin_email = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')
        if admin_email:
            type_label = {'bug': 'Bug', 'suggestion': 'Suggestion', 'autre': 'Autre'}.get(type_, type_)
            send_email(
                subject=f'[hera.] Nouveau signalement — {type_label}',
                recipients=[admin_email],
                body_text=f"Nouveau signalement reçu.\n\nType : {type_label}\nEmail : {email or 'non renseigné'}\nMessage :\n{message}",
                body_html=hera_email(f"""
                    <h2 style="font-size:1.1rem;margin-bottom:16px">Nouveau signalement — {type_label}</h2>
                    <p style="color:#555"><strong>Email :</strong> {email or 'non renseigné'}</p>
                    <div style="background:#f8f9fa;border-radius:8px;padding:16px;margin-top:12px;white-space:pre-wrap;font-size:0.92rem;color:#333">{message}</div>
                    <a href="https://hera-ximw.onrender.com/hera-admin/dashboard" style="display:inline-block;margin-top:20px;padding:10px 20px;background:#1BBFB2;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Voir dans l'admin</a>
                """)
            )
        flash('Merci, votre signalement a bien été envoyé.', 'success')
        return redirect(url_for('signaler'))
    return render_template('signaler.html')


# ── Page QR code ────────────────────────────────────────────────
@app.route('/dashboard/qrcode')
@login_required
def qrcode_page():
    if not current_user.qr_seen:
        current_user.qr_seen = True
        db.session.commit()
    registration_url = url_for('client_register', token=current_user.qr_token, _external=True)
    img = qrcode.make(registration_url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return render_template('dashboard/qrcode.html', registration_url=registration_url, qr_b64=qr_b64)


# ── Téléchargement PNG ──────────────────────────────────────────
@app.route('/dashboard/qrcode.png')
@login_required
def generate_qr():
    url = url_for('client_register', token=current_user.qr_token, _external=True)
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ── Page client (via QR code) — étape 1 : identification ────────
@app.route('/rejoindre/<token>', methods=['GET', 'POST'])
def client_register(token):
    resto = Restaurant.query.filter_by(qr_token=token).first_or_404()

    # Cookie de mémorisation
    cookie_key = f'hera_client_{token}'
    saved_email = request.cookies.get(cookie_key)
    if saved_email:
        client = Client.query.filter_by(restaurant_id=resto.id, email=saved_email).first()
        if client:
            return redirect(url_for('client_commander', token=token, email=saved_email))

    if request.method == 'POST':
        email = request.form['email'].strip()
        client = Client.query.filter_by(restaurant_id=resto.id, email=email).first()
        if client:
            resp = make_response(redirect(url_for('client_commander', token=token, email=email)))
            resp.set_cookie(cookie_key, email, max_age=30*24*3600)
            return resp
        else:
            return redirect(url_for('client_nouveau', token=token, email=email))

    return render_template('client/register.html', resto=resto)


# ── Inscription nouveau client ───────────────────────────────────
@app.route('/rejoindre/<token>/nouveau', methods=['GET', 'POST'])
def client_nouveau(token):
    resto = Restaurant.query.filter_by(qr_token=token).first_or_404()
    email = request.args.get('email') or request.form.get('email', '')

    # Anti-abus : bloquer si cet appareil a déjà créé un compte ici dans les 6h
    reg_cookie_key = f'hera_reg_{token}'
    reg_ts = request.cookies.get(reg_cookie_key)
    device_blocked = False
    if reg_ts:
        try:
            last_reg = datetime.utcfromtimestamp(float(reg_ts))
            if last_reg > datetime.utcnow() - timedelta(hours=6):
                device_blocked = True
        except Exception:
            pass

    if request.method == 'POST':
        if device_blocked:
            flash('Un compte a déjà été créé depuis cet appareil récemment. Réessaie dans quelques heures.', 'warning')
            return redirect(url_for('client_nouveau', token=token, email=email))

        first_name = request.form['first_name'].strip()
        email = request.form['email'].strip()
        consent = request.form.get('consent') == 'on'

        if not consent:
            flash('Tu dois accepter les conditions pour t\'inscrire.', 'danger')
            return redirect(url_for('client_nouveau', token=token, email=email))

        client = Client.query.filter_by(restaurant_id=resto.id, email=email).first()
        is_new = not client
        if not client:
            client = Client(restaurant_id=resto.id, first_name=first_name, email=email, rgpd_consent=True)
            db.session.add(client)
            db.session.commit()
        if is_new:
            logo_html = f'<img src="{resto.logo_data}" alt="{resto.name}" style="height:56px;object-fit:contain;margin-bottom:16px">' if resto.logo_data else f'<div style="font-size:1.4rem;font-weight:700;margin-bottom:16px">{resto.name}</div>'
            envoyer_email_client(
                client, resto,
                subject=f'Bienvenue chez {resto.name} 🎉',
                body_text=f"Bonjour {first_name},\n\nTu es inscrit(e) au programme de fidélité de {resto.name}.\nGagne {resto.points_per_visit} points à chaque visite et obtiens {resto.reward_description} dès {resto.reward_threshold} points.\n\nÀ bientôt !",
                content_html=f"""
                    {f'<div style="text-align:center;margin-bottom:20px">{logo_html}</div>' if resto.logo_data else ''}
                    <h2 style="font-size:1.2rem;margin-bottom:8px">Bienvenue, {first_name} 👋</h2>
                    <p style="color:#555;line-height:1.6">Tu es inscrit(e) au programme de fidélité de <strong>{resto.name}</strong>.</p>
                    <div style="background:#f8f9fa;border-radius:10px;padding:20px;margin:20px 0">
                        <div style="margin-bottom:8px">🎯 <strong>{resto.points_per_visit} points</strong> à chaque visite validée</div>
                        <div>🎁 <strong>{resto.reward_description}</strong> dès <strong>{resto.reward_threshold} points</strong></div>
                    </div>
                    <p style="color:#555;line-height:1.6">Présente simplement ton email à la caisse pour valider tes visites.</p>
                """,
            )

        resp = make_response(redirect(url_for('client_commander', token=token, email=email)))
        resp.set_cookie(f'hera_client_{token}', email, max_age=30*24*3600)
        resp.set_cookie(reg_cookie_key, str(time.time()), max_age=6*3600)
        return resp

    return render_template('client/nouveau.html', resto=resto, email=email, device_blocked=device_blocked)


# ── Commander et gagner des points ───────────────────────────────
@app.route('/rejoindre/<token>/commander', methods=['GET', 'POST'])
def client_commander(token):
    resto = Restaurant.query.filter_by(qr_token=token).first_or_404()
    email = request.args.get('email') or request.form.get('email', '')
    client = Client.query.filter_by(restaurant_id=resto.id, email=email).first_or_404()
    rules = PointRule.query.filter_by(restaurant_id=resto.id).all()

    six_hours_ago = datetime.utcnow() - timedelta(hours=6)
    last_visit = Visit.query.filter_by(
        client_id=client.id, restaurant_id=resto.id
    ).filter(Visit.created_at > six_hours_ago).order_by(Visit.created_at.desc()).first()

    cooldown = None
    if last_visit:
        next_allowed = last_visit.created_at + timedelta(hours=6)
        remaining = next_allowed - datetime.utcnow()
        h = int(remaining.total_seconds() // 3600)
        m = int((remaining.total_seconds() % 3600) // 60)
        cooldown = f"{h}h {m:02d}min"

    if request.method == 'POST':
        if cooldown:
            flash(f'Tu as déjà validé une visite récemment. Reviens dans {cooldown}.', 'warning')
            return redirect(url_for('client_commander', token=token, email=email))

        points = 0
        note_parts = []
        if rules:
            for rule in rules:
                qty = int(request.form.get(f'qty_{rule.id}', 0) or 0)
                if qty > 0:
                    points += rule.points * qty
                    note_parts.append(f"{qty}x {rule.label}")

        if points == 0:
            points = resto.points_per_visit

        note = ', '.join(note_parts) if note_parts else None
        visit = Visit(client_id=client.id, restaurant_id=resto.id, points_earned=points, note=note)
        client.total_points += points
        db.session.add(visit)
        db.session.commit()
        notifier_points(client, resto, points)

        flash(f'🎉 +{points} points ajoutés ! Tu as maintenant {client.total_points} points.', 'success')
        return redirect(url_for('client_profil', token=token, email=email))

    return render_template('client/commander.html', resto=resto, client=client, rules=rules, email=email, cooldown=cooldown)


# ── Profil client ───────────────────────────────────────────────
@app.route('/rejoindre/<token>/profil')
def client_profil(token):
    resto = Restaurant.query.filter_by(qr_token=token).first_or_404()
    email = request.args.get('email')
    client = Client.query.filter_by(restaurant_id=resto.id, email=email).first_or_404()
    progression = min(int((client.total_points / resto.reward_threshold) * 100), 100)
    codes = DiscountCode.query.filter_by(restaurant_id=resto.id, is_active=True).all()
    return render_template('client/profil.html', client=client, resto=resto, progression=progression, codes=codes)


# ── Choix du plan après inscription ─────────────────────────────
@app.route('/abonnement/choisir-plan')
@login_required
def choisir_plan():
    return render_template('choisir_plan.html')


# ── Portail Client Stripe (gérer / résilier) ────────────────────
@app.route('/abonnement/gerer')
@login_required
def gerer_abonnement():
    if not current_user.stripe_customer_id:
        flash("Aucun abonnement Stripe trouvé. Contactez-nous si besoin.", 'warning')
        return redirect(url_for('parametres'))
    try:
        portal = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=url_for('parametres', _external=True),
        )
        return redirect(portal.url)
    except Exception as e:
        flash(f"Impossible d'ouvrir le portail Stripe : {str(e)}", 'danger')
        return redirect(url_for('parametres'))


# ── Page abonnement ─────────────────────────────────────────────
@app.route('/abonnement')
@login_required
def abonnement():
    return render_template('abonnement.html')


# ── Créer session Stripe Checkout ───────────────────────────────
@app.route('/abonnement/checkout', methods=['POST'])
@login_required
def checkout():
    try:
        plan = request.form.get('plan', 'monthly')
        if plan == 'annual':
            price_id = app.config.get('STRIPE_PRICE_ANNUAL') or app.config.get('STRIPE_PRICE')
        else:
            price_id = app.config.get('STRIPE_PRICE')

        if not price_id:
            flash('Tarif Stripe non configuré. Contactez l\'administrateur.', 'danger')
            return redirect(url_for('choisir_plan'))

        customer = stripe.Customer.create(
            email=current_user.email,
            name=current_user.name,
        )
        current_user.stripe_customer_id = customer.id
        db.session.commit()

        # Essai 14 jours pour les nouveaux abonnés
        subscription_data = {}
        if current_user.subscription_status in ('trial', None) and not current_user.stripe_subscription_id:
            subscription_data = {'trial_period_days': 14}

        checkout_session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            subscription_data=subscription_data,
            allow_promotion_codes=True,
            success_url=url_for('abonnement_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('choisir_plan', _external=True),
        )
        return redirect(checkout_session.url)
    except Exception as e:
        flash(f'Erreur Stripe : {str(e)}', 'danger')
        return redirect(url_for('choisir_plan'))


# ── Succès paiement ──────────────────────────────────────────────
@app.route('/abonnement/success')
@login_required
def abonnement_success():
    session_id = request.args.get('session_id')
    if session_id and current_user.stripe_customer_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            # La session doit appartenir à ce compte et être aboutie,
            # sinon on n'active rien (anti-falsification du session_id).
            if (checkout_session.customer == current_user.stripe_customer_id
                    and checkout_session.status == 'complete'):
                current_user.subscription_status = 'active'
                current_user.stripe_subscription_id = checkout_session.subscription
                db.session.commit()
        except Exception:
            pass
    flash('Essai démarré ! Votre carte ne sera débitée que dans 14 jours. Bienvenue sur hera. 🎉', 'success')
    return redirect(url_for('dashboard'))


# ── Webhook Stripe ───────────────────────────────────────────────
@app.route('/stripe/webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature')
    webhook_secret = app.config.get('STRIPE_WEBHOOK_SECRET')

    if not webhook_secret:
        # En production : jamais d'événement non vérifié (sinon abonnement forgeable).
        if IS_PRODUCTION:
            app.logger.error("STRIPE_WEBHOOK_SECRET manquant — webhook Stripe rejeté.")
            return '', 400
        # Dev local uniquement : on accepte sans vérifier la signature.
        try:
            event = stripe.Event.construct_from(request.get_json(), stripe.api_key)
        except Exception:
            return '', 400
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        except Exception:
            return '', 400

    if event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        resto = Restaurant.query.filter_by(stripe_subscription_id=sub['id']).first()
        if resto:
            resto.subscription_status = 'inactive'
            db.session.commit()

    elif event['type'] in ('invoice.payment_succeeded',):
        invoice = event['data']['object']
        resto = Restaurant.query.filter_by(stripe_customer_id=invoice['customer']).first()
        if resto:
            resto.subscription_status = 'active'
            db.session.commit()

    elif event['type'] == 'invoice.payment_failed':
        invoice = event['data']['object']
        resto = Restaurant.query.filter_by(stripe_customer_id=invoice['customer']).first()
        if resto:
            resto.subscription_status = 'inactive'
            db.session.commit()

    return '', 200


# ── Cron : récap hebdomadaire aux restaurateurs ─────────────────
@app.route('/cron/recap-hebdo', methods=['GET', 'POST'])
@csrf.exempt
def cron_recap_hebdo():
    secret = app.config.get('CRON_SECRET')
    fourni = request.args.get('secret') or request.headers.get('X-Cron-Secret')
    if not secret or fourni != secret:
        return '', 403

    envoyes = 0
    for resto in Restaurant.query.all():
        try:
            if envoyer_recap_hebdo(resto):
                envoyes += 1
        except Exception:
            app.logger.exception(f"Recap hebdo échoué pour resto {resto.id}")
    app.logger.info(f"Recap hebdo : {envoyes} email(s) envoyé(s).")
    return {'envoyes': envoyes}, 200


# ── Admin — Login ────────────────────────────────────────────────
@app.route('/hera-admin', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per hour', methods=['POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Super admin via variables d'environnement
        if username == 'admin' and secrets.compare_digest(password, app.config.get('ADMIN_PASSWORD') or ''):
            session['admin_logged_in'] = True
            session['admin_is_super'] = True
            session['admin_username'] = 'admin'
            return redirect(url_for('admin_dashboard'))

        # Collaborateur en base
        user = AdminUser.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            session['admin_logged_in'] = True
            session['admin_is_super'] = False
            session['admin_username'] = user.username
            return redirect(url_for('admin_dashboard'))

        flash('Identifiants incorrects.', 'danger')
    return render_template('admin/login.html')


@app.route('/hera-admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_is_super', None)
    session.pop('admin_username', None)
    return redirect(url_for('admin_login'))


@app.route('/hera-admin/changer-mot-de-passe', methods=['POST'])
def admin_change_password():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    current = request.form.get('current_password', '')
    new_pw = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if not secrets.compare_digest(current, app.config.get('ADMIN_PASSWORD') or ''):
        flash('Mot de passe actuel incorrect.', 'danger')
    elif new_pw != confirm:
        flash('Les nouveaux mots de passe ne correspondent pas.', 'danger')
    elif len(new_pw) < 6:
        flash('Le nouveau mot de passe doit faire au moins 6 caractères.', 'danger')
    else:
        app.config['ADMIN_PASSWORD'] = new_pw
        flash('Mot de passe changé pour cette session. Pensez à mettre à jour la variable ADMIN_PASSWORD sur Render.', 'warning')
    return redirect(url_for('admin_dashboard'))


# ── Admin — Dashboard ────────────────────────────────────────────
@app.route('/hera-admin/dashboard')
@admin_required
def admin_dashboard():
    restaurants = Restaurant.query.order_by(Restaurant.created_at.desc()).all()
    reports = Report.query.order_by(Report.created_at.desc()).all()
    unread_count = Report.query.filter_by(is_read=False).count()
    now = datetime.utcnow()
    stats = {
        'total': len(restaurants),
        'actifs': sum(1 for r in restaurants if r.subscription_status == 'active'),
        'essai': sum(1 for r in restaurants if r.subscription_status == 'trial' and r.trial_ends_at and r.trial_ends_at > now),
        'gratuits': sum(1 for r in restaurants if r.is_free),
        'expires': sum(1 for r in restaurants if not r.is_free and r.subscription_status != 'active' and not (r.subscription_status == 'trial' and r.trial_ends_at and r.trial_ends_at > now)),
    }
    for r in restaurants:
        r.nb_clients = Client.query.filter_by(restaurant_id=r.id).count()
        r.nb_visits = Visit.query.filter_by(restaurant_id=r.id).count()
    collaborateurs = AdminUser.query.order_by(AdminUser.created_at.desc()).all() if session.get('admin_is_super') else []
    return render_template('admin/dashboard.html', restaurants=restaurants, now=now, stats=stats,
                           collaborateurs=collaborateurs,
                           reports=reports, unread_count=unread_count,
                           is_super=session.get('admin_is_super', False),
                           admin_username=session.get('admin_username', 'admin'))


# ── Admin — Marquer signalement comme lu ───────────────────────
@app.route('/hera-admin/signalement/<int:report_id>/lire', methods=['POST'])
@admin_required
def admin_lire_signalement(report_id):
    report = Report.query.get_or_404(report_id)
    report.is_read = True
    db.session.commit()
    return redirect(url_for('admin_dashboard'))


@app.route('/hera-admin/signalement/<int:report_id>/supprimer', methods=['POST'])
@admin_required
def admin_supprimer_signalement(report_id):
    report = Report.query.get_or_404(report_id)
    db.session.delete(report)
    db.session.commit()
    return redirect(url_for('admin_dashboard'))


# ── Admin — Toggle gratuit ───────────────────────────────────────
@app.route('/hera-admin/toggle-free/<int:resto_id>', methods=['POST'])
@admin_required
def admin_toggle_free(resto_id):
    resto = Restaurant.query.get_or_404(resto_id)
    resto.is_free = not resto.is_free
    db.session.commit()
    flash(f'{"Gratuit activé" if resto.is_free else "Gratuit désactivé"} pour {resto.name}.', 'success')
    return redirect(url_for('admin_dashboard'))


# ── Admin — Bloquer / débloquer un restaurant ───────────────────
@app.route('/hera-admin/toggle-block/<int:resto_id>', methods=['POST'])
@admin_required
def admin_toggle_block(resto_id):
    resto = Restaurant.query.get_or_404(resto_id)
    if resto.subscription_status == 'blocked':
        resto.subscription_status = 'inactive'
        flash(f'Compte de {resto.name} débloqué.', 'success')
    else:
        resto.subscription_status = 'blocked'
        flash(f'Compte de {resto.name} suspendu.', 'warning')
    db.session.commit()
    return redirect(url_for('admin_dashboard'))


# ── Admin — Prolonger l'essai ────────────────────────────────────
@app.route('/hera-admin/prolonger/<int:resto_id>', methods=['POST'])
@admin_required
def admin_prolonger(resto_id):
    resto = Restaurant.query.get_or_404(resto_id)
    jours = int(request.form.get('jours', 14))
    if resto.trial_ends_at and resto.trial_ends_at > datetime.utcnow():
        resto.trial_ends_at = resto.trial_ends_at + timedelta(days=jours)
    else:
        resto.trial_ends_at = datetime.utcnow() + timedelta(days=jours)
    resto.subscription_status = 'trial'
    db.session.commit()
    flash(f'Essai prolongé de {jours} jours pour {resto.name}.', 'success')
    return redirect(url_for('admin_dashboard'))


# ── Admin — Ajouter un restaurant ───────────────────────────────
@app.route('/hera-admin/ajouter', methods=['POST'])
@admin_required
def admin_ajouter():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    acces = request.form.get('acces', 'trial')

    if not name or not email or not password:
        flash('Tous les champs sont obligatoires.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if Restaurant.query.filter_by(email=email).first():
        flash('Cet email est déjà utilisé.', 'danger')
        return redirect(url_for('admin_dashboard'))

    resto = Restaurant(name=name, email=email)
    resto.set_password(password)

    if acces == 'gratuit':
        resto.is_free = True
        resto.subscription_status = 'trial'
        resto.trial_ends_at = datetime.utcnow() + timedelta(days=14)
    elif acces == 'actif':
        resto.subscription_status = 'active'
        resto.trial_ends_at = datetime.utcnow() + timedelta(days=14)
    else:
        resto.subscription_status = 'trial'
        resto.trial_ends_at = datetime.utcnow() + timedelta(days=14)

    db.session.add(resto)
    db.session.commit()
    flash(f'Restaurant "{name}" créé avec succès.', 'success')
    return redirect(url_for('admin_dashboard'))


# ── Admin — Collaborateurs ───────────────────────────────────────
@app.route('/hera-admin/collaborateurs/creer', methods=['POST'])
@super_admin_required
def admin_creer_collaborateur():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username or not password:
        flash('Nom d\'utilisateur et mot de passe requis.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if len(password) < 6:
        flash('Le mot de passe doit faire au moins 6 caractères.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if username == 'admin':
        flash('Le nom "admin" est réservé au super administrateur.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if AdminUser.query.filter_by(username=username).first():
        flash(f'Le nom d\'utilisateur "{username}" est déjà pris.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user = AdminUser(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f'Collaborateur "{username}" créé avec succès.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/hera-admin/collaborateurs/toggle/<int:user_id>', methods=['POST'])
@super_admin_required
def admin_toggle_collaborateur(user_id):
    user = AdminUser.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    état = 'activé' if user.is_active else 'désactivé'
    flash(f'Compte de {user.username} {état}.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/hera-admin/collaborateurs/supprimer/<int:user_id>', methods=['POST'])
@super_admin_required
def admin_supprimer_collaborateur(user_id):
    user = AdminUser.query.get_or_404(user_id)
    nom = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'Collaborateur "{nom}" supprimé.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/hera-admin/collaborateurs/reset-mdp/<int:user_id>', methods=['POST'])
@super_admin_required
def admin_reset_mdp_collaborateur(user_id):
    user = AdminUser.query.get_or_404(user_id)
    new_pw = request.form.get('new_password', '').strip()
    if len(new_pw) < 6:
        flash('Le mot de passe doit faire au moins 6 caractères.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user.set_password(new_pw)
    db.session.commit()
    flash(f'Mot de passe de {user.username} mis à jour.', 'success')
    return redirect(url_for('admin_dashboard'))


# ── Admin — Codes promo abonnement ───────────────────────────────
def _generer_code_unique():
    # Alphabet sans caractères ambigus (0/O, 1/I) pour faciliter la dictée.
    alphabet = ''.join(c for c in (string.ascii_uppercase + string.digits)
                       if c not in 'O0I1')
    while True:
        code = 'HERA-' + ''.join(secrets.choice(alphabet) for _ in range(8))
        if not SubscriptionPromoCode.query.filter_by(code=code).first():
            return code


@app.route('/hera-admin/codes-promo')
@admin_required
def admin_codes_promo():
    codes = SubscriptionPromoCode.query.order_by(SubscriptionPromoCode.created_at.desc()).all()
    # Synchronise le statut « utilisé » depuis Stripe pour les codes encore disponibles.
    if stripe.api_key:
        modifie = False
        for c in codes:
            if not c.redeemed and c.stripe_promotion_code_id:
                try:
                    pc = stripe.PromotionCode.retrieve(c.stripe_promotion_code_id)
                    if pc.get('times_redeemed', 0) >= 1 or not pc.get('active', True):
                        c.redeemed = True
                        c.redeemed_at = datetime.utcnow()
                        modifie = True
                except Exception:
                    pass
        if modifie:
            db.session.commit()
    return render_template('admin/codes_promo.html',
                           codes=codes,
                           is_super=session.get('admin_is_super', False),
                           admin_username=session.get('admin_username', 'admin'))


@app.route('/hera-admin/codes-promo/generer', methods=['POST'])
@admin_required
def admin_generer_code():
    try:
        percent = int(request.form.get('percent', 0))
    except (ValueError, TypeError):
        percent = 0
    if percent < 1 or percent > 100:
        flash('Le pourcentage doit être compris entre 1 et 100.', 'danger')
        return redirect(url_for('admin_codes_promo'))

    duration = request.form.get('duration', 'once')
    if duration not in ('once', 'repeating', 'forever'):
        duration = 'once'

    months = None
    if duration == 'repeating':
        try:
            months = int(request.form.get('duration_in_months', 0))
        except (ValueError, TypeError):
            months = 0
        if months < 1:
            flash('Indique un nombre de mois valide pour une réduction récurrente.', 'danger')
            return redirect(url_for('admin_codes_promo'))

    note = (request.form.get('note') or '').strip()[:200]

    try:
        quantite = int(request.form.get('quantite', 1))
    except (ValueError, TypeError):
        quantite = 1
    quantite = max(1, min(quantite, 50))

    if not stripe.api_key:
        flash('Stripe non configuré (STRIPE_SECRET_KEY manquant). Impossible de générer un code.', 'danger')
        return redirect(url_for('admin_codes_promo'))

    crees = []
    erreur = None
    for _ in range(quantite):
        code = _generer_code_unique()
        try:
            coupon_params = {
                'percent_off': percent,
                'duration': duration,
                'name': f'hera -{percent}%',
            }
            if duration == 'repeating':
                coupon_params['duration_in_months'] = months
            coupon = stripe.Coupon.create(**coupon_params)
            promo = stripe.PromotionCode.create(
                promotion={'type': 'coupon', 'coupon': coupon.id},
                code=code,
                max_redemptions=1,
            )
        except Exception as e:
            erreur = str(e)
            break
        db.session.add(SubscriptionPromoCode(
            code=code,
            percent_off=percent,
            duration=duration,
            duration_in_months=months,
            stripe_coupon_id=coupon.id,
            stripe_promotion_code_id=promo.id,
            note=note or None,
        ))
        crees.append(code)

    db.session.commit()

    if crees and erreur:
        flash(f'{len(crees)} code(s) généré(s), puis arrêt sur erreur Stripe : {erreur}', 'warning')
    elif crees:
        flash(f'{len(crees)} code(s) généré(s) à -{percent}%. Tu peux les copier ou les exporter.', 'success')
    else:
        flash(f'Erreur Stripe : {erreur}', 'danger')
    return redirect(url_for('admin_codes_promo'))


@app.route('/hera-admin/codes-promo/<int:code_id>/supprimer', methods=['POST'])
@admin_required
def admin_supprimer_code(code_id):
    entry = SubscriptionPromoCode.query.get_or_404(code_id)
    # Désactive le code côté Stripe pour qu'il ne soit plus utilisable.
    if stripe.api_key and entry.stripe_promotion_code_id:
        try:
            stripe.PromotionCode.modify(entry.stripe_promotion_code_id, active=False)
        except Exception:
            pass
    if stripe.api_key and entry.stripe_coupon_id:
        try:
            stripe.Coupon.delete(entry.stripe_coupon_id)
        except Exception:
            pass
    db.session.delete(entry)
    db.session.commit()
    flash('Code supprimé.', 'success')
    return redirect(url_for('admin_codes_promo'))


@app.route('/hera-admin/codes-promo/export')
@admin_required
def admin_export_codes():
    dispo = (SubscriptionPromoCode.query
             .filter_by(redeemed=False)
             .order_by(SubscriptionPromoCode.created_at.desc())
             .all())
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['code', 'reduction_percent', 'duree', 'note'])
    for c in dispo:
        writer.writerow([c.code, c.percent_off, c.duree_label, c.note or ''])
    resp = make_response(output.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename=codes_promo_disponibles.csv'
    return resp


# ── Admin — Fiche restaurant ─────────────────────────────────────
@app.route('/hera-admin/restaurant/<int:resto_id>')
@admin_required
def admin_restaurant_detail(resto_id):
    resto = Restaurant.query.get_or_404(resto_id)
    clients = Client.query.filter_by(restaurant_id=resto_id).order_by(Client.total_points.desc()).all()
    visits = Visit.query.filter_by(restaurant_id=resto_id).order_by(Visit.created_at.desc()).limit(20).all()
    now = datetime.utcnow()
    total_points = sum(c.total_points for c in clients)
    return render_template('admin/restaurant_detail.html',
                           resto=resto, clients=clients, visits=visits, now=now,
                           total_points=total_points,
                           is_super=session.get('admin_is_super', False),
                           admin_username=session.get('admin_username', 'admin'))


# ── Admin — Supprimer un restaurant ─────────────────────────────
@app.route('/hera-admin/supprimer/<int:resto_id>', methods=['POST'])
@admin_required
def admin_supprimer(resto_id):
    resto = Restaurant.query.get_or_404(resto_id)
    nom = resto.name
    Client.query.filter_by(restaurant_id=resto_id).delete()
    Visit.query.filter_by(restaurant_id=resto_id).delete()
    PointRule.query.filter_by(restaurant_id=resto_id).delete()
    db.session.delete(resto)
    db.session.commit()
    flash(f'Restaurant "{nom}" supprimé définitivement.', 'success')
    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    # debug activé uniquement si FLASK_DEBUG=1 ; jamais en production.
    debug = os.environ.get('FLASK_DEBUG') == '1'
    app.run(debug=debug)
