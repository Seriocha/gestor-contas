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


def _heranca(app, nome="Mãe"):
    """O Lar faz parte da gestão das heranças: a entidade passa a herança com o módulo «lar» (como a migração 0045)."""
    from sqlalchemy import select
    from app.models import Entidade
    with app.state.SessionLocal() as db:
        e = db.scalar(select(Entidade).where(Entidade.nome == nome))
        e.tipo = "heranca"
        e.modulos = (e.modulos or "") + ",lar"
        db.commit()


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


def _ligar(faturas, santa_casa, **kw):
    c = lar.conciliar(faturas, santa_casa, [], hoje=kw.pop("hoje", date(2026, 12, 15)), **kw)
    return {(tuple(f.numero for f in l.faturas), tuple(e.chave for e in l.lancamentos)): l for l in c.farmacia}, c


def test_farmacia_setembro_um_lancamento_por_fatura():
    """O caso real de setembro: a carta lista NLD 3077 (1,67), NLD 3298 (1,81) e FT 5755 (21,00) sem os PDF;
    cada fatura da farmácia liga-se, pelo valor, ao lançamento do mesmo mês ou do seguinte."""
    fs = [(1, farmacia(47843, "18-09-2026", "21,00")), (2, farmacia(47700, "08-09-2026", "1,67")),
          (3, farmacia(47900, "22-09-2026", "1,81"))]
    sc = [(10, carta("15-10-2026", [("15-09-2026", "NLD", 2026, 3077, "1,67"), ("30-09-2026", "NLD", 2026, 3298, "1,81"),
                                    ("30-09-2026", "FT", 2026, 5755, "21,00"), ("01-10-2026", "FT", 2026, 6000, "1.339,41")], "1.363,89"))]
    lig, c = _ligar(fs, sc)
    assert lig[(("FT U014/47843",), ("FT 2026/5755",))].metodo == "mesmo valor"
    assert lig[(("FT U014/47700",), ("NLD 2026/3077",))].ok and lig[(("FT U014/47900",), ("NLD 2026/3298",))].ok
    assert not any("FT 2026/6000" in k[1] for k in lig)                 # a mensalidade não entra
    assert not any(t[0] == "farmacia" for t in c.alertas)


def test_farmacia_uma_fatura_varios_lancamentos():
    """Uma fatura de 24,48 € cobrada pela Santa Casa em três lançamentos (1,67 + 1,81 + 21,00)."""
    fs = [(1, farmacia(47843, "18-09-2026", "24,48"))]
    sc = [(10, carta("15-10-2026", [("15-09-2026", "NLD", 2026, 3077, "1,67"), ("30-09-2026", "NLD", 2026, 3298, "1,81"),
                                    ("30-09-2026", "FT", 2026, 5755, "21,00")], "24,48"))]
    lig, _ = _ligar(fs, sc)
    l = lig[(("FT U014/47843",), ("NLD 2026/3077", "NLD 2026/3298", "FT 2026/5755"))]
    assert l.ok and l.metodo == "soma de lançamentos"


def test_farmacia_varias_faturas_num_lancamento_e_sem_par():
    fs = [(1, farmacia(1, "03-10-2026", "2,64")), (2, farmacia(2, "10-10-2026", "10,00")),     # juntas na NLD de 15/10
          (3, farmacia(3, "20-10-2026", "7,77")),                                                # ninguém a cobrou
          (4, farmacia(4, "05-12-2026", "4,00"))]                                                # ainda dentro do prazo
    sc = [(10, nld(1, "15-10-2026", "12,64")), (11, nld(2, "31-10-2026", "9,50")),
          (12, nld(3, "31-10-2026", "5,00", "26 - Reembolso de Consultas")),                    # não é farmácia
          (13, nld(9, "15-03-2026", "30,21"))]                                                  # antes da 1.ª fatura
    lig, c = _ligar(fs, sc)
    assert lig[(("FT U014/1", "FT U014/2"), ("NLD 2026/1",))].metodo == "soma de faturas"
    assert lig[((), ("NLD 2026/2",))].metodo == "sem fatura"
    assert lig[(("FT U014/3",), ())].metodo == "sem lançamento" and lig[(("FT U014/4",), ())].metodo == "sem lançamento"
    assert not any("NLD 2026/3" in k[1] for k in lig) and c.n_antigas == 1
    textos = " | ".join(t for _, t in c.alertas)
    assert "FT U014/3" in textos and "FT U014/4" not in textos and "NLD 2026/2 de 31/10/2026 (9,50 €) sem fatura" in textos


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
    docs = {1: [(10, farmacia(1, "03-11-2026", "2,64")), (11, farmacia(2, "10-11-2026", "10,00"))],
            2: [(20, nld(1, "15-11-2026", "12,64")), (21, ft(5319, "01-11-2026")),
                (22, carta("04-11-2026", [("01-11-2026", "FT", 2026, 5319, "1.339,41")], "1.339,41"))]}

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/correspondents/":
            return {"results": [{"id": 1, "name": "Farmácia Nova de Santo André"}, {"id": 2, "name": "Santa Casa STC"}]}
        if caminho == "/api/documents/":
            cid = int(params["correspondent__id"])
            return {"results": [{"id": i, "content": t} for i, t in docs[cid]]}
        raise AssertionError(caminho)
    monkeypatch.setattr(paperless, "_pedido", pedido)
    return docs


def test_pagina_lar(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    r = c.get("/lar")
    assert r.status_code == 200, r.text
    assert "Lar · conciliação" in r.text and "2 faturas da farmácia" in r.text and "certo" in r.text and "soma de faturas" in r.text
    # regularização gravada pela migração 0044: pago até 31/10/2026, validar desde 01/01/2026
    assert "tudo pago até 31/10/2026" in r.text and "FT 2026/5319" in r.text and "Lar (Santa Casa)" in r.text
    assert "por validar" in r.text and "Validar o mês" in r.text and "novembro 2026" in r.text
    from tests.test_app import _csrf
    r = c.post("/lar/config", data={"csrf": _csrf(r), "entidade": "", "santa_casa": "Santa Casa STC", "farmacia": ""},
               follow_redirects=True)
    assert "Configuração do Lar guardada" in r.text and "2 faturas da farmácia" in r.text


def test_regularizado_e_por_validar():
    """Só se mostra o que é depois de «tudo pago até» e a partir de «validar desde» (a mais tardia das duas)."""
    sc = [(1, nld(765, "15-12-2025", "30,21", ano=2025)), (2, ft(2291, "01-03-2026")), (3, nld(800, "15-11-2026", "9,99")),
          (4, carta("04-11-2026", [("15-12-2025", "NLD", 2025, 765, "31,21"), ("01-11-2026", "FT", 2026, 3000, "1.339,41")], "1.370,62"))]
    fs = [(9, farmacia(1, "02-11-2026", "1,00"))]
    kw = dict(hoje=date(2026, 12, 26), pago_ate=date(2026, 10, 31), validar_desde=date(2026, 1, 1))
    c = lar.conciliar(fs, sc, [], **kw)
    est = {e.chave: e for e in c.estados}
    assert c.inicio == date(2026, 11, 1) and set(est) == {"NLD 2026/800", "FT 2026/3000"} and c.n_fora == 2
    assert [m["mes"] for m in c.meses] == [date(2026, 11, 1)] and c.n_por_validar == 2
    assert est["FT 2026/3000"].falta(c.pago_ate) == ["PDF em falta", "recibo em falta"]
    assert est["FT 2026/3000"].estado(c.ultima_carta, c.pago_ate) == "por pagar"
    textos = " | ".join(t for _, t in c.alertas)
    assert "765" not in textos                                    # o valor mudou, mas é de antes do início: não alerta
    assert "FT 2026/3000" in textos and "NLD 2026/800" in textos  # por pagar há 55 dias; sem fatura da farmácia
    # um limite só: «validar desde» mais tardio que «pago até» também manda
    c2 = lar.conciliar(fs, sc, [], hoje=date(2026, 12, 26), pago_ate=date(2026, 2, 28), validar_desde=date(2026, 11, 10))
    assert c2.inicio == date(2026, 11, 10) and {e.chave for e in c2.estados} == {"NLD 2026/800"}
    # depois de validada, deixa de alertar
    c = lar.conciliar(fs, sc, [], validados={"NLD 2026/800": {"em": date(2026, 12, 1), "por": "Sérgio", "nota": ""}}, **kw)
    assert "NLD 2026/800" not in " | ".join(t for _, t in c.alertas) and c.n_por_validar == 1
    assert next(l for l in c.farmacia if l.lancamentos and l.lancamentos[0].chave == "NLD 2026/800").lancamentos[0].validacao == "validado"


def test_validar_e_anular(tmp_path, falso):
    from sqlalchemy import select
    from app.models import Auditoria, LarValidacao
    from tests.test_app import _csrf
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
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


def test_falta_pdf_so_na_carta():
    """Documento que só aparece numa carta (o PDF não foi importado para o Paperless)."""
    sc = [(1, carta("04-03-2026", [("01-03-2026", "FT", 2026, 999, "1.339,41")], "1.339,41"))]
    c = lar.conciliar([], sc, [], hoje=date(2026, 10, 26), validar_desde=date(2026, 1, 1))
    e = c.estados[0]
    assert not e.tem_pdf and e.mes == date(2026, 3, 1)
    assert "PDF em falta" in e.falta()


def test_validar_o_mes(tmp_path, falso):
    from sqlalchemy import select
    from app.models import LarValidacao
    from tests.test_app import _csrf
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    r = c.get("/lar")
    r = c.post("/lar/validar", data={"csrf": _csrf(r), "acao": "validar_mes", "mes_chave": ["NLD 2026/1", "FT 2026/5319"]},
               follow_redirects=True)
    assert "2 documentos validados" in r.text
    with app.state.SessionLocal() as db:
        assert {v.chave for v in db.scalars(select(LarValidacao))} == {"NLD 2026/1", "FT 2026/5319"}


def test_nao_reconhecidos_e_anulados():
    sc = [(1, ft(2291, "01-03-2026")), (2, "DOC. ANULADO\n" + nld(5, "01-03-2026", "1,00")),
          (3, "CARACTERIZAÇÃO SÓCIO-ECONÓMICA\nAtualização de Mensalidade\nNome: Maria Exemplo"), (4, "")]
    c = lar.conciliar([], sc + [(5, "Ficha antiga", "2025-05-01")], [], validar_desde=date(2026, 1, 1))
    assert c.n_anulados == 1 and [pid for pid, *_ in c.nao_reconhecidos] == [3, 4]     # a de 2025 fica escondida
    assert c.nao_reconhecidos[0][1].startswith("CARACTERIZAÇÃO SÓCIO-ECONÓMICA · Atualização")
    assert c.nao_reconhecidos[1][1] == "(sem texto: o OCR falhou?)"


def test_pagina_procurar_e_aviso_sem_datas(tmp_path, falso, monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_LINK", "http://192.168.1.232:8300")
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    falso[2].append((23, carta("05-11-2026", [("03-11-2026", "NLD", 2026, 3298, "1,81")], "1,81")))
    r = c.get("/lar?renovar=1")                                     # a página guarda o Paperless em memória 10 min
    # a NLD 3298 só está na carta (o PDF falta): ligação para a procurar no Paperless pelo n.º
    assert "http://192.168.1.232:8300/documents?query=NLDUT12026/3298" in r.text
    assert "documents?query=FTUT12026/5319" not in r.text           # esta tem PDF
    assert "As datas de regularização não estão definidas" not in r.text
    from app import preferencias
    with app.state.SessionLocal() as db:
        preferencias.gravar(db, "lar_pago_ate", "")
        db.commit()
        app.state.prefs = preferencias.carregar(db)
    r = c.get("/lar")
    assert "As datas de regularização não estão definidas (tudo pago até)" in r.text


def test_lar_so_nas_herancas(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    r = c.get("/lar")
    assert "O Lar faz parte da gestão das heranças" in r.text and 'href="/lar"' not in r.text
    assert 'href="/lar"' not in c.get("/inicio").text                       # o menu não mostra o Lar
    _heranca(app)
    assert 'href="/lar"' in c.get("/inicio").text and "Lar · conciliação" in c.get("/lar").text


def regularizacao(num, data, docs, total, via="ORIGINAL"):
    linhas = "\n".join(f"{t} {t}UT1/{a} {n} 1 {v} {v}" for t, a, n, v in docs)
    return (f"Santa Casa da Misericórdia de Exemplo {via}\nRua Exemplo n.º 1\n*165533*\n"
            f"Regularização n.º PNL NLCDIV2026/{num} Maria Exemplo\nData {data} Moeda: EUR Contribuinte: 100000000\n"
            f"Códigos do Ano/Mês: 2026/9 QTD. Valor Total IVA %\n{linhas}\n0,00 0,00 TOTAL {total}\n"
            f"Observações: Tipo Pagamento: Bancos - CCAM\nRecebido em 01 de SETEMBRO de 2026\n"
            f"Processado Por Computador. Este documento não serve de factura.")


def comprovativo(data, valor, info):
    return (f"Pág. 1/2\nID operação: 300000000001\nUtilizador: FULANO EXEMPLO Emitido a: {data} 10:06\n"
            f"Foi registada uma operação com os seguintes dados:\nDetalhe da Operação Operação\nEnviar Nacional\n"
            f"Conta a Debitar\n40000000000\nNome do Beneficiário\nSANTA CASA DA MISERICORDIA EXEMPLO\nMontante\n{valor} €\n"
            f"Tipo de Operação\nTransferência\nInformação Complementar\n{info}\nDados da Operação Data de Registo\n"
            f"{data} 10:06\nData da Operação\n{data}\nCanal\nCA Online\nEstado\nAceite")


def test_regularizacao_e_comprovativo():
    r = lar.ler_santa_casa(1, regularizacao(1436, "01-09-2026", [("NLD", 2026, 2709, "3,80"), ("NLD", 2026, 2439, "3,95"),
                                                                ("NLD", 2026, 2302, "1,77")], "9,52"))
    assert r.tipo == "regularização" and r.numero == "NLCDIV2026/1436" and r.data == date(2026, 9, 1) and r.total == 952
    assert r.docs == [("NLD 2026/2709", 380), ("NLD 2026/2439", 395), ("NLD 2026/2302", 177)]
    t = lar.ler_santa_casa(2, comprovativo("26/10/2026", "1.392,01", "01-10-2026 FTUT12026 / 00006073 c/extras"))
    assert t.tipo == "comprovativo" and t.total == 139201 and t.data == date(2026, 10, 26)
    assert t.docs == [("FT 2026/6073", 139201)] and t.numero == "transferência 300000000001"

    sc = [(1, nld(2709, "15-08-2026", "3,80")), (2, regularizacao(1436, "01-09-2026", [("NLD", 2026, 2709, "3,80")], "3,80")),
          (3, regularizacao(1436, "01-09-2026", [("NLD", 2026, 2709, "3,80")], "3,80", via="2.ª VIA")),
          (4, ft(6073, "01-10-2026", "1.392,01")),
          (5, comprovativo("26/10/2026", "1.392,01", "01-10-2026 FTUT12026 / 00006073 c/extras"))]
    movs = [SimpleNamespace(id=8, montante=-380, data_mov=date(2026, 9, 1), descricao_original="TRF SANTA CASA"),
            SimpleNamespace(id=9, montante=-139201, data_mov=date(2026, 10, 26), descricao_original="TRF SANTA CASA")]
    c = lar.conciliar([], sc, movs, hoje=date(2026, 10, 27))
    est = {e.chave: e for e in c.estados}
    assert c.nao_reconhecidos == []                                   # nenhum fica por ler
    assert est["NLD 2026/2709"].estado(c.ultima_carta) == "pago" and est["NLD 2026/2709"].recibo.tipo == "regularização"
    ft6073 = est["FT 2026/6073"]
    assert ft6073.estado(c.ultima_carta) == "transferido" and "recibo por emitir (transferência de 26/10)" in ft6073.falta()
    assert {(r.tipo, m.id if m else None) for r, m in c.pagamentos} == {("regularização", 8), ("comprovativo", 9)}
    assert len(c.pagamentos) == 2                                      # a 2.ª via não duplica
    # chega o recibo da FT: o débito passa para o recibo e a transferência sai dos pagamentos
    c = lar.conciliar([], sc + [(6, recibo(1500, "30-10-2026", [("FT", 2026, 6073, "1 392,01")], "1392,01"))], movs,
                      hoje=date(2026, 11, 2))
    assert {(r.tipo, m.id if m else None) for r, m in c.pagamentos} == {("regularização", 8), ("recibo", 9)}
    assert {e.chave: e for e in c.estados}["FT 2026/6073"].estado(c.ultima_carta) == "pago"
