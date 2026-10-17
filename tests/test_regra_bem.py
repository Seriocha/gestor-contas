"""Regras que ligam o movimento a um bem, condicionadas ao universo (entidade) do bem."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import patrimonio as pt
from app.main import create_app
from app.models import Bem, Movimento, Regra
from tests.test_app import PW, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Família", tipo="ORDEM", moeda="EUR", instituicao_id="1")   # conta 1
    _post(c, "/contas/nova?entidade=2", nome="Herança", tipo="ORDEM", moeda="EUR", instituicao_id="1")   # conta 2
    _mov(c, 1, "2026-09-10", "60,00", "DD EDP COMERC 111", sentido="saida")
    _mov(c, 2, "2026-09-11", "45,00", "DD EDP COMERC 222", sentido="saida")
    return c


def _bem(c, ent, tipo, nome, **extra):
    r = _post(c, f"/patrimonio/novo?entidade={ent}&tipo={tipo}", nome=nome, revisao_meses="0", **extra)
    assert r.status_code == 303, r.text
    return int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))


def test_regra_liga_ao_bem_so_no_seu_universo(admin, app):
    casa = _bem(admin, 1, "casa", "Casa de Lisboa", uso="principal", omissao="1")
    monte = _bem(admin, 2, "rustico", "Monte", uso="exploracao")
    f = admin.get("/regras/nova").text
    assert '<optgroup label="Família">' in f and '<optgroup label="Mãe">' in f and "Monte · propriedade rústica" in f
    # conta de outra entidade: erro
    r = _post(admin, "/regras/nova", padrao="DD EDP COMERC", modo="contem", sentido="SAIDA", conta_id="1",
              bem_id=str(monte), ativa="1", prioridade="50")
    assert r.status_code == 400 and "não é da entidade" in r.text
    # «Testar» já respeita o universo: só o movimento da herança
    r = _post(admin, "/regras/nova", padrao="DD EDP COMERC", modo="contem", sentido="SAIDA", bem_id=str(monte),
              ativa="1", prioridade="50", testar="1")
    assert "Corresponde a 1 movimento" in r.text
    r = _post(admin, "/regras/nova", padrao="DD EDP COMERC", modo="contem", sentido="SAIDA", bem_id=str(monte),
              categoria_id="", ativa="1", prioridade="50", aplicar="1")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        movs = {m.descricao_original: m for m in db.scalars(select(Movimento))}
        assert movs["DD EDP COMERC 222"].bem_id == monte                 # herança → Monte
        assert movs["DD EDP COMERC 111"].bem_id == casa                  # Família: a regra não toca; fica a omissão
        # a atribuição automática mantém o bem da regra (ganha à omissão)
        pt.atribuir(db, 2)
        pt.atribuir(db, 1)
        assert movs["DD EDP COMERC 222"].bem_id == monte and movs["DD EDP COMERC 111"].bem_id == casa
        assert db.scalar(select(Regra).where(Regra.bem_id == monte)) is not None
    assert "→ Monte" in admin.get("/regras").text
    # nova importação na herança: a regra liga ao Monte
    _mov(admin, 2, "2026-10-11", "47,00", "DD EDP COMERC 222", sentido="saida")
    with app.state.SessionLocal() as db:
        from app.qualificacao import aplicar_regras
        m = db.scalar(select(Movimento).where(Movimento.montante == -4700))
        aplicar_regras(db, [m])
        assert m.bem_id == monte
