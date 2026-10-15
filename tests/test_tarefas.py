"""Tarefas de importação do mês, lembrete por dias do mês, escolha do mês nos relatórios."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import tarefas
from app.main import create_app
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


def test_janela_e_ciclo():
    p = {"tarefas_ativas": "1", "tarefas_dia_ini": "25", "tarefas_dia_fim": "5"}
    assert tarefas.na_janela(p, date(2026, 10, 25)) and tarefas.na_janela(p, date(2026, 11, 3))
    assert not tarefas.na_janela(p, date(2026, 10, 15))
    assert not tarefas.na_janela({**p, "tarefas_ativas": "0"}, date(2026, 10, 26))
    assert tarefas.na_janela({**p, "tarefas_dia_ini": "10", "tarefas_dia_fim": "20"}, date(2026, 10, 15))
    assert tarefas.inicio_ciclo(p, date(2026, 10, 26)) == date(2026, 10, 25)
    assert tarefas.inicio_ciclo(p, date(2026, 11, 3)) == date(2026, 10, 25)
    assert tarefas.inicio_ciclo({**p, "tarefas_dia_ini": "31"}, date(2026, 3, 5)) == date(2026, 2, 28)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Refeição", tipo="CARTAO_REFEICAO", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="BiG ordem", tipo="ORDEM", moeda="EUR", instituicao_id="3")
    _post(c, "/contas/nova?entidade=1", nome="Fundo", tipo="INVESTIMENTO", moeda="EUR", instituicao_id="3")
    _post(c, "/contas/nova?entidade=1", nome="Break", tipo="CARTAO_REFEICAO", moeda="EUR", instituicao_id="7")
    return c


def test_ecra_de_tarefas(admin, app):
    _mov(admin, 3, "2026-09-10", "10,00", "DEPOSITO")          # BiG ordem atualizada agora
    with app.state.SessionLocal() as db:
        bancos = {b.nome: b for b in tarefas.do_ciclo(db, 1, date(2026, 1, 1))}
    sant = {t.conta.nome: t for t in bancos["Santander"].tarefas}
    assert sant["Santander ordem"].ficha["onde"].startswith("NetBanco › Saldos")
    assert sant["Refeição"].ficha["extrato"] == "Cartão refeição"
    big = {t.conta.nome: t for t in bancos["Banco BiG"].tarefas}
    assert big["BiG ordem"].feita and not big["Fundo"].feita and big["Fundo"].ficha is None
    assert bancos["Caixa Geral de Depósitos"].site == "https://www.cgd.pt"
    assert "Retificar saldo" in bancos["Caixa Geral de Depósitos"].tarefas[0].nota_manual

    p = admin.get("/importar/tarefas?entidade=1").text
    assert "Abrir NetBanco Particulares" in p and 'href="https://www.santander.pt"' in p and 'rel="noopener noreferrer"' in p
    assert 'href="/importar?conta=1"' in p and "4 contas por atualizar" in p
    assert "Tarefas de importação do mês" in admin.get("/importar").text


def test_lembrete_configuravel(admin, app):
    hoje = date.today().day
    _post(admin, "/admin/utilizadores", "/admin/preferencias", tarefas_ativas="1",
          tarefas_dia_ini=str(hoje), tarefas_dia_fim=str(hoje))
    assert app.state.prefs["tarefas_dia_ini"] == str(hoje)
    painel = admin.get("/?entidade=1").text
    assert "Tarefas do mês" in painel and "contas por atualizar com os extratos" in painel
    # fora do período (ou desligado) não aparece no menu nem no painel
    outro = hoje % 28 + 1 if hoje % 28 + 1 != hoje else 2
    _post(admin, "/admin/utilizadores", "/admin/preferencias", tarefas_ativas="1",
          tarefas_dia_ini=str(outro), tarefas_dia_fim=str(outro))
    assert "Tarefas do mês" not in admin.get("/?entidade=1").text
    _post(admin, "/admin/utilizadores", "/admin/preferencias", tarefas_dia_ini="1", tarefas_dia_fim="31")
    assert app.state.prefs["tarefas_ativas"] == "0" and "Tarefas do mês" not in admin.get("/?entidade=1").text
    r = _post(admin, "/admin/utilizadores", "/admin/preferencias", tarefas_ativas="1", tarefas_dia_ini="40")
    assert r.status_code == 303 and app.state.prefs["tarefas_dia_ini"] == "1"


def test_relatorio_de_um_mes_escolhido(admin):
    _mov(admin, 1, "2026-08-10", "100,00", "RECEBIDO AGOSTO")
    _mov(admin, 1, "2026-09-10", "70,00", "RECEBIDO SETEMBRO")
    r = admin.get("/relatorios?entidade=1&periodo=mes&mes=2026-08").text
    assert "Ago 2026" in r and '<option value="2026-08" selected>' in r and '<option value="2026-09"' in r
    kpi = r.split("Receitas</span><strong>")[1][:30]
    assert kpi.startswith("100,00")
    assert "Set 2026" in admin.get("/relatorios?entidade=1&periodo=mes&mes=lixo").text   # inválido → mês dos dados


def test_contas_agrupadas_no_painel(admin):
    _mov(admin, 3, "2026-09-10", "10,00", "DEPOSITO")
    p = admin.get("/?entidade=1&agrupar=banco").text
    assert '<tr class="titulo-grupo"><th>Banco BiG</th>' in p and 'class="indentada"' in p
