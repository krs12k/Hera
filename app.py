from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, make_response
from sqlalchemy import text, func
from collections import defaultdict
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
from models import db, Restaurant, Client, Visit, PointRule, AdminUser
from config import Config
from datetime import datetime, timedelta
from functools import wraps
import qrcode
import io
import base64
import time
import secrets
import stripe

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
mail = Mail(app)
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Connecte-toi pour accéder à cette page.'

@login_manager.user_loader
def load_user(user_id):
    return Restaurant.query.get(int(user_id))

with app.app_context():
    db.create_all()
    for sql in [
        "ALTER TABLE restaurants ADD COLUMN point_mode VARCHAR(20) DEFAULT 'simple'",
        "ALTER TABLE restaurants ADD COLUMN reset_token VARCHAR(100)",
        "ALTER TABLE restaurants ADD COLUMN reset_token_expires TIMESTAMP",
    ]:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
        except Exception:
            pass

stripe.api_key = app.config.get('STRIPE_SECRET_KEY')

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


# ── Inscription restaurateur ────────────────────────────────────
@app.route('/inscription', methods=['GET', 'POST'])
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
        return redirect(url_for('choisir_plan'))

    return render_template('auth/register.html')


# ── Connexion restaurateur ──────────────────────────────────────
@app.route('/connexion', methods=['GET', 'POST'])
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
                try:
                    msg = Message(
                        subject='Réinitialisation de votre mot de passe — hera.',
                        recipients=[email],
                        sender=app.config.get('MAIL_USERNAME'),
                        body=f"""Bonjour,

Vous avez demandé à réinitialiser votre mot de passe sur hera.

Cliquez sur ce lien (valable 1 heure) :
{reset_url}

Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.

— L'équipe hera."""
                    )
                    mail.send(msg)
                except Exception:
                    pass
        except Exception:
            db.session.rollback()
        flash('Si cet email est enregistré, un lien de réinitialisation a été envoyé.', 'info')
        return redirect(url_for('forgot_password'))
    return render_template('auth/forgot.html')


# ── Réinitialisation du mot de passe ────────────────────────────
@app.route('/reinitialiser-mdp/<token>', methods=['GET', 'POST'])
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
    return render_template('dashboard/parametres.html', rules=rules)


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

        if not app.config.get('MAIL_USERNAME'):
            flash(
                'Email non configuré. Renseigne MAIL_USERNAME et MAIL_PASSWORD dans les variables d\'environnement.',
                'danger'
            )
            return redirect(url_for('envoyer_message'))

        try:
            destinataires = [c.email for c in clients]
            # BCC : chaque client ne voit pas les autres emails
            msg = Message(
                subject=f'[{current_user.name}] {sujet}',
                recipients=[current_user.email],
                bcc=destinataires,
                body=f'{contenu}\n\n— {current_user.name}'
            )
            mail.send(msg)
            flash(f'Message envoyé à {len(destinataires)} client(s) !', 'success')
        except Exception as e:
            flash(f'Erreur lors de l\'envoi : {str(e)}', 'danger')

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
        if not client:
            client = Client(restaurant_id=resto.id, first_name=first_name, email=email, rgpd_consent=True)
            db.session.add(client)
            db.session.commit()

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
    return render_template('client/profil.html', client=client, resto=resto, progression=progression)


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
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Super admin via variables d'environnement
        if username == 'admin' and password == app.config.get('ADMIN_PASSWORD'):
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
    if current != app.config.get('ADMIN_PASSWORD'):
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
                           is_super=session.get('admin_is_super', False),
                           admin_username=session.get('admin_username', 'admin'))


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
    app.run(debug=True)
