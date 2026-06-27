"""Relance des clients inactifs : éligibilité, sélection, cooldown."""
from datetime import datetime, timedelta

import app as appmod
from app import db
from models import Visit


def _visite(cli, resto, jours):
    db.session.add(Visit(client_id=cli.id, restaurant_id=resto.id,
                         points_earned=10,
                         created_at=datetime.utcnow() - timedelta(days=jours)))
    db.session.commit()


def test_eligibilite(make_resto, make_cli):
    resto = make_resto()
    actif = make_cli(resto, first_name='Actif', email='a@t.be')
    inactif = make_cli(resto, first_name='Inactif', email='b@t.be')
    sans_visite = make_cli(resto, first_name='Jamais', email='c@t.be')
    optout = make_cli(resto, first_name='Optout', email='d@t.be', email_opt_out=True)
    _visite(actif, resto, 5)        # vu récemment -> exclu
    _visite(inactif, resto, 45)     # inactif -> éligible
    _visite(optout, resto, 50)      # inactif mais désinscrit -> exclu

    noms = sorted(c.first_name for c in appmod.clients_a_relancer(resto.id))
    assert noms == ['Inactif']


def test_cooldown(make_resto, make_cli):
    resto = make_resto()
    cli = make_cli(resto, email='b@t.be', last_relance_at=datetime.utcnow() - timedelta(days=10))
    _visite(cli, resto, 45)
    assert appmod.clients_a_relancer(resto.id) == []   # relancé il y a 10 j -> en pause


def test_relance_selective(make_resto, make_cli, login, client, sent):
    resto = make_resto()
    a = make_cli(resto, first_name='A', email='a@t.be')
    b = make_cli(resto, first_name='B', email='b@t.be')
    _visite(a, resto, 40)
    _visite(b, resto, 40)
    login(resto)

    resp = client.post('/dashboard/relancer-inactifs',
                       data={'client_ids': [str(a.id)]}, follow_redirects=True)
    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]['recipients'] == ['a@t.be']
    # A passe en cooldown, B reste éligible
    assert [c.first_name for c in appmod.clients_a_relancer(resto.id)] == ['B']


def test_relance_sans_selection(make_resto, make_cli, login, client, sent):
    resto = make_resto()
    a = make_cli(resto, email='a@t.be')
    _visite(a, resto, 40)
    login(resto)
    resp = client.post('/dashboard/relancer-inactifs', data={}, follow_redirects=True)
    assert sent == []
    assert 'lectionne au moins' in resp.get_data(as_text=True)
