"""Atividade por período, módulos e 2.º cenário da quota da entidade, contas por separador e cobertura transversal."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import viaverde as vv
from app.main import create_app
from app.models import Auditoria, Entidade, fracao
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def test_atividade_por_periodo(admin, app):
    with app.state.SessionLocal() as db:
        db.add(Auditoria(quando=datetime.now() - timedelta(days=3), acao="antiga_semana"))
        db.add(Auditoria(quando=datetime.now() - timedelta(days=20), acao="antiga_mes"))
        db.add(Auditoria(quando=datetime.now() - timedelta(days=60), acao="muito_antiga"))
        db.commit()
    dia = admin.get("/admin/utilizadores?periodo=dia").text
    semana = admin.get("/admin/utilizadores?periodo=semana").text
    mes = admin.get("/admin/utilizadores?periodo=mes").text
    assert "antiga_semana" not in dia and "antiga_semana" in semana and "antiga_mes" not in semana
    assert "antiga_mes" in mes and "muito_antiga" not in mes and "Último mês" in mes


def test_quota_em_dois_cenarios_e_modulos(admin, app):
    assert fracao(100 / 6) == "1/6" and fracao(50) == "1/2" and fracao(12.5) == "1/8"
    r = _post(admin, "/admin/entidades/2", nome="Herança", tipo="heranca", quota="1/6", quota_futura="1/2",
              quota_futura_nota="Por morte da mãe", modulos=["imoveis"], modulos_presentes="1")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        e = db.get(Entidade, 2)
        assert round(e.quota, 3) == 16.667 and e.quota_futura == 50 and e.quota_txt == "1/6"
        assert e.parte(600000) == 100000 and e.parte_futura(600000) == 300000
        assert e.tem("imoveis") and not e.tem("viaturas") and not e.tem("subscricoes")
    assert "→ 1/2" in admin.get("/admin/entidades").text
    p = admin.get("/patrimonio?entidade=2").text
    assert "+ Propriedade rústica" in p and "+ Carro" not in p and "Via Verde" not in p
    assert "A tua parte · 1/6" in p and "Por morte da mãe · 1/2" in p
    assert _post(admin, "/patrimonio/novo?entidade=2&tipo=carro", nome="X").status_code == 400
    assert "não usa subscrições" in admin.get("/subscricoes?entidade=2").text
    assert "não usa viagens" in admin.get("/viagens?entidade=2").text
    r = _post(admin, "/admin/entidades/2", nome="Herança", tipo="heranca", quota="abc")
    assert r.status_code == 400 and "1/6" in r.text


def test_contas_por_separador(admin, app):
    _post(admin, "/contas/nova?entidade=1", nome="Conta da família", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(admin, "/contas/nova?entidade=2", nome="Conta da mãe", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    admin.get("/")                                                           # consome a mensagem «criada»
    um = admin.get("/contas?entidade=1").text
    dois = admin.get("/contas?entidade=2").text
    assert "Conta da família" in um and "Conta da mãe" not in um and 'class="separadores"' in um
    assert "Conta da mãe" in dois and "Conta da família" not in dois


def test_cobertura_na_via_verde(admin, app):
    _post(admin, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    ext = vv.Extrato(linhas=[
        vv.Linha("1", "AA11BB", date(2026, 1, 10), "10:00", None, "Ponte", 340, "Op", "Portagens", "FT", None),
        vv.Linha("1", "AA11BB", date(2026, 9, 10), "10:00", None, "Ponte", 340, "Op", "Portagens", "FT", None)])
    with app.state.SessionLocal() as db:
        vv.importar(db, 1, ext)
        db.get(Entidade, 1).data_inicio = date(2026, 7, 1)
        db.commit()
        r = vv.resumo(db, 1)
        assert r["n"] == 1 and r["de"] == date(2026, 9, 10)             # a de janeiro fica fora da análise
