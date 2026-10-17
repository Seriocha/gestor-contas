"""Via Verde: leitura do extrato (CSV/ZIP), ligação aos débitos do banco e repartição pelos carros (matrícula)."""
import io
import re
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import patrimonio as pt
from app import viaverde as vv
from app.main import create_app
from app.models import Bem, Movimento, ViaVerde
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

HOJE = date(2026, 10, 17)
CAB = ("IDENTIFICADOR;MATRÍCULA;REFERÊNCIA MB;DATA ENTRADA;HORA ENTRADA;ENTRADA;DATA SAÍDA;HORA SAÍDA;SAÍDA;VALOR;"
       "VALOR DESCONTO;TAXA IVA;OPERADOR;SERVIÇO;NUMERO DOCUMENTO;DATA PAGAMENTO;CARTAO N.º;DURAÇÃO;QUANTIDADE;\n")


def _csv(linhas, mes="09"):
    txt = (f"sep=;\nDOCUMENTO N.º;000/{mes}/2026\nDATA DE EMISSÃO;Set-2026\nNIF;999999990\nCONTRATO N.º;111222333\n"
           f"NOME;FULANO DE TAL\nMORADA;RUA X\n\n" + CAB)
    for mat, ent, saida, data, valor, servico, pag in linhas:
        txt += (f"1;{mat};0;{data if ent else ''};{'10:00' if ent else ''};{ent};{data};11:00;{saida};{valor};0,00;23;"
                f"Operador (OP);{servico};FT X;{pag};1;;;\n")
    return txt.encode("latin-1")


SETEMBRO = [("AA-11-BB", "Lisboa", "Leiria", "06-09-2026", "10,60", "Portagens", "14-09-2026"),
            ("CC-22-DD", "", "PQ Centro", "07-09-2026", "1,40", "Parques de estacionamento", "14-09-2026"),
            ("AA-11-BB", "", "Ponte", "09-09-2026", "3,40", "Portagens", "21-09-2026"),
            ("CC-22-DD", "", "PQ Colombo", "29-09-2026", "2,10", "Parques de estacionamento", ""),
            ("CC-22-DD", "", "Via Verde Mobilidade", "29-09-2026", "1,99", "Transacção Via Verde", "")]


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _mov(c, 1, "2026-09-15", "12,00", "Via Verde", sentido="saida")      # 10,60 + 1,40
    _mov(c, 1, "2026-09-22", "3,40", "Via Verde", sentido="saida")
    _mov(c, 1, "2026-10-02", "4,09", "Via Verde", sentido="saida")       # sem data de pagamento: 2,10 + 1,99
    _mov(c, 1, "2026-10-05", "9,99", "Via Verde", sentido="saida")       # sem extrato: fica por ligar
    return c


def _carro(c, nome, mat, **extra):
    r = _post(c, "/patrimonio/novo?entidade=1&tipo=carro", nome=nome, uso="principal", matricula=mat, marca="VW",
              modelo="Golf", ano="2019", revisao_meses="0", **extra)
    assert r.status_code == 303, r.text
    return int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))


def _upload(c, ficheiros):
    f = c.get("/patrimonio?entidade=1")
    return c.post("/patrimonio/via-verde", data={"csrf": _csrf(f), "entidade": "1"},
                  files=[("ficheiros", (n, d, "application/octet-stream")) for n, d in ficheiros], follow_redirects=False)


def test_ler_csv_e_zip():
    ext = vv.ler("x.csv", _csv(SETEMBRO))
    assert len(ext.linhas) == 5 and ext.linhas[0].matricula == "AA11BB" and ext.linhas[0].valor == 1060
    assert ext.linhas[0].entrada == "Lisboa" and ext.linhas[1].entrada is None and ext.linhas[3].data_pagamento is None
    assert ext.linhas[4].servico == "Transacção Via Verde" and ext.linhas[0].contrato == "111222333"
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("EXTRACTO_09.csv", _csv(SETEMBRO))
        z.writestr("EXTRACTO_08.csv", _csv(SETEMBRO[:1], "08"))
    ext = vv.ler("x.zip", zbuf.getvalue())
    assert ext.ficheiros == 2 and len(ext.linhas) == 6
    with pytest.raises(ValueError):
        vv.ler("x.csv", b"Data;Valor\n01-01-2026;1,00\n")


def test_importar_ligar_e_repartir(admin, app):
    golf = _carro(admin, "Golf", "AA-11-BB", omissao="1")
    clio = _carro(admin, "Clio", "cc 22 dd")
    r = _upload(admin, [("set.csv", _csv(SETEMBRO))])
    assert r.status_code == 303
    assert "5 linhas novas" in admin.get("/patrimonio?entidade=1").text
    r = _upload(admin, [("set.csv", _csv(SETEMBRO))])                     # repetido: nada de novo
    with app.state.SessionLocal() as db:
        assert db.query(ViaVerde).count() == 5
        ligados = {l.saida: l.movimento.data_mov for l in db.scalars(select(ViaVerde))}
        assert ligados == {"Leiria": date(2026, 9, 15), "PQ Centro": date(2026, 9, 15), "Ponte": date(2026, 9, 22),
                           "PQ Colombo": date(2026, 10, 2), "Via Verde Mobilidade": date(2026, 10, 2)}
        # o débito de 15/09 (12,00) reparte-se: 10,60 Golf + 1,40 Clio
        movs = db.scalars(select(Movimento)).all()
        p = {(m.data_mov, bid): v for m, bid, v in pt.por_bem(db, 1, movs) if m.descricao_original == "Via Verde"}
        assert p[(date(2026, 9, 15), golf)] == -1060 and p[(date(2026, 9, 15), clio)] == -140
        assert p[(date(2026, 10, 5), db.get(Movimento, max(m.id for m in movs)).bem_id)] == -999
        c_clio = pt.custos(db, db.get(Bem, clio), date(2025, 11, 1), HOJE)
        assert c_clio.despesas == 140 + 210 + 199
        assert vv.resumo(db, 1)["sem_carro"] == [] and vv.resumo(db, 1)["soltas"] == 0
    ficha = admin.get(f"/patrimonio/{clio}").text
    assert "Via Verde" in ficha and "PQ Colombo" in ficha and "de -12,00" in ficha
    m15 = admin.get("/movimentos/1").text
    assert "Detalhe Via Verde" in m15 and "AA-11-BB" in m15


def test_ligar_quando_o_banco_chega_depois(admin, app):
    _upload(admin, [("set.csv", _csv([("AA-11-BB", "", "Ponte", "20-10-2026", "7,77", "Portagens", "30-10-2026")]))])
    with app.state.SessionLocal() as db:
        assert db.scalar(select(ViaVerde)).movimento_id is None
    _mov(admin, 1, "2026-10-31", "7,77", "Via Verde", sentido="saida")
    _post(admin, "/patrimonio?entidade=1", "/patrimonio/atribuir", entidade="1")
    with app.state.SessionLocal() as db:
        assert db.scalar(select(ViaVerde)).movimento.data_mov == date(2026, 10, 31)
    p = admin.get("/patrimonio?entidade=1").text
    assert "sem carro com esta matrícula" in p
