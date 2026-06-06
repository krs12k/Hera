from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from models import db, Restaurant, Client, Visit
from config import Config
import qrcode
import io
import base64

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
mail = Mail(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Connecte-toi pour accéder à cette page.'

@login_manager.user_loader
def load_user(user_id):
    return Restaurant.query.get(int(user_id))

with app.app_context():
    db.create_all()


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
def dashboard():
    clients = Client.query.filter_by(restaurant_id=current_user.id).order_by(Client.total_points.desc()).all()
    total_visits = Visit.query.filter_by(restaurant_id=current_user.id).count()
    return render_template('dashboard/index.html', clients=clients, total_visits=total_visits)


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

    return render_template('dashboard/parametres.html')


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
            msg = Message(
                subject=f'[{current_user.name}] {sujet}',
                recipients=destinataires,
                body=f'{contenu}\n\n— {current_user.name}'
            )
            mail.send(msg)
            flash(f'Message envoyé à {len(destinataires)} client(s) !', 'success')
        except Exception as e:
            flash(f'Erreur lors de l\'envoi : {str(e)}', 'danger')

        return redirect(url_for('envoyer_message'))

    return render_template('dashboard/message.html', clients=clients)


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

        client = Client.query.filter_by(restaurant_id=resto.id, email=email).first()
        if not client:
            client = Client(restaurant_id=resto.id, first_name=first_name, email=email)
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


if __name__ == '__main__':
    app.run(debug=True)
