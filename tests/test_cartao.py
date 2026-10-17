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


def _por_desc(app):
    with app.state.SessionLocal() as db:
        return {m.descricao_original: (m.id, m.par_id, m.transferencia) for m in db.query(Movimento)}


def test_par_gravado_e_ligado_a_no_detalhe(admin, app):
    _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")
    _importar(admin, "CC.xls", xls_cartao(CARTAO), conta_id="2")
    m = _por_desc(app)
    pag, deb = m["PAG.TRANS.BANCARIA"], m["TRF CARTAO CREDITO"]
    assert pag[1] == deb[0] and deb[1] == pag[0]
    det = admin.get(f"/movimentos/{pag[0]}").text
    assert "Ligado a" in det and "TRF CARTAO CREDITO" in det and f'href="/movimentos/{deb[0]}' in det
    det = admin.get(f"/movimentos/{deb[0]}").text
    assert "PAG.TRANS.BANCARIA" in det and "pagamento do cartão" in det
    # compras não estão ligadas
    assert "Não está ligado" in admin.get(f"/movimentos/{m['CONTINENTE MODELO'][0]}").text


def test_relatorio_pares_desligar_e_ligar_a_mao(admin, app):
    # pagamento fora da janela de 7 dias: fica sem par, com o débito como candidato
    ordem = [("10-07-2026", "Farmacia", -12.0, 778.64), ("25-06-2026", "TRF CARTAO CREDITO", -209.36, 790.64),
             ("01-06-2026", "Vencimento", 1000.0, 1000.0)]
    _importar(admin, "ordem.xls", xls_santander(ordem), conta_id="1")
    _importar(admin, "CC.xls", xls_cartao(CARTAO), conta_id="2")
    m = _por_desc(app)
    pag, deb = m["PAG.TRANS.BANCARIA"], m["TRF CARTAO CREDITO"]
    assert pag[1] is None and not deb[2]
    rel = admin.get("/relatorios/cartoes?entidade=1").text
    assert "1 sem par" in rel and "0 ligados" in rel and "Ligar" in rel
    # ligar à mão a partir do relatório
    r = admin.post(f"/movimentos/{pag[0]}/ligar", data={"csrf": _csrf(admin.get("/relatorios/cartoes")),
                   "par_id": str(deb[0]), "voltar": "/relatorios/cartoes?entidade=1"}, follow_redirects=False)
    assert r.status_code == 303
    m = _por_desc(app)
    assert m["PAG.TRANS.BANCARIA"][1] == deb[0] and m["TRF CARTAO CREDITO"][1:] == (pag[0], True)
    assert "1 ligado" in admin.get("/relatorios/cartoes?entidade=1").text
    # desligar desfaz nos dois lados, mas mantém a marca de transferência
    admin.post(f"/movimentos/{pag[0]}/desligar", data={"csrf": _csrf(admin.get("/relatorios/cartoes"))})
    m = _por_desc(app)
    assert m["PAG.TRANS.BANCARIA"][1:] == (None, True) and m["TRF CARTAO CREDITO"][1:] == (None, True)


def test_ligar_recusa_dois_debitos(admin, app):
    _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")
    m = _por_desc(app)
    r = admin.post(f"/movimentos/{m['Farmacia'][0]}/ligar",
                   data={"csrf": _csrf(admin.get("/movimentos")), "par_id": str(m["TRF CARTAO CREDITO"][0])})
    assert r.status_code == 400


def test_procurar_pares_liga_dados_da_v060(admin, app):
    """Na NAS, a v0.6.0 marcou transferências sem gravar o par: «Procurar pares agora» liga-os."""
    _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")
    _importar(admin, "CC.xls", xls_cartao(CARTAO), conta_id="2")
    with app.state.SessionLocal() as db:
        for mv in db.query(Movimento):
            mv.par_id = None
        db.commit()
    r = admin.post("/relatorios/cartoes/procurar", data={"csrf": _csrf(admin.get("/relatorios/cartoes")), "entidade": "1"})
    assert "1 pagamento(s) marcado(s) ou ligado(s)" in r.text and "1 ligado" in r.text


def test_lista_marca_pares_com_mesma_letra_e_cor(admin, app):
    import re
    _importar(admin, "ordem.xls", xls_santander(ORDEM), conta_id="1")
    _importar(admin, "CC.xls", xls_cartao(CARTAO), conta_id="2")
    # lista global: os dois lados na mesma página, com a mesma letra e cor
    pagina = admin.get("/movimentos?entidade=1").text
    etiquetas = re.findall(r'class="etiqueta-par" href="(#mov-\d+)"\s+data-alvo="\d+" style="--cor-par: (#[0-9a-f]{6})"', pagina)
    assert len(etiquetas) == 2 and etiquetas[0][1] == etiquetas[1][1]
    assert pagina.count("</span> A") == 2
    # só a conta à ordem: o outro lado está fora da página → ligação para o detalhe e nome da conta
    conta = admin.get("/contas/1/movimentos").text
    assert 'href="/movimentos/' in conta.split('class="etiqueta-par"')[1][:40] and "· Cartão Santander" in conta


def xls_cartao_lista(linhas, cartao="XXXXXXXXXXXX1234") -> bytes:
    """Formato novo «Consulta Movimentos Cartão Créd»: Data Operação | Descrição | (vazia) | Montante (EUR) com sinal."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Consulta Movimentos Cartão Créd")
    for i, t in enumerate(["Sábado, 17 de Outubro de 2026", "TITULAR FICTICIO", "", "Consulta Movimentos Cartão Créd",
                           f"Nº Cartão {cartao}", f"Nome Favorito {cartao}", "", "Listagem Movimentos"]):
        ws.write(i, 0, t)
    for j, t in enumerate(["Data Operação", "Descrição", "", "Montante (EUR)"]):
        ws.write(8, j, t)
    for i, (d, desc, v) in enumerate(linhas, start=9):
        ws.write(i, 0, d)
        ws.write(i, 1, desc)
        ws.write(i, 2, "")
        ws.write(i, 3, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


LISTA = [
    ("11-10-2026", "COMPRA MBW*1234 LOJA FICTICIA LISBOA", -674.1),
    ("08-07-2026", "COMPRA TPA*1234 CONTINENTE MODELO LISBOA", -45.10),
    ("06-07-2026", "PAG.TRANS.BANCARIA", 209.36),
    ("22-03-2026", "COMPRA ESTRANG*1234 Revolut**9999* IRL", -1000.0),
]


def test_parser_cartao_formato_lista():
    e = ler_extrato("descarga (22).xls", xls_cartao_lista(LISTA))
    assert e.parser == "santander_cartao" and e.cartao and e.conta_ref == "cartão …1234"
    assert [(m.data_mov, m.montante) for m in e.movimentos] == [
        (date(2026, 3, 22), -100000), (date(2026, 7, 6), 20936), (date(2026, 7, 8), -4510), (date(2026, 10, 11), -67410)]
    revolut = e.movimentos[0]
    assert revolut.transferencia and not e.movimentos[2].transferencia
    # o formato Extracto continua a ser reconhecido
    assert ler_extrato("CC ext 11.xls", xls_cartao(CARTAO)).parser == "santander_cartao"


def test_formatos_diferentes_nao_duplicam(admin, app):
    previa, r = _importar(admin, "CC ext.xls", xls_cartao(CARTAO), conta_id="2")
    assert r.status_code == 303
    # o mesmo pagamento (06/07, 209,36) e a mesma compra (08/07, 45,10 com outro texto) já existem
    previa, r = _importar(admin, "descarga (22).xls", xls_cartao_lista(LISTA), conta_id="2")
    assert "Importar 2 movimentos" in previa.text and previa.text.count("já existe") == 2
    assert "cartão …1234" in previa.text
    with app.state.SessionLocal() as db:
        descricoes = {m.descricao_original for m in db.query(Movimento).filter_by(conta_id=2)}
    assert "COMPRA MBW*1234 LOJA FICTICIA LISBOA" in descricoes
    assert "COMPRA TPA*1234 CONTINENTE MODELO LISBOA" not in descricoes
