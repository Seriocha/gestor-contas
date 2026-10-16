"""Contas de investimento alimentadas pelo extrato da carteira de fundos (BiG «FA»):
subscrições/resgates de fundos não contam; saldo da liquidez ignorado; limpar conta."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Importacao, Movimento, RetificacaoSaldo
from tests.test_app import PW, _csrf, _post
from tests.test_importacao import _importar

# conta de liquidez da carteira (valores fictícios), mais recente primeiro, com a cadeia de saldos do banco
FA = '''"Movimento*","Data Valor","Data Mov.","Natureza","Moeda","Original","EUR","Contabilistico*"
"Juros Juros","01-07-2026","01-07-2026","Crédito","EUR"," ","2,00","602,90"
"IRC/IRS s/ juros credores IRC/IRS s/ juros credores","01-07-2026","01-07-2026","Débito","EUR"," ","-0,60","600,90"
"Subscrição de Fundo Alfa .. Subscrição de Fundo Alfa Global","06-03-2026","04-03-2026","Débito","EUR"," ","-3.000,00","601,50"
"Subscrição de Fundo Beta Subscrição de Fundo Beta","06-03-2026","04-03-2026","Débito","USD","-1.300,00","-1.100,00","3.601,50"
"Transf. da Conta Liquidez p/ a FA Agre. Transf. da Conta Liquidez p/ a FA Agre.","03-03-2026","02-03-2026","Crédito","EUR"," ","4.000,00","4.701,50"
"Resgate de Fundo Gama Resgate de Fundo Gama","13-02-2026","11-02-2026","Crédito","EUR"," ","500,00","701,50"
"Rendimento U.Ps de 10.5 Fundo Delta.. Rendimento U.Ps de 10.5 Fundo Delta Dis","11-02-2026","11-02-2026","Crédito","EUR"," ","1,50","201,50"
'''.encode("utf-8-sig")


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="FA Agressiva", tipo="INVESTIMENTO", moeda="EUR", instituicao_id="3")
    _post(c, "/contas/nova?entidade=1", nome="BiG ordem", tipo="ORDEM", moeda="EUR", instituicao_id="3")
    return c


def test_importar_carteira(admin, app):
    previa, r = _importar(admin, "BiG  FA Agressiva.csv", FA, conta_id="1")
    assert "Importar 4 movimentos" in previa.text
    assert "3 subscrições/resgates de fundos são trocas dentro da carteira" in previa.text
    assert "subscrições 4.100,00 €, resgates 500,00 €" in previa.text and "Retificar saldo" in previa.text
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        movs = {m.descricao_original: m for m in db.query(Movimento).filter_by(conta_id=1)}
        assert not any("Subscrição" in d or "Resgate" in d for d in movs)
        aplic = movs["Transf. da Conta Liquidez p/ a FA Agre."]
        assert aplic.transferencia and aplic.montante == 400000 and aplic.saldo_apos is None
        assert movs["Juros"].categoria.nome == movs["IRC/IRS s/ juros credores"].categoria.nome == "Rendimentos de investimento"
        assert sum(m.montante for m in movs.values()) == 400000 + 200 - 60 + 150
    # o valor real entra por retificação; o desempenho separa capital e ganho de mercado
    p = admin.get("/contas/1/movimentos")
    admin.post("/contas/1/retificar", data={"csrf": _csrf(p), "data": "2026-10-16", "saldo": "40.495,70"})
    pag = admin.get("/contas/1/movimentos").text
    assert "40.495,70 €" in pag and "Desempenho" in pag


def test_mesmo_ficheiro_numa_conta_a_ordem_continua_igual(admin, app):
    previa, _ = _importar(admin, "fa.csv", FA, conta_id="2")
    assert "Importar 7 movimentos" in previa.text and "trocas dentro da carteira" not in previa.text


def test_limpar_conta(admin, app, tmp_path):
    _importar(admin, "fa.csv", FA, conta_id="1")
    p = admin.get("/contas/1/movimentos")
    admin.post("/contas/1/retificar", data={"csrf": _csrf(p), "data": "2026-10-16", "saldo": "40.495,70"})
    pag = admin.get("/contas/1/movimentos").text
    assert "Limpar a conta (para reimportar)" in pag
    # nome errado: nada acontece
    _post(admin, "/contas/1/movimentos", "/contas/1/limpar", confirmar="outra")
    with app.state.SessionLocal() as db:
        assert db.query(Movimento).filter_by(conta_id=1).count() == 5      # 4 + acerto da retificação
    r = _post(admin, "/contas/1/movimentos", "/contas/1/limpar", confirmar="FA Agressiva")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.query(Movimento).filter_by(conta_id=1).count() == 0
        assert db.query(RetificacaoSaldo).filter_by(conta_id=1).count() == 0
        assert db.query(Importacao).filter_by(conta_id=1).count() == 0
    assert list((tmp_path / "backup").glob("gestor-*.db"))
    assert "Conta «FA Agressiva» limpa" in admin.get("/contas/1/movimentos").text
    # e pode reimportar-se o mesmo ficheiro
    previa, _ = _importar(admin, "fa.csv", FA, conta_id="1")
    assert "Importar 4 movimentos" in previa.text


def test_valor_de_partida_conta_como_capital(admin, app):
    from datetime import date

    from app import investimentos, retificacoes
    from app.models import Conta
    _importar(admin, "fa.csv", FA, conta_id="1")          # 4.000 aplicados + 2,90 de rendimentos líquidos
    p = admin.get("/contas/1/movimentos")
    assert 'name="partida"' in p.text
    admin.post("/contas/1/retificar", data={"csrf": _csrf(p), "data": "2026-10-16", "saldo": "40.495,70",
                                            "partida": "1", "nota": "valor indicado pelo BiG"})
    with app.state.SessionLocal() as db:
        c = db.get(Conta, 1)
        d = investimentos.desempenho(db, c, date(2026, 10, 16))
        assert d.valor == 4049570 and d.partida == 4049570 - 400290 and d.data_partida == date(2026, 10, 16)
        assert d.capital == 400000 + d.partida and d.resultado == 290        # ganho = só os rendimentos
        assert retificacoes.avisos(db, [1]) == {}                           # partida não fica «por explicar»
    pag = admin.get("/contas/1/movimentos").text
    assert "valor de partida" in pag and "por explicar com movimentos" not in pag and 'name="partida"' not in pag
    assert "partida 36.492,80 €" in pag
    # uma 2.ª partida é recusada; uma retificação normal depois mede o ganho de mercado
    r = admin.post("/contas/1/retificar", data={"csrf": _csrf(admin.get("/contas/1/movimentos")), "data": "2026-10-20",
                                                "saldo": "41.000,00", "partida": "1"})
    assert "já tem um valor de partida" in r.text
    admin.post("/contas/1/retificar", data={"csrf": _csrf(admin.get("/contas/1/movimentos")), "data": "2026-11-30",
                                            "saldo": "41.000,00"})
    with app.state.SessionLocal() as db:
        d = investimentos.desempenho(db, db.get(Conta, 1), date(2026, 11, 30))
        assert d.resultado == 290 + 50430                                   # 41.000 − 40.495,70 = +504,30
