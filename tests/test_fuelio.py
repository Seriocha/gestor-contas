"""Fuelio: leitura do CSV de sincronização, km, ligação dos abastecimentos aos débitos e consumo."""
import io
import re
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import fuelio
from app import patrimonio as pt
from app.main import create_app
from app.models import Abastecimento, Bem, Movimento
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov

HOJE = date(2026, 10, 17)
LOG = ('"Data","Odo (km)","Fuel (litres)","Full","Price (optional)","l/100km (optional)","latitude (optional)",'
       '"longitude (optional)","City (optional)","Notes (optional)","Missed","TankNumber","FuelType","VolumePrice",'
       '"StationID (optional)","ExcludeDistance","UniqueId","TankCalc","Weather","guid","lastupdated"\n')


def _ficheiro(linhas, matricula="AA11BB"):
    txt = ('"## Vehicle"\n"Name","Description","DistUnit","FuelUnit","ConsumptionUnit","ImportCSVDateFormat","VIN",'
           '"Insurance","Plate","Make","Model","Year","TankCount"\n'
           f'"Opel","Frontera ","0","0","0","yyyy-MM-dd","VIN123","","{matricula}","","Opel",,"1"\n"## Log"\n' + LOG)
    for i, (quando, km, litros, cheio, valor, posto) in enumerate(linhas):
        txt += (f'"{quando}","{km}.0","{litros}","{cheio}","{valor}","0.0","38.8","-9.0","{posto}","","0","1","110",'
                f'"1.7","1","0.0","{i}","0.0",,"guid-{i}","1"\n')
    txt += '"## FavStations"\n"NameBrand","Latitude"\n"PRIO","38.8"\n"## TripLog"\n"title"\n"Viagem"\n'
    return txt.encode("utf-8")


ABAST = [("2026-08-01 20:00", 10000, "40.0", "1", "70.00", "INTERMARCHÉ"),
         ("2026-08-20 10:00", 10600, "20.0", "0", "35.00", "PRIO"),        # parcial
         ("2026-09-05 09:00", 11000, "20.0", "1", "36.02", "GALP"),        # banco 36,00 (diferença de 2 cêntimos)
         ("2026-09-30 09:00", 11500, "30.0", "1", "55.00", "BP")]          # sem débito no banco


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Ordem", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    _mov(c, 1, "2026-07-31", "70,00", "INTERMARCHE SANTA IRIA", sentido="saida")   # 1 dia antes (banco adianta)
    _mov(c, 1, "2026-08-22", "35,00", "PRIO VIALONGA", sentido="saida")
    _mov(c, 1, "2026-09-08", "36,00", "GALP CASCAIS", sentido="saida")
    _mov(c, 1, "2026-09-05", "36,02", "RESTAURANTE", sentido="saida")             # valor exato, mas é um restaurante
    with app.state.SessionLocal() as db:
        from app.models import Categoria
        rest = db.scalar(select(Categoria).where(Categoria.nome == "Restaurantes e cafés"))
        db.scalar(select(Movimento).where(Movimento.descricao_original == "RESTAURANTE")).categoria_id = rest.id
        db.commit()
    return c


def _carro(c, nome, **extra):
    r = _post(c, "/patrimonio/novo?entidade=1&tipo=carro", nome=nome, uso="principal", revisao_meses="0", **extra)
    assert r.status_code == 303, r.text
    return int(re.search(r"/patrimonio/(\d+)", r.headers["location"]).group(1))


def _importar(c, bid, nome, dados):
    """A importação do Fuelio é uma tarefa administrativa (Administração › Tarefas)."""
    f = c.get("/importar/validar")
    return c.post("/admin/fuelio", data={"csrf": _csrf(f), "bem_id": str(bid)},
                  files={"ficheiro": (nome, dados, "text/csv")}, follow_redirects=False)


def test_ler():
    f = fuelio.ler("v.csv", _ficheiro(ABAST))
    assert f.veiculo.matricula == "AA11BB" and f.veiculo.modelo == "Opel" and f.veiculo.descricao == "Frontera"
    assert len(f.registos) == 4 and f.registos[0].valor == 7000 and f.registos[1].cheio is False
    assert f.registos[0].km == 10000 and f.registos[0].posto == "INTERMARCHÉ" and f.registos[0].guid == "guid-0"
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("vehicle-4-sync.csv", _ficheiro(ABAST))
    assert len(fuelio.ler("v.zip", z.getvalue()).registos) == 4
    with pytest.raises(ValueError):
        fuelio.ler("x.csv", b"Data;Valor\n")


def test_importar_ligar_consumo(admin, app):
    proprio = _carro(admin, "Golf", matricula="CC-22-DD", omissao="1")          # fica com o combustível «por omissão»
    frontera = _carro(admin, "Frontera")
    r = _importar(admin, frontera, "vehicle-4-sync.csv", _ficheiro(ABAST))
    assert r.status_code == 303
    assert "4 abastecimentos novos" in admin.get(f"/patrimonio/{frontera}").text
    with app.state.SessionLocal() as db:
        b = db.get(Bem, frontera)
        assert b.matricula == "AA11BB" and b.modelo == "Frontera" and b.marca == "Opel"   # completado pelo Fuelio
        assert [r.km for r in b.registos_km] == [10000, 10600, 11000, 11500] and b.km == 11500
        lig = {a.data: (a.movimento.descricao_original if a.movimento else None) for a in db.scalars(select(Abastecimento))}
        assert lig == {date(2026, 8, 1): "INTERMARCHE SANTA IRIA", date(2026, 8, 20): "PRIO VIALONGA",
                       date(2026, 9, 5): "GALP CASCAIS", date(2026, 9, 30): None}
        m = db.scalar(select(Movimento).where(Movimento.descricao_original == "INTERMARCHE SANTA IRIA"))
        assert m.categoria.nome == "Combustível" and m.bem_id == frontera          # e não o carro «por omissão»
        rest = db.scalar(select(Movimento).where(Movimento.descricao_original == "RESTAURANTE"))
        assert rest.bem_id is None and rest.categoria.nome == "Restaurantes e cafés"
        _ = proprio
        c = fuelio.consumo(db, b, date(2025, 1, 1), HOJE)
        # do 1.º cheio (10 000) ao último cheio (11 500): 20 + 20 + 30 litros em 1500 km
        assert round(c.l100, 2) == 4.67 and c.km == 1500 and round(c.custo_km, 4) == round((35 + 36.02 + 55) / 1500, 4)
        assert c.ligados == 3
    # importar de novo: nada de novo; outro carro: recusado
    r = _importar(admin, frontera, "v.csv", _ficheiro(ABAST))
    assert "0 abastecimentos novos" in admin.get(f"/patrimonio/{frontera}").text
    _importar(admin, proprio, "v.csv", _ficheiro(ABAST))
    assert "não de CC-22-DD" in admin.get(f"/patrimonio/{proprio}").text
    p = admin.get(f"/patrimonio/{frontera}").text
    assert "Abastecimentos" in p and "l/100 km" in p and "por ligar" in p


def test_banco_depois_e_consolidacao(admin, app):
    from app import consolidacao
    from app.models import Entidade
    b = _carro(admin, "Frontera")
    _importar(admin, b, "v.csv", _ficheiro(ABAST))
    _mov(admin, 1, "2026-10-02", "55,00", "BP VILAMOURA", sentido="saida")      # chega depois (manual não liga sozinho)
    with app.state.SessionLocal() as db:
        ent = db.get(Entidade, 1)
        assert consolidacao.diagnostico(db, ent).abastecimentos_por_ligar == 1
        r = consolidacao.consolidar(db, ent, {"fuelio"})
        db.commit()
        assert r["fuelio"] == 1
        m = db.scalar(select(Movimento).where(Movimento.descricao_original == "BP VILAMOURA"))
        assert m.bem_id == b and m.categoria.nome == "Combustível"


def test_fuelio_so_nas_tarefas_e_carro_vendido(admin, app):
    b = _carro(admin, "Velho", data_venda="2026-01-31")
    p = admin.get(f"/patrimonio/{b}").text
    assert f"/patrimonio/{b}/fuelio\"" not in p and "Administração › Fuelio" in p and "marcado como vendido em 31/01/2026" in p
    t = admin.get("/admin/fuelio").text
    assert "Importar histórico do Fuelio" in t and "Velho" in t
    assert admin.post(f"/patrimonio/{b}/fuelio", data={}).status_code in (404, 405)


def test_corrigir_e_apagar_abastecimento(admin, app):
    b = _carro(admin, "Frontera")
    _importar(admin, b, "v.csv", _ficheiro(ABAST))
    with app.state.SessionLocal() as db:
        galp = db.scalar(select(Abastecimento).where(Abastecimento.data == date(2026, 9, 5)))
        assert galp.valor == 3602 and -galp.movimento.montante == 3600       # 2 cêntimos de diferença
        aid = galp.id
    ficha = admin.get(f"/patrimonio/{b}").text
    assert "Diferença" in ficha and "-0,02 €" in ficha and "Usar o valor do banco (36,00 €)" in ficha
    admin.post(f"/patrimonio/{b}/abastecimento/{aid}", data={"csrf": _csrf(admin.get(f"/patrimonio/{b}")), "usar_banco": "1"})
    with app.state.SessionLocal() as db:
        assert db.get(Abastecimento, aid).valor == 3600
    # correção do valor que deixa de bater com o débito: desliga e procura de novo (não há outro → por ligar)
    admin.post(f"/patrimonio/{b}/abastecimento/{aid}", data={"csrf": _csrf(admin.get(f"/patrimonio/{b}")),
               "data": "2026-09-05", "hora": "09:00", "km": "11010", "litros": "21,5", "valor": "45,00", "cheio": "1"})
    with app.state.SessionLocal() as db:
        a = db.get(Abastecimento, aid)
        assert (a.valor, a.litros, a.km, a.movimento_id) == (4500, 21.5, 11010, None)
        assert 11010 in [r.km for r in db.get(Bem, b).registos_km]           # a leitura de km acompanha
    admin.post(f"/patrimonio/{b}/abastecimento/{aid}/apagar", data={"csrf": _csrf(admin.get(f"/patrimonio/{b}"))})
    with app.state.SessionLocal() as db:
        assert db.get(Abastecimento, aid) is None
