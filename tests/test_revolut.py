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


# ---------------------------------------------------------------- extrato completo (Excel), produtos e moedas

COMPLETO = [
    "Carregamento,Atual,2026-03-01 10:00:00,2026-03-01 10:00:01,Carregamento com cartão *0267,500.00,0.00,EUR,CONCLUÍDA,500.00",
    # câmbio EUR → KRW (os dois lados com a mesma data/hora): 100 € = 170.000 KRW
    "Câmbio,Atual,2026-03-10 01:19:52,2026-03-10 01:19:52,Conversão cambial para KRW,-100.00,0.00,EUR,CONCLUÍDA,400.00",
    "Câmbio,Atual,2026-03-10 01:19:52,2026-03-10 01:19:52,Conversão cambial para KRW,170000,0,KRW,CONCLUÍDA,170000",
    "Pagamento com cartão,Atual,2026-03-11 12:00:00,2026-03-11 12:00:01,GS25,-17000,0,KRW,CONCLUÍDA,153000",
    # cofre e investimentos
    "Transferência,Poupanças,2026-03-12 09:00:00,2026-03-12 09:00:00,Carregamento de subconta EUR Poupança Férias de EUR,20.00,0.00,EUR,CONCLUÍDA,20.00",
    "Transferência,Atual,2026-03-12 09:00:00,2026-03-12 09:00:00,Para EUR Poupança Férias,-20.00,0.00,EUR,CONCLUÍDA,380.00",
    "Câmbio,Atual,2026-03-13 10:00:00,2026-03-13 10:00:00,Conversão cambial para USD,-200.00,0.00,EUR,CONCLUÍDA,180.00",
    "Câmbio,Investimentos,2026-03-13 10:00:00,2026-03-13 10:00:00,Conversão cambial para USD,220.00,0.00,USD,CONCLUÍDA,220.00",
    "TRADE,Investimentos,2026-03-13 11:00:00,2026-03-13 11:00:00,Investing money,-200.00,0.00,USD,CONCLUÍDA,20.00",
    "TRADE,Investimentos,2026-04-01 11:00:00,2026-04-01 11:00:00,Dividends,11.00,0.00,USD,CONCLUÍDA,31.00",
    "Taxa,Investimentos,2026-04-02 11:00:00,2026-04-02 11:00:00,Custody fees,-1.10,0.00,USD,CONCLUÍDA,29.90",
    # texto mal codificado como vem no Excel da Revolut
    "Pagamento com cartÃ£o,Atual,2026-03-14 12:00:00,2026-03-14 12:00:01,Pastelaria,-2.50,0.00,EUR,CONCLUÃ\x8dDA,177.50",
]


def xlsx_csv_numa_coluna(linhas):
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for l in linhas:
        ws.append([l])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_com_csv_numa_coluna_e_texto_estragado():
    e = ler_extrato("account-statement.xlsx", xlsx_csv_numa_coluna([CAB.replace("í", "Ã\xad")] + COMPLETO))
    assert e.parser == "revolut"
    assert set(e.subcontas) == {("Atual", "EUR"), ("Atual", "KRW"), ("Poupanças", "EUR"), ("Investimentos", "USD"),
                                ("Todas as moedas", "EUR"), ("Investimentos", "EUR")}
    atual = e.subcontas[("Atual", "EUR")]
    assert atual[-1].descricao == "Pastelaria" and atual[-1].saldo == 17750      # «CONCLUÃ\x8dDA» lido como concluída
    assert not [a for a in e.avisos if "não bate certo" in a]
    # outras moedas convertidas pelo câmbio do próprio extrato (100 € = 170.000 KRW)
    todas = {m.descricao: m for m in e.subcontas[("Todas as moedas", "EUR")]}
    assert todas["GS25 (-17.000 KRW)"].montante == -1000 and not todas["GS25 (-17.000 KRW)"].transferencia
    assert todas["Conversão cambial para KRW (170.000 KRW)"].transferencia
    # investimentos como carteira: sem as compras de ações; dividendos e taxas convertidos a euros (200 € = 220 USD)
    inv = e.subcontas[("Investimentos", "USD")]
    assert [m.descricao for m in inv] == ["Conversão cambial para USD", "Dividends", "Custody fees"]
    assert inv[0].transferencia and not inv[1].transferencia and all(m.saldo is None for m in inv)
    inv_eur = {m.descricao: m.montante for m in e.subcontas[("Investimentos", "EUR")]}
    assert inv_eur == {"Conversão cambial para USD (220 USD)": 20000, "Dividends (11 USD)": 1000,
                       "Custody fees (-1,10 USD)": -100}
    assert any("1 compra(s)/venda(s) de ações" in a for a in e.avisos)


def test_cada_conta_recebe_a_sua_subconta(tmp_path):
    c = TestClient(create_app(tmp_path, backups_automaticos=False))
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    _post(c, "/contas/nova?entidade=1", nome="Revolut cofres", tipo="POUPANCA", moeda="EUR", instituicao_id="5")
    _post(c, "/contas/nova?entidade=1", nome="Revolut ações", tipo="INVESTIMENTO", moeda="EUR", instituicao_id="5")
    ficheiro = xlsx_csv_numa_coluna([CAB] + COMPLETO)
    previa, r = _importar(c, "account-statement.xlsx", ficheiro, conta_id="1")
    assert "Importar 7 movimentos" in previa.text and "Todas as moedas" in previa.text   # 5 em euros + 2 convertidos
    previa, _ = _importar(c, "account-statement.xlsx", ficheiro, conta_id="2")
    assert "Importar 1 movimentos" in previa.text and "Poupanças" in previa.text
    previa, _ = _importar(c, "account-statement.xlsx", ficheiro, conta_id="3")
    assert "Importar 3 movimentos" in previa.text and "trocas dentro da carteira" in previa.text
