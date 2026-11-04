"""Gestão do Lar › Farmácia Nova (pagamento direto): faturas, faturas-recibo e recibos da farmácia ↔ débitos na conta
da herança. Textos sintéticos com a estrutura das faturas reais; o recibo segue o formato mais comum (n.º, data, linhas
«FT … valor», total) até haver um real."""
from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import lar, lar_farmacia as lf, paperless
from app.main import create_app
from app.routes import lar as rotas_lar
from tests.test_app import PW, _csrf, _post
from tests.test_lar import _heranca, farmacia


def recibo(num, data, faturas, total=None, meio="Transferência bancária"):
    linhas = "\n".join(f"FT U014/{n} {d} {v} {v}" for n, d, v in faturas)
    tot = total or f"{sum(lar.cent(v) for _, _, v in faturas) / 100:.2f}".replace(".", ",")
    return (f"FARMACIA NOVA DE SANTO ANDRE\nOriginal Recibo n.º: RC U014/{num}\nData: {data}\nCliente N.º: 1\nMARIA EXEMPLO\n"
            f"Documento Data Valor Valor pago\n{linhas}\nMeio de pagamento: {meio}\nTotal recebido: {tot} €")


def fatura_recibo(num, data, total):
    return (f"Original Fatura-Recibo n.º: FR U014/{num}\nData: {data} 10:00:00\nFARMACIA NOVA Exmo(s). Sr(s).\n"
            f"Total a pagar {total} €\nTotal Utente (Euros): {total}\nNumerário {total} €")


def mov(i, data, valor, descricao="TRF FARMACIA NOVA"):
    return SimpleNamespace(id=i, data_mov=data, montante=-lar.cent(valor), descricao_original=descricao)


def test_leitura():
    f = lf.ler_fatura(1, farmacia(49113, "17-11-2026", "2,64"))
    assert f.numero == "FT U014/49113" and f.total == 264 and not f.no_ato
    fr = lf.ler_fatura(2, fatura_recibo(7, "18-11-2026", "5,10"))
    assert fr.numero == "FR U014/7" and fr.total == 510 and fr.no_ato and fr.estado() == "pago no ato"
    r = lf.ler_recibo(3, recibo(55, "30-11-2026", [(49113, "17-11-2026", "2,64"), (49200, "20-11-2026", "1.010,00")]))
    assert r.numero == "RC U014/55" and r.data == date(2026, 11, 30) and r.total == 101264
    assert r.linhas == [("FT U014/49113", 264), ("FT U014/49200", 101000)] and "Transfer" in r.meio
    assert lf.ler_recibo(4, farmacia(1, "01-11-2026", "1,00")) is None         # uma fatura não é recibo
    assert lf.ler_recibo(5, fatura_recibo(8, "01-11-2026", "1,00")) is None    # nem uma fatura-recibo
    assert lf.ler_fatura(6, "um papel qualquer") is None


def test_recibo_liga_faturas_e_debito():
    docs = [(1, farmacia(1, "03-11-2026", "2,64")), (2, farmacia(2, "10-11-2026", "10,00")),
            (3, recibo(9, "30-11-2026", [(1, "03-11-2026", "2,64"), (2, "10-11-2026", "10,00")]))]
    movs = [mov(70, date(2026, 11, 28), "12,64"), mov(71, date(2026, 11, 28), "12,64", "OUTRA COISA")]
    a = lf.analisar(docs, movs, desde=date(2026, 11, 1), hoje=date(2026, 12, 5))
    p, = a.pagamentos
    assert p.recibo.numero == "RC U014/9" and p.movimento.id == 70 and p.ok     # o débito da farmácia, não o outro
    assert {f.estado() for f in a.faturas} == {"pago"} and a.em_aberto == 0 and not a.alertas
    assert a.meses == [{"mes": date(2026, 11, 1), "faturas": 2, "faturado": 1264, "pago": 1264, "em_aberto": 0}]


def test_pagamento_sem_recibo_junta_faturas():
    """Pagamento misto: um débito paga uma fatura com o mesmo valor; outro junta várias; uma fica por pagar."""
    docs = [(1, farmacia(1, "02-11-2026", "4,00")), (2, farmacia(2, "05-11-2026", "3,00")),
            (3, farmacia(3, "12-11-2026", "7,50")), (4, farmacia(4, "20-10-2026", "9,99")),
            (5, farmacia(5, "02-10-2026", "50,00"))]                 # antes do pagamento direto: é da Santa Casa
    movs = [mov(1, date(2026, 11, 13), "7,50"), mov(2, date(2026, 11, 30), "7,00"),
            mov(3, date(2026, 11, 30), "33,33")]                     # à farmácia, mas sem fatura que dê o valor
    a = lf.analisar(docs, movs, desde=date(2026, 10, 15), hoje=date(2026, 12, 20))
    est = {f.numero: f.estado() for f in a.faturas}
    assert est == {"FT U014/1": "recibo em falta", "FT U014/2": "recibo em falta", "FT U014/3": "recibo em falta",
                   "FT U014/4": "por pagar"} and a.n_antes == 1
    metodos = sorted(p.metodo for p in a.pagamentos)
    assert metodos == ["mesmo valor", "sem recibo nem fatura", "soma de faturas"]
    textos = " | ".join(a.alertas)
    assert "FT U014/4" in textos and "por pagar há 61 dias" in textos
    assert "sem fatura nem recibo" in textos and "33,33" in textos
    assert "sem recibo da farmácia" in textos           # os pagamentos sem recibo há mais de 15 dias


def test_recibo_com_problemas():
    docs = [(1, farmacia(1, "03-11-2026", "2,64")),
            (2, recibo(3, "30-11-2026", [(1, "03-11-2026", "2,46"), (999, "04-11-2026", "5,00")], total="8,00")),
            (3, fatura_recibo(8, "06-11-2026", "1,20"))]
    a = lf.analisar(docs, [], desde=date(2026, 11, 1), hoje=date(2026, 12, 20))
    p, = a.pagamentos
    probs = " | ".join(p.problemas())
    assert "sem débito no banco" in probs and "FT U014/999" in probs and "o recibo diz 2,46" in probs and "somam" in probs
    assert {f.numero: f.estado() for f in a.faturas} == {"FT U014/1": "recibo sem débito", "FR U014/8": "pago no ato"}
    assert any("sem débito igual na conta" in t for t in a.alertas)


def test_santa_casa_deixa_as_faturas_do_pagamento_direto():
    fs = [(1, farmacia(1, "20-10-2026", "2,64")), (2, farmacia(2, "05-11-2026", "3,00"))]
    c = lar.conciliar(fs, [], [], hoje=date(2026, 12, 20), farmacia_ate=date(2026, 11, 1))
    assert c.n_faturas == 1 and [f.numero for l in c.farmacia for f in l.faturas] == ["FT U014/1"]


def test_pagina_farmacia(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTOR_PAPERLESS_URL", "http://paperless:8000")
    monkeypatch.setenv("GESTOR_PAPERLESS_TOKEN", "token-admin")
    docs = [(10, farmacia(1, "03-11-2026", "2,64")), (11, recibo(4, "30-11-2026", [(1, "03-11-2026", "2,64")])),
            (12, "FARMACIA NOVA\nAviso de campanha")]

    def pedido(caminho, params=None, binario=False, token=None):
        if caminho == "/api/correspondents/":
            return {"results": [{"id": 1, "name": "Farmácia Nova de Santo André"}, {"id": 2, "name": "Santa Casa STC"}]}
        if caminho == "/api/documents/":
            return {"results": [{"id": i, "content": t} for i, t in docs] if int(params["correspondent__id"]) == 1 else []}
        raise AssertionError(caminho)
    monkeypatch.setattr(paperless, "_pedido", pedido)
    monkeypatch.setattr(rotas_lar, "_CACHE", {})          # a cache é do módulo: não passa para os outros testes
    app = create_app(tmp_path, backups_automaticos=False)
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _heranca(app)
    r = c.get("/lar/farmacia")
    assert r.status_code == 200, r.text
    assert "Gestão do Lar · Farmácia Nova" in r.text and "Santa Casa" in r.text and "RC U014/4" in r.text
    assert "Falta a data em que passaste" in r.text and "não leu" in r.text and "sem débito igual" in r.text
    r = c.post("/lar/farmacia/config", data={"csrf": _csrf(r), "desde": "01/11/2026", "farmacia": "", "texto_banco": "FARM NOVA"},
               follow_redirects=True)
    assert "Configuração da farmácia guardada" in r.text and "pagamento direto desde 01/11/2026" in r.text
    assert app.state.prefs["lar_farm_desde"] == "2026-11-01" and app.state.prefs["lar_farm_texto"] == "FARM NOVA"
    r = c.get("/lar")
    assert "Farmácia Nova" in r.text                       # o separador também aparece na Santa Casa
