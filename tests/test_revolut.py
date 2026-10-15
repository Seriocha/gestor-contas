"""Revolut: CSV (pt/en), subcontas, comissões e ligação de carregamentos às contas dos bancos."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Movimento
from app.parsers import ler_extrato
from tests.test_app import PW, _post
from tests.test_importacao import _importar, xls_santander, xlsx_activobank

CAB = "Tipo,Produto,Data de início,Data de Conclusão,Descrição,Montante,Comissão,Moeda,Estado,Saldo"
LINHAS = [
    "Carregamento,Atual,2026-03-27 10:00:00,2026-03-27 10:00:01,Carregamento com cartão *0267,1000.00,0.00,EUR,CONCLUÍDA,1000.00",
    "Pagamento com cartão,Atual,2026-03-28 12:00:00,2026-03-30 08:00:00,Coop,-45.29,0.45,EUR,CONCLUÍDA,954.26",
    "Pagamento com cartão,Atual,2026-03-29 12:00:00,,Royal Car Rental,-382.40,0.00,EUR,REVERTIDA,",
    "Carregamento,Atual,2026-04-01 09:00:00,2026-04-01 09:00:01,Carregamento com cartão *0267,300.00,0.00,EUR,CONCLUÍDA,1254.26",
    "Transferência,Atual,2026-04-02 09:00:00,2026-04-02 09:00:00,Para EUR Conta reforçada,-100.00,0.00,EUR,CONCLUÍDA,1154.26",
    "Transferência,Depósito,2026-04-02 09:00:00,2026-04-02 09:00:00,Para EUR Conta reforçada,100.00,0.00,EUR,CONCLUÍDA,100.00",
    'Juros,Depósito,2026-04-10 02:00:00,2026-04-10 02:00:00,"Pagamento de juros líquidos para subconta ""Conta reforçada"" relativos a Apr 10, 2026",0.02,0.01,EUR,CONCLUÍDA,100.01',
    "Cobrança,Atual,2026-04-06 09:00:00,2026-04-06 09:00:00,Comissão de manutenção de conta pacote Premium,0.00,9.99,EUR,CONCLUÍDA,1144.27",
]
CSV = ("\n".join([CAB] + LINHAS) + "\n").encode("utf-8-sig")


def test_parser_subcontas_comissoes_e_revertidos():
    e = ler_extrato("account-statement.csv", CSV)
    assert e.parser == "revolut" and set(e.subcontas) == {("Atual", "EUR"), ("Depósito", "EUR")}
    assert any("1 movimento(s) revertidos" in a for a in e.avisos)
    assert not [a for a in e.avisos if "não bate certo" in a]  # saldos coerentes nas duas subcontas
    atual = e.subcontas[("Atual", "EUR")]
    assert [(m.descricao.split()[0], m.montante, m.transferencia) for m in atual] == [
        ("Carregamento", 100000, True),
        ("Coop", -4529, False), ("Comissão", -45, False),
        ("Carregamento", 30000, True),
        ("Para", -10000, True),
        ("Comissão", -999, False),
    ]
    assert atual[2].descricao == "Comissão Revolut (Pagamento com cartão): Coop"
    assert atual[-1].saldo == 114427
    dep = e.subcontas[("Depósito", "EUR")]
    assert dep[1].descricao.startswith("Pagamento de juros líquidos para subconta \"Conta reforçada\" relativos a Apr 10, 2026")


def test_parser_em_ingles():
    cab = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"
    csv_en = f"{cab}\nTopup,Current,2026-03-27 10:00:00,2026-03-27 10:00:01,Top-up by *0267,50.00,0.00,EUR,COMPLETED,50.00\n"
    e = ler_extrato("statement.csv", csv_en.encode())
    assert e.parser == "revolut" and e.movimentos[0].transferencia and e.movimentos[0].montante == 5000


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="ActivoBank", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    _post(c, "/contas/nova?entidade=1", nome="Revolut reforçada", tipo="POUPANCA", moeda="EUR", instituicao_id="5")
    return c


def _por_conta(app, conta_id):
    with app.state.SessionLocal() as db:
        return [(m.descricao_original, m.montante, m.transferencia, m.par_id is not None)
                for m in db.query(Movimento).filter_by(conta_id=conta_id).order_by(Movimento.data_mov, Movimento.ordem)]


def test_importar_subcontas_e_ligar_carregamentos(admin, app):
    from datetime import datetime
    # ActivoBank: o carregamento de 1000 € aparece 4 dias depois; um TRF de 300 € coincide com o 2.º carregamento
    _importar(admin, "mov.xlsx", xlsx_activobank([
        (datetime(2026, 3, 31), "COMPRA 6344 Revolut 8080 Dublin", -1000.0, 4000.0),
        (datetime(2026, 3, 31), "TRF P/ Condominio Povoa", -300.0, 3700.0),
    ]), conta_id="1")
    previa, r = _importar(admin, "revolut.csv", CSV, conta_id="2")
    assert "Importar 6 movimentos" in previa.text and "Depósito EUR" in previa.text
    assert "2 transferência(s) ligada(s)" not in admin.get(r.headers["location"]).text  # só 1 é ligável ao banco
    activo = {d: (t, par) for d, _, t, par in _por_conta(app, 1)}
    assert activo["COMPRA 6344 Revolut 8080 Dublin"] == (True, True)   # ligado ao carregamento (4 dias)
    assert activo["TRF P/ Condominio Povoa"] == (False, False)         # NÃO: valor igual mas não é Revolut
    # a mesma exportação, na conta poupança: só a subconta Depósito; a transferência interna fica ligada
    previa, r = _importar(admin, "revolut.csv", CSV, conta_id="3")
    assert "Importar 3 movimentos" in previa.text and "1 transferência(s) ligada(s)" in admin.get(r.headers["location"]).text
    rev = {d: (t, par) for d, _, t, par in _por_conta(app, 2)}
    assert rev["Para EUR Conta reforçada"] == (True, True)
    assert rev["Carregamento com cartão *0267"] in ((True, True), (True, False))
    # relatório: comissões contam como despesa, carregamentos e transferências internas não
    pagina = admin.get("/relatorios?periodo=personalizado&de=2026-03-01&ate=2026-04-30").text
    despesas = pagina.split("Despesas por categoria")[1].split("Receitas por categoria")[0]
    assert "Comissões bancárias" in despesas and "1.000,00" not in despesas


def test_ficheiro_revolut_sem_a_subconta(admin):
    previa, r = _importar(admin, "revolut.csv", CSV.replace(b"Dep\xc3\xb3sito", b"Atual"), conta_id="3")
    assert r is None and "não tem movimentos da subconta «Depósito»" in previa.text
