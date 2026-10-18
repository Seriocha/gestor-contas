"""Página de registo de abastecimentos sem login (com código secreto) e validação com login."""
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Abastecimento, AbastecimentoPendente, Bem, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

ONTEM = (date.today() - timedelta(days=1)).isoformat()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    r = _post(c, "/patrimonio/novo?entidade=1&tipo=carro", nome="Frontera", uso="principal", matricula="BU-07-US",
              revisao_meses="0")
    assert r.status_code == 303
    _mov(c, 1, ONTEM, "84,91", "INTERAZOIA", sentido="saida")
    return c


def _link(c):
    f = c.get("/patrimonio/abastecimentos?entidade=1")
    c.post("/patrimonio/abastecimentos/link", data={"csrf": _csrf(f), "acao": "gerar"}, follow_redirects=False)
    p = c.get("/patrimonio/abastecimentos?entidade=1").text
    return re.search(r"/abastecer/([\w-]+)", p).group(1)


def test_sem_link_nao_ha_pagina(app):
    anonimo = TestClient(app)
    assert anonimo.get("/abastecer/qualquer").status_code == 404
    assert anonimo.get("/abastecer/").status_code == 404


def test_registar_sem_login_e_validar(admin, app):
    codigo = _link(admin)
    anonimo = TestClient(app)                                    # sem sessão
    assert anonimo.get("/abastecer/errado").status_code == 404
    pg = anonimo.get(f"/abastecer/{codigo}")
    assert pg.status_code == 200 and "ab-carro-nome\">Frontera" in pg.text and "BU-07-US" in pg.text and "Saldo" not in pg.text
    bid = re.search(r'name="bem_id" value="(\d+)"', pg.text).group(1)
    r = anonimo.post(f"/abastecer/{codigo}", data={"csrf": _csrf(pg), "bem_id": bid, "data": ONTEM, "hora": "20:03",
                                                   "km": "18 064", "litros": "41,04", "valor": "84,91", "cheio": "1",
                                                   "posto": "Intermarché", "quem": "Ana"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Fica por validar" in anonimo.get(r.headers["location"]).text
    erro = anonimo.post(f"/abastecer/{codigo}", data={"csrf": _csrf(pg), "bem_id": bid, "litros": "x", "valor": "1"})
    assert erro.status_code == 400 and "litros" in erro.text
    with app.state.SessionLocal() as db:
        assert db.query(AbastecimentoPendente).count() == 1 and db.query(Abastecimento).count() == 0
    # nada conta antes de validar; a lista avisa
    assert "1 abastecimento" in admin.get("/patrimonio?entidade=1").text
    v = admin.get("/patrimonio/abastecimentos?entidade=1")
    assert "registado" in v.text and "por Ana" in v.text and "2,069 €/l" in v.text
    pid = re.search(r"/patrimonio/abastecimentos/(\d+)/validar", v.text).group(1)
    r = admin.post(f"/patrimonio/abastecimentos/{pid}/validar", data={
        "csrf": _csrf(v), "data": ONTEM, "hora": "20:03", "km": "18064", "litros": "41,04", "valor": "84,91",
        "cheio": "1", "posto": "Intermarché"}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        a = db.scalar(select(Abastecimento))
        assert a.guid == f"web:{pid}" and a.valor == 8491 and "registado por Ana" in a.notas
        assert a.movimento.descricao_original == "INTERAZOIA"            # ligado ao débito
        b = db.get(Bem, a.bem_id)
        assert b.km == 18064 and db.scalar(select(Movimento).where(Movimento.descricao_original == "INTERAZOIA")).bem_id == b.id
        assert db.query(AbastecimentoPendente).count() == 0


def test_rejeitar_e_link_novo_invalida(admin, app):
    codigo = _link(admin)
    anonimo = TestClient(app)
    pg = anonimo.get(f"/abastecer/{codigo}")
    bid = re.search(r'name="bem_id" value="(\d+)"', pg.text).group(1)
    anonimo.post(f"/abastecer/{codigo}", data={"csrf": _csrf(pg), "bem_id": bid, "data": ONTEM, "litros": "10",
                                               "valor": "20"})
    v = admin.get("/patrimonio/abastecimentos?entidade=1")
    pid = re.search(r"/patrimonio/abastecimentos/(\d+)/validar", v.text).group(1)
    admin.post(f"/patrimonio/abastecimentos/{pid}/rejeitar", data={"csrf": _csrf(v)})
    with app.state.SessionLocal() as db:
        assert db.query(AbastecimentoPendente).count() == 0 and db.query(Abastecimento).count() == 0
    novo = _link(admin)
    assert novo != codigo and anonimo.get(f"/abastecer/{codigo}").status_code == 404
    assert anonimo.get(f"/abastecer/{novo}").status_code == 200
    # quem não é administrador não mexe no link
    f = admin.get("/patrimonio/abastecimentos?entidade=1")
    admin.post("/patrimonio/abastecimentos/link", data={"csrf": _csrf(f), "acao": "desligar"})
    assert anonimo.get(f"/abastecer/{novo}").status_code == 404


def test_duas_viaturas_e_diagnostico(admin, app):
    from datetime import date, timedelta
    _post(admin, "/patrimonio/novo?entidade=1&tipo=carro", nome="Carro da empresa", uso="empresa", matricula="AC-71-ZE",
          data_venda=(date.today() + timedelta(days=200)).isoformat(), revisao_meses="0")        # devolução no futuro
    _post(admin, "/patrimonio/novo?entidade=1&tipo=carro", nome="Velho", data_venda="2020-01-01", revisao_meses="0")
    codigo = _link(admin)
    pg = TestClient(app).get(f"/abastecer/{codigo}").text
    assert "Frontera" in pg and "Carro da empresa" in pg and "Velho" not in pg
    v = admin.get("/patrimonio/abastecimentos?entidade=1").text
    assert "Carros na página" in v and "Velho</a> não aparece: vendido em 01/01/2020" in v


def test_ler_talao_por_fotografia(admin, app, monkeypatch):
    import json
    from app import ia
    codigo = _link(admin)
    anonimo = TestClient(app)
    pg = anonimo.get(f"/abastecer/{codigo}")
    assert "Fotografar o talão" not in pg.text                       # sem chave da API não aparece
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    enviado = {}

    def falso(sistema, conteudo, **k):
        enviado["conteudo"] = conteudo
        return json.dumps({"data": ONTEM, "hora": "20:03", "litros": "41.04", "preco_litro": 2.069, "valor": None,
                           "posto": "Intermarché Santa Iria", "combustivel": "gasóleo", "km": None, "confianca": 0.9})
    monkeypatch.setattr(ia, "_chamar", falso)
    pg = anonimo.get(f"/abastecer/{codigo}")
    assert "Fotografar o talão" in pg.text
    r = anonimo.post(f"/abastecer/{codigo}/ler", data={"csrf": _csrf(pg)},
                     files={"talao": ("t.jpg", b"fake-jpeg", "image/jpeg")})
    d = r.json()
    assert d["ok"] and d["campos"]["litros"] == 41.04 and d["campos"]["valor"] == 84.91   # valor = litros × €/l
    assert d["campos"]["data"] == ONTEM and d["campos"]["posto"] == "Intermarché Santa Iria"
    assert enviado["conteudo"][1]["type"] == "image"
    r = anonimo.post(f"/abastecer/{codigo}/ler", data={"csrf": _csrf(pg)})
    assert r.status_code == 400 and "Falta a fotografia" in r.json()["erro"]
    assert anonimo.post("/abastecer/errado/ler", data={"csrf": _csrf(pg)}).status_code == 404
