"""Relatórios: período «Vários anos» (anos seguidos ou não, comparação ano a ano) e filtro por categoria
(no lugar da etiqueta; a categoria de cima inclui as subcategorias)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import relatorios as rel
from app.main import create_app
from app.models import Categoria, Movimento
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    for d, v, desc, sentido in [("2023-03-01", "1000,00", "Ordenado", "entrada"), ("2023-03-05", "100,00", "Luz", "saida"),
                                ("2024-03-01", "1100,00", "Ordenado", "entrada"), ("2024-03-05", "150,00", "Luz", "saida"),
                                ("2024-04-07", "40,00", "Restaurante", "saida"),
                                ("2025-03-01", "1200,00", "Ordenado", "entrada"), ("2025-03-05", "120,00", "Luz", "saida")]:
        _mov(c, 1, d, v, desc, sentido=sentido)
    with app.state.SessionLocal() as db:       # «Luz» numa subcategoria de «Casa»; «Restaurante» noutra categoria
        casa = Categoria(nome="Casa T", natureza="DESPESA")
        rest = Categoria(nome="Restaurantes T", natureza="DESPESA")
        db.add_all([casa, rest])
        db.flush()
        luz = Categoria(nome="Luz T", natureza="DESPESA", pai_id=casa.id)
        db.add(luz)
        db.flush()
        for m in db.scalars(select(Movimento)):
            if m.descricao_original == "Luz":
                m.categoria_id = luz.id
            elif m.descricao_original == "Restaurante":
                m.categoria_id = rest.id
        db.commit()
        c.ids = {"casa": casa.id, "rest": rest.id}
    return c


def test_periodo_anos_rotulos():
    ref = date(2026, 10, 19)
    assert rel.periodo_anos([2024, 2023, 2025], ref).rotulo == "2023–2025"
    p = rel.periodo_anos([2016, 2019], ref)
    assert p.rotulo == "2016, 2019" and (p.ini, p.fim) == (date(2016, 1, 1), date(2019, 12, 31))
    assert [m.year for m in p.meses()] == [2016] * 12 + [2019] * 12          # 2017 e 2018 ficam de fora
    assert rel.periodo_anos([2026], ref).fim == ref


def test_varios_anos_nao_seguidos(admin):
    r = admin.get("/relatorios?entidade=1&periodo=anos&anos=2023&anos=2025")
    assert r.status_code == 200
    t = r.text
    assert 'value="anos" selected' in t and "2023, 2025" in t and "Comparação por ano" in t
    assert 'name="anos" value="2023" checked' in t and 'name="anos" value="2024" >' in t
    # 2024 não entra: receitas 1000 + 1200
    assert "2.200,00" in t and "2.300,00" not in t
    assert "Média/ano" in t and "1.100,00" in t                              # média das receitas
    assert "vs. anterior" not in t


def test_varios_anos_por_omissao_os_3_mais_recentes(admin):
    t = admin.get("/relatorios?entidade=1&periodo=anos").text
    assert "2023–2025" in t or "2024–2026" in t
    assert 'name="anos" value="2025" checked' in t


def test_filtro_por_categoria_inclui_subcategorias(admin):
    t = admin.get(f"/relatorios?entidade=1&periodo=anos&anos=2023&anos=2024&anos=2025&categoria={admin.ids['casa']}").text
    assert "só Casa T" in t
    assert "370,00" in t                       # luz 100 + 150 + 120 (subcategoria incluída)
    assert "Ordenado" not in t and "40,00" not in t
    assert 'name="etiqueta"' not in t and 'name="categoria"' in t
    t = admin.get(f"/relatorios?entidade=1&periodo=ano&categoria={admin.ids['rest']}").text
    assert t.count("Restaurantes T") >= 1
    r = admin.get(f"/relatorios/csv?entidade=1&periodo=anos&anos=2024&categoria={admin.ids['casa']}")
    assert r.status_code == 200 and "150,00" in r.text and "1100,00" not in r.text
