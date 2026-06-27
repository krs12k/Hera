"""Sécurité du webhook Stripe : signature obligatoire en production."""
import app as appmod
from app import app as flask_app


def test_prod_sans_secret_rejete(client, monkeypatch):
    monkeypatch.setattr(appmod, 'IS_PRODUCTION', True)
    monkeypatch.setitem(flask_app.config, 'STRIPE_WEBHOOK_SECRET', None)
    resp = client.post('/stripe/webhook',
                       data=b'{"type":"invoice.payment_succeeded"}',
                       content_type='application/json')
    assert resp.status_code == 400


def test_signature_invalide_rejetee(client, monkeypatch):
    monkeypatch.setitem(flask_app.config, 'STRIPE_WEBHOOK_SECRET', 'whsec_test')
    resp = client.post('/stripe/webhook',
                       data=b'{"type":"invoice.payment_succeeded"}',
                       content_type='application/json',
                       headers={'Stripe-Signature': 'mauvaise'})
    assert resp.status_code == 400


def test_dev_sans_secret_accepte(client, monkeypatch):
    monkeypatch.setattr(appmod, 'IS_PRODUCTION', False)
    monkeypatch.setitem(flask_app.config, 'STRIPE_WEBHOOK_SECRET', None)
    resp = client.post('/stripe/webhook',
                       data=b'{"type":"customer.subscription.deleted",'
                            b'"data":{"object":{"id":"sub_x"}}}',
                       content_type='application/json')
    assert resp.status_code == 200
