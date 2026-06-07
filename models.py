from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import secrets

db = SQLAlchemy()

class Restaurant(UserMixin, db.Model):
    __tablename__ = 'restaurants'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    address = db.Column(db.String(200))
    phone = db.Column(db.String(20))
    qr_token = db.Column(db.String(32), unique=True, default=lambda: secrets.token_hex(16))
    points_per_visit = db.Column(db.Integer, default=10)
    reward_threshold = db.Column(db.Integer, default=100)
    reward_description = db.Column(db.String(200), default='1 repas offert')
    minimum_amount = db.Column(db.Float, default=0.0)

    # Mode fidélité
    point_mode = db.Column(db.String(20), default='simple')

    # Reset mot de passe
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)

    # Abonnement
    subscription_status = db.Column(db.String(20), default='trial')  # trial, active, inactive
    is_free = db.Column(db.Boolean, default=False)
    trial_ends_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(days=14))
    stripe_customer_id = db.Column(db.String(100), nullable=True)
    stripe_subscription_id = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    clients = db.relationship('Client', backref='restaurant', lazy=True)
    point_rules = db.relationship('PointRule', backref='restaurant', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def can_access(self):
        if self.is_free:
            return True
        if self.subscription_status == 'active':
            return True
        if self.subscription_status == 'trial' and self.trial_ends_at and self.trial_ends_at > datetime.utcnow():
            return True
        return False

    @property
    def trial_days_left(self):
        if self.subscription_status == 'trial':
            delta = self.trial_ends_at - datetime.utcnow()
            return max(0, delta.days)
        return 0


class Client(db.Model):
    __tablename__ = 'clients'

    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey('restaurants.id'), nullable=False)
    first_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    total_points = db.Column(db.Integer, default=0)
    rgpd_consent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    visits = db.relationship('Visit', backref='client', lazy=True)

    __table_args__ = (
        db.UniqueConstraint('restaurant_id', 'email', name='uq_client_restaurant_email'),
    )


class Visit(db.Model):
    __tablename__ = 'visits'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    restaurant_id = db.Column(db.Integer, db.ForeignKey('restaurants.id'), nullable=False)
    points_earned = db.Column(db.Integer, nullable=False)
    amount_spent = db.Column(db.Float, nullable=True)
    note = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PointRule(db.Model):
    __tablename__ = 'point_rules'

    id = db.Column(db.Integer, primary_key=True)
    restaurant_id = db.Column(db.Integer, db.ForeignKey('restaurants.id'), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    points = db.Column(db.Integer, nullable=False)
