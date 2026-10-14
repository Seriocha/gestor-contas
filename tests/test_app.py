import re

import pytest
from fastapi.testclient import TestClient

from app.formatos import fmt_montante, mascarar_iban, parse_montante
from app.main import create_app

PW = "password-segura-1"


def _csrf(resp) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', resp.text).group(1)


def _post(client, url_form, url_post=None, **dados):
    """GET ao formulário para obter o token CSRF e depois POST."""
    r = client.get(url_form)
    return client.post(url_post or url_form, data={"csrf": _csrf(r), **dados}, follow_redirects=False)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    r = _post(c, "/setup", nome="Sérgio", login="sergio", password=PW, password2=PW)
    assert r.status_code == 303
    return c


def _ids_entidades(app):
    from app.models import Entidade
    with app.state.SessionLocal() as db:
        return {e.nome: e.id for e in db.query(Entidade)}


def test_formatos():
    assert parse_montante("1.234,56") == 123456
    assert parse_montante("1234.56") == 123456
    assert parse_montante("-12,5 €") == -1250
    assert parse_montante("1.000") == 100000
    assert parse_montante("") == 0
    assert fmt_montante(-123456) == "-1.234,56 €"
    assert mascarar_iban("PT50 0018 0000 1234 5678 9012 3") == "PT50 •••• 0123"


def test_sem_utilizadores_redireciona_para_setup(app):
    c = TestClient(app)
    assert c.get("/", follow_redirects=True).url.path == "/setup"


def test_setup_so_uma_vez(admin, app):
    c = TestClient(app)
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_csrf_obrigatorio(app):
    c = TestClient(app)
    c.get("/setup")
    r = c.post("/setup", data={"nome": "x", "login": "x", "password": PW, "password2": PW})
    assert r.status_code == 400


def test_login_e_bloqueio(admin, app):
    c = TestClient(app)
    for _ in range(5):
        r = _post(c, "/login", login="sergio", password="errada-errada")
        assert r.status_code == 401
    r = _post(c, "/login", login="sergio", password=PW)
    assert r.status_code == 429  # bloqueado mesmo com a password certa


def test_criar_conta_e_cartao(admin, app):
    ids = _ids_entidades(app)
    fam = ids["Família"]
    r = _post(admin, f"/contas/nova?entidade={fam}", nome="Santander ordem", tipo="ORDEM", moeda="EUR",
              instituicao_id="1", saldo_inicial="1.500,00", iban="PT50000000001234567890123")
    assert r.status_code == 303
    r = _post(admin, f"/contas/nova?entidade={fam}", nome="Cartão Santander", tipo="CARTAO_CREDITO",
              moeda="EUR", instituicao_id="1", saldo_inicial="-200", conta_pagamento_id="1", dia_pagamento="25")
    assert r.status_code == 303
    pagina = admin.get("/contas").text
    assert "1.500,00 €" in pagina and "paga por Santander ordem, dia 25" in pagina
    assert "PT50 •••• 0123" in pagina
    # nome duplicado na mesma entidade é rejeitado
    r = _post(admin, f"/contas/nova?entidade={fam}", nome="Santander ordem", tipo="ORDEM", moeda="EUR",
              instituicao_id="1")
    assert r.status_code == 400


def test_mulher_nao_ve_mae(admin, app):
    ids = _ids_entidades(app)
    _post(admin, f"/contas/nova?entidade={ids['Mãe']}", nome="CA Mãe", tipo="ORDEM", moeda="EUR",
          instituicao_id="6")
    # papel por omissão do novo utilizador: editar na Família, sem acesso à Mãe
    novo = admin.get("/admin/utilizadores/novo").text
    assert re.search(rf'name="papel_{ids["Família"]}">.*?value="editar" selected', novo, re.S)
    r = _post(admin, "/admin/utilizadores/novo", nome="Ana", login="ana", password=PW, password2=PW,
              **{f"papel_{ids['Família']}": "editar"})
    assert r.status_code == 303

    ana = TestClient(app)
    assert _post(ana, "/login", login="ana", password=PW).status_code == 303
    contas = ana.get("/contas").text
    assert "CA Mãe" not in contas and "+ Nova conta" not in contas
    assert ana.get("/contas/1/editar").status_code == 404           # conta da Mãe: invisível
    assert ana.get(f"/contas/nova?entidade={ids['Família']}").status_code == 403  # editar ≠ admin
    assert ana.get("/admin/utilizadores").status_code == 403


def test_troca_de_password_termina_outras_sessoes(admin, app):
    outra = TestClient(app)
    _post(outra, "/login", login="sergio", password=PW)
    assert outra.get("/", follow_redirects=False).status_code == 200
    nova = "outra-password-1"
    r = _post(admin, "/perfil", atual=PW, password=nova, password2=nova)
    assert r.status_code == 303
    assert admin.get("/", follow_redirects=False).status_code == 200
    assert outra.get("/", follow_redirects=False).status_code == 303


def test_ultimo_admin_nao_pode_ser_despromovido(admin):
    r = _post(admin, "/admin/utilizadores/1", nome="Sérgio", ativo="1")
    assert r.status_code == 400


def test_backup_manual(admin, app):
    r = _post(admin, "/admin/utilizadores", "/admin/backup")
    assert r.status_code == 303
    assert list(app.state.defs.backup_dir.glob("gestor-*.db"))
