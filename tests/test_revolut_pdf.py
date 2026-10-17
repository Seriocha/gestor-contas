"""Revolut — «Extrato personalizado» consolidado em PDF: interpretação por posições, subcontas,
conversão das outras moedas a euros, juros da Conta reforçada e deduplicação com o CSV."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Movimento
from app.parsers import revolut_pdf
from app.parsers.revolut_pdf import interpretar
from tests.test_app import PW, _post
from tests.test_importacao import _importar

X = {"data": 40, "descricao": 125, "categoria": 257, "montante": 342, "saldo": 384, "imposto": 427, "outros": 469,
     "comissoes": 534}


def _linha(top, **cols):
    """Palavras de uma linha: cada coluna com o seu texto (separado em palavras como no PDF)."""
    ws = []
    for nome, texto in cols.items():
        x = X[nome]
        for i, palavra in enumerate(texto.split()):
            ws.append({"text": palavra, "x0": x + i * 12, "top": top})
    return ws


def _texto(top, x, texto):
    return [{"text": p, "x0": x + i * 30, "top": top} for i, p in enumerate(texto.split())]


def _cabecalho(top):
    return (_texto(top, 40, "Data") + [{"text": "Descrição", "x0": 125, "top": top},
            {"text": "Categoria", "x0": 257, "top": top}, {"text": "Saldo", "x0": 384, "top": top},
            {"text": "Comissões", "x0": 513, "top": top}])


def paginas_ficticias():
    resumo = _texto(100, 40, "Contas-correntes Resumos") + _texto(120, 40, "Conta Pessoal (EUR)") \
        + _texto(140, 40, "Data de abertura 27 de nov. de 2018")
    eur = (_texto(10, 356, "Extrato personalizado") + _texto(144, 40, "Contas-correntes Extratos de operações")
           + _texto(185, 40, "Conta Pessoal (EUR)") + _cabecalho(246)
           + _linha(271, data="5 de jan. de 2026", descricao="Amazon", categoria="Comerciante", montante="-8,12€",
                    saldo="275,11€", imposto="0,00€", outros="0,00€", comissoes="0,00€")
           + _linha(290, data="6 de jan. de 2026", descricao="Transferência imediata", categoria="Outros",
                    montante="20,00€", saldo="295,11€", imposto="0,00€", outros="0,00€", comissoes="0,00€")
           + _linha(300, descricao="intrabancária de FULANO")
           + _linha(320, data="29 de jan. de 2026", descricao="Carregamento de FULANO", categoria="Carregar",
                    montante="250,00€", saldo="545,11€", imposto="0,00€", outros="0,00€", comissoes="0,00€")
           + _linha(340, data="24 de fev. de 2026", descricao="Conversão cambial para KRW", categoria="Câmbio",
                    montante="-20,00€", saldo="525,11€", imposto="0,00€", outros="0,00€", comissoes="0,00€")
           + _texto(360, 40, "Total 0,00€"))
    krw = (_texto(185, 40, "Conta Pessoal (KRW)") + _cabecalho(208)
           + _linha(233, data="24 de fev. de 2026", descricao="Conversão cambial para KRW", categoria="Câmbio",
                    montante="33.419", saldo="33.419")
           + _linha(246, montante="KRW", saldo="KRW") + _linha(256, montante="19,66€", saldo="19,66€")
           + _linha(270, data="26 de fev. de 2026", descricao="K-ETA", categoria="Comerciante",
                    montante="-20.612", saldo="12.807")
           + _linha(283, montante="KRW", saldo="KRW") + _linha(293, montante="-12,24€", saldo="7,60€")
           + _texto(320, 40, "Total"))
    juros = (_texto(100, 40, "Savings Accounts Transaction Statements")
             + _texto(120, 40, "Poupança de Acesso Imediato (EUR)")
             + _texto(160, 40, "Data") + [{"text": "Descrição", "x0": 70, "top": 160}, {"text": "TAE", "x0": 300, "top": 160}]
             + [{"text": "10/08", "x0": 40, "top": 200}, {"text": "Pagamento", "x0": 70, "top": 200},
                {"text": "0,02€", "x0": 330, "top": 200}, {"text": "0,01€", "x0": 520, "top": 200}]
             + [{"text": "/2026", "x0": 40, "top": 210}, {"text": "de", "x0": 70, "top": 210}]
             + _texto(300, 40, "Cripto Extratos de operações")
             + _linha(330, data="1 de mar. de 2026", descricao="Bitcoin", montante="-50,00€", saldo="0,00€"))
    return [resumo, eur, krw, juros]


def test_interpretar():
    e = interpretar(paginas_ficticias())
    from app.parsers import verificar_saldos
    assert verificar_saldos(e.subcontas[("Atual", "EUR")]) == [] and verificar_saldos(e.subcontas[("Atual", "KRW")]) == []
    assert set(e.subcontas) == {("Atual", "EUR"), ("Atual", "KRW"), ("Todas as moedas", "EUR"), ("Depósito", "EUR")}
    eur = e.subcontas[("Atual", "EUR")]
    assert [(m.data_mov, m.montante, m.saldo) for m in eur][:2] == [(date(2026, 1, 5), -812, 27511),
                                                                     (date(2026, 1, 6), 2000, 29511)]
    assert eur[1].descricao == "Transferência imediata intrabancária de FULANO" and not eur[1].transferencia
    assert all(m.transferencia for m in eur if "Conversão" in m.descricao or "Carregamento" in m.descricao)
    krw = e.subcontas[("Atual", "KRW")]
    assert [(m.montante, m.saldo) for m in krw] == [(3341900, 3341900), (-2061200, 1280700)]
    todas = e.subcontas[("Todas as moedas", "EUR")]
    keta = next(m for m in todas if m.descricao.startswith("K-ETA"))
    assert keta.descricao == "K-ETA (-20.612 KRW)" and keta.montante == -1224 and keta.saldo is None
    assert next(m for m in todas if m.descricao == "Conversão cambial para KRW (33.419 KRW)").montante == 1966
    assert [(m.data_mov, m.montante) for m in e.subcontas[("Depósito", "EUR")]] == [(date(2026, 8, 10), 1)]
    assert e.dedup_dias == 4 and e.movimentos == todas
    assert not any("Bitcoin" in m.descricao for v in e.subcontas.values() for m in v)   # cripto fica de fora


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(revolut_pdf, "ler_pdf", lambda conteudo: interpretar(paginas_ficticias()))
    c = TestClient(create_app(tmp_path, backups_automaticos=False))
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    _post(c, "/contas/nova?entidade=1", nome="Revolut KRW", tipo="CARTEIRA_DIGITAL", moeda="KRW", instituicao_id="5")
    _post(c, "/contas/nova?entidade=1", nome="Revolut reforçada", tipo="POUPANCA", moeda="EUR", instituicao_id="5")
    return c


PDF = b"%PDF-1.7 ficticio"


def test_importar_pdf_nas_tres_contas(admin, tmp_path):
    previa, r = _importar(admin, "consolidated-statement.pdf", PDF, conta_id="1")
    assert r.status_code == 303 and "Importar 6 movimentos" in previa.text      # 4 em euros + 2 convertidos
    previa, r = _importar(admin, "consolidated-statement.pdf", PDF, conta_id="2")
    assert "Importar 2 movimentos" in previa.text
    previa, r = _importar(admin, "consolidated-statement.pdf", PDF, conta_id="3")
    assert "Importar 1 movimentos" in previa.text
    previa, _ = _importar(admin, "consolidated-statement.pdf", PDF, conta_id="1")
    assert "Importar 0 movimentos" in previa.text                                  # reimportar não duplica


def test_nao_duplica_o_que_veio_do_csv(admin, app=None):
    # o mesmo «Amazon» importado antes pelo CSV, com a data de início um dia antes e o mesmo saldo
    csv = ("Tipo,Produto,Data de início,Data de Conclusão,Descrição,Montante,Comissão,Moeda,Estado,Saldo\n"
           "Pagamento com cartão,Atual,2026-01-04 20:00:00,2026-01-05 09:00:00,Amazon,-8.12,0.00,EUR,CONCLUÍDA,275.11\n")
    previa, r = _importar(admin, "revolut.csv", csv.encode(), conta_id="1")
    assert r.status_code == 303
    previa, _ = _importar(admin, "consolidated-statement.pdf", PDF, conta_id="1")
    assert "Importar 5 movimentos" in previa.text and previa.text.count("já existe") == 1


def test_comissao_separada_no_csv_e_data_de_conclusao(admin):
    # CSV: carregamento bruto 252,50 e comissão −2,50 em linha à parte (saldo final 545,11), 3 dias antes da data do PDF
    csv = ("Tipo,Produto,Data de início,Data de Conclusão,Descrição,Montante,Comissão,Moeda,Estado,Saldo\n"
           "Carregamento,Atual,2026-01-26 10:00:00,2026-01-29 10:00:00,Carregamento de FULANO,252.50,2.50,EUR,CONCLUÍDA,545.11\n")
    _importar(admin, "revolut.csv", csv.encode(), conta_id="1")
    previa, _ = _importar(admin, "consolidated-statement.pdf", PDF, conta_id="1")
    assert previa.text.count("já existe") == 1 and "Importar 5 movimentos" in previa.text
