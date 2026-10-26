"""Desempenho: medição por rota, diagnóstico da base (índices em falta, redundantes, ações), cache do histórico dos
saldos (invalidada pelas escritas) e os índices da migração 0046."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import desempenho
from app import relatorios as rel
from app.db import versao_dados
from app.main import create_app
from app.models import Conta
from tests.test_app import PW, _csrf, _post
from tests.test_previsoes import _mov


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, backups_automaticos=False)


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    _post(c, "/contas/nova?entidade=1", nome="Activo", tipo="ORDEM", moeda="EUR", instituicao_id="2")
    return c


def test_pagina_mede_as_rotas(admin):
    admin.get("/inicio")
    admin.get("/inicio")
    r = admin.get("/admin/desempenho")
    assert r.status_code == 200
    assert "GET /inicio" in r.text and "Sugestões para a base de dados" in r.text and "movimento" in r.text
    rota = admin.app.state.metricas.rotas["GET /inicio"]
    assert rota.n == 2 and rota.queries > 0 and rota.max_ms > 0
    r = admin.post("/admin/desempenho", data={"csrf": _csrf(r), "acao": "recomecar"}, follow_redirects=True)
    assert "Medição recomeçada" in r.text and "GET /inicio" not in admin.app.state.metricas.rotas


def test_so_administradores(admin):
    _post(admin, "/admin/utilizadores/novo", nome="Ana", login="ana", password=PW, password2=PW)
    c = TestClient(admin.app)
    _post(c, "/login", login="ana", password=PW)
    assert c.get("/admin/desempenho").status_code == 403


def test_indices_da_migracao(app):
    con = sqlite3.connect(app.state.defs.db_path)
    nomes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_movimento_montante_data", "ix_movimento_par_id", "ix_movimento_importacao_id"} <= nomes
    # as sugestões de pares (débito −x ↔ crédito +x) procuram pelo valor: usam o índice
    plano = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM movimento d JOIN movimento c ON c.montante = -d.montante "
        "AND c.conta_id != d.conta_id WHERE d.montante < 0"))
    assert "ix_movimento_montante_data" in plano
    con.close()
    d = desempenho.diagnosticar(app.state.defs.db_path)
    assert d.em_falta == [] and d.estatisticas


def test_diagnostico_e_acoes(tmp_path):
    from sqlalchemy import create_engine
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE pai (id INTEGER PRIMARY KEY);
        CREATE TABLE filho (id INTEGER PRIMARY KEY, pai_id INTEGER REFERENCES pai(id), nome TEXT, extra TEXT);
        CREATE INDEX ix_filho_nome ON filho (nome);
        CREATE INDEX ix_filho_nome_extra ON filho (nome, extra);
        CREATE TABLE lixo (id INTEGER PRIMARY KEY, t TEXT);
    """)
    con.executemany("INSERT INTO pai (id) VALUES (?)", [(i,) for i in range(10)])
    con.executemany("INSERT INTO filho (pai_id, nome) VALUES (?, ?)", [(i % 10, f"n{i}") for i in range(1500)])
    con.executemany("INSERT INTO lixo (t) VALUES (?)", [("x" * 2000,) for _ in range(3000)])
    con.commit()
    con.execute("DELETE FROM lixo")
    con.commit()
    con.close()
    d = desempenho.diagnosticar(db)
    assert d.em_falta == [("filho", "pai_id", "pai", 1500)]
    assert d.redundantes == [("filho", "ix_filho_nome", "ix_filho_nome_extra")]
    acoes = {s.acao for s in d.sugestoes}
    assert {"indices", "analyze", "vacuum"} <= acoes and not d.estatisticas and d.fragmentacao > 0.5
    eng = create_engine(f"sqlite:///{db.as_posix()}")
    assert "1 índice(s) criado(s)" in desempenho.executar("indices", db, eng)
    assert "Base compactada" in desempenho.executar("vacuum", db, eng)
    assert "Checkpoint" in desempenho.executar("checkpoint", db, eng)
    d = desempenho.diagnosticar(db)
    assert d.em_falta == [] and d.estatisticas and d.fragmentacao < 0.05
    assert "indices" not in {s.acao for s in d.sugestoes}
    eng.dispose()


def test_acao_pela_pagina(admin):
    r = admin.get("/admin/desempenho")
    r = admin.post("/admin/desempenho", data={"csrf": _csrf(r), "acao": "analyze"}, follow_redirects=True)
    assert "Estatísticas atualizadas" in r.text
    assert admin.post("/admin/desempenho", data={"csrf": _csrf(r), "acao": "DROP"}).status_code == 400


def test_cache_do_historico_invalida_com_escritas(admin, app):
    _mov(admin, 1, "2026-03-03", "100,00", "SALARIO")
    with app.state.SessionLocal() as db:
        conta = db.get(Conta, 1)
        assert rel.HistoricoConta(db, conta).saldo_em(rel.date(2026, 12, 31)) == 10000
    url = app.state.SessionLocal.kw["bind"].url
    v = versao_dados(url)
    _mov(admin, 1, "2026-03-04", "30,00", "COMPRA", sentido="saida")
    assert versao_dados(url) > v                     # a escrita no movimento subiu a versão
    with app.state.SessionLocal() as db:
        conta = db.get(Conta, 1)
        assert rel.HistoricoConta(db, conta).saldo_em(rel.date(2026, 12, 31)) == 7000
        conta.saldo_inicial = 500                    # mudar o saldo inicial também conta
        db.commit()
        assert rel.HistoricoConta(db, conta).saldo_em(rel.date(2026, 12, 31)) == 7500
