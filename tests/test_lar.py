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
    """A carta lista NLD 3077 (1,67), NLD 3298 (1,81), NLD 3400 (21,00) e FT 5755 (21,00) sem os PDF; cada fatura da
    farmácia liga-se, pelo valor, à NLD do mesmo mês ou do seguinte. As FT (FTUT) são faturação interna da Santa Casa."""
    fs = [(1, farmacia(47843, "18-09-2026", "21,00")), (2, farmacia(47700, "08-09-2026", "1,67")),
          (3, farmacia(47900, "22-09-2026", "1,81"))]
    sc = [(10, carta("15-10-2026", [("15-09-2026", "NLD", 2026, 3077, "1,67"), ("30-09-2026", "NLD", 2026, 3298, "1,81"),
                                    ("30-09-2026", "NLD", 2026, 3400, "21,00"), ("30-09-2026", "FT", 2026, 5755, "21,00"),
                                    ("01-10-2026", "FT", 2026, 6000, "1.339,41")], "1.384,89"))]
    lig, c = _ligar(fs, sc)
    assert lig[(("FT U014/47843",), ("NLD 2026/3400",))].metodo == "mesmo valor"
    assert not any("FT 2026/5755" in k[1] for k in lig)                 # FT pequena: interna, não é farmácia
    assert lig[(("FT U014/47700",), ("NLD 2026/3077",))].ok and lig[(("FT U014/47900",), ("NLD 2026/3298",))].ok
    assert not any("FT 2026/6000" in k[1] for k in lig)                 # a mensalidade não entra
    assert not any(t[0] == "farmacia" for t in c.alertas)


def test_farmacia_uma_fatura_varios_lancamentos():
    """Uma fatura de 24,48 € cobrada pela Santa Casa em três NLD (1,67 + 1,81 + 21,00)."""
    fs = [(1, farmacia(47843, "18-09-2026", "24,48"))]
    sc = [(10, carta("15-10-2026", [("15-09-2026", "NLD", 2026, 3077, "1,67"), ("30-09-2026", "NLD", 2026, 3298, "1,81"),
                                    ("30-09-2026", "NLD", 2026, 3400, "21,00")], "24,48"))]
    lig, _ = _ligar(fs, sc)
    l = next(v for k, v in lig.items() if set(k[1]) == {"NLD 2026/3077", "NLD 2026/3298", "NLD 2026/3400"})
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
    assert "tudo pago até 31/10/2026" in r.text and "FT 2026/5319" in r.text and "Gestão do Lar" in r.text
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
    assert t.docs == [("FT 2026/6073", None)] and t.extras and t.numero == "300000000001"   # «c/extras»: resolve-se pela carta

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


def test_config_datas_no_formato_do_campo(tmp_path, falso):
    """O campo de data envia dd/mm/aaaa (datas.js): as datas têm de ficar gravadas (antes ficavam vazias)."""
    from tests.test_app import _csrf
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    r = c.get("/lar")
    r = c.post("/lar/config", data={"csrf": _csrf(r), "entidade": "", "santa_casa": "Santa Casa STC", "farmacia": "",
                                    "lar_pago_ate": "30/09/2026", "lar_validar_desde": "01/01/2026"}, follow_redirects=True)
    assert "Configuração do Lar guardada." in r.text
    assert app.state.prefs["lar_pago_ate"] == "2026-09-30" and app.state.prefs["lar_validar_desde"] == "2026-01-01"
    assert "As datas de regularização não estão definidas" not in r.text and "tudo pago até 30/09/2026" in r.text
    r = c.post("/lar/config", data={"csrf": _csrf(r), "entidade": "", "santa_casa": "", "farmacia": "",
                                    "lar_pago_ate": "31/02/2026", "lar_validar_desde": "01/01/2026"}, follow_redirects=True)
    assert "«Tudo pago até»: data inválida" in r.text and app.state.prefs["lar_pago_ate"] == "2026-09-30"


def _carta_outubro():
    """A carta real de novembro (estrutura e valores do documento do utilizador; números fictícios do resto)."""
    return carta("02-11-2026", [("15-10-2026", "NLD", 2026, 3564, "3,90"), ("30-10-2026", "FT", 2026, 6452, "21,00"),
                                ("31-10-2026", "NLD", 2026, 3768, "32,64"), ("01-11-2026", "FT", 2026, 6725, "1.367,53")],
                 "1.425,07")


def test_tabela_da_carta_cruzada_com_pdf_e_farmacia():
    fa = [(10, farmacia(1, "10-10-2026", "3,90")), (11, farmacia(2, "25-10-2026", "21,00")),
          (12, farmacia(3, "20-10-2026", "20,00")), (13, farmacia(4, "28-10-2026", "12,64"))]
    sc = [(20, nld(3564, "15-10-2026", "3,90")),                  # bate com o PDF e com a fatura de 10/10
          # FT 6452 (21,00): sem PDF no Paperless — a fatura da farmácia existe, mas o PDF falta
          (21, nld(3768, "31-10-2026", "33,64")),                 # o PDF diz 33,64, a carta 32,64
          (22, ft(6725, "01-11-2026", "1.367,53")),               # mensalidade: só o PDF, sem farmácia
          (23, _carta_outubro()),
          (24, recibo(1500, "05-11-2026", [("FT", 2026, 6725, "1.367,53"), ("NLD", 2026, 3564, "3,90")], "1400,00"))]
    c = lar.conciliar(fa, sc, [], hoje=date(2026, 11, 10))
    tab = {t.origem: t for t in c.tabelas}
    ca = tab["carta"]
    assert ca.total == 142507 and ca.soma == 142507 and not ca.diverge and ca.estado == "diverge"
    ln = {l.chave: l for l in ca.linhas}
    assert ln["NLD 2026/3564"].estado == "bate" and [f.numero for f in ln["NLD 2026/3564"].farmacia] == ["FT U014/1"]
    assert ln["FT 2026/6452"].estado == "falta" and ln["FT 2026/6452"].pdf is None
    assert ln["FT 2026/6452"].farmacia == [] and not ln["FT 2026/6452"].da_farmacia      # FTUT: faturação interna
    assert any("PDF da FT em falta" in x for x in ln["FT 2026/6452"].falta)
    l3 = ln["NLD 2026/3768"]
    assert l3.estado == "diverge" and "o PDF diz 33,64" in l3.diverge[0]
    assert sorted(f.numero for f in l3.farmacia) == ["FT U014/3", "FT U014/4"] and l3.soma_farmacia == 3264
    l4 = ln["FT 2026/6725"]
    assert l4.estado == "bate" and not l4.da_farmacia and l4.farmacia == []
    assert ca.contagem() == {"bate": 2, "falta": 1, "diverge": 1}
    rec = tab["recibo"]
    assert rec.soma == 137143 and rec.estado == "diverge" and "o total pago foi 1400,00" in rec.diverge[0].replace(".", "").replace(" €", "")
    textos = " | ".join(t for _, t in c.alertas)
    assert "NLD 2026/3768 — o PDF diz" in textos and "Recibo PB2026/1500" in textos


def test_tabela_sem_fatura_da_farmacia():
    sc = [(20, nld(3564, "15-10-2026", "3,90")), (23, _carta_outubro())]
    c = lar.conciliar([], sc, [], hoje=date(2026, 11, 10))
    ln = {l.chave: l for l in c.tabelas[0].linhas}
    assert any("fatura da farmácia" in x for x in ln["NLD 2026/3564"].falta)
    assert ln["NLD 2026/3768"].da_farmacia and ln["NLD 2026/3768"].estado == "falta"   # NLD sem PDF: é da farmácia
    assert not ln["FT 2026/6725"].da_farmacia and not ln["FT 2026/6452"].da_farmacia    # FTUT: faturação interna


def test_tabela_comprovativo_sem_valores_e_regularizado():
    sc = [(20, nld(3564, "15-10-2026", "3,90")), (22, ft(6725, "01-11-2026", "1.367,53")),
          (25, "Crédito Agrícola\nID operação: 99\nEnviar Nacional SANTA CASA DA MISERICORDIA\nMontante 1.371,43 €\n"
               "Data da Operação 06/11/2026\nInformação Complementar FTUT12026 / 00006725 NLDUT12026 / 00003564")]
    c = lar.conciliar([], sc, [], hoje=date(2026, 11, 10))
    t = next(t for t in c.tabelas if t.origem == "comprovativo")
    assert [l.valor for l in t.linhas] == [None, None] and t.soma == 137143 and not t.diverge
    c = lar.conciliar([], sc[:2] + [(25, sc[2][1].replace("1.371,43", "1.380,00"))], [], hoje=date(2026, 11, 10),
                      validados={k: {"em": date(2026, 11, 9), "por": "Admin", "nota": ""} for k in ("FT 2026/6725", "NLD 2026/3564")})
    t = next(t for t in c.tabelas if t.origem == "comprovativo")
    assert t.diverge and not any("Transferência" in x for _, x in c.alertas)      # documentos validados: sem alerta
    c = lar.conciliar([], sc[:2] + [(25, sc[2][1].replace("1.371,43", "1.380,00"))], [], hoje=date(2026, 11, 10))
    assert any("Transferência" in x and "somam" in x for _, x in c.alertas)


def test_pagina_mostra_validacao_pela_tabela(tmp_path, falso):
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    r = c.get("/lar")
    assert r.status_code == 200, r.text
    assert "Conciliação por carta informativa" in r.text and "Carta de" in r.text and "documentos de outubro 2026" in r.text
    assert "etiqueta-ok\">bate" in r.text and "Pagamentos desta carta" in r.text


def test_conciliacao_parte_da_carta_do_mes_anterior():
    """A carta de 02/11 lista os documentos de outubro (e a mensalidade de 01/11); o que é de outubro e não vem na carta
    fica à vista, e os recibos de depois da carta ficam como pagamentos dela."""
    fa = [(10, farmacia(1, "10-10-2026", "3,90")), (11, farmacia(2, "25-10-2026", "21,00")),
          (12, farmacia(3, "20-10-2026", "20,00")), (13, farmacia(4, "28-10-2026", "12,64")),
          (14, farmacia(5, "29-10-2026", "7,77")),                # outubro, sem lançamento (ainda dentro do prazo)
          (15, farmacia(6, "05-10-2026", "5,55"))]                # cobrada na NLD 3500, paga antes da carta
    sc = [(20, nld(3564, "15-10-2026", "3,90")), (21, nld(3768, "31-10-2026", "32,64")),
          (22, ft(6725, "01-11-2026", "1.367,53")), (23, _carta_outubro()),
          (26, nld(3500, "07-10-2026", "5,55")),
          (27, recibo(1490, "20-10-2026", [("NLD", 2026, 3500, "5,55")], "5,55")),
          (28, nld(3700, "22-10-2026", "9,99", cod="90 - Cabeleireiro")),   # outubro, nem na carta nem pago
          (24, recibo(1500, "05-11-2026", [("FT", 2026, 6725, "1.367,53"), ("NLD", 2026, 3564, "3,90")], "1371,43")),
          (29, carta("01-12-2026", [("31-10-2026", "NLD", 2026, 3768, "32,64"), ("30-10-2026", "FT", 2026, 6452, "21,00")],
                     "53,64"))]
    c = lar.conciliar(fa, sc, [], hoje=date(2026, 11, 20))
    assert [cc.tabela.data for cc in c.por_carta] == [date(2026, 12, 1), date(2026, 11, 2)]
    cc = c.por_carta[1]
    assert cc.mes_ref == date(2026, 10, 1) and cc.tabela.total == 142507
    assert [p.numero for p in cc.pagamentos] == ["PB2026/1500"] and cc.pagamentos[0].estado == "bate"
    assert cc.pago == 137143 and cc.em_aberto == 5364
    assert cc.pagamento["FT 2026/6725"].startswith("pago · Recibo PB2026/1500")
    assert cc.pagamento["NLD 2026/3768"] == "por pagar · transitou para a carta de 01/12/2026"
    fora = {x.numero: x for x in cc.fora}
    assert fora["NLD 2026/3700"].estado == "falta" and "não está na carta" in fora["NLD 2026/3700"].nota
    assert fora["NLD 2026/3500"].estado == "bate" and "pago antes da carta" in fora["NLD 2026/3500"].nota
    assert fora["FT U014/5"].estado == "bate" and "ainda sem lançamento" in fora["FT U014/5"].nota
    assert fora["FT U014/6"].estado == "bate" and "cobrada em NLD 2026/3500" in fora["FT U014/6"].nota
    assert "FT U014/1" not in fora and cc.estado == "falta"
    assert c.por_carta[0].pagamento["NLD 2026/3768"] == "por pagar" and c.recibos_sem_carta[0].numero == "PB2026/1490"
    assert any("NLD 2026/3700" in t and "não está na carta de 02/11/2026" in t for _, t in c.alertas)
    # passado o prazo (fim do mês seguinte), a fatura de outubro sem lançamento passa a faltar
    c = lar.conciliar(fa, sc, [], hoje=date(2026, 12, 15))
    assert {x.numero: x for x in c.por_carta[1].fora}["FT U014/5"].estado == "falta"


def _mov(i, valor, d, desc="TRF SANTA CASA"):
    return SimpleNamespace(id=i, montante=-valor, data_mov=d, descricao_original=desc)


def test_debito_parte_dos_documentos_ligados():
    """A + B: o débito no banco → os documentos ligados no Gestor (documento_ligado) → a tabela de documentos de cada
    um → as NLD/FT pagas → o PDF e as faturas da farmácia."""
    fa = [(10, farmacia(1, "10-10-2026", "3,90")), (12, farmacia(3, "20-10-2026", "20,00")),
          (13, farmacia(4, "28-10-2026", "12,64"))]
    sc = [(20, nld(3564, "15-10-2026", "3,90")), (21, nld(3768, "31-10-2026", "32,64")),
          (22, ft(6725, "01-11-2026", "1.367,53")), (23, _carta_outubro()),
          (24, recibo(1500, "05-11-2026", [("FT", 2026, 6725, "1.367,53"), ("NLD", 2026, 3564, "3,90")], "1371,43")),
          (25, recibo(1510, "20-11-2026", [("NLD", 2026, 3768, "32,64")], "32,64"))]
    movs = [_mov(1, 137143, date(2026, 11, 4)),            # ligado ao recibo 1500: bate, linha a linha
            _mov(2, 3264, date(2026, 11, 19)),             # sem ligação: achado pelo valor do recibo 1510
            _mov(3, 2100, date(2026, 11, 6)),              # ligado à carta e a um documento que não é da Santa Casa
            _mov(4, 5000, date(2026, 11, 7)),              # ligado à NLD 3564 diretamente: não bate
            _mov(5, 999, date(2026, 11, 8), "OUTRA COISA")]  # nada: não aparece
    ligados = {1: [24], 3: [23, 999], 4: [20]}
    c = lar.conciliar(fa, sc, movs, hoje=date(2026, 11, 25), ligados=ligados)
    b = {p.movimento.id: p for p in c.banco}
    assert set(b) == {1, 2, 3, 4}
    p1 = b[1]
    assert p1.origem == "ligado" and [t.numero for t in p1.documentos] == ["PB2026/1500"] and p1.estado == "bate"
    assert [l.chave for l in p1.linhas] == ["FT 2026/6725", "NLD 2026/3564"] and p1.soma == 137143
    assert [f.numero for f in p1.linhas[1].farmacia] == ["FT U014/1"] and not p1.linhas[0].da_farmacia
    p2 = b[2]
    assert p2.origem == "pelo valor" and p2.estado == "falta" and "não está ligado" in p2.falta[0]
    assert sorted(f.numero for f in p2.linhas[0].farmacia) == ["FT U014/3", "FT U014/4"]
    p3 = b[3]
    assert p3.outros == [999] and any("sem recibo nem comprovativo" in x for x in p3.falta)
    assert len(p3.linhas) == 4 and p3.estado == "diverge" and "o débito foi 21,00" in p3.diverge[0]
    p4 = b[4]
    assert [l.chave for l in p4.linhas] == ["NLD 2026/3564"] and p4.soma == 390 and p4.estado == "diverge"
    assert any("Débito de 07/11/2026" in t for _, t in c.alertas) and not any("Débito de 04/11/2026" in t for _, t in c.alertas)


def test_pagina_debito_com_documento_ligado(tmp_path, falso):
    from sqlalchemy import select
    from app.models import Conta, DocumentoLigado, Entidade, Instituicao, Movimento
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    with app.state.SessionLocal() as db:
        ent = db.scalar(select(Entidade).where(Entidade.nome == "Mãe"))
        conta = Conta(entidade=ent, instituicao=db.scalar(select(Instituicao)) or Instituicao(nome="CCAM"), nome="Ordem", tipo="ORDEM")
        db.add(conta)
        db.flush()
        m = Movimento(conta_id=conta.id, data_mov=date.today(), montante=-133941, descricao_original="TRF SANTA CASA",
                      descricao_norm="TRF SANTA CASA", hash_dedup="sc1")
        db.add(m)
        db.flush()
        db.add(DocumentoLigado(paperless_id=22, titulo="Carta", movimento_id=m.id))
        db.commit()
    r = c.get("/lar")
    assert r.status_code == 200, r.text
    assert "Pagamentos no banco ↔ documentos ligados" in r.text and "documentos ligados" in r.text
    assert "TRF SANTA CASA" in r.text and "sem recibo nem comprovativo ligado" in r.text


def test_transferencia_c_extras_paga_a_carta_inteira():
    """O caso de novembro: a carta de 05/11 (sem os PDF das NLD/FT no Paperless) e a transferência de 09/11 de
    1.425,07 € com «01-11-2026 FT FTUT12026 / 00006725 c/extras» — paga a carta toda, não só a mensalidade."""
    fa = [(10, farmacia(48395, "01-10-2026", "0,92")), (11, farmacia(48426, "01-10-2026", "0,17")),
          (12, farmacia(48757, "10-10-2026", "2,81")), (13, farmacia(49113, "17-10-2026", "2,64")),
          (14, farmacia(49321, "23-10-2026", "26,55")), (15, farmacia(49322, "23-10-2026", "2,53")),
          (16, farmacia(49578, "29-10-2026", "0,92"))]
    carta_nov = carta("05-11-2026", [("15-10-2026", "NLD", 2026, 3564, "3,90"), ("30-10-2026", "FT", 2026, 6452, "21,00"),
                                     ("31-10-2026", "NLD", 2026, 3768, "32,64"), ("01-11-2026", "FT", 2026, 6725, "1.367,53")],
                      "1.425,07")
    sem_pagar = lar.conciliar(fa, [(23, carta_nov)], [], hoje=date(2026, 11, 9))
    ln = {l.chave: l for l in sem_pagar.por_carta[0].tabela.linhas}
    assert ln["NLD 2026/3564"].estado == "bate" and "confere com as faturas da farmácia" in ln["NLD 2026/3564"].notas[0]
    assert ln["NLD 2026/3768"].soma_farmacia == 3264 and ln["NLD 2026/3768"].estado == "bate"
    assert ln["FT 2026/6725"].estado == "falta"                      # sem PDF e ainda por pagar
    comp = (3496, comprovativo("09/11/2026", "1.425,07", "01-11-2026 FT FTUT12026 / 00006725 c/extras"))
    movs = [_mov(1, 142507, date(2026, 11, 9))]
    c = lar.conciliar(fa, [(23, carta_nov), comp], movs, hoje=date(2026, 11, 9))
    cc = c.por_carta[0]
    assert cc.pago == 142507 and cc.em_aberto == 0 and cc.estado == "bate"
    assert all(v.startswith("transferido · Transferência 300000000001 de 09/11/2026") for v in cc.pagamento.values())
    ln = {l.chave: l for l in cc.tabela.linhas}
    assert ln["FT 2026/6725"].estado == "bate" and "paga (Transferência" in ln["FT 2026/6725"].notas[0]
    t = cc.pagamentos[0]
    assert t.origem == "comprovativo" and t.soma == 142507 and not t.diverge
    assert t.notas == ["paga a carta de 05/11/2026 inteira («c/extras»)"]
    assert c.pagamentos[0][1].id == 1
    # sem carta com esse total: não se inventa — fica em falta
    c = lar.conciliar(fa, [(23, carta_nov), (3496, comp[1].replace("1.425,07", "1.400,00"))], [], hoje=date(2026, 11, 9))
    t = next(t for t in c.tabelas if t.origem == "comprovativo")
    assert any("c/extras" in x for x in t.falta) and c.por_carta[0].pagos == {"FT 2026/6725"}   # só a FT indicada
