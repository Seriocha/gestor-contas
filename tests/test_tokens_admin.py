"""Preferência «tokens_pelo_admin»: as chaves da API do Paperless e do Immich de cada pessoa passam a ser configuradas
pelo administrador na ficha do utilizador; no Perfil só se vê o estado. As APIs são simuladas."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import immich, paperless
from app.main import create_app
from app.models import Utilizador
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def apis(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_IMMICH_URL", "http://immich_server:2283")
    for v in ("GESTOR_PAPERLESS_TOKEN", "GESTOR_IMMICH_TOKEN"):
        monkeypatch.delenv(v, raising=False)

    def paperless_falso(caminho, params=None, binario=False, token=None):
        if token == "errado":
            raise paperless.ErroPaperless("O Paperless respondeu 401. Confirma o token.")
        return {"count": 3, "results": []}

    def immich_falso(caminho, metodo="GET", corpo=None, binario=False, chave=None):
        if chave == "errado":
            raise immich.ErroImmich("O Immich respondeu 401. Confirma a chave da API.")
        return {"name": "Maria"}
    monkeypatch.setattr(paperless, "_pedido", paperless_falso)
    monkeypatch.setattr(immich, "_pedido", immich_falso)


@pytest.fixture
def app(tmp_path, apis):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/admin/utilizadores/novo", nome="Maria", login="maria", password=PW, password2=PW, papel_1="ver")
    return c


def _prefs(c, tokens: bool):
    dados = {"csrf": _csrf(c.get("/admin/preferencias")), "tarefas_ativas": "1", "tarefas_dia_ini": "25", "tarefas_dia_fim": "5"}
    if tokens:
        dados["tokens_pelo_admin"] = "1"
    return c.post("/admin/preferencias", data=dados)


def test_por_omissao_cada_pessoa_no_perfil(admin):
    assert 'action="/perfil/immich"' in admin.get("/perfil").text
    f = admin.get("/admin/utilizadores/2").text
    assert 'id="ligacoes"' not in f and "liga a opção em" in f
    assert 'name="tokens_pelo_admin" value="1" >' in admin.get("/admin/preferencias").text or \
           'name="tokens_pelo_admin" value="1">' in admin.get("/admin/preferencias").text
    # sem a opção, o administrador não pode gravar chaves dos outros
    r = admin.post("/admin/utilizadores/2/ligacao", data={"csrf": _csrf(admin.get("/admin/utilizadores/2")), "servico": "immich",
                                                         "acao": "guardar", "token": "chave-maria"})
    assert "Liga primeiro" in r.text


def test_chaves_configuradas_pelo_administrador(admin, app):
    _prefs(admin, True)
    assert "checked" in admin.get("/admin/preferencias").text.split('name="tokens_pelo_admin"')[1][:30]
    f = admin.get("/admin/utilizadores/2").text
    assert 'id="ligacoes"' in f and "Ligações de Maria" in f and f.count('name="servico"') == 2

    def post(servico, acao, token=""):
        return admin.post("/admin/utilizadores/2/ligacao", data={"csrf": _csrf(admin.get("/admin/utilizadores/2")),
                                                                 "servico": servico, "acao": acao, "token": token})
    assert "recusada pelo Immich" in post("immich", "guardar", "errado").text
    r = post("immich", "guardar", "chave-maria")
    assert "Chave do Immich de Maria guardada. Ligação ao Immich confirmada: Maria." in r.text
    r = post("paperless", "guardar", "token-maria")
    assert "Token do Paperless de Maria guardado." in r.text
    with app.state.SessionLocal() as db:
        maria = db.scalar(select(Utilizador).where(Utilizador.login == "maria"))
        segredo = app.state.defs.secret_key
        assert immich.decifrar(maria.immich_token, segredo) == "chave-maria"
        assert paperless.decifrar(maria.paperless_token, segredo) == "token-maria"
        admin_u = db.scalar(select(Utilizador).where(Utilizador.login == "admin"))
        assert admin_u.immich_token is None                                   # só o da Maria
    assert "Ligação ao Immich confirmada" in post("immich", "testar").text
    assert "foi removida" in post("immich", "remover").text

    # no Perfil: só o estado; gravar lá é recusado
    p = admin.get("/perfil").text
    assert 'action="/perfil/immich"' not in p and 'action="/perfil/paperless"' not in p
    assert "configurada pelo administrador" in p and "configurado pelo administrador" in p
    r = admin.post("/perfil/immich", data={"csrf": _csrf(admin.get("/perfil")), "acao": "guardar", "token": "x"})
    assert "A chave do Immich é configurada pelo administrador" in r.text
    # desligar volta ao Perfil, e as chaves guardadas ficam
    _prefs(admin, False)
    assert 'action="/perfil/immich"' in admin.get("/perfil").text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(Utilizador).where(Utilizador.login == "maria")).paperless_token
