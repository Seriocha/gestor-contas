"""Eletricidade das casas na ficha do imóvel: contratos por período, mudança de operador (gerida pela Manie, em
qualquer mês), histórico a partir dos débitos, débitos de cada período e atribuição à casa certa."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import fornecedores as fo
from app import luz
from app.main import create_app
from app.models import Bem, ContratoLuz, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_patrimonio import _criar
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    for d in ("2026-05-12", "2026-06-12", "2026-07-12"):
        _mov(c, 1, d, "60,00", "DD EDP COMERCIAL 60230660104", sentido="saida")
    for d in ("2026-08-08", "2026-09-08", "2026-10-08"):
        _mov(c, 1, d, "48,00", "DD G9 ENERGIA 777000111", sentido="saida")
    return c


def _luz(admin, bid, **dados):
    f = admin.get(f"/patrimonio/{bid}")
    return admin.post(f"/patrimonio/{bid}/luz/mudar", data={"csrf": _csrf(f), **dados}, follow_redirects=True)


def test_registar_mudar_e_debitos_por_periodo(admin, app):
    bid = _criar(admin, nome="Casa de Lisboa", omissao="")
    r = admin.get(f"/patrimonio/{bid}")
    assert "Eletricidade" in r.text and "Registar o contrato atual" in r.text
    r = _luz(admin, bid, data="01/05/2026", comercializador="EDP", tarifa="bi-horaria", potencia="6,9 kVA",
             cpe="PT 0002 0000 1234 5678 AB", referencia="60230660104")
    assert "EDP desde 01/05/2026" in r.text
    # a Manie muda para a G9 a 20 de julho: o contrato EDP acaba na véspera, o novo herda tarifa, potência e CPE
    r = _luz(admin, bid, data="20/07/2026", comercializador="G9", gestor="Manie", preco="dinamico",
             motivo="Manie: −12 % no kWh")
    assert "EDP → G9 a 20/07/2026" in r.text
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        edp, g9 = b.contratos_luz
        assert edp.fim == date(2026, 7, 19) and g9.fim is None and luz.atual(b) is g9
        assert (g9.tarifa, g9.potencia, g9.cpe, g9.gestor, g9.referencia) == ("bi-horaria", "6,9 kVA", "PT0002000012345678AB", "Manie", None)
        p = {x.contrato.comercializador: x for x in luz.periodos(db, b)}
        assert len(p["EDP"].debitos) == 3 and p["EDP"].total == 18000 and p["EDP"].media_mes == 6000
        assert len(p["G9"].debitos) == 3 and p["G9"].total == 14400
        # os débitos vão para a casa (atribuição das despesas), pelo operador e pelo período
        assert {m.bem_id for m in db.scalars(select(Movimento))} == {bid}
    r = admin.get(f"/patrimonio/{bid}")
    assert "Histórico de operadores" in r.text and "gerido por Manie" in r.text and "Manie: −12 % no kWh" in r.text
    # uma mudança para antes do início do contrato atual não se aceita
    r = _luz(admin, bid, data="01/07/2026", comercializador="Iberdrola")
    assert "tem de ser depois do início do contrato atual" in r.text


def test_duas_casas_mesmo_operador_mudanca_a_meio(admin, app):
    """A Manie passa a casa de férias da EDP para a G9 em agosto; a casa principal continua na G9 (outra referência)."""
    lisboa = _criar(admin, nome="Casa de Lisboa", omissao="")
    praia = _criar(admin, nome="Casa da Praia", uso="ferias", omissao="")
    _mov(admin, 1, "2026-09-09", "30,00", "DD G9 ENERGIA 555000222", sentido="saida")
    _luz(admin, lisboa, data="01/01/2026", comercializador="G9", referencia="555000222")
    _luz(admin, praia, data="01/01/2026", comercializador="EDP", referencia="60230660104")
    _luz(admin, praia, data="01/08/2026", comercializador="G9", referencia="777000111", gestor="Manie")
    with app.state.SessionLocal() as db:
        por_casa = {}
        for m in db.scalars(select(Movimento)):
            por_casa.setdefault(m.bem_id, []).append(m.descricao_original)
        assert por_casa[lisboa] == ["DD G9 ENERGIA 555000222"] and len(por_casa[praia]) == 6
        a = fo.analisar(db, 1, None, ["EDP", "G9"], date(2026, 10, 27))
        casas = {r.nome: r for r in a.casas}
        assert set(casas) == {"Casa de Lisboa", "Casa da Praia"} and len(casas["Casa da Praia"].movimentos) == 6
        assert {x for x, _ in a.atribuicao.values()} == {"Casa de Lisboa", "Casa da Praia"}


def test_historico_a_partir_dos_debitos(admin, app):
    bid = _criar(admin, nome="Casa de Lisboa")          # por omissão: fica com os débitos de luz
    with app.state.SessionLocal() as db:
        for m in db.scalars(select(Movimento)):
            m.bem_id, m.bem_manual = bid, True
        db.commit()
    r = admin.get(f"/patrimonio/{bid}")
    assert "Criar o histórico a partir dos débitos" in r.text
    r = admin.post(f"/patrimonio/{bid}/luz/historico", data={"csrf": _csrf(r), "gestor": "Manie"}, follow_redirects=True)
    assert "2 contrato(s) de luz criados" in r.text
    with app.state.SessionLocal() as db:
        edp, g9 = db.get(Bem, bid).contratos_luz
        assert (edp.comercializador, edp.inicio, edp.fim, edp.referencia) == ("EDP", date(2026, 4, 1), date(2026, 6, 30), "60230660104")
        assert (g9.comercializador, g9.inicio, g9.fim, g9.gestor) == ("G9", date(2026, 7, 1), None, "Manie")


def test_editar_sem_sobreposicoes_e_apagar(admin, app):
    bid = _criar(admin, nome="Casa de Lisboa", omissao="")
    _luz(admin, bid, data="01/05/2026", comercializador="EDP")
    _luz(admin, bid, data="20/07/2026", comercializador="G9")
    with app.state.SessionLocal() as db:
        edp_id, g9_id = [c.id for c in db.get(Bem, bid).contratos_luz]
    f = admin.get(f"/patrimonio/{bid}")
    r = admin.post(f"/patrimonio/{bid}/luz/{edp_id}", data={"csrf": _csrf(f), "comercializador": "EDP", "inicio": "01/05/2026",
                                                          "fim": "25/07/2026"}, follow_redirects=True)
    assert "Não guardado" in r.text and "sobrepõe-se" in r.text
    r = admin.post(f"/patrimonio/{bid}/luz/{edp_id}", data={"csrf": _csrf(r), "comercializador": "EDP", "inicio": "01/04/2026",
                                                          "fim": "19/07/2026", "notas": "contrato antigo"}, follow_redirects=True)
    assert "Contrato EDP guardado" in r.text
    r = admin.post(f"/patrimonio/{bid}/luz/{g9_id}", data={"csrf": _csrf(r), "acao": "apagar"}, follow_redirects=True)
    assert "Contrato G9 apagado" in r.text
    with app.state.SessionLocal() as db:
        assert [(c.comercializador, c.inicio, c.notas) for c in db.scalars(select(ContratoLuz))] == \
            [("EDP", date(2026, 4, 1), "contrato antigo")]


def test_so_casas(admin):
    carro = _criar(admin, tipo="carro")
    f = admin.get(f"/patrimonio/{carro}")
    assert "Eletricidade" not in f.text
    r = admin.post(f"/patrimonio/{carro}/luz/mudar", data={"csrf": _csrf(f), "data": "01/05/2026", "comercializador": "EDP"})
    assert r.status_code == 404
