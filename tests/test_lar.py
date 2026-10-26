"""Lar (Santa Casa): leitura das faturas da farmácia, notas de lançamento, faturas, cartas de aviso e recibos (textos
sintéticos com a estrutura dos documentos reais; nomes e números fictícios) e a conciliação entre eles."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import lar, paperless
from app.main import create_app
from tests.test_app import PW, _post


def farmacia(num, data, total, produtos=(("Paracetamol 1 g", "2,64"),)):
    linhas = "\n".join(f"{p}\n6% 1 5,51 € 0,00 € 52,09% 2,87 € 2,87 € {v} €\nComp 10 ml" for p, v in produtos)
    return (f"Original Fatura n.º: FT U014/{num}\nData: {data} 12:13:36\nNº Venda: 1 Op: X\nFARMACIA NOVA Exmo(s). Sr(s). "
            f"Cliente N.º: 1\nMARIA EXEMPLO\nProduto IVA Qtd. PVP PRef %CompE CompE CompT Líquido\n{linhas}\n"
            f"Total a pagar {total} €\nTotal Utente (Euros): {total}\nATCUD: X-{num} Debitado em Conta: (Euros) {total} €")


def nld(num, data, valor, cod="22 - Reembolso de Medicamentos", ano=2026):
    return (f"Santa Casa da Misericórdia de Exemplo ORIGINAL\nNota Lançamento(Débito) n.º NLD NLDUT1{ano}/{num}\n"
            f"Data {data} CAE: 87301 V/ Nº Contrib. 100000000\nCódigos Qtd. Valor\n{cod} 1,00 {valor}\n"
            f"{cod} 1,00 {valor}\nTotal {valor}\nObservações:")


def ft(num, data, total="1.339,41", ano=2026):
    return (f"ATCUD:X-{num}\nSanta Casa da Misericórdia de Exemplo ORIGINAL\nFactura n.º FT FTUT1{ano}/{num} CAE: 87301\n"
            f"Data {data} Moeda: EUR\nCódigos do Ano/Mês: {ano}/4 QTD. Valor Total Desconto IVA %\n"
            f"39 - Mensalidade ERPI 1 697,58 697,58 0\n41 - Comp. Descend. Lar 1 641,83 641,83 0\n"
            f"SUB TOTAL 1339,41 IVA 0,00 TOTAL {total.replace('.', '')}")


def carta(data, docs, total):
    linhas = "\n".join(f"{d} {t} {t}UT1{a} / {n:08d} {v} € {v} € {d}" for d, t, a, n, v in docs)
    return (f"Santa Casa da Misericórdia de Exemplo\nCarta Informativa/Aviso\nData: {data}\nUtente: 2-1 Maria Exemplo\n"
            f"Data Doc. Tipo Número Valor Valor por Pagar Limite Pagamento\n{linhas}\nTotal : {total} €\nTotal por Pagar : {total} €")


def recibo(num, data, docs, total):
    linhas = "\n".join(f"{t} {t}UT1/{a} {n} D {v} {v}" for t, a, n, v in docs)
    return (f"ATCUD:X\nSanta Casa da Misericórdia de Exemplo ORIGINAL\nRecebimento n.º REC PB2026/{num} Maria Exemplo\n"
            f"Data {data} Moeda: EUR\nDocumentos C\\D Valor Pag. Valor Pag. IVA %\n{linhas}\n0,00 0,00 TOTAL {total}\n"
            f"Observações: Tipo Pagamento: Bancos - CCAM")


def test_cent_e_leitores():
    assert lar.cent("1.339,41") == 133941 and lar.cent("1 339,41") == 133941 and lar.cent("2,64 €") == 264
    f = lar.ler_farmacia(1, farmacia(49113, "17-10-2026", "2,64"))
    assert f.numero == "FT U014/49113" and f.data == date(2026, 10, 17) and f.total == 264 and f.debitado
    assert f.itens == [("Paracetamol 1 g", 264)]
    n = lar.ler_santa_casa(2, nld(221, "31-01-2026", "43,40"))
    assert n.chave == "NLD 2026/221" and n.medicamentos == 4340 and n.total == 4340 and len(n.linhas) == 1   # linha repetida
    x = lar.ler_santa_casa(3, ft(2291, "01-04-2026"))
    assert x.chave == "FT 2026/2291" and x.total == 133941 and [c for c, _, _ in x.linhas] == ["39", "41"] and x.medicamentos == 0
    c = lar.ler_santa_casa(4, carta("04-04-2026", [("15-03-2026", "NLD", 2026, 765, "30,21"), ("01-04-2026", "FT", 2026, 2291, "1.339,41")], "1.369,62"))
    assert c.data == date(2026, 4, 4) and [d[0] for d in c.docs] == ["NLD 2026/765", "FT 2026/2291"] and c.total == 136962
    r = lar.ler_santa_casa(5, recibo(1265, "09-04-2026", [("FT", 2026, 2291, "1 339,41")], "1339,41"))
    assert r.numero == "PB2026/1265" and r.docs == [("FT 2026/2291", 133941)] and r.total == 133941 and "CCAM" in r.pagamento
    assert lar.ler_santa_casa(6, "DOC. ANULADO\n" + nld(1, "01-01-2026", "1,00")) is None
    assert lar.ler_farmacia(7, "uma fatura qualquer") is None


def test_conciliar_medicamentos():
    fs = [lar.ler_farmacia(i, t) for i, t in enumerate([
        farmacia(1, "03-10-2026", "2,64"), farmacia(2, "10-10-2026", "10,00"),       # 1.ª quinzena = 12,64
        farmacia(3, "17-10-2026", "2,53"), farmacia(4, "23-10-2026", "26,55"),       # 2.ª quinzena = 29,08
        farmacia(5, "28-10-2026", "5,00"),                                           # esquecida na NLD de 31/10
        farmacia(6, "02-11-2026", "4,00")], 1)]                                      # ainda por cobrar
    ns = [lar.ler_santa_casa(10, nld(1, "15-10-2026", "12,64")), lar.ler_santa_casa(11, nld(2, "31-10-2026", "29,08")),
          lar.ler_santa_casa(12, nld(3, "15-11-2026", "9,99")), lar.ler_santa_casa(13, nld(4, "15-11-2026", "5,00", "26 - Reembolso de Consultas"))]
    linhas, por_cobrar = lar.conciliar_medicamentos(fs, ns)
    assert [(l.nld.numero, l.ok, l.metodo) for l in linhas] == [(1, True, "quinzena"), (2, True, "combinação"), (3, False, "sem correspondência")]
    assert linhas[2].diferenca == 999 - 400                     # a quinzena só tem a fatura de 4,00
    assert [f.numero for f in por_cobrar] == ["FT U014/5", "FT U014/6"]
    # NLD antiga, de antes da 1.ª fatura da farmácia no Paperless: não é erro
    l, _ = lar.conciliar_medicamentos(fs, [lar.ler_santa_casa(14, nld(9, "15-03-2026", "30,21"))])
    assert l[0].metodo == "sem faturas no Paperless"


def test_conciliar_cartas_recibos_pagamentos():
    sc = [(1, nld(765, "15-03-2026", "30,21")), (2, ft(2291, "01-04-2026")),
          (3, carta("04-04-2026", [("15-03-2026", "NLD", 2026, 765, "30,21"), ("01-04-2026", "FT", 2026, 2291, "1.339,41")], "1.369,62")),
          (4, carta("04-05-2026", [("15-03-2026", "NLD", 2026, 765, "31,21"), ("01-05-2026", "FT", 2026, 3018, "1.339,41")], "1.370,62")),
          (5, recibo(1265, "09-04-2026", [("FT", 2026, 2291, "1 339,41")], "1339,41")),
          (6, recibo(1300, "09-05-2026", [("FT", 2026, 3018, "1 339,41")], "1339,41"))]
    movs = [SimpleNamespace(id=7, montante=-133941, data_mov=date(2026, 4, 7), descricao_original="TRF SANTA CASA")]
    c = lar.conciliar([], sc, movs, hoje=date(2026, 7, 1))
    est = {e.chave: e for e in c.estados}
    assert est["FT 2026/2291"].estado(c.ultima_carta) == "pago" and est["FT 2026/2291"].recibo.numero == "PB2026/1265"
    assert est["NLD 2026/765"].estado(c.ultima_carta) == "por pagar" and est["NLD 2026/765"].valores == [3021, 3121]
    assert c.pagamentos[1][1].id == 7 and c.pagamentos[0][1] is None
    textos = " | ".join(t for _, t in c.alertas)
    assert "o valor mudou" in textos and "continua por pagar" in textos and "PB2026/1300" in textos and "sem débito" in textos


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    docs = {1: [(10, farmacia(1, "03-10-2026", "2,64")), (11, farmacia(2, "10-10-2026", "10,00"))],
            2: [(20, nld(1, "15-10-2026", "12,64")), (21, ft(5319, "01-10-2026")),
                (22, carta("04-10-2026", [("01-10-2026", "FT", 2026, 5319, "1.339,41")], "1.339,41"))]}

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/correspondents/":
            return {"results": [{"id": 1, "name": "Farmácia Nova de Santo André"}, {"id": 2, "name": "Santa Casa STC"}]}
        if caminho == "/api/documents/":
            cid = int(params["correspondent__id"])
            return {"results": [{"id": i, "content": t} for i, t in docs[cid]]}
        raise AssertionError(caminho)
    monkeypatch.setattr(paperless, "_pedido", pedido)


def test_pagina_lar(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    r = c.get("/lar")
    assert r.status_code == 200, r.text
    assert "Lar · conciliação" in r.text and "2 faturas da farmácia" in r.text and "certo" in r.text and "quinzena" in r.text
    # regularização gravada pela migração 0044: pago até 31/10/2026, validar desde 01/01/2026
    assert "tudo pago até 31/10/2026" in r.text and "FT 2026/5319" in r.text and "Lar (Santa Casa)" in r.text
    assert "por validar" in r.text and "Validar marcados" in r.text
    from tests.test_app import _csrf
    r = c.post("/lar/config", data={"csrf": _csrf(r), "entidade": "", "santa_casa": "Santa Casa STC", "farmacia": ""},
               follow_redirects=True)
    assert "Configuração do Lar guardada" in r.text and "2 faturas da farmácia" in r.text


def test_regularizado_e_por_validar():
    sc = [(1, nld(765, "15-12-2025", "30,21", ano=2025)), (2, ft(2291, "01-03-2026")), (3, nld(800, "15-03-2026", "9,99")),
          (4, carta("04-03-2026", [("15-12-2025", "NLD", 2025, 765, "30,21"), ("01-03-2026", "FT", 2026, 2291, "1.339,41")], "1.369,62")),
          (5, carta("04-04-2026", [("15-12-2025", "NLD", 2025, 765, "31,21"), ("01-03-2026", "FT", 2026, 2291, "1.339,41")], "1.370,62"))]
    fs = [(9, farmacia(1, "02-03-2026", "1,00"))]
    c = lar.conciliar(fs, sc, [], hoje=date(2026, 10, 26), pago_ate=date(2026, 10, 31), validar_desde=date(2026, 1, 1))
    est = {e.chave: e for e in c.estados}
    assert est["NLD 2025/765"].validacao == "regularizado" and est["FT 2026/2291"].validacao == "por validar"
    assert est["FT 2026/2291"].estado(c.ultima_carta, c.pago_ate) == "pago (declarado)"
    assert c.n_por_validar == 2                                  # FT 2291 e NLD 800
    textos = " | ".join(t for _, t in c.alertas)
    assert "NLD 2025/765" not in textos                          # regularizado: o valor mudou, mas não alerta
    assert "continua por pagar" not in textos and "NLD 800" in textos   # a NLD de 2026 não bate com a farmácia
    # depois de validada, deixa de alertar
    c = lar.conciliar(fs, sc, [], hoje=date(2026, 10, 26), pago_ate=date(2026, 10, 31), validar_desde=date(2026, 1, 1),
                      validados={"NLD 2026/800": {"em": date(2026, 10, 26), "por": "Sérgio", "nota": ""}})
    assert "NLD 800" not in " | ".join(t for _, t in c.alertas) and c.n_por_validar == 1
    assert next(l for l in c.medicamentos if l.nld.numero == 800).validacao == "validado"


def test_validar_e_anular(tmp_path, falso):
    from sqlalchemy import select
    from app.models import Auditoria, LarValidacao
    from tests.test_app import _csrf
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    r = c.get("/lar?ver=por_validar")
    assert "NLD 2026/1" in r.text
    r = c.post("/lar/validar", data={"csrf": _csrf(r), "acao": "validar", "chave": ["NLD 2026/1", "FT 2026/5319"],
                                     "nota": "confirmado com a carta"}, follow_redirects=True)
    assert "2 documentos validados" in r.text and "tudo validado" in r.text
    with app.state.SessionLocal() as db:
        v = db.scalar(select(LarValidacao).where(LarValidacao.chave == "NLD 2026/1"))
        assert v.nota == "confirmado com a carta" and v.utilizador_id == 1
        assert db.scalar(select(Auditoria).where(Auditoria.acao == "lar_validacao"))
    r = c.post("/lar/validar", data={"csrf": _csrf(r), "acao": "anular:NLD 2026/1"}, follow_redirects=True)
    assert "1 documento com a validação anulada" in r.text and "1 por validar" in r.text
