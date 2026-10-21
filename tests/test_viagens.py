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


def test_gerir_modelos_renomear_apagar_e_recalcular(admin, app):
    vid = _criar(admin)
    with app.state.SessionLocal() as db:
        base = db.query(ModeloViagem).one().id
    lista = admin.get("/viagens?entidade=1").text
    assert "Renomear" in lista and "em uso" in lista          # o modelo base está em uso: sem «Apagar»
    # renomear (e não deixar repetir nomes)
    _post(admin, "/viagens?entidade=1", f"/viagens/modelos/{base}/renomear", nome="Férias", voltar="lista")
    r = _post(admin, f"/viagens/modelos/{base}", f"/viagens/modelos/{base}/copiar")
    copia = int(r.headers["location"].rsplit("/", 1)[1])
    r = admin.post(f"/viagens/modelos/{copia}/renomear",
                   data={"csrf": _csrf(admin.get(f"/viagens/modelos/{copia}")), "nome": "Férias"})
    assert "Já existe um modelo «Férias»" in r.text
    _post(admin, f"/viagens/modelos/{copia}", f"/viagens/modelos/{copia}/renomear", nome="Trabalho")
    # apagar só quando não está em uso
    r = admin.post(f"/viagens/modelos/{base}/apagar", data={"csrf": _csrf(admin.get(f"/viagens/modelos/{base}"))})
    assert "está associado a 1 viagem" in r.text
    # trocar o modelo da viagem recalcula as sugestões; o antigo deixa de estar em uso e pode ser apagado
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/modelo", modelo_id=str(copia))
    with app.state.SessionLocal() as db:
        assert db.get(Viagem, vid).modelo_id == copia
    r = _post(admin, f"/viagens/modelos/{base}", f"/viagens/modelos/{base}/apagar")
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        assert db.get(ModeloViagem, base) is None and [m.nome for m in db.query(ModeloViagem)] == ["Trabalho"]
    assert "Trabalho" in admin.get("/viagens?entidade=1").text          # o padrão não é recriado


def test_ajustar_o_modelo_recalcula_as_sugestoes(admin, app):
    vid = _criar(admin)
    with app.state.SessionLocal() as db:
        mid = db.get(Viagem, vid).modelo_id
        antes = len(vg.candidatos(db, db.get(Viagem, vid)))
    assert antes == 4
    # tirar o Revolut das contas de viagem e excluir UBER: o restaurante e o uber deixam de ser sugeridos
    r = admin.post(f"/viagens/modelos/{mid}", data={
        "csrf": _csrf(admin.get(f"/viagens/modelos/{mid}")), "nome": "Viagem (padrão)", "dias_antes": "90",
        "dias_depois": "3", "tipos_conta": ["CARTAO_CREDITO"], "incluir": "RYANAIR\nAIRBNB", "excluir": "UBER"})
    assert "Sugestões recalculadas — Maurícias 2026: 2" in r.text and "Viagens que usam este modelo" in r.text
    p = admin.get(f"/viagens/{vid}").text
    assert "2 movimentos que podem ser desta viagem" in p and "↻ Recalcular" in p


def test_recalcular_inclui_movimentos_ja_etiquetados_e_ensina_o_modelo(admin, app):
    from app.models import Etiqueta, ViagemConfirmado
    vid = _criar(admin)
    rest, uber = _ids(app, "RESTAURANT LE PORT", "UBER *TRIP")
    with app.state.SessionLocal() as db:
        et_id = db.get(Viagem, vid).etiqueta_id
        mid = db.get(Viagem, vid).modelo_id
    # etiquetados à mão em Movimentos (ação em massa), fora do ecrã da viagem
    p = admin.get("/movimentos?entidade=1")
    admin.post("/movimentos-lote", data={"csrf": _csrf(p), "voltar": "/movimentos?entidade=1", "ambito": "selecao",
                                         "ids": [str(rest), str(uber)], "acao": f"etiqueta_mais:{et_id}"})
    with app.state.SessionLocal() as db:
        assert vg.resumo(db, db.get(Viagem, vid)).total == 6300                  # já contam na viagem
        assert db.query(ModeloPadrao).count() == 0                               # mas o modelo ainda não sabe
    r = _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/modelo", modelo_id=str(mid))
    with app.state.SessionLocal() as db:
        padroes = {p.padrao: p.positivos for p in db.query(ModeloPadrao)}
        assert padroes == {"RESTAURANT LE PORT": 1, "UBER": 1}
        assert db.query(ViagemConfirmado).count() == 2
    # recalcular outra vez não conta duas vezes
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/modelo", modelo_id=str(mid))
    with app.state.SessionLocal() as db:
        assert {p.padrao: p.positivos for p in db.query(ModeloPadrao)} == {"RESTAURANT LE PORT": 1, "UBER": 1}
    # retirar da viagem desfaz a aprendizagem
    _post(admin, f"/viagens/{vid}", f"/viagens/{vid}/desassociar", ids=[str(uber)])
    with app.state.SessionLocal() as db:
        assert {p.padrao: p.positivos for p in db.query(ModeloPadrao)}["UBER"] == 0
        assert db.query(ViagemConfirmado).count() == 1


def test_escolher_etiqueta_existente_ao_criar(admin, app):
    from app.models import Etiqueta
    with app.state.SessionLocal() as db:
        et = Etiqueta(nome="Maurícias")
        db.add(et)
        db.flush()
        for m in db.query(Movimento).filter(Movimento.descricao_original.in_(["RESTAURANT LE PORT", "UBER *TRIP"])):
            m.etiquetas.append(et)
        db.commit()
        et_id = et.id
    assert f'value="{et_id}"' in admin.get("/viagens/nova?entidade=1").text
    r = _post(admin, "/viagens/nova?entidade=1", nome="Maurícias 2026", data_ini="2026-07-09", data_fim="2026-07-15",
              pessoas="2", etiqueta_id=str(et_id))
    vid = int(re.search(r"/viagens/(\d+)", r.headers["location"]).group(1))
    assert "2 movimento(s) que já tinham a etiqueta entraram na viagem e no modelo" in admin.get(f"/viagens/{vid}").text
    with app.state.SessionLocal() as db:
        v = db.get(Viagem, vid)
        assert v.etiqueta_id == et_id and vg.resumo(db, v).total == 6300
        assert {p.padrao for p in db.query(ModeloPadrao)} == {"RESTAURANT LE PORT", "UBER"}
    # apagar a viagem não apaga uma etiqueta que era tua
    _post(admin, f"/viagens/{vid}/editar", f"/viagens/{vid}/apagar")
    with app.state.SessionLocal() as db:
        assert db.get(Etiqueta, et_id) is not None


def test_reutiliza_etiqueta_com_o_mesmo_nome(admin, app):
    from app.models import Etiqueta
    with app.state.SessionLocal() as db:
        db.add(Etiqueta(nome="Viagem: Maurícias 2026"))
        db.commit()
    vid = _criar(admin)
    with app.state.SessionLocal() as db:
        assert db.get(Viagem, vid).etiqueta.nome == "Viagem: Maurícias 2026"
        assert db.query(Etiqueta).filter(Etiqueta.nome.like("Viagem:%")).count() == 1


def test_acertar_as_datas_na_revisao(admin, app):
    """Na ficha (revisão das sugestões) o intervalo acerta-se sem ir ao Editar; as sugestões seguem as novas datas."""
    vid = _criar(admin, data_ini="2026-07-11", data_fim="2026-07-15")
    p = admin.get(f"/viagens/{vid}").text
    assert f'action="/viagens/{vid}/datas"' in p and 'name="data_ini" value="2026-07-11"' in p
    with app.state.SessionLocal() as db:
        antes = {s.mov.descricao_original for s in vg.candidatos(db, db.get(Viagem, vid))}
    assert "RESTAURANT LE PORT" not in antes                                    # 10/07: fora do intervalo
    r = admin.post(f"/viagens/{vid}/datas", data={"csrf": _csrf(admin.get(f"/viagens/{vid}")),
                                                  "data_ini": "2026-07-09", "data_fim": "2026-07-15"}, follow_redirects=True)
    assert "Datas da viagem: 09/07/2026 a 15/07/2026" in r.text
    with app.state.SessionLocal() as db:
        v = db.get(Viagem, vid)
        assert (v.data_ini, v.data_fim) == (date(2026, 7, 9), date(2026, 7, 15))
        assert "RESTAURANT LE PORT" in {s.mov.descricao_original for s in vg.candidatos(db, v)}
    r = admin.post(f"/viagens/{vid}/datas", data={"csrf": _csrf(admin.get(f"/viagens/{vid}")),
                                                  "data_ini": "2026-07-20", "data_fim": "2026-07-15"}, follow_redirects=True)
    assert "o regresso não pode ser antes da partida" in r.text
    with app.state.SessionLocal() as db:
        assert db.get(Viagem, vid).data_ini == date(2026, 7, 9)
