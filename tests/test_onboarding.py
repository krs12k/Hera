"""Onboarding : drapeaux et visibilité de la checklist."""
from app import db


def test_checklist_visible_pour_nouveau_resto(make_resto, login, client):
    resto = make_resto()   # onboarding_configured et qr_seen = False (défaut modèle)
    login(resto)
    html = client.get('/dashboard').get_data(as_text=True)
    assert 'Bien démarrer sur hera' in html


def test_qrcode_marque_qr_seen(make_resto, login, client):
    resto = make_resto()
    assert resto.qr_seen is False
    login(resto)
    client.get('/dashboard/qrcode')
    db.session.refresh(resto)
    assert resto.qr_seen is True


def test_parametres_marque_configured(make_resto, login, client):
    resto = make_resto()
    login(resto)
    client.post('/dashboard/parametres', data={
        'name': resto.name, 'email': resto.email,
        'point_mode': 'simple', 'points_per_visit': '10',
        'reward_threshold': '100', 'reward_description': '1 café',
        'minimum_amount': '0',
    })
    db.session.refresh(resto)
    assert resto.onboarding_configured is True


def test_checklist_cachee_quand_tout_fait(make_resto, make_cli, login, client):
    resto = make_resto(logo_data='data:image/png;base64,xxx')
    resto.onboarding_configured = True
    resto.qr_seen = True
    db.session.commit()
    make_cli(resto)   # premier client
    login(resto)
    html = client.get('/dashboard').get_data(as_text=True)
    assert 'Bien démarrer sur hera' not in html
