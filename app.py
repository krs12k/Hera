import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, make_response
from translations import TRANSLATIONS
from sqlalchemy import text, func
from collections import defaultdict
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from models import db, Restaurant, Client, Visit, PointRule, AdminUser, Report, DiscountCode, SubscriptionPromoCode
from config import Config
from datetime import datetime, timedelta
from functools import wraps
import qrcode
import io
import base64
import time
import secrets
import string
import csv
import stripe

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
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

with app.app_context():
    db.create_all()
    for sql in [
        "ALTER TABLE restaurants ADD COLUMN point_mode VARCHAR(20) DEFAULT 'simple'",
        "ALTER TABLE restaurants ADD COLUMN reset_token VARCHAR(100)",
        "ALTER TABLE restaurants ADD COLUMN reset_token_expires TIMESTAMP",
        "ALTER TABLE restaurants ADD COLUMN logo_data TEXT",
        "ALTER TABLE restaurants ADD COLUMN notify_clients BOOLEAN DEFAULT TRUE",
    ]:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
        except Exception:
            pass

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

def send_email(subject, recipients, body_text, body_html=None):
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

    def _send():
        try:
            import requests as req
            resp = req.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=15
            )
            resp.raise_for_status()
            app.logger.info(f"Email envoyé via Brevo API : {resp.status_code}")
        except Exception as e:
            app.logger.error(f"Erreur Brevo API : {e}")

    import threading
    threading.Thread(target=_send, daemon=True).start()


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
        body_html = hera_email(f"""
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
        body_html = hera_email(f"""
            {logo_html}
            <h2 style="font-size:1.25rem;margin:0 0 8px;text-align:center">+{points_ajoutes} points 🎉</h2>
            <p style="color:#555;line-height:1.6;text-align:center">Ta visite chez <strong>{resto.name}</strong> vient d'être validée.</p>
            <div style="background:#f8f9fa;border-radius:12px;padding:22px;margin:20px 0">
                <div style="text-align:center;font-size:1.6rem;font-weight:700;color:#1a1a2e;margin-bottom:4px">{total} points</div>
                {progression}
            </div>
        """)

    send_email(subject=subject, recipients=[client.email], body_text=body_text, body_html=body_html)


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
    # Visites par jour sur 30 jours
    thirty_days_ago = datetime.utcnow() - timedelta(days=29)
    all_visits = Visit.query.filter_by(restaurant_id=current_user.id)\
        .filter(Visit.created_at >= thirty_days_ago).all()

    visits_by_day = defaultdict(int)
    for v in all_visits:
        visits_by_day[v.created_at.strftime('%d/%m')] += 1

    labels, data = [], []
    for i in range(30):
        day = (thirty_days_ago + timedelta(days=i)).strftime('%d/%m')
        labels.append(day)
        data.append(visits_by_day.get(day, 0))

    # Stats globales
    total_visits = Visit.query.filter_by(restaurant_id=current_user.id).count()
    total_clients = Client.query.filter_by(restaurant_id=current_user.id).count()
    total_points = db.session.query(func.sum(Visit.points_earned))\
        .filter(Visit.restaurant_id == current_user.id).scalar() or 0

    first_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)
    visits_this_month = Visit.query.filter_by(restaurant_id=current_user.id)\
        .filter(Visit.created_at >= first_of_month).count()

    clients_rewarded = Client.query.filter(
        Client.restaurant_id == current_user.id,
        Client.total_points >= current_user.reward_threshold
    ).count()

    top_clients = Client.query.filter_by(restaurant_id=current_user.id)\
        .order_by(Client.total_points.desc()).limit(5).all()

    return render_template('dashboard/stats.html',
        labels=labels, data=data,
        total_visits=total_visits, total_clients=total_clients,
        total_points=total_points, visits_this_month=visits_this_month,
        clients_rewarded=clients_rewarded, top_clients=top_clients
    )


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
        current_user.points_per_visit = int(request.form['points_per_visit'])
        current_user.reward_threshold = int(request.form['reward_threshold'])
        current_user.reward_description = request.form['reward_description']
        try:
            current_user.minimum_amount = float(request.form['minimum_amount'].replace(',', '.'))
        except ValueError:
            current_user.minimum_amount = 0.0
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

    if request.method == 'POST':
        sujet = request.form['sujet']
        contenu = request.form['contenu']

        if not clients:
            flash('Aucun client inscrit pour le moment.', 'warning')
            return redirect(url_for('envoyer_message'))

        api_key = os.environ.get('BREVO_API_KEY') or os.environ.get('MAIL_PASSWORD')
        sender = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('MAIL_USERNAME')
        if not api_key or not sender:
            flash('Email non configuré. Vérifie BREVO_API_KEY sur Render.', 'danger')
            return redirect(url_for('envoyer_message'))

        destinataires = [c.email for c in clients]
        resto_email = current_user.email
        resto_name = current_user.name
        resto_logo = current_user.logo_data

        logo_html = f'<img src="{resto_logo}" alt="{resto_name}" style="height:48px;object-fit:contain;margin-bottom:16px">' if resto_logo else f'<strong>{resto_name}</strong>'

        def _envoyer():
            try:
                import requests as req
                html = f"""<div style="font-family:Inter,sans-serif;max-width:520px;margin:auto;padding:32px 24px;color:#1a1a2e">
                    <div style="text-align:center;margin-bottom:24px">{logo_html}</div>
                    <p style="line-height:1.7;white-space:pre-line">{contenu}</p>
                    <hr style="margin:24px 0;border:none;border-top:1px solid #eee">
                    <p style="font-size:0.85rem;color:#999">— {resto_name}</p>
                </div>"""
                payload = {
                    "sender": {"email": sender},
                    "to": [{"email": resto_email}],
                    "bcc": [{"email": e} for e in destinataires],
                    "subject": f'[{resto_name}] {sujet}',
                    "textContent": f'{contenu}\n\n— {resto_name}',
                    "htmlContent": html,
                }
                resp = req.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={"api-key": api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=15
                )
                resp.raise_for_status()
                app.logger.info(f"Message envoyé à {len(destinataires)} clients : {resp.status_code}")
            except Exception as e:
                app.logger.error(f"Erreur envoi message clients : {e}")

        import threading
        threading.Thread(target=_envoyer, daemon=True).start()
        flash(f'Message en cours d\'envoi à {len(destinataires)} client(s) !', 'success')

        return redirect(url_for('envoyer_message'))

    return render_template('dashboard/message.html', clients=clients)


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
            send_email(
                subject=f'Bienvenue chez {resto.name} 🎉',
                recipients=[email],
                body_text=f"Bonjour {first_name},\n\nTu es inscrit(e) au programme de fidélité de {resto.name}.\nGagne {resto.points_per_visit} points à chaque visite et obtiens {resto.reward_description} dès {resto.reward_threshold} points.\n\nÀ bientôt !",
                body_html=hera_email(f"""
                    {f'<div style="text-align:center;margin-bottom:20px">{logo_html}</div>' if resto.logo_data else ''}
                    <h2 style="font-size:1.2rem;margin-bottom:8px">Bienvenue, {first_name} 👋</h2>
                    <p style="color:#555;line-height:1.6">Tu es inscrit(e) au programme de fidélité de <strong>{resto.name}</strong>.</p>
                    <div style="background:#f8f9fa;border-radius:10px;padding:20px;margin:20px 0">
                        <div style="margin-bottom:8px">🎯 <strong>{resto.points_per_visit} points</strong> à chaque visite validée</div>
                        <div>🎁 <strong>{resto.reward_description}</strong> dès <strong>{resto.reward_threshold} points</strong></div>
                    </div>
                    <p style="color:#555;line-height:1.6">Présente simplement ton email à la caisse pour valider tes visites.</p>
                """)
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
    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
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

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        else:
            event = stripe.Event.construct_from(request.get_json(), stripe.api_key)
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
