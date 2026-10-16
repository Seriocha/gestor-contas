"""Arquivo organizado dos extratos importados e pasta de entrada.

Cada importação confirmada guarda uma cópia do ficheiro original com um nome
que diz o que é, numa árvore por entidade, banco, conta e ano:

    <arquivo>/Família/Santander/Santander ordem/2026/2026-09-01_a_2026-09-30_Santander ordem.xls

<arquivo> é GESTOR_ARQUIVO_DIR — na NAS, uma pasta partilhada montada no
contentor, para os extratos ficarem visíveis (e no backup) como ficheiros
normais. A subpasta _Entrada é uma caixa de entrada: o que lá se gravar (do
PC, por SMB) aparece em Importar, pronto a pré-visualizar sem upload; depois
de importado sai da _Entrada para o arquivo.
"""
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ENTRADA = "_Entrada"
EXTENSOES = {".xls", ".xlsx", ".csv", ".txt"}
_PROIBIDOS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def nome_seguro(texto: str, maximo: int = 80) -> str:
    """Nome válido em Windows/SMB e Linux (sem / \\ : * ? " < > |, sem pontos no fim)."""
    t = _PROIBIDOS.sub("-", str(texto or "")).strip().strip(".")
    return re.sub(r"\s+", " ", t)[:maximo].strip() or "sem nome"


def _permissoes(p: Path, pasta: bool) -> None:
    # O contentor corre como root: sem isto, os ficheiros na pasta partilhada ficavam só de leitura por SMB.
    try:
        p.chmod(0o777 if pasta else 0o666)
    except OSError:
        pass


def _mkdir(p: Path) -> Path:
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        _permissoes(p, True)
    return p


def pasta_entrada(raiz: Path) -> Path:
    return _mkdir(raiz / ENTRADA)


def caminho_relativo(entidade: str, banco: str, conta: str, data_ini, data_fim, extensao: str) -> Path:
    ano = (data_fim or data_ini or datetime.now().date()).year
    periodo = (f"{data_ini:%Y-%m-%d}_a_{data_fim:%Y-%m-%d}" if data_ini and data_fim
               else f"{datetime.now():%Y-%m-%d}")
    ext = extensao.lower() if extensao.lower() in EXTENSOES else ""
    return (Path(nome_seguro(entidade)) / nome_seguro(banco) / nome_seguro(conta) / str(ano)
            / f"{periodo}_{nome_seguro(conta)}{ext}")


def arquivar(raiz: Path, relativo: Path, conteudo: bytes) -> str:
    """Grava a cópia; se já houver um ficheiro diferente com o mesmo nome, acrescenta _2, _3…
    Devolve o caminho relativo efetivo (com /)."""
    destino = raiz / relativo
    _mkdir(destino.parent)
    n = 1
    while destino.exists():
        if destino.read_bytes() == conteudo:
            return destino.relative_to(raiz).as_posix()
        n += 1
        destino = destino.with_name(f"{relativo.stem}_{n}{relativo.suffix}")
    destino.write_bytes(conteudo)
    _permissoes(destino, False)
    return destino.relative_to(raiz).as_posix()


def resolver(raiz: Path, relativo: str) -> Path | None:
    """Caminho absoluto de um ficheiro do arquivo, só se estiver mesmo dentro dele."""
    if not relativo:
        return None
    p = (raiz / relativo).resolve()
    try:
        p.relative_to(raiz.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


@dataclass
class FicheiroEntrada:
    nome: str
    tamanho: int
    modificado: datetime


def listar_entrada(raiz: Path, maximo: int = 50) -> list[FicheiroEntrada]:
    pasta = pasta_entrada(raiz)
    out = []
    for f in pasta.iterdir():
        if f.is_file() and f.suffix.lower() in EXTENSOES and not f.name.startswith((".", "~$")):
            st = f.stat()
            out.append(FicheiroEntrada(f.name, st.st_size, datetime.fromtimestamp(st.st_mtime)))
    return sorted(out, key=lambda x: x.modificado, reverse=True)[:maximo]


def ficheiro_entrada(raiz: Path, nome: str) -> Path:
    """Só nomes simples de ficheiros que existem na _Entrada (nada de ../)."""
    if not nome or nome != Path(nome).name or Path(nome).suffix.lower() not in EXTENSOES:
        raise FileNotFoundError(nome)
    p = pasta_entrada(raiz) / nome
    if not p.is_file():
        raise FileNotFoundError(nome)
    return p


def retirar_da_entrada(raiz: Path, nome: str) -> bool:
    try:
        ficheiro_entrada(raiz, nome).unlink()
        return True
    except OSError:
        return False


@dataclass
class Estado:
    raiz: Path
    partilhado: bool
    existe: bool
    escrita: bool
    livre: int | None
    n_ficheiros: int
    n_entrada: int
    erro: str | None = None


def estado(raiz: Path, partilhado: bool) -> Estado:
    e = Estado(raiz, partilhado, raiz.is_dir(), False, None, 0, 0)
    try:
        _mkdir(raiz)
        e.existe = True
        teste = raiz / ".teste-escrita"
        teste.write_bytes(b"ok")
        teste.unlink()
        e.escrita = True
        e.livre = shutil.disk_usage(raiz).free
        e.n_entrada = len(listar_entrada(raiz, maximo=10_000))
        e.n_ficheiros = sum(1 for _, _, fs in os.walk(raiz) for f in fs if Path(f).suffix.lower() in EXTENSOES) - e.n_entrada
    except OSError as ex:
        e.erro = str(ex)
    return e
