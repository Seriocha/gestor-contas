"""Cobertura dos dados e data de início da análise (movimentos anteriores ignorados)."""
import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import cobertura
from app.main import create_app
from app.models import Entidade
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Activo", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    _post(c, "/contas/nova?entidade=1", nome="Fundo", tipo="INVESTIMENTO", moeda="EUR", instituicao_id="3")
    _post(c, "/contas/nova?entidade=1", nome="Vazia", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _mov(c, 1, "2026-01-10", "1.000,00", "RECEITA JANEIRO")
    _mov(c, 1, "2026-05-10", "500,00", "RECEITA MAIO")
    _mov(c, 2, "2026-03-15", "200,00", "RECEITA MARCO")
    _mov(c, 2, "2026-06-01", "50,00", "COMPRA JUNHO", sentido="saida")
    _mov(c, 3, "2025-12-01", "10,00", "GANHO")                 # investimento: não conta para a sugestão
    return c


def test_calculo_e_sugestao(admin, app):
    with app.state.SessionLocal() as db:
        cob = cobertura.calcular(db, 1, date(2026, 10, 16))
    linhas = {l.conta.nome: l for l in cob.linhas}
    assert (linhas["Santander"].primeiro, linhas["Santander"].ultimo, linhas["Santander"].n) == (
        date(2026, 1, 10), date(2026, 5, 10), 2)
    assert linhas["Vazia"].primeiro is None and not linhas["Fundo"].conta_para_sugestao
    assert cob.sugestao == date(2026, 4, 1) and cob.limitada_por.conta.nome == "Activo"
    assert cob.ini == date(2025, 12, 1) and cob.linhas[0].conta.nome == "Fundo" and cob.linhas[-1].conta.nome == "Vazia"
    assert 0 <= linhas["Activo"].esq < 100 and cob.marcas


def test_pagina_e_corte(admin, app):
    p = admin.get("/contas/cobertura?entidade=1").text
    assert "Sugestão: <strong>01-04-2026</strong>" in p and "cron-barra" in p and 'value="2026-04-01"' in p
    assert "Cobertura dos dados" in admin.get("/contas").text

    antes = admin.get("/relatorios?entidade=1&periodo=personalizado&de=2026-01-01&ate=2026-06-30").text
    assert "Receitas</span><strong>1.700,00" in antes
    r = _post(admin, "/contas/cobertura?entidade=1", "/contas/cobertura", entidade="1", data_inicio="2026-04-01")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.get(Entidade, 1).data_inicio == date(2026, 4, 1)

    depois = admin.get("/relatorios?entidade=1&periodo=personalizado&de=2026-01-01&ate=2026-06-30").text
    assert "Receitas</span><strong>500,00" in depois and "anteriores a 01-04-2026 ignorados" in depois
    meses = re.findall(r'<option value="(\d{4}-\d{2})"', admin.get("/relatorios?entidade=1&periodo=mes").text)
    assert meses and min(meses) == "2026-04"
    painel = admin.get("/?entidade=1").text
    dados = json.loads(re.search(r'id="dados-graficos">(.*?)</script>', painel, re.S).group(1))
    assert all(d >= "2026-04-01" for d in dados["patrimonio"]["datas"]) and "Análise de" in painel
    # «o que mudou» começa no corte; os saldos não mudam
    v = admin.get("/painel/variacao?entidade=1&de=2026-01-01&ate=2026-06-30").text
    assert "RECEITA JANEIRO" not in v and "RECEITA MAIO" in v
    assert "1.500,00 €" in admin.get("/contas").text           # Santander: 1000 + 500

    _post(admin, "/contas/cobertura?entidade=1", "/contas/cobertura", entidade="1", acao="remover")
    with app.state.SessionLocal() as db:
        assert db.get(Entidade, 1).data_inicio is None
    assert "Receitas</span><strong>1.700,00" in admin.get(
        "/relatorios?entidade=1&periodo=personalizado&de=2026-01-01&ate=2026-06-30").text
