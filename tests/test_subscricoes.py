"""Subscrições: deteção (catálogo e recorrências), registo, ligação aos débitos, estado e fidelização."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import subscricoes as sb
from app.main import create_app
from app.models import Subscricao
from tests.test_app import PW, _post
from tests.test_previsoes import _mov

HOJE = date(2026, 10, 17)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Revolut", tipo="CARTEIRA_DIGITAL", moeda="EUR", instituicao_id="5")
    for d in ("2026-06-29", "2026-07-29", "2026-08-29"):
        _mov(c, 1, d, "16,99", "Spotify", sentido="saida")
    _mov(c, 1, "2026-09-29", "17,99", "Spotify", sentido="saida")                 # aumentou
    for d in ("2026-06-05", "2026-07-06"):
        _mov(c, 1, d, "13,99", "NETFLIX.COM", sentido="saida")                  # parou em julho
    for d in ("2026-07-02", "2026-08-03", "2026-09-02"):
        _mov(c, 1, d, "36,21", "LOJA DO CONDOMINIO", sentido="saida")           # recorrente fora do catálogo
    for d, v in (("2026-07-10", "1,65"), ("2026-07-11", "2,10"), ("2026-07-15", "1,65"), ("2026-08-12", "4,50"),
                 ("2026-09-03", "1,20"), ("2026-09-04", "1,65")):
        _mov(c, 1, d, v, "ROYAL VERSAILLES", sentido="saida")                  # cafés: não
    for d, v in (("2026-07-20", "12,00"), ("2026-08-20", "89,90"), ("2026-09-21", "5,49")):
        _mov(c, 1, d, v, "AMAZON MKTP", sentido="saida")                       # valores variáveis: não
    _mov(c, 1, "2026-10-10", "5,00", "OUTRA COISA", sentido="saida")           # extrato até 10/10
    return c


def test_detetar(admin, app):
    with app.state.SessionLocal() as db:
        sug = {s.nome: s for s in sb.detetar(db, 1, HOJE)}
    assert set(sug) == {"Spotify", "Netflix", "Loja Do Condominio"}
    sp = sug["Spotify"]
    assert (sp.fonte, sp.periodicidade, sp.dia, sp.valor, sp.inicio, sp.n) == ("catálogo", "mensal", 29, 1799,
                                                                              date(2026, 6, 29), 4)
    assert sp.ativa and not sug["Netflix"].ativa                            # a Netflix parou em julho
    assert sug["Loja Do Condominio"].fonte == "recorrente" and sug["Loja Do Condominio"].valor == 3621
    p = admin.get("/subscricoes?entidade=1").text
    assert "Sugestões dos teus movimentos" in p and "Adicionar" in p and "nome=Spotify" in p


def _criar(c, **dados):
    base = {"nome": "Spotify", "servico": "spotify", "conta_id": "1", "valor": "16,99", "periodicidade": "mensal",
            "dia": "29", "inicio": "2026-06-29", "padrao": "SPOTIFY", "fim_fidelizacao": "", "fim": ""}
    r = _post(c, "/subscricoes/nova?entidade=1", **{**base, **dados})
    assert r.status_code == 303, r.text
    return int(re.search(r"/subscricoes/(\d+)", r.headers["location"]).group(1))


def test_registar_ligar_e_estado(admin, app):
    sid = _criar(admin, fim_fidelizacao="2027-06-29")
    with app.state.SessionLocal() as db:
        e = sb.estado(db, db.get(Subscricao, sid), HOJE)
    assert len(e.pagos) == 4 and e.total_pago == 3 * 1699 + 1799
    assert e.situacao == "em dia" and e.valor_mudou == 1799 and e.proxima == date(2026, 10, 29)
    assert e.fidelizada_dias == (date(2027, 6, 29) - HOJE).days and e.meses == 3 and e.custo_anual == 12 * 1699
    p = admin.get(f"/subscricoes/{sid}").text
    assert "Débitos encontrados" in p and "valor mudou" not in p.split("Situação")[0] and "17,99" in p
    lista = admin.get("/subscricoes?entidade=1").text
    assert "valor mudou: 17,99 €" in lista and "faltam" in lista and "203,88 €" in lista     # 16,99 × 12
    # já coberta: deixa de ser sugerida
    with app.state.SessionLocal() as db:
        assert "Spotify" not in {s.nome for s in sb.detetar(db, 1, HOJE)}


def test_em_atraso_e_terminada(admin, app):
    sid = _criar(admin, nome="Netflix", servico="netflix", valor="13,99", dia="5", inicio="2026-06-05", padrao="NETFLIX")
    with app.state.SessionLocal() as db:
        e = sb.estado(db, db.get(Subscricao, sid), HOJE)
        assert e.situacao == "em atraso" and e.falhas == [date(2026, 8, 5), date(2026, 9, 5)]
        # 05/10 ainda dentro do último extrato (10/10)? 05/10 + 7 dias > 10/10 → ainda «aguarda»
    _post(admin, f"/subscricoes/{sid}/editar", f"/subscricoes/{sid}/editar", nome="Netflix", servico="netflix",
          conta_id="1", valor="13,99", periodicidade="mensal", dia="5", inicio="2026-06-05", fim="2026-07-31",
          padrao="NETFLIX", fim_fidelizacao="")
    with app.state.SessionLocal() as db:
        e = sb.estado(db, db.get(Subscricao, sid), HOJE)
        assert e.situacao == "terminada" and e.total_pago == 2 * 1399 and e.proxima is None
    assert "Terminadas" in admin.get("/subscricoes?entidade=1").text


def test_ignorar_validar_e_apagar(admin, app):
    _post(admin, "/subscricoes?entidade=1", "/subscricoes/ignorar", entidade="1", padrao="LOJA DO CONDOMINIO")
    with app.state.SessionLocal() as db:
        assert "Loja Do Condominio" not in {s.nome for s in sb.detetar(db, 1, HOJE)}
    r = _post(admin, "/subscricoes/nova?entidade=1", nome="", valor="", inicio="", fim="")
    assert r.status_code == 400 and "Dá um nome" in r.text and "Indica o valor" in r.text
    sid = _criar(admin)
    _post(admin, f"/subscricoes/{sid}/editar", f"/subscricoes/{sid}/apagar")
    with app.state.SessionLocal() as db:
        assert db.get(Subscricao, sid) is None
    assert "Subscrições" in admin.get("/").text   # menu


def test_falha_antiga_nao_poe_em_atraso(admin, app):
    # falta o débito de agosto, mas os seguintes existem: está em dia (a falha fica registada)
    _mov(admin, 1, "2026-10-02", "36,21", "LOJA DO CONDOMINIO", sentido="saida")
    sid = _criar(admin, nome="Condomínio", servico="", valor="36,21", dia="2", inicio="2026-07-02",
                 padrao="LOJA DO CONDOMINIO")
    with app.state.SessionLocal() as db:
        from app.models import Movimento
        db.query(Movimento).filter(Movimento.descricao_original == "LOJA DO CONDOMINIO",
                                   Movimento.data_mov == date(2026, 8, 3)).delete()
        db.commit()
        e = sb.estado(db, db.get(Subscricao, sid), HOJE)
    assert e.falhas == [date(2026, 8, 2)] and e.situacao == "em dia"


def test_associar_movimento_na_ficha(admin, app):
    from app.models import Movimento
    from tests.test_app import _csrf
    sid = _criar(admin, nome="Ginásio", servico="", valor="35,00", dia="5", inicio="2026-07-05", padrao="")
    _mov(admin, 1, "2026-09-05", "35,00", "TRF 0042 SEPA DD PT12", sentido="saida")      # texto que não diz nada
    with app.state.SessionLocal() as db:
        mid = db.scalar(select(Movimento.id).where(Movimento.descricao_original == "TRF 0042 SEPA DD PT12"))
    ficha = admin.get(f"/movimentos/{mid}")
    assert "Subscrição / assinatura" in ficha.text and "Ginásio" in ficha.text and "Criar uma subscrição a partir" in ficha.text
    admin.post(f"/subscricoes/movimento/{mid}", data={"csrf": _csrf(ficha), "subscricao_id": str(sid)})
    with app.state.SessionLocal() as db:
        s = db.get(Subscricao, sid)
        assert db.get(Movimento, mid).subscricao_id == sid and s.padrao        # ficou com o texto do extrato
        e = sb.estado(db, s, HOJE)
        assert mid in [m.id for m in e.pagos] and e.total_pago == 3500
    # criar uma subscrição a partir de outro movimento associa-o logo
    _mov(admin, 1, "2026-09-10", "9,99", "APPLE.COM/BILL", sentido="saida")
    with app.state.SessionLocal() as db:
        m2 = db.scalar(select(Movimento.id).where(Movimento.descricao_original == "APPLE.COM/BILL"))
    f = admin.get(f"/subscricoes/nova?entidade=1&nome=iCloud&valor=9,99&dia=10&inicio=2026-09-10&mov={m2}")
    assert "APPLE" in f.text and f"mov={m2}" in f.text
    r = admin.post(f"/subscricoes/nova?entidade=1&mov={m2}", data={"csrf": _csrf(f), "nome": "iCloud", "valor": "9,99",
                   "periodicidade": "mensal", "dia": "10", "inicio": "2026-09-10", "padrao": "APPLE.COM"}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.SessionLocal() as db:
        nova = db.scalar(select(Subscricao).where(Subscricao.nome == "iCloud"))
        assert db.get(Movimento, m2).subscricao_id == nova.id
