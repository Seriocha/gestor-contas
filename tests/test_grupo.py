"""Perfil do grupo (entidade): contas bancárias incluídas (passar contas entre entidades), bancos/importadores e fontes
automáticas de extratos (pasta da NAS, Paperless)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import pasta_importacao as pi
from app import tarefas
from app.main import create_app
from app.models import Bem, Conta, Entidade, Movimento
from app.parsers import Extrato
from tests.test_app import PW, _csrf, _post
from tests.test_patrimonio import _criar
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")       # 1
    _post(c, "/contas/nova?entidade=1", nome="ActivoBank", tipo="ORDEM", moeda="EUR", instituicao_id="2")     # 2
    r = _post(c, "/admin/entidades", nome="João", tipo="pessoal")
    assert r.status_code == 303
    return c


def _eid(app, nome):
    with app.state.SessionLocal() as db:
        return db.scalar(select(Entidade.id).where(Entidade.nome == nome))


def _guardar(admin, eid, **extra):
    f = admin.get(f"/admin/entidades/{eid}")
    dados = {"csrf": _csrf(f), "nome": extra.pop("nome", None) or "", "tipo": "pessoal", "modulos_presentes": "1",
             "modulos": ["imoveis"], "contas_presentes": "1", "perfil_presente": "1", **extra}
    return admin.post(f"/admin/entidades/{eid}", data=dados, follow_redirects=True)


def test_passar_conta_para_o_grupo_e_devolver(admin, app):
    joao = _eid(app, "João")
    casa = _criar(admin, nome="Casa de Lisboa")                     # casa da Família, por omissão
    _mov(admin, 2, "2026-09-10", "45,00", "CONDOMINIO", sentido="saida")
    with app.state.SessionLocal() as db:
        m = db.scalar(select(Movimento))
        m.bem_id, m.bem_manual = casa, True
        db.commit()
    r = admin.get(f"/admin/entidades/{joao}")
    assert "Contas bancárias deste grupo" in r.text and "Perfil de importação" in r.text and "ActivoBank" in r.text
    r = _guardar(admin, joao, nome="João", contas=["2"])
    assert "Contas movidas: ActivoBank (Família → João)" in r.text
    with app.state.SessionLocal() as db:
        c = db.get(Conta, 2)
        assert c.entidade_id == joao
        m = db.scalar(select(Movimento))
        assert m.bem_id is None and not m.bem_manual             # a casa era da Família
    # retirar sem dizer para onde: erro, nada muda
    r = _guardar(admin, joao, nome="João")
    assert r.status_code == 400 and "Escolhe para que entidade vão as contas retiradas" in r.text
    r = _guardar(admin, joao, nome="João", destino=str(_eid(app, "Família")))
    assert "ActivoBank (João → Família)" in r.text
    with app.state.SessionLocal() as db:
        assert db.get(Conta, 2).entidade_id == _eid(app, "Família")


def test_nome_repetido_no_destino(admin, app):
    joao = _eid(app, "João")
    _post(admin, f"/contas/nova?entidade={joao}", nome="Santander", tipo="ORDEM", moeda="EUR", instituicao_id="1")
    r = _guardar(admin, joao, nome="João", contas=["1", "3"])
    assert r.status_code == 400 and "já existe uma conta com esse nome" in r.text
    with app.state.SessionLocal() as db:
        assert db.get(Conta, 1).entidade_id == _eid(app, "Família")


def test_bancos_do_perfil(admin, app):
    joao = _eid(app, "João")
    r = _guardar(admin, joao, nome="João", bancos=["2"], fontes=["pasta"])
    assert "Entidade «João» guardada" in r.text
    with app.state.SessionLocal() as db:
        e = db.get(Entidade, joao)
        assert e.ids_bancos == {2} and e.tem_banco(2) and not e.tem_banco(1) and e.fontes == "pasta"
    f = admin.get(f"/contas/nova?entidade={joao}")
    assert "ActivoBank" in f.text and ">Santander<" not in f.text           # só os bancos do grupo
    _post(admin, f"/contas/nova?entidade={joao}", nome="ActivoBank João", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    with app.state.SessionLocal() as db:
        c = db.scalar(select(Conta).where(Conta.nome == "ActivoBank João"))
        c2 = Conta(entidade_id=joao, instituicao_id=1, nome="Santander antigo", tipo="ORDEM", moeda="EUR")
        db.add(c2)
        db.commit()
        nomes = [t.conta.nome for b in tarefas.do_ciclo(db, joao, date(2026, 10, 1)) for t in b.tarefas]
        assert nomes == ["ActivoBank João"]                                 # Santander fora do perfil


def test_fontes_automaticas(admin, app):
    joao = _eid(app, "João")
    _post(admin, f"/contas/nova?entidade={joao}", nome="ActivoBank João", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    r = _guardar(admin, joao, nome="João", contas=["3"], fontes=["paperless"])   # sem a pasta da NAS
    assert "Entidade «João» guardada" in r.text
    ext = Extrato(parser="activobank", banco="ActivoBank", movimentos=[])
    with app.state.SessionLocal() as db:
        da_pasta = {c.nome for c in pi._candidatas(db, ext, "ActivoBank")}
        do_paperless = {c.nome for c in pi._candidatas(db, ext, "Paperless")}
    assert da_pasta == {"ActivoBank"} and do_paperless == {"ActivoBank", "ActivoBank João"}
