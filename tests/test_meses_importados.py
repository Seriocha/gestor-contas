"""Contas › Meses importados: grelha anos × meses por conta (importado / em falta / mês atual), filtro por tipo de
conta (contas à ordem primeiro) e por conta."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import meses_importados as mi
from app.main import create_app
from app.models import Conta, Importacao
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Cartão", tipo="CARTAO_CREDITO", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    for d in ("2025-11-03", "2025-11-20", "2026-01-15", "2026-02-02"):     # falta dezembro
        _mov(c, 2, d, "10,00", "Compra", sentido="saida")
    _mov(c, 1, "2026-01-10", "5,00", "Compra cartão", sentido="saida")
    return c


def test_grelha_meses_importados_e_em_falta(admin, app):
    with app.state.SessionLocal() as db:
        ordem = db.get(Conta, 2)
        g, = mi.calcular(db, [ordem], date(2026, 4, 10))
        anos = {ano: celulas for ano, celulas, _, _ in g.anos}
        assert list(anos) == [2026, 2025]                               # o mais recente em cima
        assert [c.estado for c in anos[2025]] == ["fora"] * 10 + ["ok", "falta"]
        assert [c.estado for c in anos[2026][:5]] == ["ok", "ok", "falta", "curso", "fora"]
        assert anos[2025][10].n == 2
        assert (g.n_ok, g.n_falta, g.faltas) == (3, 2, ["dez 2025", "mar 2026"])

        # um extrato mensal sem movimentos também cobre o mês (o período da importação)
        db.add(Importacao(conta_id=2, ficheiro_nome="dez.pdf", sha256="x" * 64, parser="santander_pdf",
                          data_ini=date(2025, 12, 1), data_fim=date(2025, 12, 31), estado="confirmada"))
        db.flush()
        g, = mi.calcular(db, [ordem], date(2026, 4, 10))
        assert g.faltas == ["mar 2026"]


def test_intervalos_seguidos():
    faltas = [(2020, m) for m in range(1, 13)] + [(2021, 1), (2023, 3), (2024, 11), (2024, 12)]
    assert mi._intervalos(faltas) == ["jan 2020 – jan 2021", "mar 2023", "nov–dez 2024"]


def test_pagina_filtro_por_tipo(admin):
    r = admin.get("/contas/meses?entidade=1")
    assert r.status_code == 200 and "Meses importados" in r.text
    # por omissão as contas à ordem, primeiras na lista de tipos
    assert 'id="conta-2"' in r.text and 'id="conta-1"' not in r.text
    assert r.text.index('value="ORDEM"') < r.text.index('value="CARTAO_CREDITO"')
    assert "/contas/2/movimentos?mes=2025-11&anteriores=1" in r.text and "Em falta:" in r.text
    r = admin.get("/contas/meses?entidade=1&tipo=CARTAO_CREDITO")
    assert 'id="conta-1"' in r.text and 'id="conta-2"' not in r.text
    r = admin.get("/contas/meses?entidade=1&tipo=todas&conta=1")
    assert 'id="conta-1"' in r.text and 'id="conta-2"' not in r.text
    assert 'href="/contas/meses?entidade=1"' in admin.get("/contas?entidade=1").text      # separador em Contas
