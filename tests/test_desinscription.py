"""Désinscription RGPD : opt-out, réabonnement, token invalide."""
import app as appmod
from app import db


def test_desinscription_et_reabonnement(make_resto, make_cli, client):
    resto = make_resto()
    cli = make_cli(resto, email='z@t.be')
    token = appmod._unsub_serializer().dumps(cli.id)

    resp = client.get(f'/desinscription/{token}')
    assert resp.status_code == 200
    assert 'désinscrit' in resp.get_data(as_text=True)
    db.session.refresh(cli)
    assert cli.email_opt_out is True

    resp = client.get(f'/reabonnement/{token}')
    assert 'réabonné' in resp.get_data(as_text=True)
    db.session.refresh(cli)
    assert cli.email_opt_out is False


def test_token_invalide(client):
    resp = client.get('/desinscription/nimportequoi')
    assert resp.status_code == 200
    assert 'invalide' in resp.get_data(as_text=True)


def test_desinscrit_ne_recoit_plus_la_relance(make_resto, make_cli, sent):
    from datetime import datetime, timedelta
    from models import Visit
    resto = make_resto()
    cli = make_cli(resto, email='z@t.be', email_opt_out=True)
    db.session.add(Visit(client_id=cli.id, restaurant_id=resto.id, points_earned=10,
                         created_at=datetime.utcnow() - timedelta(days=45)))
    db.session.commit()
    assert appmod.clients_a_relancer(resto.id) == []
    appmod.envoyer_relance(cli, resto)
    assert sent == []
