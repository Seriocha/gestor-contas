"""Viagens: registo, sugestão dos gastos, associação, aprendizagem do modelo."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import viagens as vg
from app.main import create_app
from app.models import ModeloPadrao, ModeloViagem, Movimento, Viagem
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    for d in ("2026-04-05", "2026-05-05", "2026-06-05", "2026-08-05"):
        _mov(c, 1, d, "12,00", "NETFLIX.COM", sentido="saida")                 # despesa habitual
    _mov(c, 1, "2026-06-01", "300,00", "COMPRA 6344 RYANAIR DAC", sentido="saida")
    _mov(c, 1, "2026-06-15", "800,00", "COMPRA 6344 AIRBNB * HMQX", sentido="saida")
    _mov(c, 1, "2026-05-01", "30,00", "MERCADO LOCAL", sentido="saida")            # antes, sem padrão: não
    _mov(c, 2, "2026-07-10", "45,00", "RESTAURANT LE PORT", sentido="saida")      # durante, conta de viagem
    _mov(c, 1, "2026-07-11", "60,00", "COMPRA CONTINENTE", sentido="saida")       # durante, conta normal: 1 pt
    _mov(c, 1, "2026-07-12", "12,00", "NETFLIX.COM", sentido="saida")             # habitual: não
    _mov(c, 1, "2026-07-10", "50,00", "CONDOMINIO PREDIO", sentido="saida")       # excluído pelo modelo
    _mov(c, 2, "2026-07-13", "18,00", "UBER *TRIP", sentido="saida")
    return c


def _criar(c, **extra):
    dados = {"nome": "Maurícias 2026", "destino": "Grand Baie", "pais": "Maurícias", "data_ini": "2026-07-09",
             "data_fim": "2026-07-15", "pessoas": "2", "motivo": "lazer", "orcamento": "2.000,00", "moeda": "MUR"}
    r = _post(c, "/viagens/nova?entidade=1", **{**dados, **extra})
    assert r.status_code == 303, r.text
    return int(re.search(r"/viagens/(\d+)", r.headers["location"]).group(1))


def _ids(app, *descricoes):
    with app.state.SessionLocal() as db:
        return [db.query(Movimento).filter(Movimento.descricao_original == d).order_by(Movimento.id.desc()).first().id
                for d in descricoes]


def test_registar_e_sugerir(admin, app):
    vid = _criar(admin)
    with app.state.SessionLocal() as db:
        v = db.get(Viagem, vid)
        assert v.etiqueta.nome == "Viagem: Maurícias 2026" and v.modelo.nome == vg.MODELO_OMISSAO and v.dias == 7
        sug = {s.mov.descricao_original: s for s in vg.candidatos(db, v)}
    assert set(sug) == {"COMPRA 6344 RYANAIR DAC", "COMPRA 6344 AIRBNB * HMQX", "RESTAURANT LE PORT", "UBER *TRIP"}
    assert sug["COMPRA 6344 RYANAIR DAC"].fase == "antes" and sug["COMPRA 6344 RYANAIR DAC"].marcar
    assert sug["RESTAURANT LE PORT"].pontos == 3 and "conta de viagem (Revolut)" in sug["RESTAURANT LE PORT"].razoes
    p = admin.get(f"/viagens/{vid}").text
    assert "4 movimentos que podem ser desta viagem" in p and "Maurícias" in p and "padrão de viagem «RYANAIR»" in p
    assert "Viagens" in admin.get("/").text   # menu


def test_associar_rejeitar_e_resumo(admin, app):
    vid = _criar(admin)
    ids = _ids(app, "COMPRA 6344 RYANAIR DAC", "COMPRA 6344 AIRBNB * HMQX", "RESTAURANT LE PORT")
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/associar", ids=[str(i) for i in ids], acao="associar", aprender="1")
    uber, = _ids(app, "UBER *TRIP")
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/associar", ids=[str(uber)], acao="rejeitar", aprender="1")
    with app.state.SessionLocal() as db:
        v = db.get(Viagem, vid)
        r = vg.resumo(db, v)
        assert (r.total, r.antes, r.por_pessoa) == (114500, 110000, 57250)
        assert vg.candidatos(db, v) == []                                  # associados e rejeitados saem das sugestões
        padroes = {p.padrao: (p.positivos, p.negativos) for p in db.query(ModeloPadrao)}
        assert padroes["RESTAURANT LE PORT"] == (1, 0) and padroes["UBER"] == (0, 1)
        assert padroes["RYANAIR DAC"] == (1, 0)
    p = admin.get(f"/viagens/{vid}").text
    assert "1.145,00 €" in p and "Antes da partida" in p and "1.100,00 €" in p
    assert "1.145,00 €" in admin.get("/viagens?entidade=1").text
    # retirar um movimento da viagem
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/desassociar", ids=[str(ids[2])])
    with app.state.SessionLocal() as db:
        assert vg.resumo(db, db.get(Viagem, vid)).total == 110000


def test_o_modelo_aprende_para_a_proxima_viagem(admin, app):
    vid = _criar(admin)
    rest, uber = _ids(app, "RESTAURANT LE PORT", "UBER *TRIP")
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/associar", ids=[str(rest)], acao="associar", aprender="1")
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/associar", ids=[str(uber)], acao="rejeitar", aprender="1")
    # 2.ª viagem: um «RESTAURANT LE PORT» antes da partida já é reconhecido (aprendido); o UBER rejeitado 2× desaparece
    _mov(admin, 1, "2027-01-20", "40,00", "RESTAURANT LE PORT", sentido="saida")
    _mov(admin, 2, "2027-02-03", "20,00", "UBER *TRIP", sentido="saida")
    v2 = _criar(admin, nome="Lisboa 2027", data_ini="2027-02-01", data_fim="2027-02-05")
    with app.state.SessionLocal() as db:
        sug = {s.mov.descricao_original: s for s in vg.candidatos(db, db.get(Viagem, v2))}
    assert "confirmado em viagens anteriores (1×)" in sug["RESTAURANT LE PORT"].razoes
    assert "UBER *TRIP" in sug
    uber2, = _ids(app, "UBER *TRIP")
    _post(admin, f"/viagens/{v2}", f"/viagens/{v2}/associar", ids=[str(uber2)], acao="rejeitar", aprender="1")
    _mov(admin, 2, "2027-02-04", "15,00", "UBER *TRIP", sentido="saida")
    with app.state.SessionLocal() as db:
        assert "UBER *TRIP" not in {s.mov.descricao_original for s in vg.candidatos(db, db.get(Viagem, v2))}


def test_editar_modelo_copiar_e_esquecer(admin, app):
    _criar(admin)
    with app.state.SessionLocal() as db:
        mid = db.query(ModeloViagem).one().id
    p = admin.get(f"/viagens/modelos/{mid}").text
    assert "RYANAIR" in p and 'value="CARTEIRA_DIGITAL" checked' in p
    _post(admin, f"/viagens/modelos/{mid}", f"/viagens/modelos/{mid}", nome="Férias", dias_antes="120", dias_depois="2",
          tipos_conta=["CARTEIRA_DIGITAL"], incluir="ryanair\nuber", excluir="condominio", valor_min="5,00")
    with app.state.SessionLocal() as db:
        m = db.get(ModeloViagem, mid)
        assert (m.nome, m.dias_antes, m.incluir, m.tipos_conta, m.valor_min) == ("Férias", 120, "RYANAIR\nUBER",
                                                                                  "CARTEIRA_DIGITAL", 500)
        db.add(ModeloPadrao(modelo_id=mid, padrao="X", positivos=1, negativos=0))
        db.commit()
        pid = db.query(ModeloPadrao).one().id
    r = _post(admin, f"/viagens/modelos/{mid}", f"/viagens/modelos/{mid}/copiar")
    assert r.status_code == 303 and "Férias (cópia)" in admin.get(r.headers["location"]).text
    _post(admin, f"/viagens/modelos/{mid}", f"/viagens/modelos/{mid}/padroes/{pid}/apagar")
    with app.state.SessionLocal() as db:
        assert db.query(ModeloPadrao).filter_by(modelo_id=mid).count() == 0


def test_validacoes_e_apagar(admin, app):
    r = _post(admin, "/viagens/nova?entidade=1", nome="", data_ini="2026-07-10", data_fim="2026-07-01")
    assert r.status_code == 400 and "Dá um nome" in r.text and "regresso não pode ser antes" in r.text
    vid = _criar(admin)
    rest, = _ids(app, "RESTAURANT LE PORT")
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/associar", ids=[str(rest)], acao="associar", aprender="1")
    _post(admin, f"/viagens/{vid}/editar", f"/viagens/{vid}/apagar")
    with app.state.SessionLocal() as db:
        assert db.get(Viagem, vid) is None and db.get(Movimento, rest).etiquetas == []
