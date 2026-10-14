"""Cartão de crédito Santander: parser, ligação dos pagamentos e resumo do ciclo."""
import io
from datetime import date

import pytest
import xlwt
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Conta, Movimento
from app.parsers import ler_extrato
from app.servicos import resumo_cartao
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import _importar, xls_santander


def xls_cartao(linhas) -> bytes:
    """linhas: (n.º, 'dd-mm-aaaa', descrição, estado, 'C'|'D', valor positivo) — como o Santander exporta:
    o cabeçalho tem 5 títulos, mas cada linha tem 6 colunas (C/D sem título)."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Consulta Movimentos Extracto")
    for i, t in enumerate(["Quinta-feira, 15 de Outubro de 2026", "TITULAR FICTICIO", "",
                           "Consulta Movimentos Extracto", "", "Listagem Movimentos"]):
        ws.write(i, 0, t)
    for j, t in enumerate(["Movimento", "Data", "Descrição", "Estado", "Montante"]):
        ws.write(6, j, t)
    for i, linha in enumerate(linhas, start=7):
        for j, v in enumerate(linha):
            ws.write(i, j, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CARTAO = [
    ("1", "06-07-2026", "PAG.TRANS.BANCARIA", "EXTRACTADO", "C", 209.36),
    ("2", "08-07-2026", "CONTINENTE MODELO", "EXTRACTADO", "D", 45.10),
    ("3", "09-07-2026", "DEVOLUCAO ZARA", "EXTRACTADO", "C", 19.99),
    ("4", "10-07-2026", "AMAZON EU", "PENDENTE", "D", 12.00),
]


def test_parser_cartao():
    e = ler_extrato("CC ext 11.xls", xls_cartao(CARTAO))
    assert e.parser == "santander_cartao" and e.cartao
    assert [(m.descricao, m.montante) for m in e.movimentos] == [
        ("PAG.TRANS.BANCARIA", 20936), ("CONTINENTE MODELO", -4510), ("DEVOLUCAO ZARA", 1999)]
    assert any("em curso" in a for a in e.avisos)
    # o extrato da conta à ordem do Santander continua a ser reconhecido como tal
    assert ler_extrato("d.xls", xls_santander([("13-10-2026", "X", -1.0, 10.0)])).parser == "santander"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Cartão Santander", tipo="CARTAO_CREDITO", moeda="EUR",
          instituicao_id="1", conta_pagamento_id="1", dia_fecho="20", dia_pagamento="5", saldo_inicial="-209,36")
    return c


ORDEM = [  # mais recente primeiro; inclui o débito do pagamento do cartão 2 dias antes
    ("10-07-2026", "Farmacia", -12.0, 778.64),
    ("04-07-2026", "TRF CARTAO CREDITO", -209.36, 790.64),
    ("01-07-2026", "Vencimento", 1000.0, 1000.0),
]


def _trf(app):
    with app.state.SessionLocal() as db:
        return {m.descricao_original: m.transferencia for m in db.query(Movimento)}


@pytest.mark.parametrize("ordem_primeiro", [True, False])
def test_pagamento_ligado_nos_dois_lados_em_qualquer_ordem(admin, app, ordem_primeiro):
    passos = [("1", "ordem.xls", xls_santander(ORDEM)), ("2", "CC ext.xls", xls_cartao(CARTAO))]
    for conta, nome, conteudo in (passos if ordem_primeiro else passos[::-1]):
        previa, r = _importar(admin, nome, conteudo, conta_id=conta)
        assert r.status_code == 303, previa.text
    trf = _trf(app)
    assert trf["PAG.TRANS.BANCARIA"] and trf["TRF CARTAO CREDITO"]
    assert not trf["CONTINENTE MODELO"] and not trf["DEVOLUCAO ZARA"] and not trf["Farmacia"]
    # relatório de julho: despesas = compras do cartão (45,10) − devolução (sem regra: entrada) + farmácia;
    # o pagamento do cartão (209,36) não conta duas vezes
    pagina = admin.get("/relatorios?periodo=personalizado&de=2026-07-01&ate=2026-07-31").text
    assert "209,36" not in pagina.split("Receitas por categoria")[0].split("Despesas por categoria")[1]


def test_extrato_de_cartao_numa_conta_a_ordem_e_recusado(admin):
    previa, r = _importar(admin, "CC ext.xls", xls_cartao(CARTAO), conta_id="1")
    assert r is None and "extrato de cartão de crédito" in previa.text


def test_resumo_do_cartao(admin, app):
    _importar(admin, "CC ext.xls", xls_cartao(CARTAO), conta_id="2")
    with app.state.SessionLocal() as db:
        cartao = db.get(Conta, 2)
        r = resumo_cartao(db, cartao, date(2026, 7, 25))
        # dívida: 209,36 inicial − 209,36 pago + 45,10 − 19,99
        assert r.divida == 2511
        assert (r.ultimo_fecho, r.proximo_fecho, r.proximo_pagamento) == (date(2026, 7, 20), date(2026, 8, 20), date(2026, 8, 5))
        r = resumo_cartao(db, cartao, date(2026, 7, 15))
        assert (r.ultimo_fecho, r.proximo_fecho) == (date(2026, 6, 20), date(2026, 7, 20))
        assert r.gastos_ciclo == 4510 and r.pagamentos_ciclo == 20936
    pagina = admin.get("/contas/2/movimentos").text
    assert "Em dívida" in pagina and "25,11" in pagina and "Pago por" in pagina
