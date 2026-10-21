"""Revolut — extrato consolidado em Excel («consolidated-statement-v2_…_pt.xlsx»): o CSV numa só coluna, acentos
perdidos («C�mbio»), euro estragado («â‚¬»), conta em euros, conta em USD com valores em dobro, cofre, juros da
Conta reforçada, conferência com o Total, e importação sem duplicar o que veio do PDF ou do CSV."""
import csv
import io
from datetime import date

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.parsers import FormatoDesconhecido, ler_extrato, revolut_consolidado
from tests.test_app import PW, _post
from tests.test_importacao import _importar

E = "�"
EURO = "€".encode("utf-8").decode("cp1252")          # «â‚¬», como no ficheiro da Revolut


def _eur(v: str) -> str:
    return v + EURO


LINHAS = [
    ["Contas-correntes Resumos"], [],
    ["Conta Pessoal (EUR)"], [], ["Dados da conta-corrente"],
    [f"N{E}mero de conta (LT IBAN)", "LT000000000000000000", "Data de abertura", "27 de nov. de 2018"],
    ["Tipo de conta", "Atual"], ["---------"], [],
    ["Contas-correntes Extratos de opera" + E + E + "es"], [],
    ["Conta Pessoal (EUR)"], [], [f"Extrato de opera{E}{E}es"],
    ["Data", f"Descri{E}{E}o", "Categoria", "Dinheiro a entrar/sair", "Saldo", "Imposto retido", "Outros impostos", f"Comiss{E}es"],
    ["18 de jan. de 2020", "Carregamento de FULANO", "Carregar", _eur("300,00"), _eur("300,00"), _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["21 de jan. de 2020", "Amazon", "Comerciante", _eur("-1.234,56"), _eur("-934,56"), _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["21 de jan. de 2020", "Pingo Doce", "Comerciante", _eur("-15,44"), _eur("-950,00"), _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["1 de set. de 2020", f"Convers{E}o cambial para USD", f"C{E}mbio", _eur("-50,00"), _eur("-1.000,00"), _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["17 de dez. de 2020", f"Para Poupan{E}a F{E}rias", "Outros", _eur("-1,00"), _eur("-1.001,00"), _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["Total", "", "", _eur("-1.001,00"), "", _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["---------"], [],
    ["Conta Pessoal (USD)"], [], [f"Extrato de opera{E}{E}es"],
    ["Data", f"Descri{E}{E}o", "Categoria", "Dinheiro a entrar/sair", "Dinheiro a entrar/sair", "Saldo", "Imposto retido",
     "Imposto retido", "Outros impostos", "Outros impostos", f"Comiss{E}es", f"Comiss{E}es"],
    ["1 de set. de 2020", f"Convers{E}o cambial de EUR", f"C{E}mbio", "59,00$", _eur("50,00"), "59,00$", "0,00$", _eur("0,00"),
     "0,00$", _eur("0,00"), "0,00$", _eur("0,00")],
    ["3 de set. de 2020", "Apple", "Comerciante", "-9,99$", _eur("-8,47"), "49,01$", "0,00$", _eur("0,00"), "0,00$",
     _eur("0,00"), "0,00$", _eur("0,00")],
    ["Total", "", "", "49,01$", _eur("41,53"), "", "0,00$", _eur("0,00"), "0,00$", _eur("0,00"), "0,00$", _eur("0,00")],
    ["---------"], [],
    [f"Poupan{E}a F{E}rias (EUR)"], [], [f"Extrato de opera{E}{E}es"],
    ["Data", f"Descri{E}{E}o", "Categoria", "Dinheiro a entrar/sair", "Saldo", "Imposto retido", "Outros impostos", f"Comiss{E}es"],
    ["17 de dez. de 2020", "De EUR Conta Pessoal", "Outros", _eur("1,00"), _eur("1,00"), _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["Total", "", "", _eur("1,00"), "", _eur("0,00"), _eur("0,00"), _eur("0,00")],
    ["---------"], ["---------"], [],
    ["Savings Accounts Transaction Statements"], [],
    [f"Poupan{E}a de Acesso Imediato  (EUR)"], [], [f"Extrato da opera{E}{E}o (apenas recibo de juros)"],
    ["Data", f"Descri{E}{E}o", "TAE", "TANB", "Gross interest", "Impostos retidos", "Other taxes", "Fees", "Net interest"],
    ["10 de ago. de 2020", "Pagamento de juros", "2,00%", "1,98%", _eur("0,02"), _eur("0,01"), _eur("0,00"), _eur("0,00"), _eur("0,01")],
    ["---------"], [],
    ["Investment Services Extratos de opera" + E + E + "es"],
    ["Data", f"Descri{E}{E}o e s{E}mbolo", "ISIN"],
    ["13 de nov. de 2020", "Dividendo XYZ", "US0000000000"],
]


def xlsx(linhas=LINHAS) -> bytes:
    """Como a exportação da Revolut: cada linha do CSV inteira numa só célula."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for l in linhas:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(l + [""] * (23 - len(l)) if l else [""] * 23)
        ws.append([buf.getvalue()])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_le_as_subcontas_como_o_pdf():
    e = ler_extrato("consolidated-statement-v2_2020-01-01_2020-12-31_pt.xlsx", xlsx())
    assert e.parser == "revolut_consolidado" and e.dedup_dias == 4 and not e.avisos
    assert set(e.subcontas) == {("Atual", "EUR"), ("Todas as moedas", "EUR"), ("Atual", "USD"),
                                ("Poupança Férias", "EUR"), ("Depósito", "EUR")}
    eur = e.subcontas[("Atual", "EUR")]
    assert [(m.data_mov, m.montante, m.saldo) for m in eur][:2] == [(date(2020, 1, 18), 30000, 30000),
                                                                   (date(2020, 1, 21), -123456, -93456)]
    assert eur[0].transferencia and not eur[1].transferencia                  # carregamento
    assert eur[3].descricao == "Conversão cambial para USD" and eur[3].transferencia   # «C?mbio» reconhecido
    assert eur[4].descricao == "Para Poupança Férias" and eur[4].transferencia
    usd = e.subcontas[("Atual", "USD")]
    assert [(m.montante, m.saldo) for m in usd] == [(5900, 5900), (-999, 4901)]
    todas = e.subcontas[("Todas as moedas", "EUR")]
    assert len(todas) == 7 and e.movimentos == todas
    apple = next(m for m in todas if m.descricao.startswith("Apple"))
    assert apple.montante == -847 and apple.descricao == "Apple (-9,99 USD)" and apple.saldo is None
    assert [m.montante for m in e.subcontas[("Depósito", "EUR")]] == [1]
    assert e.subcontas[("Poupança Férias", "EUR")][0].saldo == 100


def test_total_que_nao_bate_da_aviso():
    linhas = [l if l[:1] != ["Total"] or l[3] != _eur("-1.001,00") else ["Total", "", "", _eur("-999,00")] for l in LINHAS]
    e = ler_extrato("x.xlsx", xlsx(linhas))
    assert any("não bate com o Total" in a and "Atual EUR" in a for a in e.avisos)


def test_nao_confunde_com_outros_excel():
    linhas = [["Data", "Descrição", "Montante"], ["2020-01-01", "x", "1,00"]]
    with pytest.raises(FormatoDesconhecido):
        ler_extrato("x.xlsx", xlsx(linhas))
    assert not revolut_consolidado.Parser.detetar([["Contas-correntes Extratos de operações"]])


@pytest.fixture
def admin(tmp_path):
    c = TestClient(__import__("app.main", fromlist=["create_app"]).create_app(tmp_path, backups_automaticos=False))
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    _post(c, "/contas/nova?entidade=1", nome="Revolut cofre Férias", tipo="POUPANCA", moeda="EUR", instituicao_id="5")
    return c


def test_importar_e_reimportar_sem_duplicar(admin):
    previa, r = _importar(admin, "consolidated-statement-v2.xlsx", xlsx(), conta_id="1")
    assert r.status_code == 303 and "Importar 7 movimentos" in previa.text        # 5 em euros + 2 de USD convertidos
    previa, r = _importar(admin, "consolidated-statement-v2.xlsx", xlsx(), conta_id="2")
    assert "Importar 1 movimentos" in previa.text                                  # o cofre «Férias»
    previa, _ = _importar(admin, "consolidated-statement-v2.xlsx", xlsx(), conta_id="1")
    assert "Importar 0 movimentos" in previa.text


def test_nao_duplica_o_que_veio_do_csv(admin):
    csv_ = ("Tipo,Produto,Data de início,Data de Conclusão,Descrição,Montante,Comissão,Moeda,Estado,Saldo\n"
            "Pagamento com cartão,Atual,2020-01-20 20:00:00,2020-01-21 09:00:00,Amazon,-1234.56,0.00,EUR,CONCLUÍDA,-934.56\n")
    _, r = _importar(admin, "revolut.csv", csv_.encode(), conta_id="1")
    assert r.status_code == 303
    previa, _ = _importar(admin, "consolidated-statement-v2.xlsx", xlsx(), conta_id="1")
    assert "Importar 6 movimentos" in previa.text and previa.text.count("já existe") == 1
