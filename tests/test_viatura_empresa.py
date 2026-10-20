"""Viatura da empresa: com a data de devolução no futuro continua em uso (manutenção, km, plano da IA), tem os mesmos
separadores que os outros carros e um plafond de manutenção (valor em N anos) que as idas à oficina vão gastando."""
from datetime import date, timedelta

from sqlalchemy import select

from app import manutencao as mt
from app.models import Bem
from tests.test_app import _post
from tests.test_patrimonio import _criar, admin, app  # noqa: F401  (fixtures)

FUTURO = (date.today() + timedelta(days=400)).isoformat()
INICIO = (date.today() - timedelta(days=365 * 2)).isoformat()


def test_devolucao_futura_nao_e_vendido():
    b = Bem(tipo="carro", uso="empresa", nome="x", data_venda=date.today() + timedelta(days=30))
    assert not b.vendido
    b.data_venda = date.today()
    assert b.vendido


def test_viatura_empresa_igual_aos_outros_carros_e_plafond(admin, app):
    bid = _criar(admin, "carro", nome="Frontera", uso="empresa", marca="Opel", modelo="Frontera", ano="2024",
                 km="20 000", combustivel="gasoleo", data_aquisicao=INICIO, data_venda=FUTURO,
                 plafond_manutencao="3.000,00", plafond_anos="6", plafond_inicio=INICIO)
    with app.state.SessionLocal() as db:
        b = db.get(Bem, bid)
        assert (b.plafond_manutencao, b.plafond_anos, b.plafond_inicio.isoformat()) == (300000, 6, INICIO)
    f = admin.get(f"/patrimonio/{bid}").text
    # mesmos separadores e secção que um carro próprio; plano e km disponíveis apesar da data de devolução
    assert 'data-tab="despesas">Despesas e receitas</a>' in f and ">Combustível</a>" not in f
    assert "Pedir o plano de manutenção" in f and 'action="/patrimonio/%d/km"' % bid in f
    assert "Plafond de manutenção" in f and "3.000,00 €" in f and "em 6 anos" in f
    # idas à oficina gastam o plafond
    for d, custo, km in ((date.today() - timedelta(days=300), "450,00", "12 000"), (date.today() - timedelta(days=30), "300,00", "19 000")):
        _post(admin, f"/patrimonio/{bid}", f"/patrimonio/{bid}/oficina", data=d.isoformat(), km=km,
              itens=[], outro="Revisão", custo=custo)
    with app.state.SessionLocal() as db:
        p = mt.plafond(db.get(Bem, bid), date.today())
        assert (p.usado, p.resta, p.n) == (75000, 225000, 2)
        assert 0.3 < p.decorrido(date.today()) < 0.36 and p.projecao(date.today()) > 200000
    f = admin.get(f"/patrimonio/{bid}").text
    assert "750,00 €" in f and "2.250,00 €" in f and "2 idas à oficina" in f
    # o formulário mostra o plafond para editar
    e = admin.get(f"/patrimonio/{bid}/editar").text
    assert 'name="plafond_manutencao" inputmode="decimal" value="3000,00"' in e or 'value="3.000,00"' in e


def test_carro_proprio_sem_plafond(admin, app):
    bid = _criar(admin, "carro")
    f = admin.get(f"/patrimonio/{bid}").text
    assert "Plafond de manutenção</strong>" not in f and 'data-tab="despesas">Despesas e receitas</a>' in f
    with app.state.SessionLocal() as db:
        assert db.scalar(select(Bem.plafond_manutencao).where(Bem.id == bid)) is None
