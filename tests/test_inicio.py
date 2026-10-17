"""Página Início: património (ativos, passivos, líquido) com os bens e a agenda das próximas tarefas."""
import json
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import inicio
from app.main import create_app
from app.models import AvaliacaoBem, Bem, Entidade, Subscricao
from tests.test_app import PW, _post
from tests.test_previsoes import _mov

HOJE = date.today()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    r = _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1",
          saldo_inicial="5.000,00", data_saldo_inicial=(HOJE - timedelta(days=400)).isoformat())
    _post(c, "/contas/nova?entidade=1", nome="Cartão", tipo="CARTAO_CREDITO", moeda="EUR", instituicao_id="1",
          conta_pagamento_id="1", dia_fecho="5", dia_pagamento="20")
    _mov(c, 2, (HOJE - timedelta(days=2)).isoformat(), "300,00", "COMPRA LOJA", sentido="saida")
    return c


def test_login_vai_para_o_inicio(app):
    c = TestClient(app)
    r = _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    c.post("/logout", data={})
    f = c.get("/login")
    from tests.test_app import _csrf
    r = c.post("/login", data={"csrf": _csrf(f), "login": "admin", "password": PW, "next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/inicio"


def test_patrimonio_com_bens_e_agenda(admin, app):
    r = _post(admin, "/patrimonio/novo?entidade=1&tipo=casa", nome="Casa", uso="principal", tipologia="T2",
              localizacao="Lisboa", revisao_meses="0", valor_aquisicao="200.000,00",
              data_aquisicao=(HOJE - timedelta(days=800)).isoformat())
    casa = int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))
    r = _post(admin, "/patrimonio/novo?entidade=1&tipo=carro", nome="Golf", uso="principal", revisao_meses="12")
    carro = int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))
    with app.state.SessionLocal() as db:
        db.add(AvaliacaoBem(bem_id=casa, data=HOJE - timedelta(days=30), valor=25000000, fonte="manual"))
        db.add(Subscricao(entidade_id=1, nome="Netflix", valor=1399, periodicidade="mensal",
                          dia_pagamento=(HOJE + timedelta(days=5)).day, data_inicio=HOJE - timedelta(days=60)))
        db.commit()
        ent = db.get(Entidade, 1)
        v = inicio.visao(db, ent, HOJE)
        assert v.hoje["ativos"] == 500000 + 25000000 and v.hoje["passivos"] == 30000
        assert v.hoje["liquido"] == 500000 + 25000000 - 30000
        assert dict(v.composicao)["Casas"] == 25000000 and v.liquido[0] < v.liquido[-1]   # a avaliação subiu o valor
        proximos, depois = inicio.agenda(db, [ent], app.state.prefs, HOJE)
        titulos = [i.titulo for i in proximos]
        assert "Netflix" in titulos and "Pagamento do cartão · Cartão" in titulos
        assert "Registar os km · Golf" in titulos and "Rever o valor · Golf" in titulos   # sem avaliação → pendente
        assert proximos[0].atrasado                                                        # atrasados primeiro
    p = admin.get("/inicio?entidade=1").text
    assert "Património líquido" in p and 'data-grafico="inicio_evolucao"' in p and "Agenda" in p and "Netflix" in p
    dados = json.loads(re.search(r'id="dados-graficos">(.*?)</script>', p, re.S).group(1))
    assert dados["inicio_composicao"][0]["nome"] == "Casas"
    assert 'href="/inicio"' in p                                                       # menu / logótipo


def test_cortica_e_heranca_no_inicio(admin, app):
    _post(admin, "/admin/entidades/2", nome="Herança", tipo="heranca", quota="1/6", quota_futura="1/2",
          quota_futura_nota="Por morte da mãe", modulos=["imoveis"], modulos_presentes="1")
    _post(admin, "/patrimonio/novo?entidade=2&tipo=rustico", nome="Monte", uso="exploracao", area_ha="100",
          localizacao="Ourique", tem_cortica="1", anos_tiragem=str(HOJE.year - 9 + 1), revisao_meses="0",
          valor_aquisicao="600.000,00")
    with app.state.SessionLocal() as db:
        proximos, depois = inicio.agenda(db, [db.get(Entidade, 1), db.get(Entidade, 2)], app.state.prefs, HOJE)
        todos = proximos + depois
        cortica = next(i for i in todos if i.titulo.startswith("Tiragem da cortiça"))
        assert cortica.data == date(HOJE.year + 1, 6, 1) and cortica.entidade == "Herança"
    p = admin.get("/inicio?entidade=2").text
    assert "a tua parte (1/6): 100.000,00 €" in p and "Por morte da mãe (1/2): 300.000,00 €" in p
    assert "Por entidade" in p
