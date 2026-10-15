"""Contas de investimento: natureza dos movimentos (aplicação/resgate/ganho/perda) e desempenho."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import investimentos
from app.main import create_app
from app.models import Conta, Movimento
from tests.test_app import PW, _csrf, _post


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


def _mov(c, natureza, data, valor, desc):
    f = c.get("/contas/1/movimentos/novo")
    r = c.post("/contas/1/movimentos/novo", data={"csrf": _csrf(f), "natureza": natureza, "data": data,
                                                   "valor": valor, "descricao": desc}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(re.search(r"/movimentos/(\d+)", r.headers["location"]).group(1))


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Fundo Agressivo", tipo="INVESTIMENTO", moeda="EUR", instituicao_id="3")
    _mov(c, "aplicacao", "2026-01-05", "1.000,00", "Aplicação no fundo")
    _mov(c, "ganho", "2026-02-28", "50,00", "Valorização fevereiro")
    _mov(c, "perda", "2026-03-31", "20,00", "Desvalorização março")
    _mov(c, "resgate", "2026-04-10", "200,00", "Resgate parcial")
    p = c.get("/contas/1/movimentos")
    c.post("/contas/1/retificar", data={"csrf": _csrf(p), "data": "2026-05-31", "saldo": "900,00"})
    return c


def test_natureza_e_qualificacao(admin, app):
    with app.state.SessionLocal() as db:
        movs = {m.descricao_original: m for m in db.query(Movimento)}
        assert movs["Aplicação no fundo"].transferencia and movs["Aplicação no fundo"].montante == 100000
        assert movs["Resgate parcial"].transferencia and movs["Resgate parcial"].montante == -20000
        assert movs["Valorização fevereiro"].categoria.nome == "Rendimentos de investimento"
        assert movs["Desvalorização março"].montante == -2000 and movs["Desvalorização março"].categoria.natureza == "RECEITA"
        assert [investimentos.natureza(movs[k]) for k in ("Aplicação no fundo", "Resgate parcial", "Valorização fevereiro",
                                                          "Desvalorização março")] == ["aplicacao", "resgate", "ganho", "perda"]


def test_desempenho(admin, app):
    with app.state.SessionLocal() as db:
        d = investimentos.desempenho(db, db.get(Conta, 1), date(2026, 6, 15))
    # 1000 − 200 = 800 de capital; valor real 900 (retificação: +70 por explicar = ganho de mercado)
    assert (d.valor, d.capital, d.resultado, d.aplicado_total, d.resgatado_total) == (90000, 80000, 10000, 100000, 20000)
    assert round(d.rentabilidade, 2) == 12.5
    assert d.resultado_ano == 5000 - 2000 + 7000
    mai = next(x for x in d.meses if x.mes == date(2026, 5, 1))
    assert (mai.resultado, mai.capital_fim, mai.valor_fim) == (7000, 80000, 90000)
    jan = next(x for x in d.meses if x.mes == date(2026, 1, 1))
    assert (jan.aplicacoes, jan.capital_fim, jan.valor_fim) == (100000, 100000, 100000)


def test_pagina_da_conta(admin):
    p = admin.get("/contas/1/movimentos").text
    assert "Desempenho" in p and 'data-grafico="investimento"' in p and '"capital":' in p
    assert "+100,00 €" in p and "12,50% sobre o capital" in p
    for n in ("natureza-aplicacao", "natureza-resgate", "natureza-ganho", "natureza-perda"):
        assert n in p
    f = admin.get("/contas/1/movimentos/novo").text
    assert 'name="natureza" value="ganho"' in f and 'name="transferencia"' not in f


def test_relatorio_ganhos_e_perdas_sao_receita(admin):
    r = admin.get("/relatorios?periodo=personalizado&de=2026-01-01&ate=2026-04-30").text
    receitas = r.split("Receitas por categoria")[1]
    assert "Rendimentos de investimento" in receitas and "30,00 €" in receitas   # 50 − 20
    despesas = r.split("Despesas por categoria")[1].split("Receitas por categoria")[0]
    assert "Rendimentos de investimento" not in despesas and "1.000,00" not in despesas  # aplicação não é despesa


def test_conta_a_ordem_mantem_formulario_normal(admin):
    _post(admin, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    f = admin.get("/contas/2/movimentos/novo").text
    assert 'name="sentido"' in f and 'name="natureza"' not in f
    assert "Desempenho" not in admin.get("/contas/2/movimentos").text
