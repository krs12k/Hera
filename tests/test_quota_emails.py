"""Limite anti-spam des envois groupés vers les clients."""
from datetime import datetime, timedelta

import app as appmod
from app import db
from models import EmailCampaign, Visit


def _campagne(resto, jours_avant=0):
    db.session.add(EmailCampaign(restaurant_id=resto.id, kind='message', recipients=5,
                                 created_at=datetime.utcnow() - timedelta(days=jours_avant)))
    db.session.commit()


def test_quota_limite_journaliere(make_resto):
    resto = make_resto()
    ok, _ = appmod.quota_campagne(resto.id)
    assert ok is True
    _campagne(resto)                       # 1 campagne aujourd'hui
    ok, msg = appmod.quota_campagne(resto.id)
    assert ok is False and 'aujourd' in msg


def test_quota_limite_hebdo(make_resto):
    resto = make_resto()
    for _ in range(3):
        _campagne(resto, jours_avant=2)    # 3 campagnes il y a 2 jours
    ok, msg = appmod.quota_campagne(resto.id)
    assert ok is False and 'semaine' in msg.lower()
    assert appmod.envois_restants_semaine(resto.id) == 0


def test_envois_restants(make_resto):
    resto = make_resto()
    assert appmod.envois_restants_semaine(resto.id) == 3
    _campagne(resto, jours_avant=2)
    assert appmod.envois_restants_semaine(resto.id) == 2


def test_relance_bloquee_si_quota_atteint(make_resto, make_cli, login, client, sent):
    resto = make_resto()
    cli = make_cli(resto, email='a@t.be')
    db.session.add(Visit(client_id=cli.id, restaurant_id=resto.id, points_earned=10,
                         created_at=datetime.utcnow() - timedelta(days=45)))
    db.session.commit()
    _campagne(resto)                       # quota du jour déjà consommé
    login(resto)
    resp = client.post('/dashboard/relancer-inactifs',
                       data={'client_ids': [str(cli.id)]}, follow_redirects=True)
    assert sent == []                      # aucun email envoyé
    assert 'aujourd' in resp.get_data(as_text=True)


def test_relance_enregistre_une_campagne(make_resto, make_cli, login, client, sent):
    resto = make_resto()
    cli = make_cli(resto, email='a@t.be')
    db.session.add(Visit(client_id=cli.id, restaurant_id=resto.id, points_earned=10,
                         created_at=datetime.utcnow() - timedelta(days=45)))
    db.session.commit()
    login(resto)
    client.post('/dashboard/relancer-inactifs',
                data={'client_ids': [str(cli.id)]}, follow_redirects=True)
    assert len(sent) == 1
    assert appmod.campagnes_recentes(resto.id, 7) == 1


def test_message_exclut_desinscrits(make_resto, make_cli, login, client):
    resto = make_resto()
    make_cli(resto, first_name='Abonne', email='ok@t.be')
    make_cli(resto, first_name='Parti', email='no@t.be', email_opt_out=True)
    login(resto)
    html = client.get('/dashboard/message').get_data(as_text=True)
    assert '1 client(s) abonné' in html
    assert 'désinscrit' in html
