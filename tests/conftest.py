"""Fixtures communes. Configure une base SQLite temporaire et désactive
CSRF + rate-limiting AVANT d'importer l'application."""
import os
import tempfile

# Base de test isolée — définie avant l'import de l'app (config lit l'env).
_db_fd, _db_path = tempfile.mkstemp(suffix='.db')
os.close(_db_fd)
os.environ['DATABASE_URL'] = 'sqlite:///' + _db_path.replace('\\', '/')
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'test-admin')

import pytest                       # noqa: E402
import app as appmod                # noqa: E402
from app import app as flask_app, db  # noqa: E402
from models import Restaurant, Client  # noqa: E402

flask_app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=False,
    RATELIMIT_ENABLED=False,
    SERVER_NAME='localhost',
)
appmod.limiter.enabled = False


def pytest_unconfigure(config):
    try:
        os.remove(_db_path)
    except OSError:
        pass


@pytest.fixture
def app_ctx():
    """Contexte applicatif + base remise à zéro pour chaque test."""
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


@pytest.fixture
def client(app_ctx):
    return flask_app.test_client()


@pytest.fixture
def sent(monkeypatch):
    """Capture les emails au lieu de les envoyer."""
    box = []
    monkeypatch.setattr(appmod, 'send_email', lambda **kw: box.append(kw))
    return box


@pytest.fixture
def make_resto(app_ctx):
    def _make(**kw):
        params = dict(name='Resto Test', email='resto@test.be',
                      reward_threshold=100, reward_description='1 café offert',
                      notify_clients=True)
        params.update(kw)
        r = Restaurant(**params)
        r.set_password('motdepasse')
        db.session.add(r)
        db.session.commit()
        return r
    return _make


@pytest.fixture
def make_cli(app_ctx):
    def _make(resto, **kw):
        params = dict(restaurant_id=resto.id, first_name='Client',
                      email='client@test.be', total_points=0)
        params.update(kw)
        c = Client(**params)
        db.session.add(c)
        db.session.commit()
        return c
    return _make


@pytest.fixture
def login(client):
    def _login(resto):
        with client.session_transaction() as s:
            s['_user_id'] = str(resto.id)
    return _login
