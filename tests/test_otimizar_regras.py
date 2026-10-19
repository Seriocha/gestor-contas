"""Otimizar regras: duplicadas, sombreadas (redundantes ou em conflito), sem uso e agregáveis (pela simulação)."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import analise_regras as ar
from app.main import create_app
from app.models import Categoria, Movimento, Regra
from app.qualificacao import regras_ativas
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

D = (date.today() - timedelta(days=3)).isoformat()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    for desc in ("CONTINENTE AMADORA", "CONTINENTE BOM DIA LUZ", "PINGO DOCE LISBOA", "LIDL AGRADECE", "ALDI BENFICA"):
        _mov(c, 1, D, "20,00", desc, sentido="saida")
    with app.state.SessionLocal() as db:
        db.execute(delete(Regra))                        # sem as regras de origem, para o teste ser previsível
        cat = {c.nome: c.id for c in db.scalars(select(Categoria))}
        sup, rest, outras = cat["Supermercado"], cat["Restaurantes e cafés"], cat["Outras despesas"]
        regras = [
            ("CONTINENTE", sup, 10), ("PINGO DOCE", sup, 20),       # agregáveis (mesma ação)
            ("CONTINENTE BOM DIA", sup, 30),                        # tapada pela 1.ª, faz o mesmo → redundante
            ("PINGO DOCE", sup, 40),                                # duplicada da 2.ª
            ("CONTINENTE", rest, 50),                               # tapada pela 1.ª com outra ação → conflito
            ("XPTO NUNCA", sup, 60),                                # sem uso
            ("ALDI", outras, 5), ("LIDL AGRADECE", rest, 55), ("LIDL", outras, 70),  # agregar ALDI+LIDL mudaria o LIDL
        ]
        for padrao, c_id, p in regras:
            db.add(Regra(padrao=padrao, modo="contem", sentido="SAIDA", categoria_id=c_id, prioridade=p))
        db.commit()
    return c


def test_analise(admin, app):
    with app.state.SessionLocal() as db:
        a = ar.analisar(regras_ativas(db), db.scalars(select(Movimento)).all())
        por = lambda lista: sorted(r.padrao for r in lista)
        assert [(r.padrao, r.prioridade, o.prioridade) for r, o in a.duplicadas] == [("PINGO DOCE", 40, 20), ("CONTINENTE", 50, 10)]
        somb = {(s.regra.padrao, s.regra.prioridade): s for s in a.sombreadas}
        assert somb[("CONTINENTE BOM DIA", 30)].redundante
        assert ("CONTINENTE", 50) not in somb                                # é duplicada (com outra ação)
        assert ("LIDL", 70) in somb and not somb[("LIDL", 70)].redundante     # tapada pela «LIDL AGRADECE»
        assert por(a.sem_uso) == ["XPTO NUNCA"]
        grupos = {g.padrao: g for g in a.agregaveis}
        sup = next(g for g in a.agregaveis if g.regras[0].padrao == "CONTINENTE" and g.regras[0].prioridade == 10)
        assert sup.seguro and sup.padrao == "CONTINENTE|PINGO DOCE|CONTINENTE BOM DIA|XPTO NUNCA"
        aldi = next(g for g in a.agregaveis if g.regras[0].padrao == "ALDI")
        assert not aldi.seguro and aldi.mudariam == 1                        # o «LIDL AGRADECE» passaria a «Outras»


def test_pagina_agregar_e_apagar(admin, app):
    p = admin.get("/regras/analise").text
    assert "Otimizar regras" in p and "Agregáveis" in p and "Redundantes" in p and "Sem uso" in p
    assert "conflito: tapada por outra ação" in p and "duplicada" in p and "ação diferente" in p and "Agregar mudaria o resultado em 1 movimento" in p
    assert 'href="/regras/analise">🔍 Otimizar regras' in admin.get("/regras").text
    with app.state.SessionLocal() as db:
        ids = [r.id for r in db.scalars(select(Regra).where(Regra.prioridade.in_((10, 20, 30, 60))).order_by(Regra.prioridade))]
        cont = db.scalar(select(Movimento).where(Movimento.descricao_original == "CONTINENTE AMADORA"))
        cont.regra_id = ids[1]                          # um movimento qualificado por uma regra que vai sair
        db.commit()
        mid = cont.id
    admin.post("/regras/analise/agregar", data={"csrf": _csrf(admin.get("/regras/analise")), "ids": [str(i) for i in ids]})
    with app.state.SessionLocal() as db:
        fica = db.get(Regra, ids[0])
        assert fica.padrao == "CONTINENTE|PINGO DOCE|CONTINENTE BOM DIA|XPTO NUNCA" and fica.prioridade == 10
        assert all(db.get(Regra, i) is None for i in ids[1:])
        assert db.get(Movimento, mid).regra_id == ids[0]
        conflito = db.scalar(select(Regra).where(Regra.prioridade == 50)).id
    # agregar regras com ações diferentes é recusado
    r = admin.post("/regras/analise/agregar", data={"csrf": _csrf(admin.get("/regras/analise")), "ids": [str(ids[0]), str(conflito)]})
    assert "não se podem agregar" in r.text
    admin.post("/regras/analise/apagar", data={"csrf": _csrf(admin.get("/regras/analise")), "ids": [str(conflito)]})
    with app.state.SessionLocal() as db:
        assert db.get(Regra, conflito) is None
