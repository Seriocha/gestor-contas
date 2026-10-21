"""Vista da TV: emparelhamento por código, só consulta, entidades escolhidas, desligar e invalidação pela password."""
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Auditoria, DispositivoTV
from tests.test_app import PW, _csrf, _ids_entidades, _post, admin, app  # noqa: F401 (fixtures)


def _codigo(resp) -> str:
    return re.search(r'class="tv-codigo">(\d{3}) (\d{3})<', resp.text).expand(r"\1\2")


def _emparelhar(app, admin, entidades=None, nome="TV da sala"):
    tvc = TestClient(app)
    r = tvc.get("/tv", follow_redirects=True)
    assert r.url.path == "/tv/emparelhar"
    codigo = _codigo(r)
    assert "<svg" in r.text                                      # código QR
    assert tvc.get("/tv/emparelhar/estado").json()["estado"] == "espera"
    ids = list(_ids_entidades(app).values()) if entidades is None else entidades
    r = _post(admin, f"/tv/aprovar?codigo={codigo}", "/tv/aprovar", codigo=codigo, nome=nome, entidade=ids)
    assert r.status_code == 303, r.text
    assert tvc.get("/tv/emparelhar/estado").json()["estado"] == "aprovado"
    return tvc


def test_emparelhar_e_ver_paginas(admin, app):
    tvc = _emparelhar(app, admin)
    for url in ("/tv", "/tv/contas", "/tv/mes", "/tv/agenda", "/tv/mes?m=2020-01", "/tv/mes?m=lixo"):
        r = tvc.get(url, follow_redirects=False)
        assert r.status_code == 200, url
        assert "Desligar esta TV" in r.text
    assert "Património líquido" in tvc.get("/tv").text
    # a TV não tem sessão da app: não abre as páginas normais nem consegue alterar nada
    assert tvc.get("/inicio", follow_redirects=False).headers["location"].startswith("/login")
    assert tvc.get("/tv/aprovar", follow_redirects=False).status_code == 303
    with app.state.SessionLocal() as db:
        d = db.scalar(select(DispositivoTV))
        assert d.nome == "TV da sala" and len(d.token_hash) == 64
        assert tvc.cookies.get("gestor_tv") != d.token_hash          # na base só o hash
        assert db.scalar(select(Auditoria).where(Auditoria.acao == "tv_emparelhar"))


def test_so_mostra_entidades_escolhidas(admin, app):
    ids = _ids_entidades(app)
    tvc = _emparelhar(app, admin, entidades=[ids["Família"]])
    r = tvc.get("/tv")
    assert "Família" in r.text and "Mãe" not in r.text
    # pedir outra entidade pela URL não a mostra: fica na escolhida
    r = tvc.get(f"/tv?e={ids['Mãe']}")
    assert "<small>Família</small>" in r.text


def test_codigo_errado_e_bloqueio(admin, app):
    tvc = TestClient(app)
    codigo = _codigo(tvc.get("/tv/emparelhar"))
    errado = "000000" if codigo != "000000" else "111111"
    ids = list(_ids_entidades(app).values())
    for _ in range(5):
        r = _post(admin, "/tv/aprovar", codigo=errado, nome="x", entidade=ids)
        assert r.status_code == 400
    r = _post(admin, "/tv/aprovar", codigo=codigo, nome="x", entidade=ids)
    assert r.status_code == 429                                  # bloqueado mesmo com o código certo
    assert tvc.get("/tv/emparelhar/estado").json()["estado"] == "espera"


def test_sem_entidades_nao_emparelha(admin, app):
    tvc = TestClient(app)
    codigo = _codigo(tvc.get("/tv/emparelhar"))
    r = _post(admin, "/tv/aprovar", codigo=codigo, nome="x")
    assert r.status_code == 400 and "pelo menos uma entidade" in r.text


def test_desligar_no_perfil(admin, app):
    tvc = _emparelhar(app, admin)
    r = admin.get("/perfil")
    assert "TV da sala" in r.text
    with app.state.SessionLocal() as db:
        did = db.scalar(select(DispositivoTV.id))
    r = admin.post(f"/tv/dispositivos/{did}/desligar", data={"csrf": _csrf(r)}, follow_redirects=False)
    assert r.status_code == 303
    assert tvc.get("/tv", follow_redirects=False).headers["location"] == "/tv/emparelhar"


def test_desligar_na_propria_tv(admin, app):
    tvc = _emparelhar(app, admin)
    r = tvc.post("/tv/desligar", follow_redirects=False)
    assert r.headers["location"] == "/tv/emparelhar"
    with app.state.SessionLocal() as db:
        assert db.scalar(select(DispositivoTV)) is None


def test_mudar_password_desliga_a_tv(admin, app):
    tvc = _emparelhar(app, admin)
    novo = "outra-password-boa"
    r = _post(admin, "/perfil", atual=PW, password=novo, password2=novo)
    assert r.status_code in (200, 303)
    assert tvc.get("/tv", follow_redirects=False).headers["location"] == "/tv/emparelhar"


def test_login_volta_ao_aprovar_com_codigo(app, admin):
    c = TestClient(app)
    r = c.get("/tv/aprovar?codigo=123456", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/tv/aprovar%3Fcodigo%3D123456"
    r = c.get(r.headers["location"])
    r = c.post("/login", data={"csrf": _csrf(r), "login": "sergio", "password": PW,
                               "next": "/tv/aprovar?codigo=123456"}, follow_redirects=False)
    assert r.headers["location"] == "/tv/aprovar?codigo=123456"
