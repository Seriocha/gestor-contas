"""Cartão refeição Santander: parser, importação e categoria do carregamento."""
import io
from datetime import date

import pytest
import xlwt
from fastapi.testclient import TestClient

from app import previsoes
from app.main import create_app
from app.models import Movimento
from app.parsers import ler_extrato
from tests.test_app import PW, _post
from tests.test_importacao import _importar, xls_santander


def xls_refeicao(linhas, cartao="XXXXXXXXXXXX1234") -> bytes:
    """linhas: ('dd-mm-aaaa', descrição, 'montante em texto com ponto') — como o NetBanco exporta."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Consultar Movimentos de Cartão")
    for i, t in enumerate(["Quinta-feira, 15 de Outubro de 2026", "TITULAR FICTICIO", "",
                           "Consultar Movimentos de Cartão", f"Nº Cartão {cartao}", f"Nome Favorito {cartao}", "",
                           "Listagem Movimentos"]):
        ws.write(i, 0, t)
    for j, t in enumerate(["Data Operação", "Data Valor", "Descrição", "Montante"]):
        ws.write(8, j, t)
    for i, (d, desc, v) in enumerate(linhas, start=9):
        for j, x in enumerate((d, d, desc, v)):
            ws.write(i, j, x)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


REFEICAO = [
    ("27-04-2026", "COMPRA *1234 PADARIA FICTICIA", "-2.50"),
    ("24-04-2026", "SUB REFEICAO BST", "234.97"),
    ("23-04-2026", "COMPRA *1234 RESTAURANTE EXEMPLO", "-29.60"),
    ("20-04-2026", "COMPRA *1234 CAFE TESTE", "-1.20"),
    ("20-04-2026", "COMPRA *1234 CAFE TESTE", "-1.65"),
]


def test_parser_refeicao():
    e = ler_extrato("descarga (15).xls", xls_refeicao(REFEICAO))
    assert e.parser == "santander_refeicao" and e.refeicao and not e.cartao
    assert e.conta_ref == "cartão …1234"
    assert [(m.data_mov, m.montante) for m in e.movimentos][:2] == [(date(2026, 4, 20), -165), (date(2026, 4, 20), -120)]
    assert e.movimentos[-1].descricao == "COMPRA *1234 PADARIA FICTICIA" and e.movimentos[-1].saldo is None
    # os outros extratos do Santander continuam a ser reconhecidos como antes
    assert ler_extrato("d.xls", xls_santander([("13-10-2026", "X", -1.0, 10.0)])).parser == "santander"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Refeição", tipo="CARTAO_REFEICAO", moeda="EUR", instituicao_id="1",
          saldo_inicial="10,00", data_saldo_inicial="2026-04-19")
    return c


def test_importar_no_cartao_refeicao(admin, app):
    previa, r = _importar(admin, "r.xls", xls_refeicao(REFEICAO), conta_id="2")
    assert "Importar 5 movimentos" in previa.text and "cartão …1234" in previa.text
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        sub = db.query(Movimento).filter_by(descricao_original="SUB REFEICAO BST").one()
        assert sub.categoria.nome == previsoes.CATEGORIA_REFEICAO
    assert "210,02 €" in admin.get("/contas/2/movimentos").text   # 10 + 234,97 − 34,95
    # reimportar o mesmo período não duplica nada
    previa, _ = _importar(admin, "r2.xls", xls_refeicao(REFEICAO), conta_id="2")
    assert "Importar 0 movimentos" in previa.text and previa.text.count("já existe") == 5


def test_conta_errada(admin):
    previa, r = _importar(admin, "r.xls", xls_refeicao(REFEICAO), conta_id="1")
    assert "extrato de cartão refeição" in previa.text and r is None
