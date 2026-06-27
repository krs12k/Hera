"""Récap hebdomadaire au restaurateur + endpoint cron sécurisé."""
from datetime import datetime, timedelta

import app as appmod
from app import app as flask_app, db
from models import Client, Visit


def _seed(resto, make_cli):
    now = datetime.utcnow()
    c1 = make_cli(resto, first_name='Recent', email='r@t.be', created_at=now - timedelta(days=2))
    c2 = make_cli(resto, first_name='Vieux', email='v@t.be', created_at=now - timedelta(days=60))
    db.session.add(Visit(client_id=c1.id, restaurant_id=resto.id, points_earned=10,
                         created_at=now - timedelta(days=1)))
    db.session.add(Visit(client_id=c2.id, restaurant_id=resto.id, points_earned=10,
                         created_at=now - timedelta(days=45)))  # inactif -> à relancer
    db.session.commit()


def test_recap_envoye(make_resto, make_cli, sent):
    resto = make_resto()
    _seed(resto, make_cli)
    assert appmod.envoyer_recap_hebdo(resto) is True
    m = sent[0]
    assert resto.email in m['recipients']
    assert 'semaine' in m['subject'].lower()
    assert '1 client' in m['body_text'] or 'à relancer' in m['body_text']


def test_pas_de_recap_si_desactive(make_resto, make_cli, sent):
    resto = make_resto(weekly_digest=False)
    _seed(resto, make_cli)
    assert appmod.envoyer_recap_hebdo(resto) is False
    assert sent == []


def test_pas_de_recap_si_compte_vide(make_resto, sent):
    resto = make_resto()
    assert appmod.envoyer_recap_hebdo(resto) is False
    assert sent == []


def test_pas_de_recap_si_acces_bloque(make_resto, make_cli, sent):
    resto = make_resto(subscription_status='blocked')
    _seed(resto, make_cli)
    assert appmod.envoyer_recap_hebdo(resto) is False
    assert sent == []


def test_cron_sans_secret_refuse(client, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'CRON_SECRET', 'topsecret')
    resp = client.get('/cron/recap-hebdo')                       # pas de secret fourni
    assert resp.status_code == 403


def test_cron_mauvais_secret_refuse(client, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'CRON_SECRET', 'topsecret')
    resp = client.get('/cron/recap-hebdo?secret=faux')
    assert resp.status_code == 403


def test_cron_secret_absent_desactive(client, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'CRON_SECRET', None)
    resp = client.get('/cron/recap-hebdo?secret=peu importe')
    assert resp.status_code == 403


def test_cron_bon_secret_envoie(make_resto, make_cli, client, sent, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'CRON_SECRET', 'topsecret')
    resto = make_resto()
    _seed(resto, make_cli)
    resp = client.get('/cron/recap-hebdo?secret=topsecret')
    assert resp.status_code == 200
    assert resp.get_json()['envoyes'] == 1
    assert len(sent) == 1
