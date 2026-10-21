"""Backups da base SQLite e pacote diário para a cloud.

Backup: cópia com a API de backup online do sqlite3 (segura com a app a correr) para GESTOR_BACKUP_DIR, escrita com
nome temporário, verificada (PRAGMA integrity_check) e só então renomeada; retenção em dias.

Pacote para a cloud (um por dia): um único ficheiro .gcpk em GESTOR_PACOTE_DIR (omissão: <backup>/cloud) com TUDO o
que é preciso para restaurar noutra máquina — a base (cópia verificada), secret.key (sem ela os tokens guardados do
Paperless/Immich não se decifram), os extratos originais (uploads/, arquivo/) e um MANIFESTO.json com o sha256 de cada
ficheiro. É um zip cifrado com AES-256-GCM e chave derivada (scrypt) de GESTOR_PACOTE_SENHA: sem senha não se gera,
porque vai sair da NAS. Um job do HBS3 (ou rclone) sincroniza essa pasta com a cloud. Depois de escrito, o pacote é
lido outra vez, decifrado e verificado.

Linha de comandos (restauro sem a app a correr):
  python -m app.backup pacote                         gera o pacote agora
  python -m app.backup verificar <ficheiro.gcpk>      decifra e confere o manifesto
  python -m app.backup restaurar <ficheiro.gcpk> <pasta vazia>
A senha vem de GESTOR_PACOTE_SENHA ou é pedida no terminal.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import Definicoes

log = logging.getLogger("gestor.backup")
_PREFIXO = "gestor-"
_PACOTE = "gestor-pacote-"
_EXT = ".gcpk"
MAGIA = b"GESTORPK1\n"
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
_ESTADO = "estado.json"


class ErroBackup(Exception):
    pass


# ------------------------------------------------------------------ cópia da base
def _copiar_sqlite(origem_path: Path, destino: Path) -> None:
    """Cópia online para um nome temporário, verificada; só depois fica com o nome final."""
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.unlink(missing_ok=True)
    origem = sqlite3.connect(origem_path)
    try:
        alvo = sqlite3.connect(tmp)
        try:
            origem.backup(alvo)
            res = alvo.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            alvo.close()
    finally:
        origem.close()
    if res != "ok":
        tmp.unlink(missing_ok=True)
        raise ErroBackup(f"A cópia da base não passou a verificação de integridade: {res}")
    os.replace(tmp, destino)


def fazer_backup(defs: Definicoes, prefixo: str = _PREFIXO) -> Path:
    defs.backup_dir.mkdir(parents=True, exist_ok=True)
    destino = defs.backup_dir / f"{prefixo}{datetime.now():%Y%m%d-%H%M%S}.db"
    try:
        _copiar_sqlite(defs.db_path, destino)
    except Exception as e:
        _registar(defs, "backup_erro", str(e))
        raise
    limpar_antigos(defs)
    _registar(defs, "backup_ok", destino.name)
    log.info("Backup criado: %s", destino)
    return destino


def backup_antes_de_migrar(db_path: Path, backup_dir: Path, de: str, para: str) -> Path:
    """Antes de uma migração da base (arranque com versão nova): cópia verificada que não expira com a retenção."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    destino = backup_dir / f"antes-migracao-{datetime.now():%Y%m%d-%H%M%S}-{de}-{para}.db"
    _copiar_sqlite(db_path, destino)
    log.info("Backup antes da migração %s → %s: %s", de, para, destino)
    return destino


def limpar_antigos(defs: Definicoes) -> None:
    limite = time.time() - defs.backup_retencao_dias * 86400
    for f in defs.backup_dir.glob(f"{_PREFIXO}*.db"):
        if f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)
    # das cópias antes de migrar ficam sempre as 5 mais recentes
    for f in sorted(defs.backup_dir.glob("antes-migracao-*.db"))[:-5]:
        f.unlink(missing_ok=True)


def _backups(defs: Definicoes) -> list[Path]:
    return sorted(defs.backup_dir.glob(f"{_PREFIXO}2*.db")) if defs.backup_dir.exists() else []


def ultimo_backup(defs: Definicoes) -> datetime | None:
    ficheiros = _backups(defs)
    return datetime.fromtimestamp(ficheiros[-1].stat().st_mtime) if ficheiros else None


# ------------------------------------------------------------------ estado (último sucesso / erro)
def _ler_estado(defs: Definicoes) -> dict:
    try:
        return json.loads((defs.backup_dir / _ESTADO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _registar(defs: Definicoes, chave: str, valor) -> None:
    try:
        defs.backup_dir.mkdir(parents=True, exist_ok=True)
        e = _ler_estado(defs)
        e[chave] = {"quando": datetime.now().isoformat(timespec="seconds"), "valor": valor}
        if chave.endswith("_ok"):
            e.pop(chave[:-3] + "_erro", None)
        (defs.backup_dir / _ESTADO).write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.exception("Não consegui gravar o estado dos backups")


def verificar_base(defs: Definicoes) -> str:
    """PRAGMA quick_check na base viva (só leitura)."""
    con = sqlite3.connect(f"file:{defs.db_path.as_posix()}?mode=ro", uri=True)
    try:
        res = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    _registar(defs, "integridade_ok" if res == "ok" else "integridade_erro", res)
    return res


# ------------------------------------------------------------------ pacote cifrado para a cloud
def pacote_dir(defs: Definicoes) -> Path:
    return Path(os.environ.get("GESTOR_PACOTE_DIR") or defs.backup_dir / "cloud")


def _senha() -> str:
    return os.environ.get("GESTOR_PACOTE_SENHA", "")


def _retencao_pacotes() -> int:
    try:
        return max(1, int(os.environ.get("GESTOR_PACOTE_RETENCAO_DIAS", "14")))
    except ValueError:
        return 14


def _chave(senha: str, sal: bytes) -> bytes:
    return hashlib.scrypt(senha.encode(), salt=sal, dklen=32, maxmem=64 * 1024 * 1024, **_SCRYPT)


def cifrar(dados: bytes, senha: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sal, nonce = os.urandom(16), os.urandom(12)
    return MAGIA + sal + nonce + AESGCM(_chave(senha, sal)).encrypt(nonce, dados, MAGIA)


def decifrar(blob: bytes, senha: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIA):
        raise ErroBackup("Não é um pacote do Gestor de Contas.")
    sal, nonce, corpo = blob[len(MAGIA):len(MAGIA) + 16], blob[len(MAGIA) + 16:len(MAGIA) + 28], blob[len(MAGIA) + 28:]
    try:
        return AESGCM(_chave(senha, sal)).decrypt(nonce, corpo, MAGIA)
    except InvalidTag:
        raise ErroBackup("Senha errada ou pacote danificado.") from None


def _ficheiros_dados(defs: Definicoes) -> list[Path]:
    """Tudo o que está em GESTOR_DATA_DIR menos a base (vai a cópia verificada) e os backups."""
    fora = {defs.db_path.name, defs.db_path.name + "-wal", defs.db_path.name + "-shm", defs.db_path.name + "-journal"}
    out = []
    for f in sorted(defs.data_dir.rglob("*")):
        if not f.is_file() or f.name in fora or f.name.endswith(".tmp"):
            continue
        if defs.backup_dir.resolve() in f.resolve().parents:
            continue
        out.append(f)
    return out


def _contagens(db_file: Path) -> dict:
    con = sqlite3.connect(db_file)
    try:
        out = {}
        for t in ("movimento", "conta", "importacao", "utilizador", "documento_ligado"):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                pass
        try:
            out["revisao"] = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        except sqlite3.Error:
            pass
        return out
    finally:
        con.close()


LEIA_ME = """Pacote de backup do Gestor de Contas
====================================

Conteúdo: gestor.db (base de dados, cópia verificada), secret.key (necessária para os tokens guardados do Paperless e
do Immich e para as sessões), uploads/ e arquivo/ (extratos originais) e MANIFESTO.json (sha256 de cada ficheiro).

Restaurar (com o código do Gestor, ex.: git clone do repositório ou dentro do container):
  GESTOR_PACOTE_SENHA=... python -m app.backup verificar  gestor-pacote-AAAAMMDD-HHMMSS.gcpk
  GESTOR_PACOTE_SENHA=... python -m app.backup restaurar  gestor-pacote-AAAAMMDD-HHMMSS.gcpk  /pasta/vazia
Depois parar o container, trocar o conteúdo de /share/Container/gestor-contas/data pelo da pasta restaurada e arrancar.
"""


@dataclass
class Pacote:
    caminho: Path
    tamanho: int
    ficheiros: int
    contagens: dict


def fazer_pacote(defs: Definicoes, versao: str = "", senha: str | None = None) -> Pacote:
    senha = _senha() if senha is None else senha
    if len(senha) < 12:
        raise ErroBackup("Falta a senha do pacote (GESTOR_PACOTE_SENHA, com pelo menos 12 caracteres): o pacote vai "
                         "para fora da NAS e tem de ir cifrado.")
    destino_dir = pacote_dir(defs)
    destino_dir.mkdir(parents=True, exist_ok=True)
    agora = datetime.now()
    destino = destino_dir / f"{_PACOTE}{agora:%Y%m%d-%H%M%S}{_EXT}"
    try:
        tmp_db = destino_dir / f".snapshot-{agora:%H%M%S}.db"
        _copiar_sqlite(defs.db_path, tmp_db)
        try:
            manifesto = {"app": "gestor-contas", "versao": versao, "criado": agora.isoformat(timespec="seconds"),
                         "contagens": _contagens(tmp_db), "ficheiros": {}}
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                itens = [(tmp_db, "gestor.db")] + [(f, f.relative_to(defs.data_dir).as_posix()) for f in _ficheiros_dados(defs)]
                for f, nome in itens:
                    dados = f.read_bytes()
                    manifesto["ficheiros"][nome] = {"sha256": hashlib.sha256(dados).hexdigest(), "bytes": len(dados)}
                    z.writestr(nome, dados)
                z.writestr("MANIFESTO.json", json.dumps(manifesto, ensure_ascii=False, indent=1))
                z.writestr("LEIA-ME.txt", LEIA_ME)
        finally:
            tmp_db.unlink(missing_ok=True)
        blob = cifrar(buf.getvalue(), senha)
        tmp = destino.with_name(destino.name + ".tmp")
        tmp.write_bytes(blob)
        verificar_pacote(tmp, senha)                     # lê outra vez do disco, decifra e confere
        os.replace(tmp, destino)
    except Exception as e:
        _registar(defs, "pacote_erro", str(e))
        for f in destino_dir.glob("*.tmp"):
            f.unlink(missing_ok=True)
        raise
    limpar_pacotes(defs)
    p = Pacote(destino, destino.stat().st_size, len(manifesto["ficheiros"]), manifesto["contagens"])
    _registar(defs, "pacote_ok", destino.name)
    log.info("Pacote para a cloud: %s (%d ficheiros, %d bytes)", destino.name, p.ficheiros, p.tamanho)
    return p


def verificar_pacote(caminho: Path, senha: str) -> dict:
    """Decifra, testa o zip e confere o sha256 de cada ficheiro com o manifesto. Devolve o manifesto."""
    dados = decifrar(Path(caminho).read_bytes(), senha)
    with zipfile.ZipFile(io.BytesIO(dados)) as z:
        if z.testzip() is not None:
            raise ErroBackup("O zip dentro do pacote está danificado.")
        manifesto = json.loads(z.read("MANIFESTO.json"))
        for nome, info in manifesto["ficheiros"].items():
            if hashlib.sha256(z.read(nome)).hexdigest() != info["sha256"]:
                raise ErroBackup(f"{nome} não confere com o manifesto.")
        if "gestor.db" not in manifesto["ficheiros"]:
            raise ErroBackup("O pacote não tem a base de dados.")
    return manifesto


def restaurar_pacote(caminho: Path, senha: str, pasta: Path) -> dict:
    pasta = Path(pasta)
    if pasta.exists() and any(pasta.iterdir()):
        raise ErroBackup(f"{pasta} não está vazia.")
    manifesto = verificar_pacote(caminho, senha)
    pasta.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(decifrar(Path(caminho).read_bytes(), senha))) as z:
        for nome in manifesto["ficheiros"]:
            alvo = (pasta / nome).resolve()
            if pasta.resolve() not in alvo.parents:
                raise ErroBackup(f"Caminho inválido no pacote: {nome}")
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_bytes(z.read(nome))
    con = sqlite3.connect(pasta / "gestor.db")
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ErroBackup("A base restaurada não passou a verificação de integridade.")
    finally:
        con.close()
    return manifesto


def _pacotes(defs: Definicoes) -> list[Path]:
    d = pacote_dir(defs)
    return sorted(d.glob(f"{_PACOTE}*{_EXT}")) if d.exists() else []


def limpar_pacotes(defs: Definicoes) -> None:
    limite = time.time() - _retencao_pacotes() * 86400
    pacotes = _pacotes(defs)
    for f in pacotes[:-1]:                               # o mais recente fica sempre
        if f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)


def ultimo_pacote(defs: Definicoes) -> Path | None:
    p = _pacotes(defs)
    return p[-1] if p else None


# ------------------------------------------------------------------ estado para Administração › Backups
def _tamanho(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def estado(defs: Definicoes) -> dict:
    """O que a página mostra: onde estão os dados (no container e na NAS), tamanhos, últimos backups e pacotes,
    erros e avisos."""
    e = _ler_estado(defs)
    backups = _backups(defs)
    pacotes = _pacotes(defs)
    ultimo = datetime.fromtimestamp(backups[-1].stat().st_mtime) if backups else None
    up = ultimo_pacote(defs)
    avisos = []
    if defs.backup_intervalo_horas > 0 and (ultimo is None or (datetime.now() - ultimo).total_seconds()
                                            > 2 * defs.backup_intervalo_horas * 3600 + 3600):
        avisos.append("O último backup é mais antigo do que devia.")
    try:
        if defs.backup_dir.resolve() == defs.data_dir.resolve() or defs.data_dir.resolve() in defs.backup_dir.resolve().parents:
            avisos.append("Os backups estão dentro da pasta dos dados: se essa pasta se perder, perdem-se os dois.")
    except OSError:
        pass
    if not _senha():
        avisos.append("Sem GESTOR_PACOTE_SENHA não se gera o pacote diário para a cloud.")
    elif up is None or date.fromtimestamp(up.stat().st_mtime) < date.today():
        avisos.append("Ainda não há pacote de hoje para a cloud.")
    for k, txt in (("backup_erro", "Falhou o último backup"), ("pacote_erro", "Falhou o último pacote"),
                   ("integridade_erro", "A verificação de integridade da base falhou")):
        if k in e:
            avisos.append(f"{txt} ({e[k]['quando'].replace('T', ' ')}): {e[k]['valor']}")
    try:
        disco = shutil.disk_usage(defs.backup_dir if defs.backup_dir.exists() else defs.data_dir)
        livre = disco.free
        if disco.free < 1024 ** 3:
            avisos.append("Menos de 1 GB livre no disco dos backups.")
    except OSError:
        livre = None
    db = defs.db_path
    return {
        "db": db, "db_nas": _na_nas(db, defs.data_dir, "GESTOR_DATA_HOST"),
        "db_bytes": _tamanho(db), "wal_bytes": _tamanho(db.with_name(db.name + "-wal")),
        "dados_dir": defs.data_dir, "dados_nas": os.environ.get("GESTOR_DATA_HOST") or None,
        "backup_dir": defs.backup_dir, "backup_nas": os.environ.get("GESTOR_BACKUP_HOST") or None,
        "backups": len(backups), "backups_bytes": sum(_tamanho(f) for f in backups), "ultimo_backup": ultimo,
        "intervalo_horas": defs.backup_intervalo_horas, "retencao_dias": defs.backup_retencao_dias,
        "pacote_dir": pacote_dir(defs), "pacote_nas": _na_nas(pacote_dir(defs), defs.backup_dir, "GESTOR_BACKUP_HOST"),
        "pacote_senha": bool(_senha()), "pacotes": len(pacotes), "pacotes_bytes": sum(_tamanho(f) for f in pacotes),
        "ultimo_pacote": up, "ultimo_pacote_quando": datetime.fromtimestamp(up.stat().st_mtime) if up else None,
        "ultimo_pacote_bytes": _tamanho(up) if up else 0, "retencao_pacotes": _retencao_pacotes(),
        "integridade": e.get("integridade_ok") or e.get("integridade_erro"), "integridade_ok": "integridade_ok" in e,
        "livre_bytes": livre, "avisos": avisos,
    }


def _na_nas(caminho: Path, base: Path, var: str) -> str | None:
    """O mesmo caminho visto na NAS (GESTOR_DATA_HOST / GESTOR_BACKUP_HOST dizem para onde está montada a pasta)."""
    host = os.environ.get(var)
    if not host:
        return None
    try:
        return host.rstrip("/") + "/" + Path(caminho).relative_to(base).as_posix()
    except ValueError:
        return None


# ------------------------------------------------------------------ ciclo de fundo
def _tarefa_diaria(defs: Definicoes, versao: str) -> None:
    ultimo = ultimo_backup(defs)
    if ultimo is None or (datetime.now() - ultimo).total_seconds() >= defs.backup_intervalo_horas * 3600:
        verificar_base(defs)
        fazer_backup(defs)
    up = ultimo_pacote(defs)
    if _senha() and (up is None or date.fromtimestamp(up.stat().st_mtime) < date.today()):
        fazer_pacote(defs, versao)


async def ciclo_backups(defs: Definicoes, versao: str = "") -> None:
    """Verifica de hora a hora: backup quando o último tem mais do que o intervalo; pacote para a cloud uma vez por dia."""
    if defs.backup_intervalo_horas <= 0:
        return
    while True:
        try:
            await asyncio.to_thread(_tarefa_diaria, defs, versao)
        except Exception:
            log.exception("Falha no backup automático")
        await asyncio.sleep(3600)


# ------------------------------------------------------------------ linha de comandos
def main(argv=None) -> int:
    import argparse
    import getpass
    from .config import carregar_definicoes
    p = argparse.ArgumentParser(prog="python -m app.backup", description="Backups e pacote cifrado do Gestor de Contas.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pacote", help="gera o pacote cifrado agora")
    v = sub.add_parser("verificar", help="decifra e confere um pacote")
    v.add_argument("ficheiro")
    r = sub.add_parser("restaurar", help="restaura um pacote para uma pasta vazia")
    r.add_argument("ficheiro")
    r.add_argument("pasta")
    a = p.parse_args(argv)
    senha = _senha() or getpass.getpass("Senha do pacote: ")
    try:
        if a.cmd == "pacote":
            from .main import VERSAO
            pk = fazer_pacote(carregar_definicoes(), VERSAO, senha)
            print(f"{pk.caminho} ({pk.ficheiros} ficheiros, {pk.tamanho} bytes)")
        elif a.cmd == "verificar":
            m = verificar_pacote(Path(a.ficheiro), senha)
            print(f"OK — criado {m['criado']}, versão {m.get('versao') or '?'}, {len(m['ficheiros'])} ficheiros, {m['contagens']}")
        else:
            m = restaurar_pacote(Path(a.ficheiro), senha, Path(a.pasta))
            print(f"Restaurado em {a.pasta}: {len(m['ficheiros'])} ficheiros, {m['contagens']}")
    except ErroBackup as e:
        print(f"Erro: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
