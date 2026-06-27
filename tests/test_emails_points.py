"""Emails automatiques de points / récompense (notifier_points)."""
import app as appmod


def test_email_sous_le_seuil(make_resto, make_cli, sent):
    resto = make_resto(reward_threshold=100)
    cli = make_cli(resto, total_points=30)
    appmod.notifier_points(cli, resto, 30)
    assert len(sent) == 1
    m = sent[0]
    assert '+30 points' in m['subject']
    assert 'Plus que 70 point' in m['body_text']


def test_email_franchit_le_seuil(make_resto, make_cli, sent):
    resto = make_resto(reward_threshold=100)
    cli = make_cli(resto, total_points=100)
    appmod.notifier_points(cli, resto, 20)   # 80 -> 100 : franchit
    assert len(sent) == 1
    assert 'récompense' in sent[0]['subject'].lower() or 'attend' in sent[0]['subject'].lower()
    assert 'débloqu' in sent[0]['body_html'].lower()


def test_email_deja_au_dessus(make_resto, make_cli, sent):
    resto = make_resto(reward_threshold=100)
    cli = make_cli(resto, total_points=140)
    appmod.notifier_points(cli, resto, 40)   # etait deja au-dessus
    assert 'disponible' in sent[0]['body_text']


def test_pas_email_si_notifications_coupees(make_resto, make_cli, sent):
    resto = make_resto(notify_clients=False)
    cli = make_cli(resto, total_points=30)
    appmod.notifier_points(cli, resto, 30)
    assert sent == []


def test_pas_email_si_desinscrit(make_resto, make_cli, sent):
    resto = make_resto()
    cli = make_cli(resto, total_points=30, email_opt_out=True)
    appmod.notifier_points(cli, resto, 30)
    assert sent == []


def test_pas_email_si_zero_point(make_resto, make_cli, sent):
    resto = make_resto()
    cli = make_cli(resto, total_points=0)
    appmod.notifier_points(cli, resto, 0)
    assert sent == []


def test_pied_desinscription_present(make_resto, make_cli, sent):
    resto = make_resto()
    cli = make_cli(resto, total_points=10)
    appmod.notifier_points(cli, resto, 10)
    assert 'desinscription' in sent[0]['body_html']
    assert 'desinscription' in sent[0]['body_text']
