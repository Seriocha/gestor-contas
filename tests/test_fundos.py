"""Fundos de investimento e de pensões: posições (unidades × cotação), valias, liquidez e saldo da conta."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import investimentos
from app.main import create_app
from app.models import Conta, Posicao, RetificacaoSaldo
from app.relatorios import HistoricoConta
from tests.test_app import PW, _post

HOJE = date.today()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def _conta(c, nome, tipo, **extra):
    r = _post(c, "/contas/nova?entidade=1", nome=nome, tipo=tipo, moeda="EUR", instituicao_id="1", **extra)
    assert r.status_code == 303, r.text
    with c.app.state.SessionLocal() as db:
        return db.scalar(select(Conta.id).where(Conta.nome == nome))


def test_fundo_de_investimento(admin, app):
    cid = _conta(admin, "CA Investimento", "INVESTIMENTO", iban="45009220388")
    pagina = f"/contas/{cid}/movimentos"
    assert "Sem posições" in admin.get(pagina).text
    r = _post(admin, pagina, f"/contas/{cid}/posicoes", nome="CA PATRIMÓNIO CRESCENTE CLASSE B",
              gestora="Square Asset Management", categoria="Imobiliário", risco="2", unidades="810",
              cotacao="24,9230", data_cotacao=HOJE.isoformat(), valor_aquisicao="14.947,72")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        p = db.scalar(select(Posicao))
        assert p.valor == 2018763 and p.valias == 523991 and round(p.valias_pct, 2) == 35.05   # 20.187,63 · 5.239,91 · 35,05 %
        conta = db.get(Conta, cid)
        assert HistoricoConta(db, conta).saldo_em(HOJE) == 2018763                    # saldo acertado
    _post(admin, pagina, f"/contas/{cid}/posicoes/liquidez", liquidez="7.267,35")
    with app.state.SessionLocal() as db:
        assert HistoricoConta(db, db.get(Conta, cid)).saldo_em(HOJE) == 2018763 + 726735
        assert db.query(RetificacaoSaldo).filter_by(partida=False).count() == 1      # a do dia é atualizada, não duplicada
        d = investimentos.desempenho(db, db.get(Conta, cid), HOJE)
        assert d.capital == 1494772 + 726735 and d.resultado == 523991               # ganho = valias do fundo
    # atualização rápida da cotação
    _post(admin, pagina, f"/contas/{cid}/posicoes", id=str(p.id), so_cotacao="1", cotacao="25", data_cotacao=HOJE.isoformat())
    with app.state.SessionLocal() as db:
        assert db.get(Posicao, p.id).valor == 2025000 and db.get(Posicao, p.id).unidades == 810
    html = admin.get(pagina).text
    assert "CA PATRIMÓNIO CRESCENTE CLASSE B" in html and "Square Asset Management" in html and "Valias" in html
    assert 'title="Risco 2 de 7"' in html and "4500 •••• 0388" in html
    _post(admin, pagina, f"/contas/{cid}/posicoes", nome="X", unidades="abc")
    erro = admin.get(pagina).text
    assert "Quantidade de unidades inválida" in erro and "Indica a cotação" in erro
    _post(admin, pagina, f"/contas/{cid}/posicoes/{p.id}/apagar")
    with app.state.SessionLocal() as db:
        assert db.query(Posicao).count() == 0
        assert HistoricoConta(db, db.get(Conta, cid)).saldo_em(HOJE) == 726735      # fica a liquidez


def test_fundo_de_pensoes(admin, app):
    cid = _conta(admin, "Fundos de Pensões Abertos", "PENSOES", iban="14029", data_contrato="2018-11-30")
    with app.state.SessionLocal() as db:
        c = db.get(Conta, cid)
        assert c.data_contrato == date(2018, 11, 30) and investimentos.e_investimento(c)
    html = admin.get(f"/contas/{cid}/movimentos").text
    assert "Fundo de pensões" in html and "início 30/11/2018" in html and "Retificar saldo" in html or "retific" in html.lower()
    form = admin.get(f"/contas/{cid}/editar").text
    assert "Data de início do contrato" in form and "N.º da conta / contrato" in form
