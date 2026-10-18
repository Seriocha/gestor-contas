"""Extratos colocados numa pasta partilhada da NAS, lidos periodicamente.

Estrutura: <pasta partilhada>/<banco>/<ficheiro> — uma subpasta por banco (ex.: Santander, ActivoBank, Revolut).
A leitura (de hora a hora, por omissão, ou com «Ler agora» em Administração › Tarefas) não importa nada: lê cada
ficheiro novo, deteta a conta e deixa-o à espera de validação. Validado (com a pré-visualização habitual), é importado
e o ficheiro passa para <banco>/importados/AAAA-MM-DD_<ficheiro>. Rejeitado, fica onde está e não volta a ser proposto.
"""
import asyncio
import hashlib
import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import servicos
from .models import Conta, ImportacaoPendente, Instituicao
from .parsers import Extrato, FormatoDesconhecido, ler_extrato

log = logging.getLogger(__name__)

PASTA_IMPORTADOS = "importados"
IGNORAR = {PASTA_IMPORTADOS, "rejeitados"}


def _de_sistema(nome: str) -> bool:
    """Pastas do QTS/SMB que não são bancos: @Recycle, @Recently-Snapshot, @eaDir, #recycle, .streams…"""
    return nome.startswith(("@", "#", ".")) or nome.lower() in IGNORAR
INTERVALOS = {15: "15 minutos", 30: "30 minutos", 60: "1 hora", 180: "3 horas", 720: "12 horas", 1440: "1 dia"}


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


@dataclass
class Leitura:
    novos: list = field(default_factory=list)       # ImportacaoPendente criados
    erros: list = field(default_factory=list)       # ImportacaoPendente com erro
    ignorados: int = 0                              # já conhecidos (pelo conteúdo)
    pasta_ok: bool = True
    mensagem: str = ""


def estado_pasta(raiz: Path | None) -> tuple[bool, str]:
    if raiz is None:
        return False, "Pasta partilhada não configurada (GESTOR_IMPORT_DIR)."
    if not raiz.is_dir():
        return False, f"A pasta {raiz} não existe ou não está montada no container."
    return True, str(raiz)


def ficheiros(raiz: Path) -> list[tuple[str, Path]]:
    """(subpasta do banco, ficheiro) — só o 1.º nível de cada subpasta; ignora importados/ e ficheiros escondidos."""
    out = []
    for pasta in sorted(p for p in raiz.iterdir() if p.is_dir() and not _de_sistema(p.name)):
        for f in sorted(pasta.iterdir()):
            if f.is_file() and not f.name.startswith((".", "~$")) and f.suffix.lower() not in (".tmp", ".part"):
                out.append((pasta.name, f))
    return out


def _candidatas(db: Session, extrato: Extrato, pasta: str) -> list[Conta]:
    contas = db.scalars(select(Conta).where(Conta.ativa).join(Instituicao)).all()
    chave_parser = (extrato.parser or "").split("_")[0]
    p = _norm(pasta)
    por_banco = [c for c in contas if (c.instituicao.parser and c.instituicao.parser == chave_parser)
                 or (p and (p in _norm(c.instituicao.nome) or _norm(c.instituicao.nome) in p))]
    if extrato.cartao:
        tipo = lambda c: c.tipo == "CARTAO_CREDITO"
    elif extrato.emprestimo:
        tipo = lambda c: c.tipo == "EMPRESTIMO"
    elif extrato.refeicao:
        tipo = lambda c: c.tipo == "CARTAO_REFEICAO"
    else:
        tipo = lambda c: c.tipo not in ("CARTAO_CREDITO", "EMPRESTIMO", "CARTAO_REFEICAO")
    return [c for c in por_banco if tipo(c) and c.moeda == extrato.moeda]


def detetar_conta(db: Session, extrato: Extrato, pasta: str) -> tuple[Conta | None, str]:
    """A conta do extrato: pelo banco (formato/subpasta), tipo e moeda; depois pelo n.º de conta no ficheiro;
    por fim pelos movimentos que já existem na conta (o mesmo extrato sobreposto). Sem certeza, None."""
    cands = _candidatas(db, extrato, pasta)
    if not cands:
        return None, f"nenhuma conta do {extrato.banco} deste tipo e moeda"
    if extrato.conta_ref:
        ref = re.sub(r"\D", "", extrato.conta_ref)
        if len(ref) >= 6:
            pelo_n = [c for c in cands if c.iban and ref[-8:] in re.sub(r"\D", "", c.iban)]
            if len(pelo_n) == 1:
                return pelo_n[0], f"n.º de conta {extrato.conta_ref}"
    if len(cands) == 1:
        return cands[0], f"única conta {extrato.banco} deste tipo"
    sobrepostos = []
    for c in cands:
        erro, _ = servicos.escolher_subconta(extrato, c)
        if erro:
            continue
        n = sum(1 for l in servicos.preparar(db, c, extrato) if l.duplicado)
        sobrepostos.append((n, c))
    sobrepostos.sort(key=lambda x: -x[0])
    if sobrepostos and sobrepostos[0][0] > 0 and (len(sobrepostos) == 1 or sobrepostos[1][0] < sobrepostos[0][0]):
        n, c = sobrepostos[0]
        return c, f"{n} movimento{'s' if n != 1 else ''} já existente{'s' if n != 1 else ''} nesta conta"
    return None, f"{len(cands)} contas possíveis no {extrato.banco} — escolhe a conta"


def resumo_para(db: Session, p: ImportacaoPendente, extrato: Extrato) -> None:
    """Período, n.º de linhas e de movimentos novos para a conta escolhida."""
    p.n_novos = None
    conta = db.get(Conta, p.conta_id) if p.conta_id else None
    erro = servicos.escolher_subconta(extrato, conta)[0] if conta else None   # Revolut: escolhe a subconta da conta
    movs = extrato.movimentos
    p.n_lidos = len(movs)
    p.data_ini = movs[0].data_mov if movs else None
    p.data_fim = movs[-1].data_mov if movs else None
    if conta and not erro:
        p.n_novos = sum(1 for l in servicos.preparar(db, conta, extrato) if not l.duplicado)


def ler(db: Session, raiz: Path | None, data_dir: Path) -> Leitura:
    """Uma passagem pela pasta: regista os ficheiros novos (pelo conteúdo) como pendentes de validação."""
    r = Leitura()
    ok, msg = estado_pasta(raiz)
    if not ok:
        r.pasta_ok, r.mensagem = False, msg
        return r
    conhecidos = set(db.scalars(select(ImportacaoPendente.sha256)))
    for pasta, f in ficheiros(raiz):
        try:
            if f.stat().st_size > servicos.TAMANHO_MAX:
                conteudo = None
            else:
                conteudo = f.read_bytes()
        except OSError as e:
            log.warning("Não foi possível ler %s: %s", f, e)
            continue
        sha = hashlib.sha256(conteudo).hexdigest() if conteudo is not None else f"grande:{f.name}:{f.stat().st_size}"[:64]
        if sha in conhecidos:
            r.ignorados += 1
            continue
        conhecidos.add(sha)
        p = ImportacaoPendente(pasta=pasta[:120], ficheiro=f.name[:255], caminho=f.relative_to(raiz).as_posix()[:500], sha256=sha,
                               n_lidos=0, estado="pendente")
        if conteudo is None:
            p.estado, p.erro = "erro", "Ficheiro demasiado grande (máx. 10 MB)."
        else:
            try:
                extrato = ler_extrato(f.name, conteudo)
            except FormatoDesconhecido as e:
                p.estado, p.erro = "erro", str(e)[:500]
            except Exception as e:  # ficheiro corrompido / colunas inesperadas
                p.estado, p.erro = "erro", f"Não foi possível ler o ficheiro: {e}"[:500]
            else:
                servicos.guardar_upload(data_dir, f.name, conteudo)
                p.parser, p.banco = extrato.parser, extrato.banco
                conta, como = detetar_conta(db, extrato, pasta)
                p.conta_id, p.deteccao = (conta.id if conta else None), como[:200]
                resumo_para(db, p, extrato)
                if not extrato.movimentos:
                    p.estado, p.erro = "erro", "O ficheiro não tem movimentos."
        db.add(p)
        (r.erros if p.estado == "erro" else r.novos).append(p)
    db.flush()
    return r


def extrato_de(p: ImportacaoPendente, data_dir: Path) -> tuple[str, bytes]:
    return p.ficheiro, servicos.ler_upload(data_dir, p.sha256)


def mover_para_importados(raiz: Path | None, p: ImportacaoPendente, hoje: date) -> str | None:
    """Move o ficheiro para <banco>/importados/AAAA-MM-DD_<ficheiro>. Devolve um aviso se não foi possível."""
    if raiz is None:
        return "pasta partilhada não configurada: o ficheiro não foi movido."
    origem = raiz / p.caminho
    if not origem.is_file():
        return "o ficheiro já não estava na pasta."
    destino_dir = origem.parent / PASTA_IMPORTADOS
    try:
        destino_dir.mkdir(exist_ok=True)
        destino = destino_dir / f"{hoje.isoformat()}_{origem.name}"
        n = 2
        while destino.exists():
            destino = destino_dir / f"{hoje.isoformat()}_{n}_{origem.name}"
            n += 1
        shutil.move(str(origem), str(destino))
        p.caminho = destino.relative_to(raiz).as_posix()[:500]
    except OSError as e:
        return f"não foi possível mover o ficheiro ({e}). Confirma as permissões da pasta partilhada."
    return None


def pendentes(db: Session) -> list[ImportacaoPendente]:
    return db.scalars(select(ImportacaoPendente).where(ImportacaoPendente.estado.in_(("pendente", "erro")))
                      .order_by(ImportacaoPendente.estado.desc(), ImportacaoPendente.pasta, ImportacaoPendente.data_ini)).all()


def n_por_validar(db: Session) -> int:
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(ImportacaoPendente)
                     .where(ImportacaoPendente.estado == "pendente")) or 0


def intervalo_minutos(prefs: dict) -> int:
    try:
        v = int(prefs.get("pasta_intervalo_minutos", "60"))
    except ValueError:
        v = 60
    return v if v in INTERVALOS else 60


async def ciclo(SessionLocal, defs, prefs) -> None:
    """Lê a pasta partilhada no arranque e depois de N em N minutos (preferência)."""
    if defs.import_dir is None:
        return
    while True:
        try:
            def correr():
                with SessionLocal() as db:
                    r = ler(db, defs.import_dir, defs.data_dir)
                    db.commit()
                    return r
            r = await asyncio.to_thread(correr)
            if r.novos or r.erros:
                log.info("Pasta de extratos: %d novos por validar, %d com erro", len(r.novos), len(r.erros))
        except Exception:
            log.exception("Falha a ler a pasta de extratos")
        await asyncio.sleep(intervalo_minutos(prefs()) * 60)
