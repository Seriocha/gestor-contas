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


def test_duas_casas_contratos_e_dia_do_mes(app, admin):
    """EDP com um contrato por casa (referências diferentes); a G9 cobra as duas com a mesma referência, em dias diferentes."""
    from app.models import Bem, ContratoServico
    with app.state.SessionLocal() as db:
        for m in db.scalars(select(Movimento)):
            db.delete(m)
        povoa, algarve = Bem(entidade_id=1, tipo="casa", nome="Póvoa", uso="principal"), Bem(entidade_id=1, tipo="casa", nome="Algarve", uso="ferias")
        db.add_all([povoa, algarve])
        db.flush()
        for ano, mes in [(2024, m) for m in range(1, 13)] + [(2025, m) for m in range(1, 13)]:
            if (ano, mes) < (2024, 12):
                _add(db, date(ano, mes, 25), "DÉBITO DIRETO-EDP COMERCIAL-CO-60230660104", 160, admin.luz)
                _add(db, date(ano, mes, 3), "DÉBITO DIRETO-EDP COMERCIAL-CO-60232351784", 20, admin.luz)
            else:
                _add(db, date(ano, mes, 17), "DÉBITO DIRETO-G9,SA-10006302807", 144, admin.luz)
                _add(db, date(ano, mes, 25), "DÉBITO DIRETO-G9,SA-10006302807", 12, admin.luz)
        db.commit()
        ids = (povoa.id, algarve.id)
        a = fo.analisar(db, 1, admin.luz, [], HOJE)
    props = {(p.fornecedor, p.referencia, p.dia_de): (p.n, p.media) for p in a.propostas}
    assert props == {("EDP", "60230660104", None): (11, 16000), ("EDP", "60232351784", None): (11, 2000),
                     ("G9", "10006302807", 1): (13, 14400), ("G9", "10006302807", 18): (13, 1200)}
    t = admin.get(f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}").text
    assert "Contratos encontrados" in t and t.count('action="/relatorios/fornecedores/contrato"') == 4

    def ligar(texto, ref, de, ate, bem):
        return admin.post("/relatorios/fornecedores/contrato", data={
            "csrf": _csrf(admin.get("/relatorios")), "entidade": "1", "texto": texto, "referencia": ref, "dia_de": de,
            "dia_ate": ate, "bem": str(bem), "voltar": f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}"})
    assert "EDP 60230660104 → Póvoa." in ligar("EDP", "60230660104", "", "", ids[0]).text
    ligar("EDP", "60232351784", "", "", ids[1])
    ligar("G9", "10006302807", "1", "17", ids[0])
    r = ligar("G9", "10006302807", "18", "31", ids[1])
    assert "G9 10006302807, dias 18–31 → Algarve." in r.text
    with app.state.SessionLocal() as db:
        assert db.scalar(select(ContratoServico).where(ContratoServico.dia_de == 18)).bem_id == ids[1]
        a = fo.analisar(db, 1, admin.luz, [], HOJE)
    assert [c.nome for c in a.casas] == ["Póvoa", "Algarve"] and not a.propostas
    povoa_r, algarve_r = a.casas
    assert (povoa_r.mudancas[0].media_antes, povoa_r.mudancas[0].media_depois) == (16000, 14400)
    assert (algarve_r.mudancas[0].media_antes, algarve_r.mudancas[0].media_depois) == (2000, 1200)
    t = admin.get(f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}").text
    assert 'data-grafico="fornecedores_casas"' in t and 'data-grafico="fornecedores_mensal"' in t
    import json, re
    dados = json.loads(re.search(r'id="dados-graficos">(.*?)</script>', t, re.S).group(1))
    assert {s["pilha"] for s in dados["fornecedores_casas"]["series"]} == {"Póvoa", "Algarve"}
    assert "Póvoa · EDP" in [s["nome"] for s in dados["fornecedores_casas"]["series"]]
    assert "160,00 €/mês" in t and "12,00 €/mês" in t


def test_ver_debitos_da_proposta_e_atribuir_a_mao(app, admin):
    from app.models import Bem
    with app.state.SessionLocal() as db:
        povoa, algarve = Bem(entidade_id=1, tipo="casa", nome="Póvoa", uso="principal"), Bem(entidade_id=1, tipo="casa", nome="Algarve", uso="ferias")
        db.add_all([povoa, algarve])
        db.commit()
        ids = (povoa.id, algarve.id)
    t = admin.get(f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}").text
    assert "Ver os 23 débitos" in t and 'action="/relatorios/fornecedores/atribuir"' in t
    assert "DÉBITO DIRETO-EDP COMERCIAL CO-60230660104" in t and "por atribuir" in t
    with app.state.SessionLocal() as db:
        edp = db.scalars(select(Movimento.id).where(Movimento.descricao_original.contains("EDP")).order_by(Movimento.data_mov)).all()

    def atribuir(ids_movs, bem):
        return admin.post("/relatorios/fornecedores/atribuir", data={
            "csrf": _csrf(admin.get("/relatorios")), "entidade": "1", "bem": str(bem), "ids": [str(i) for i in ids_movs],
            "voltar": f"/relatorios/fornecedores?entidade=1&categoria={admin.luz}"})
    r = atribuir(edp[:3], ids[1])
    assert "3 débitos → Algarve (à mão)." in r.text and "Algarve <span class=\"nota\">· à mão</span>" in r.text
    with app.state.SessionLocal() as db:
        m = db.get(Movimento, edp[0])
        assert (m.bem_id, m.bem_manual) == (ids[1], True)
        a = fo.analisar(db, 1, admin.luz, [], HOJE)
    algarve_r = next(c for c in a.casas if c.nome == "Algarve")
    assert len(algarve_r.movimentos) == 3 and any(c.nome == fo.POR_ATRIBUIR for c in a.casas)
    # a escolha à mão ganha a um contrato que diria outra casa
    admin.post("/relatorios/fornecedores/contrato", data={"csrf": _csrf(admin.get("/relatorios")), "entidade": "1", "texto": "EDP",
                                                         "referencia": "60230660104", "bem": str(ids[0])})
    with app.state.SessionLocal() as db:
        a = fo.analisar(db, 1, admin.luz, [], HOJE)
        assert a.atribuicao[edp[0]] == ("Algarve", "à mão") and a.atribuicao[edp[5]] == ("Póvoa", "contrato")
    r = atribuir(edp[:3], "contratos")
    assert "3 débitos voltam aos contratos." in r.text
    with app.state.SessionLocal() as db:
        assert not db.get(Movimento, edp[0]).bem_manual
        assert fo.analisar(db, 1, admin.luz, [], HOJE).atribuicao[edp[0]] == ("Póvoa", "contrato")
