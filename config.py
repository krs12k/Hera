import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'local-dev-only-not-for-production'

    # Render fournit DATABASE_URL pour PostgreSQL, sinon SQLite en local
    database_url = os.environ.get('DATABASE_URL') or 'sqlite:///fidelite.db'
    # Render utilise "postgres://" mais SQLAlchemy requiert "postgresql://"
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_USERNAME')
