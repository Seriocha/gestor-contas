"""Entidades editáveis (pessoal / herança com quota) e propriedades rústicas com cortiça."""
import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import ia
from app import patrimonio as pt
from app.main import create_app
from app.models import Bem, Entidade
from tests.test_app import PW, _post
from tests.test_previsoes import _mov

HOJE = date(2026, 10, 17)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def test_editar_entidade_heranca(admin, app):
    p = admin.get("/admin/entidades").text
    assert 'href="/admin/entidades/2"' in p
    r = _post(admin, "/admin/entidades/2", nome="Mãe", tipo="heranca", quota="")
    assert r.status_code == 400 and "percentagem" in r.text
    r = _post(admin, "/admin/entidades/2", nome="Família", tipo="pessoal")
    assert r.status_code == 400 and "Já existe" in r.text
    r = _post(admin, "/admin/entidades/2", nome="Herança Monte Silva", descricao="Partilhas", tipo="heranca", quota="33,33")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        e = db.get(Entidade, 2)
        assert (e.nome, e.tipo, e.quota) == ("Herança Monte Silva", "heranca", 33.33) and e.parte(30000) == 9999
        assert db.get(Entidade, 1).parte(30000) == 30000
    assert "33,33%" in admin.get("/admin/entidades").text
    _post(admin, "/admin/entidades", "/admin/entidades", nome="Herança Avós", tipo="heranca", quota="25")
    with app.state.SessionLocal() as db:
        assert db.query(Entidade).filter_by(nome="Herança Avós").one().quota == 25
    assert "a tua parte (33,33%)" in admin.get("/?entidade=2").text


def _rustico(c, **extra):
    base = {"nome": "Herdade do Monte", "uso": "exploracao", "area_ha": "120,5", "artigo": "45 B",
            "localizacao": "Santana da Serra, Ourique", "ocupacao": ["montado_sobro", "pastagem"], "agua": ["charca"],
            "acesso": "terra_bom", "tem_cortica": "1", "anos_tiragem": "2008 2017; 2026", "sobreiros": "4 500",
            "producao_arrobas": "3200", "revisao_meses": "12", "padroes": "VENDA CORTICA"}
    r = _post(c, "/patrimonio/novo?entidade=2&tipo=rustico", **{**base, **extra})
    assert r.status_code == 303, r.text
    return int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))


def test_propriedade_rustica_cortica_e_ia(admin, app, monkeypatch):
    _post(admin, "/admin/entidades/2", nome="Herança", tipo="heranca", quota="25")
    _post(admin, "/contas/nova?entidade=2", nome="Conta herança", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _mov(admin, 1, "2026-09-10", "8.000,00", "VENDA CORTICA LDA", sentido="entrada")
    _mov(admin, 1, "2026-09-12", "40,00", "CONDOMINIO", sentido="saida")
    bid = _rustico(admin)
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        assert (b.area_ha, b.tiragens, b.proxima_tiragem, b.sobreiros, b.omissao) == (120.5, [2008, 2017, 2026], 2035, 4500, False)
        assert b.lista_ocupacao == ["montado_sobro", "pastagem"] and b.anos_tiragem == "2008, 2017, 2026"
        txt = pt.descricao_para_ia(b)
        assert "Prédio rústico com 120,5 ha" in txt and "Montado de sobro" in txt and "próxima prevista em 2035" in txt
        assert "Herdade" not in txt and pt.falta_para_ia(b) == []
    monkeypatch.setattr(ia, "disponivel", lambda: True)
    monkeypatch.setattr(ia, "_chamar", lambda *a, **k: json.dumps(
        {"valor": 900000, "minimo": 800000, "maximo": 1000000, "preco_ha": 7500, "justificacao": "Montado.", "confianca": 0.5}))
    with app.state.SessionLocal() as db:
        a = pt.avaliar_ia(db, db.get(Bem, bid), HOJE)
        db.commit()
        assert a.valor == 90000000 and "7500 €/ha" in a.justificacao
    p = admin.get(f"/patrimonio/{bid}").text
    assert "próxima em 2035" in p and "faltam 9 anos" in p and "Montado de sobro" in p and "a tua parte (25%): 225.000,00 €" in p
    lista = admin.get("/patrimonio?entidade=2").text
    assert "Propriedades rústicas" in lista and "próxima em 2035" in lista and "é uma herança" in lista
    assert "recebeu 8.000,00 €" in lista                                   # a venda da cortiça (padrão) conta no prédio
    r = _post(admin, "/patrimonio/novo?entidade=2&tipo=rustico", nome="X", area_ha="abc", anos_tiragem="17")
    assert r.status_code == 400 and "hectares" in r.text and "tiragem" in r.text
