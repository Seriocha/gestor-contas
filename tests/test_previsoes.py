"""Receitas previstas: ordenados e cartões refeição simulados no fluxo de caixa do mês."""
import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import previsoes
from app.main import create_app
from app.models import ReceitaPrevista
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


def _mov(c, conta, data, valor, desc, sentido="entrada"):
    f = c.get(f"/contas/{conta}/movimentos/novo")
    r = c.post(f"/contas/{conta}/movimentos/novo", data={"csrf": _csrf(f), "sentido": sentido, "data": data,
                                                         "valor": valor, "descricao": desc}, follow_redirects=False)
    assert r.status_code == 303, r.text


def _sankey(html):
    dados = json.loads(re.search(r'id="dados-graficos">(.*?)</script>', html, re.S).group(1))
    return {f["from"]: f["flow"] for f in dados["sankey"] if f["to"] == "Rendimento"}


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Activo", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    _post(c, "/contas/nova?entidade=1", nome="Caixa Break", tipo="CARTAO_REFEICAO", moeda="EUR", instituicao_id="7")
    _mov(c, 1, "2026-07-23", "2.000,00", "TRF VENCIMENTO EMPRESA X")
    _mov(c, 1, "2026-08-21", "2.100,00", "TRF VENCIMENTO EMPRESA X")
    _mov(c, 2, "2026-08-25", "2.190,39", "TRANSFERENCIA - VENCIMENTO")
    _mov(c, 2, "2026-09-25", "2.190,39", "TRANSFERENCIA - VENCIMENTO")
    _mov(c, 1, "2026-09-10", "300,00", "COMPRA CONTINENTE", sentido="saida")
    return c


def _criar(c, **dados):
    base = {"ativa": "1", "valor": "", "padrao": "", "categoria_id": ""}
    r = _post(c, "/previsoes/nova?entidade=1", **{**base, **dados})
    assert r.status_code == 303, r.text


@pytest.fixture
def com_previsoes(admin, app):
    _criar(admin, descricao="Ordenado Santander", conta_id="1", dia="23", padrao="VENCIMENTO")
    _criar(admin, descricao="Ordenado Activo", conta_id="2", dia="25", valor="2.190,39", padrao="VENCIMENTO")
    with app.state.SessionLocal() as db:
        cat = previsoes.categoria_refeicao(db).id
        db.commit()
    _criar(admin, descricao="Cartão refeição", conta_id="3", dia="23", valor="198,00", categoria_id=str(cat))
    return admin


def test_estado_do_mes(com_previsoes, app):
    with app.state.SessionLocal() as db:
        est = {e.prev.descricao: e for e in previsoes.estado_mes(db, 1, date(2026, 9, 1))}
        assert est["Ordenado Santander"].recebida is None
        assert est["Ordenado Santander"].valor == 205000           # média de 2.000 e 2.100
        assert est["Ordenado Santander"].data == date(2026, 9, 23)
        assert est["Ordenado Activo"].recebida.data_mov == date(2026, 9, 25)
        assert est["Cartão refeição"].pendente and est["Cartão refeição"].valor == 19800
        # agosto: o ordenado do Santander entrou a 21 (dia útil antes) e conta como recebido
        ago = {e.prev.descricao: e for e in previsoes.estado_mes(db, 1, date(2026, 8, 1))}
        assert ago["Ordenado Santander"].recebida.data_mov == date(2026, 8, 21)


def test_fluxo_do_mes_soma_previstas(com_previsoes):
    p = com_previsoes.get("/?entidade=1&periodo=mes").text
    assert "Receitas do mês" in p and "inclui 2.248,00 € previstos" in p
    assert "4.438,39 €" in p                                  # 2.190,39 real + 2.050 + 198 previstos
    fluxos = _sankey(p)
    assert fluxos["Ordenado Santander (previsto)"] == 2050 and fluxos["Cartão refeição (previsto)"] == 198
    assert "em atraso" in p and "recebida a 25-09-2026" in p

    real = com_previsoes.get("/?entidade=1&periodo=mes&previstas=0").text
    assert "previstos" not in real.split("Taxa de poupança")[0].split("Fluxo de caixa")[1]
    assert not any("(previsto)" in k for k in _sankey(real))
    # a escolha fica memorizada; noutros períodos não há simulação
    assert "(previsto)" not in json.dumps(_sankey(com_previsoes.get("/?entidade=1&periodo=mes").text))
    assert "Receitas do mês" not in com_previsoes.get("/?entidade=1&periodo=trimestre&previstas=1").text


def test_pagina_e_edicao(com_previsoes, app):
    lista = com_previsoes.get("/previsoes?entidade=1").text
    assert "Ordenado Santander" in lista and "≈ 2.050,00 €" in lista and "✓ recebida 25-09-2026" in lista
    with app.state.SessionLocal() as db:
        pid = db.query(ReceitaPrevista).filter_by(descricao="Cartão refeição").one().id
    r = _post(com_previsoes, f"/previsoes/{pid}", descricao="Cartão refeição", conta_id="3", dia="23",
              valor="", padrao="", categoria_id="")
    assert r.status_code == 400 and "Indica o valor" in r.text
    r = _post(com_previsoes, f"/previsoes/{pid}", descricao="Cartão refeição", conta_id="3", dia="32", valor="1")
    assert r.status_code == 400 and "entre 1 e 31" in r.text
    r = _post(com_previsoes, f"/previsoes/{pid}", f"/previsoes/{pid}/apagar")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.get(ReceitaPrevista, pid) is None


def test_sem_permissao_de_edicao(com_previsoes):
    _post(com_previsoes, "/admin/utilizadores/novo", nome="Leitora", login="leitora", password=PW, password2=PW)
    c = TestClient(com_previsoes.app)
    _post(c, "/login", login="leitora", password=PW)
    assert c.get("/previsoes/nova?entidade=1").status_code in (403, 404)
