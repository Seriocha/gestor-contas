"""Banco BiG: CSV da conta à ordem; página de importadores."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Movimento
from app.parsers import ler_extrato, todos
from app.parsers.big import descricao_sem_duplicado
from tests.test_app import PW, _post
from tests.test_importacao import _importar

CSV = '''"Movimento*","Data Valor","Data Mov.","Natureza","Moeda","Original","EUR","Contabilistico*"
"Juros 5512487550 Juros 5512487550","01-04-2026","01-04-2026","Crédito","EUR"," ","1,60","201,60"
"Transf. da Conta Liquidez p/ a FA Agre. Transf. da Conta Liquidez p/ a FA Agre.","09-03-2026","09-03-2026","Débito","EUR"," ","-800,00","200,00"
"Transf. da Conta Liquidez p/ a FA Equi. Transf. da Conta Liquidez p/ a FA Equi.","03-03-2026","02-03-2026","Débito","EUR"," ","-10.078,58","1.000,00"
"TRF IMEDIATA A CREDITO SEPA +SERGIO MANUEL.. TRF IMEDIATA A CREDITO SEPA +SERGIO MANUEL PEREIRA NUNES MATIAS","02-03-2026","02-03-2026","Crédito","EUR"," ","11.000,00","11.078,58"
"Levantamento SuperConta Levantamento SuperConta","10-02-2026","10-02-2026","Crédito","EUR"," ","2,00","78,58"
'''.encode("utf-8-sig")


def test_descricao_sem_duplicado():
    assert descricao_sem_duplicado("Juros 1 Juros 1") == "Juros 1"
    assert descricao_sem_duplicado("TRF +ANA .. TRF +ANA MELO") == "TRF +ANA MELO"
    assert descricao_sem_duplicado("TRF +SERGIO.. TRF +SERGIO MATIAS") == "TRF +SERGIO MATIAS"
    assert descricao_sem_duplicado("Pago em 3.. dias") == "Pago em 3.. dias"  # «..» sem repetição: fica igual


def test_parser_big():
    e = ler_extrato("BiG movimentos.csv", CSV)
    assert e.parser == "big" and e.moeda == "EUR" and not e.avisos  # cadeia de saldos coerente
    m = e.movimentos
    assert [x.descricao for x in m][:2] == ["Levantamento SuperConta",
                                            "TRF IMEDIATA A CREDITO SEPA +SERGIO MANUEL PEREIRA NUNES MATIAS"]
    assert (m[2].montante, m[2].saldo, m[2].transferencia) == (-1007858, 100000, True)  # aplicação em fundo
    assert m[0].transferencia and not m[1].transferencia and m[-1].montante == 160


@pytest.fixture
def admin(tmp_path):
    c = TestClient(create_app(tmp_path, backups_automaticos=False))
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="BiG ordem", tipo="ORDEM", moeda="EUR", instituicao_id="3")
    return c


def test_importar_big_e_pagina_de_importadores(admin):
    previa, r = _importar(admin, "BiG movimentos.csv", CSV)
    assert "Importar 5 movimentos" in previa.text and "parece ser" not in previa.text
    assert r.status_code == 303
    pagina = admin.get("/admin/importadores")
    assert pagina.status_code == 200
    for p in todos():
        assert p.banco in pagina.text and p.FICHA["onde"] in pagina.text
    assert "Contabilistico*" in pagina.text and "última" in pagina.text  # utilização do importador BiG


def test_todos_os_importadores_tem_ficha():
    campos = {"extrato", "ficheiro", "onde", "colunas", "ordem", "saldo", "destino", "notas"}
    for p in todos():
        assert set(p.FICHA) == campos, p.chave
