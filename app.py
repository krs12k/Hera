from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
from models import db, Restaurant, Client, Visit, PointRule
from config import Config
from datetime import datetime
from functools import wraps
import qrcode
import io
import base64
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

stripe.api_key = app.config.get('STRIPE_SECRET_KEY')

def subscription_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
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
        flash('Compte créé avec succès !', 'success')
        return redirect(url_for('dashboard'))

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


# ── Dashboard restaurateur ──────────────────────────────────────
@app.route('/dashboard')
@login_required
@subscription_required
def dashboard():
    clients = Client.query.filter_by(restaurant_id=current_user.id).order_by(Client.total_points.desc()).all()
    total_visits = Visit.query.filter_by(restaurant_id=current_user.id).count()
    rules = PointRule.query.filter_by(restaurant_id=current_user.id).all()
    return render_template('dashboard/index.html', clients=clients, total_visits=total_visits, rules=rules)


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
        current_user.address = request.form.get('address', '')
        current_user.phone = request.form.get('phone', '')
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


# ── Page client (via QR code) ───────────────────────────────────
@app.route('/rejoindre/<token>', methods=['GET', 'POST'])
def client_register(token):
    resto = Restaurant.query.filter_by(qr_token=token).first_or_404()

    if request.method == 'POST':
        first_name = request.form['first_name']
        email = request.form['email']

        consent = request.form.get('consent') == 'on'
        client = Client.query.filter_by(restaurant_id=resto.id, email=email).first()
        if not client:
            if not consent:
                flash('Tu dois accepter les conditions pour t\'inscrire.', 'danger')
                return redirect(url_for('client_register', token=token))
            client = Client(restaurant_id=resto.id, first_name=first_name, email=email, rgpd_consent=True)
            db.session.add(client)
            db.session.commit()
            flash('Bienvenue ! Tu es maintenant inscrit au programme de fidélité.', 'success')
        else:
            flash(f'Content de te revoir, {client.first_name} ! Tu as {client.total_points} points.', 'info')

        return redirect(url_for('client_profil', token=token, email=email))

    return render_template('client/register.html', resto=resto)


# ── Profil client ───────────────────────────────────────────────
@app.route('/rejoindre/<token>/profil')
def client_profil(token):
    resto = Restaurant.query.filter_by(qr_token=token).first_or_404()
    email = request.args.get('email')
    client = Client.query.filter_by(restaurant_id=resto.id, email=email).first_or_404()
    progression = min(int((client.total_points / resto.reward_threshold) * 100), 100)
    return render_template('client/profil.html', client=client, resto=resto, progression=progression)


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
        price_id = app.config.get('STRIPE_PRICE')
        if not price_id:
            # Crée le prix dynamiquement si pas encore configuré
            product = stripe.Product.create(name='hera Pro')
            price = stripe.Price.create(
                product=product.id,
                unit_amount=790,
                currency='eur',
                recurring={'interval': 'month'},
            )
            price_id = price.id

        customer = stripe.Customer.create(
            email=current_user.email,
            name=current_user.name,
        )
        current_user.stripe_customer_id = customer.id
        db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('abonnement_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('abonnement', _external=True),
        )
        return redirect(checkout_session.url)
    except Exception as e:
        flash(f'Erreur Stripe : {str(e)}', 'danger')
        return redirect(url_for('abonnement'))


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
    flash('Abonnement activé ! Bienvenue sur hera Pro.', 'success')
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
        password = request.form.get('password')
        if password == app.config.get('ADMIN_PASSWORD'):
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Mot de passe incorrect.', 'danger')
    return render_template('admin/login.html')


@app.route('/hera-admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))


# ── Admin — Dashboard ────────────────────────────────────────────
@app.route('/hera-admin/dashboard')
@admin_required
def admin_dashboard():
    restaurants = Restaurant.query.order_by(Restaurant.created_at.desc()).all()
    return render_template('admin/dashboard.html', restaurants=restaurants, now=datetime.utcnow())


# ── Admin — Toggle gratuit ───────────────────────────────────────
@app.route('/hera-admin/toggle-free/<int:resto_id>', methods=['POST'])
@admin_required
def admin_toggle_free(resto_id):
    resto = Restaurant.query.get_or_404(resto_id)
    resto.is_free = not resto.is_free
    db.session.commit()
    flash(f'{"Gratuit activé" if resto.is_free else "Gratuit désactivé"} pour {resto.name}.', 'success')
    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)
