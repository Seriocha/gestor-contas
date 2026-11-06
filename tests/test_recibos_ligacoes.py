"""Administração › Paperless › Recibos: ligações (pessoa/token, tipo de documento, empregador, entidade) e o uso delas em
Importar › Recibos (períodos e valores). O Paperless é simulado em memória."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import paperless
from app.main import create_app
from app.models import DocumentoLigado, LigacaoRecibos
from tests.test_app import PW, _csrf, _ids_entidades, _post
from tests.test_recibos import CAMPOS, DEZ_2025, _credito

TIPOS = [{"id": 4, "name": "Recibo de vencimento"}, {"id": 5, "name": "Recibo Ana"}]
CORRESPONDENTES = [{"id": 10, "name": "Empresa A"}, {"id": 11, "name": "Banco B"}]
DOCS = [
    {"id": 1, "title": "A jan", "document_type": 4, "correspondent": 10, "content": "", "tags": [],
     "custom_fields": [{"field": 3, "value": "2025-01"}, {"field": 8, "value": "EUR1.00"}]},
    {"id": 2, "title": "B fev", "document_type": 4, "correspondent": 11, "content": "", "tags": [],
     "custom_fields": [{"field": 3, "value": "2025-02"}, {"field": 8, "value": "EUR2.00"}]},
    {"id": 3, "title": "Ana dez", "document_type": 5, "correspondent": 10, "content": DEZ_2025, "tags": [],
     "custom_fields": []},
]


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    pedidos, patches = [], []

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/document_types/":
            return {"results": TIPOS}
        if caminho == "/api/correspondents/":
            return {"results": CORRESPONDENTES}
        if caminho == "/api/custom_fields/":
            return {"results": CAMPOS}
        if caminho == "/api/tags/":
            return {"results": []}
        if caminho == "/api/documents/":
            pedidos.append(params)
            return {"results": [dict(d) for d in DOCS if d["document_type"] == params["document_type__id"]
                                and params.get("correspondent__id") in (None, d["correspondent"])], "next": None}
        raise AssertionError(caminho)

    def enviar(metodo, caminho, corpo, *, token):
        patches.append((caminho, corpo))
        return {"id": 99}
    monkeypatch.setattr(paperless, "_pedido", pedido)
    monkeypatch.setattr(paperless, "enviar", enviar)
    return pedidos


@pytest.fixture
def admin(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    c.app_ = app
    return c


def _ligacao(c, **dados):
    base = {"nome": "Vencimento", "utilizador": "1", "entidade": "", "tipo_documento": "Recibo de vencimento",
            "correspondente": ""}
    return _post(c, "/admin/paperless/recibos", **{**base, **dados})


def test_criar_editar_apagar(admin):
    r = admin.get("/admin/paperless/recibos")
    assert r.status_code == 200 and "Ainda não há ligações" in r.text
    assert '<option value="Recibo Ana">' in r.text and '<option value="Banco B">' in r.text      # sugestões do Paperless
    fam = _ids_entidades(admin.app_)["Família"]
    assert _ligacao(admin, nome="Ana", tipo_documento="Recibo Ana", correspondente="Empresa A",
                    entidade=str(fam)).status_code == 303
    with admin.app_.state.SessionLocal() as db:
        lig = db.scalar(select(LigacaoRecibos))
        assert (lig.nome, lig.utilizador_id, lig.entidade_id, lig.tipo_documento, lig.correspondente, lig.ativo) == \
            ("Ana", 1, fam, "Recibo Ana", "Empresa A", True)
    t = admin.get("/admin/paperless/recibos").text
    assert "Recibo Ana" in t and "Empresa A" in t and "Família" in t
    assert "Alterar «Ana»" in admin.get(f"/admin/paperless/recibos/{lig.id}").text
    # alterar: sem «ativo» → inativa; empregador vazio → todos
    _post(admin, f"/admin/paperless/recibos/{lig.id}", "/admin/paperless/recibos", id=str(lig.id), nome="Ana 2",
          utilizador="1", entidade="", tipo_documento="Recibo Ana", correspondente="")
    with admin.app_.state.SessionLocal() as db:
        lig = db.get(LigacaoRecibos, lig.id)
        assert (lig.nome, lig.entidade_id, lig.correspondente, lig.ativo) == ("Ana 2", None, None, False)
    _post(admin, "/admin/paperless/recibos", f"/admin/paperless/recibos/{lig.id}/apagar")
    with admin.app_.state.SessionLocal() as db:
        assert db.scalar(select(LigacaoRecibos)) is None


def test_validacao(admin):
    r = _ligacao(admin, nome="", utilizador="0")
    assert r.status_code == 400 and "Dá um nome" in r.text and "Escolhe a pessoa" in r.text


def test_periodos_com_tipo_e_empregador(admin, falso):
    _ligacao(admin, nome="Só A", correspondente="Empresa A")
    _ligacao(admin, nome="Ana", tipo_documento="Recibo Ana")
    pagina = admin.get("/importar/recibos").text
    assert 'name="ligacao"' in pagina and "Só A · Recibo de vencimento · Empresa A" in pagina
    with admin.app_.state.SessionLocal() as db:
        ids = {l.nome: l.id for l in db.scalars(select(LigacaoRecibos))}
    t = admin.get(f"/importar/recibos/periodos?ligacao={ids['Só A']}").text
    assert falso[-1]["document_type__id"] == 4 and falso[-1]["correspondent__id"] == 10
    assert "documents/1/details" in t and "documents/2/details" not in t and "Banco B ·" not in t
    t = admin.get(f"/importar/recibos/periodos?ligacao={ids['Ana']}").text
    assert falso[-1]["document_type__id"] == 5 and "correspondent__id" not in falso[-1]
    assert "documents/3/details" in t and "2025-12" in t


def test_tipo_ou_empregador_inexistente(admin):
    _ligacao(admin, nome="Errada", tipo_documento="Não existe")
    _ligacao(admin, nome="Errada 2", correspondente="Ninguém")
    r = admin.get("/importar/recibos/periodos?ligacao=1")
    assert r.status_code == 400 and "Não há o tipo de documento «Não existe»" in r.text
    r = admin.get("/importar/recibos/periodos?ligacao=2")
    assert r.status_code == 400 and "Não há o correspondente «Ninguém»" in r.text


def test_valores_so_na_entidade_da_ligacao(admin):
    ids = _ids_entidades(admin.app_)
    _post(admin, f"/contas/nova?entidade={ids['Família']}", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    with admin.app_.state.SessionLocal() as db:
        _credito(db, date(2025, 12, 22), 229037)
        db.commit()
    _ligacao(admin, nome="Mãe", tipo_documento="Recibo Ana", entidade=str(ids["Mãe"]))
    _ligacao(admin, nome="Família", tipo_documento="Recibo Ana", entidade=str(ids["Família"]))
    t = admin.get("/importar/recibos")
    r = admin.post("/importar/recibos/valores", data={"csrf": _csrf(t), "acao": "gravar", "ligacao": "1"})
    assert r.status_code == 200 and "sem movimento" in r.text                 # o crédito está na Família, não na Mãe
    r = admin.post("/importar/recibos/valores", data={"csrf": _csrf(t), "acao": "gravar", "ligacao": "2", "forcar": "sim"})
    with admin.app_.state.SessionLocal() as db:
        assert [x.paperless_id for x in db.scalars(select(DocumentoLigado))] == [3]


def test_quem_nao_e_admin(admin):
    _ligacao(admin, nome="Do admin")
    _post(admin, "/admin/utilizadores/novo", nome="Ana", login="ana", password=PW, password2=PW)
    ana = TestClient(admin.app_)
    _post(ana, "/login", login="ana", password=PW)
    assert ana.get("/admin/paperless/recibos").status_code == 403
    assert "Do admin" not in ana.get("/importar/recibos").text                 # só vê as ligações dela
    assert ana.get("/importar/recibos/periodos?ligacao=1").status_code == 403
