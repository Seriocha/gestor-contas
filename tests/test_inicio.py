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
    comp = {x["nome"]: x["valor"] for x in dados["inicio_composicao"]}
    assert comp == {"Ativos bancários": 5000.0, "Património imóvel": 250000.0}       # o carro sem valor não entra
    assert "Detalhe dos ativos" in p and "Contas à ordem" in p and "este mês" in p and "Passivos" in p
    assert 'data-grafico="inicio_linha"' in p and 'class="tile"' in p
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
    assert "A tua parte · 1/6</span><strong>100.000,00 €" in p and "Por morte da mãe · 1/2</span><strong>300.000,00 €" in p
    assert "Património por entidade</h2>" in p
    # a agenda é só da entidade escolhida: a cortiça da herança não aparece no Início da família
    agenda = lambda html: html[html.index('id="agenda"'):]
    assert "Tiragem da cortiça" in agenda(p)
    p1 = admin.get("/inicio?entidade=1").text
    assert "Tiragem da cortiça" not in agenda(p1) and "· Herança" not in agenda(p1)


def test_heranca_no_inicio_da_familia_e_preferencia(admin, app):
    from tests.test_app import _csrf
    _post(admin, "/admin/entidades/2", nome="Herança", tipo="heranca", quota="1/6", quota_futura="1/2",
          quota_futura_nota="Por morte da mãe", modulos=["imoveis"], modulos_presentes="1")
    _post(admin, "/patrimonio/novo?entidade=2&tipo=rustico", nome="Monte", uso="exploracao", area_ha="100",
          localizacao="Ourique", revisao_meses="0", valor_aquisicao="600.000,00")
    familia = admin.get("/inicio?entidade=1").text
    assert "herancas-transpostas" in familia and "Herança · 1/2" in familia and "300.000,00 €" in familia
    assert "Família + herança" in familia
    assert "este mês" in familia and 'class="var-chip' in familia
    # preferência desligada: nada da minha parte, nem a herança transposta
    f = admin.get("/admin/utilizadores")
    admin.post("/admin/preferencias", data={"csrf": _csrf(f), "tarefas_ativas": "1", "avaliacao_automatica": "1"})
    assert app.state.prefs["mostrar_minha_parte"] == "0"
    assert "herancas-transpostas" not in admin.get("/inicio?entidade=1").text
    assert "A tua parte" not in admin.get("/inicio?entidade=2").text


def test_layout_personalizado_por_utilizador(admin):
    from app import layout_inicio
    r = admin.get("/inicio?entidade=1")
    assert 'id="personalizar"' in r.text and "inicio_layout.js" in r.text
    ordem = re.findall(r'data-bloco="(\w+)"', r.text)
    assert ordem[:5] == ["topo", "composicao", "detalhe", "evolucao", "agenda"]
    csrf = re.search(r'data-csrf="([^"]+)"', r.text).group(1)
    novo = [{"id": "agenda", "w": 4, "on": True}, {"id": "topo", "w": 8, "on": True},
            {"id": "composicao", "w": 6, "on": False}, {"id": "xpto", "w": 6}, {"id": "agenda", "w": 12}]
    r = admin.post("/inicio/layout", data={"csrf": csrf, "layout": json.dumps(novo)})
    assert r.json()["ok"]
    r = admin.get("/inicio?entidade=1")
    ordem = re.findall(r'data-bloco="(\w+)"', r.text)
    assert ordem[:3] == ["agenda", "topo", "composicao"] and "xpto" not in ordem
    assert 'data-bloco="agenda" data-w="4"' in r.text
    assert 'class="bloco-inicio oculto" data-bloco="composicao"' in r.text
    # largura inválida → a de omissão; blocos em falta voltam no fim
    assert layout_inicio.normalizar([{"id": "detalhe", "w": 5}])[0] == {"id": "detalhe", "w": 8, "on": True}
    assert [b["id"] for b in layout_inicio.normalizar([{"id": "agenda"}])][-1] == "entidades"
    # JSON inválido e CSRF errado são recusados
    assert admin.post("/inicio/layout", data={"csrf": csrf, "layout": "{x"}).status_code == 400
    assert admin.post("/inicio/layout", data={"csrf": "mau", "layout": ""}).status_code == 400
    # repor
    assert admin.post("/inicio/layout", data={"csrf": csrf, "layout": ""}).json()["ok"]
    r = admin.get("/inicio?entidade=1")
    assert re.findall(r'data-bloco="(\w+)"', r.text)[:2] == ["topo", "composicao"]
