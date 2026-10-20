"""Relatórios › Comparar fornecedores: um serviço (ex.: Eletricidade) ano a ano por fornecedor, a mudança EDP → G9 com os
12 meses antes/depois, a grelha mês a mês, a sugestão do fornecedor novo sem categoria e a regra para o categorizar."""
import hashlib
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import fornecedores as fo
from app.main import create_app
from app.models import Categoria, Movimento, Regra
from tests.test_app import PW, _csrf, _post

HOJE = date(2026, 10, 20)


def _add(db, d: date, desc: str, valor: float, cat_id=None):
    h = hashlib.sha256(f"{d}{desc}{valor}".encode()).hexdigest()
    db.add(Movimento(conta_id=1, data_mov=d, descricao_original=desc, descricao_norm=desc.upper(), montante=round(-valor * 100),
                     moeda="EUR", hash_dedup=h, categoria_id=cat_id))


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    with app.state.SessionLocal() as db:
        casa = Categoria(nome="Casa T", natureza="DESPESA")
        db.add(casa)
        db.flush()
        luz = Categoria(nome="Eletricidade", natureza="DESPESA", pai_id=casa.id)
        db.add(luz)
        db.flush()
        # EDP de jan/2023 a nov/2024: 150 €/mês; G9 de dez/2024 em diante: 135 €/mês (sem categoria)
        for ano in (2023, 2024):
            for mes in range(1, 13):
                if (ano, mes) < (2024, 12):
                    _add(db, date(ano, mes, 10), "DÉBITO DIRETO-EDP COMERCIAL CO-60230660104", 150, luz.id)
        for ano, mes in [(2024, 12)] + [(2025, m) for m in range(1, 13)] + [(2026, m) for m in range(1, 10)]:
            _add(db, date(ano, mes, 26), "DÉBITO DIRETO-G9,SA-10006302807", 135)
        _add(db, date(2025, 3, 5), "COMPRA *1264 SUPERMERCADO", 40)            # não é do serviço
        for mes in range(1, 6):
            _add(db, date(2025, mes, 8), "DD VODAFONE PORTU 0722", 34)         # telecom: não é sugerido na luz
        db.commit()
        c.luz = luz.id
    return c


def test_analise_ano_a_ano_e_mudanca(app, admin):
    with app.state.SessionLocal() as db:
        a = fo.analisar(db, 1, admin.luz, ["G9"], HOJE)
    assert a.fornecedores == ["EDP", "G9"] and len(a.sem_categoria) == 22
    anos = {x.ano: x for x in a.anos}
    assert anos[2023].por_fornecedor == {"EDP": (12, 180000)} and anos[2023].media_mes == 15000
    assert anos[2024].por_fornecedor == {"EDP": (11, 165000), "G9": (1, 13500)}
    mu, = a.mudancas
    assert (mu.data, mu.de, mu.para) == (date(2024, 12, 26), "EDP", "G9")
    assert (mu.media_antes, mu.media_depois, mu.meses_antes, mu.meses_depois) == (15000, 13500, 12, 12)
    assert round(mu.variacao, 1) == -10.0
    # sem os textos extra, só a categoria: a G9 (sem categoria) não entra
    with app.state.SessionLocal() as db:
        assert fo.analisar(db, 1, admin.luz, [], HOJE).fornecedores == ["EDP"]


def test_pagina_sugestao_e_regra(app, admin):
    t = admin.get(f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}").text
    assert "Há débitos sem categoria de <strong>G9</strong> (22)" in t and "Vodafone" not in t
    assert "Comparar fornecedores" in admin.get("/relatorios?entidade=1").text
    t = admin.get(f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}&extras=G9").text
    assert "EDP → G9" in t and "150,00 €/mês" in t and "135,00 €/mês" in t and "-10,0%" in t
    assert "-180,00 € por ano" in t and "«G9» → Eletricidade" in t
    assert 'class="valor-mes"' in t
    r = admin.post("/relatorios/fornecedores/categorizar", data={"csrf": _csrf(admin.get("/relatorios")), "entidade": "1",
                                                                 "categoria": str(admin.luz), "texto": "G9", "extras": "G9"})
    assert "Regra «G9» → Casa T › Eletricidade criada; 22 débitos categorizados." in r.text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(Regra).where(Regra.padrao == "G9")).categoria_id == admin.luz
        a = fo.analisar(db, 1, admin.luz, [], HOJE)
        assert a.fornecedores == ["EDP", "G9"] and not a.sem_categoria          # agora já pela categoria
