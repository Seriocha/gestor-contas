"""Resiliência: backups verificados, cópia antes de migrar, pacote diário cifrado para a cloud (gerar, verificar,
restaurar, senha errada, adulterado), página Administração › Backups com a localização dos dados e /healthz."""
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backup as bk
from app.config import carregar_definicoes
from app.main import aplicar_migracoes, create_app
from tests.test_app import PW, _csrf, _post

SENHA = "uma-senha-bem-longa-123"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("GESTOR_PACOTE_SENHA", raising=False)
    monkeypatch.delenv("GESTOR_PACOTE_DIR", raising=False)
    monkeypatch.setenv("GESTOR_BACKUP_DIR", str(tmp_path / "bk"))
    a = create_app(tmp_path / "dados", backups_automaticos=False)
    up = tmp_path / "dados" / "uploads" / "ab"
    up.mkdir(parents=True)
    (up / "extrato.pdf").write_bytes(b"%PDF-1.4 extrato original")
    return a


@pytest.fixture
def admin(app):
    c = TestClient(app)
    _post(c, "/setup", nome="Admin", login="admin", password=PW, password2=PW)
    return c


def test_backup_verificado_e_atomico(app):
    d = bk.fazer_backup(app.state.defs)
    assert d.exists() and not list(d.parent.glob("*.tmp"))
    assert sqlite3.connect(d).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert bk._ler_estado(app.state.defs)["backup_ok"]["valor"] == d.name


def test_pacote_sem_senha_nao_se_gera(app):
    with pytest.raises(bk.ErroBackup, match="cifrado"):
        bk.fazer_pacote(app.state.defs)
    assert not bk._pacotes(app.state.defs)


def test_pacote_gera_verifica_e_restaura(app, tmp_path, monkeypatch):
    monkeypatch.setenv("GESTOR_PACOTE_SENHA", SENHA)
    defs = app.state.defs
    p = bk.fazer_pacote(defs, "9.9.9")
    assert p.caminho.parent == defs.backup_dir / "cloud" and p.caminho.suffix == ".gcpk"
    blob = p.caminho.read_bytes()
    assert blob.startswith(bk.MAGIA) and b"SQLite format" not in blob and b"extrato original" not in blob   # cifrado
    m = bk.verificar_pacote(p.caminho, SENHA)
    assert m["versao"] == "9.9.9" and {"gestor.db", "secret.key", "uploads/ab/extrato.pdf"} <= set(m["ficheiros"])
    assert m["contagens"]["utilizador"] == 0 and m["contagens"]["revisao"]
    with pytest.raises(bk.ErroBackup, match="Senha errada"):
        bk.verificar_pacote(p.caminho, "outra-senha-qualquer")
    adulterado = tmp_path / "x.gcpk"
    adulterado.write_bytes(blob[:-5] + bytes(b ^ 1 for b in blob[-5:]))
    with pytest.raises(bk.ErroBackup):
        bk.verificar_pacote(adulterado, SENHA)
    destino = tmp_path / "restauro"
    bk.restaurar_pacote(p.caminho, SENHA, destino)
    assert (destino / "uploads/ab/extrato.pdf").read_bytes() == b"%PDF-1.4 extrato original"
    assert (destino / "secret.key").read_text() == (defs.data_dir / "secret.key").read_text()
    # a base restaurada abre-se na app
    TestClient(create_app(destino, backups_automaticos=False)).get("/healthz").raise_for_status()
    with pytest.raises(bk.ErroBackup, match="não está vazia"):
        bk.restaurar_pacote(p.caminho, SENHA, destino)


def test_retencao_dos_pacotes_mantem_o_mais_recente(app, monkeypatch):
    import os
    monkeypatch.setenv("GESTOR_PACOTE_SENHA", SENHA)
    monkeypatch.setenv("GESTOR_PACOTE_RETENCAO_DIAS", "1")
    d = bk.pacote_dir(app.state.defs)
    d.mkdir(parents=True)
    velhos = [d / "gestor-pacote-20200101-000000.gcpk", d / "gestor-pacote-20200102-000000.gcpk"]
    for f in velhos:
        f.write_bytes(b"x")
        os.utime(f, (0, 0))
    bk.limpar_pacotes(app.state.defs)
    assert [f.name for f in bk._pacotes(app.state.defs)] == [velhos[-1].name]
    bk.fazer_pacote(app.state.defs)
    assert len(bk._pacotes(app.state.defs)) == 1


def test_tarefa_diaria_faz_backup_e_pacote_uma_vez(app, monkeypatch):
    monkeypatch.setenv("GESTOR_PACOTE_SENHA", SENHA)
    bk._tarefa_diaria(app.state.defs, "1.0")
    bk._tarefa_diaria(app.state.defs, "1.0")
    assert len(bk._backups(app.state.defs)) == 1 and len(bk._pacotes(app.state.defs)) == 1
    assert bk._ler_estado(app.state.defs)["integridade_ok"]["valor"] == "ok"


def test_copia_antes_de_migrar(tmp_path):
    defs = carregar_definicoes(tmp_path)
    aplicar_migracoes(defs.db_path, defs.backup_dir)
    assert not list(defs.backup_dir.glob("antes-migracao-*")) if defs.backup_dir.exists() else True   # base nova: nada
    con = sqlite3.connect(defs.db_path)
    atual = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    con.close()
    from alembic import command
    from alembic.config import Config
    from app.config import BASE_DIR
    from app.db import url_sqlite
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url_sqlite(defs.db_path))
    command.downgrade(cfg, "-1")
    aplicar_migracoes(defs.db_path, defs.backup_dir)
    copias = list(defs.backup_dir.glob("antes-migracao-*.db"))
    assert len(copias) == 1 and copias[0].name.endswith(f"-{atual}.db")


def test_pagina_mostra_localizacao_e_avisos(admin, app, monkeypatch):
    monkeypatch.setenv("GESTOR_DATA_HOST", "/share/Container/gestor-contas/data")
    monkeypatch.setenv("GESTOR_BACKUP_HOST", "/share/Container/gestor-contas/backup")
    r = admin.get("/admin/backups")
    assert "Onde estão os dados" in r.text and "/share/Container/gestor-contas/data/gestor.db" in r.text
    assert "/share/Container/gestor-contas/backup/cloud" in r.text
    assert "Sem GESTOR_PACOTE_SENHA" in r.text and "Gerar pacote agora" not in r.text
    monkeypatch.setenv("GESTOR_PACOTE_SENHA", SENHA)
    r = admin.get("/admin/backups")
    assert "Ainda não há pacote de hoje" in r.text
    r = admin.post("/admin/pacote", data={"csrf": _csrf(r)}, follow_redirects=True)
    assert "Pacote criado e verificado" in r.text and "Ainda não há pacote de hoje" not in r.text
    r = admin.post("/admin/verificar-base", data={"csrf": _csrf(r)}, follow_redirects=True)
    assert "sem problemas" in r.text


def test_backups_dentro_dos_dados_da_aviso(tmp_path, monkeypatch):
    monkeypatch.delenv("GESTOR_BACKUP_DIR", raising=False)
    a = create_app(tmp_path, backups_automaticos=False)
    assert any("dentro da pasta dos dados" in x for x in bk.estado(a.state.defs)["avisos"])
    # o pacote não se inclui a si próprio nem aos backups
    monkeypatch.setenv("GESTOR_PACOTE_SENHA", SENHA)
    bk.fazer_backup(a.state.defs)
    p = bk.fazer_pacote(a.state.defs)
    nomes = set(bk.verificar_pacote(p.caminho, SENHA)["ficheiros"])
    assert not any(n.startswith("backup/") for n in nomes)


def test_healthz_falha_sem_base(app):
    c = TestClient(app)
    assert c.get("/healthz").text == "ok"
    app.state.SessionLocal.kw["bind"].dispose()
    Path(app.state.defs.db_path).unlink()
    (app.state.defs.db_path).mkdir()                       # já não é uma base sqlite
    assert c.get("/healthz").status_code == 503
