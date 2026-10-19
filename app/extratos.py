"""Extratos originais: de onde veio cada movimento, a entrada de extratos pelo Paperless e a validação em lote.

Cada importação guarda o ficheiro original em data/uploads/<sha256> (desde sempre) e, quando se sabe, onde mais está:
  - Importacao.paperless_id — o documento no Paperless (vindo de lá, ou encontrado pelo MD5 do ficheiro);
  - Importacao.origem       — o ficheiro na pasta partilhada da NAS (<banco>/importados/…).
O detalhe do movimento mostra o extrato de onde veio e os outros extratos da mesma conta que o cobrem (ex.: o
movimento entrou pelo .xls e também está no extrato mensal em PDF).
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import paperless, servicos
from .models import Conta, Importacao, ImportacaoPendente, Movimento, Utilizador
from .parsers import FormatoDesconhecido, ler_extrato

log = logging.getLogger(__name__)

PASTA_PAPERLESS = "Paperless"


# ------------------------------------------------------------------ ligação movimento → extrato original
def href(imp: Importacao) -> str:
    """Onde abrir o extrato: no Paperless, se lá estiver; senão a cópia guardada pelo Gestor."""
    if imp.paperless_id and paperless.configurado():
        return f"{paperless.url_browser()}/documents/{imp.paperless_id}/details"
    return f"/importar/{imp.id}/original"


def do_movimento(db: Session, m: Movimento, limite: int = 4) -> list[Importacao]:
    """A importação de onde o movimento veio e as outras (confirmadas, da mesma conta) cujo período o inclui."""
    out = []
    if m.importacao_id and (imp := db.get(Importacao, m.importacao_id)):
        out.append(imp)
    outras = db.scalars(select(Importacao).where(
        Importacao.conta_id == m.conta_id, Importacao.estado == "confirmada", Importacao.id != (m.importacao_id or 0),
        Importacao.data_ini <= m.data_mov, Importacao.data_fim >= m.data_mov)
        .order_by(Importacao.paperless_id.is_(None), Importacao.id.desc()).limit(limite)).all()
    vistos = {i.sha256 for i in out}
    for i in outras:
        if i.sha256 not in vistos:
            vistos.add(i.sha256)
            out.append(i)
    return out[:limite]


def md5_upload(data_dir: Path, sha: str) -> str | None:
    try:
        return hashlib.md5(servicos.ler_upload(data_dir, sha)).hexdigest()
    except FileNotFoundError:
        return None


def procurar_no_paperless(db: Session, imp: Importacao, data_dir: Path, token: str | None) -> bool:
    """Liga a importação ao documento do Paperless com o mesmo ficheiro (MD5). True se ligou."""
    if imp.paperless_id or not token:
        return False
    md5 = md5_upload(data_dir, imp.sha256)
    if not md5:
        return False
    pid = paperless.por_checksum(md5, token=token)
    if pid:
        imp.paperless_id = pid
        return True
    return False


def ligar_existentes(db: Session, data_dir: Path, token: str, limite: int = 500) -> int:
    """Importações antigas ainda sem documento: procura cada original no Paperless (pelo MD5)."""
    n = 0
    for imp in db.scalars(select(Importacao).where(Importacao.paperless_id.is_(None), Importacao.estado == "confirmada")
                          .order_by(Importacao.id.desc()).limit(limite)):
        if procurar_no_paperless(db, imp, data_dir, token):
            n += 1
    return n


# ------------------------------------------------------------------ entrada pelo Paperless
@dataclass
class LeituraPaperless:
    novos: list = field(default_factory=list)
    erros: list = field(default_factory=list)
    ignorados: int = 0
    ligados: int = 0          # já importados por outra via: só ficou a ligação ao documento
    mensagem: str = ""


def ler_paperless(db: Session, data_dir: Path, token: str, nome: str) -> LeituraPaperless:
    """Os documentos do Paperless com a etiqueta/tipo «nome» que ainda não se conhecem ficam por validar, como os da
    pasta partilhada (nada entra nos movimentos sem validação)."""
    from . import pasta_importacao as pi
    r = LeituraPaperless()
    docs = paperless.extratos(nome, token=token)
    if not docs:
        r.mensagem = (f"Não há documentos no Paperless com a etiqueta ou o tipo «{nome}». Põe essa etiqueta nos extratos "
                      "(ou escreve outro nome em Importar › Por validar › Paperless).")
        return r
    conhecidos_id = set(db.scalars(select(ImportacaoPendente.paperless_id).where(ImportacaoPendente.paperless_id.is_not(None))))
    conhecidos_id |= set(db.scalars(select(Importacao.paperless_id).where(Importacao.paperless_id.is_not(None))))
    pendentes_sha = {p.sha256: p for p in db.scalars(select(ImportacaoPendente))}
    for d in docs:
        if d.id in conhecidos_id:
            r.ignorados += 1
            continue
        try:
            conteudo = paperless.descarregar(d.id, token=token)
        except paperless.ErroPaperless as e:
            log.warning("Paperless %s: %s", d.id, e)
            continue
        sha = hashlib.sha256(conteudo).hexdigest()
        ja_importado = db.scalars(select(Importacao).where(Importacao.sha256 == sha)).all()
        if ja_importado or sha in pendentes_sha:
            # o mesmo ficheiro já entrou (ou está à espera) pela pasta ou à mão: fica só a ligação ao documento
            for imp in ja_importado:
                imp.paperless_id = imp.paperless_id or d.id
            if sha in pendentes_sha and pendentes_sha[sha].paperless_id is None:
                pendentes_sha[sha].paperless_id = d.id
            r.ligados += 1
            continue
        nome_fich = (d.ficheiro or f"{d.titulo}.pdf")[:255]
        p = ImportacaoPendente(pasta=PASTA_PAPERLESS, ficheiro=nome_fich, caminho=f"paperless:{d.id}", sha256=sha,
                               n_lidos=0, estado="pendente", paperless_id=d.id)
        if len(conteudo) > servicos.TAMANHO_MAX:
            p.estado, p.erro = "erro", "Ficheiro demasiado grande (máx. 10 MB)."
        else:
            pi.analisar(db, p, conteudo, data_dir)
        db.add(p)
        pendentes_sha[sha] = p
        (r.erros if p.estado == "erro" else r.novos).append(p)
    db.flush()
    return r


# ------------------------------------------------------------------ validação em lote
def _problema(conta: Conta, extrato) -> str | None:
    """Porque é que este extrato não pode ser validado sem se olhar para ele (None = pode)."""
    if extrato.debito_cartao:
        return "ficheiro de cartão de débito (valida-o à mão)"
    if extrato.moeda != conta.moeda:
        return f"moeda {extrato.moeda} ≠ {conta.moeda}"
    if extrato.emprestimo != (conta.tipo == "EMPRESTIMO") or extrato.cartao != (conta.tipo == "CARTAO_CREDITO") \
            or extrato.refeicao != (conta.tipo == "CARTAO_REFEICAO"):
        return "o tipo do extrato não corresponde ao da conta"
    if conta.instituicao.parser and conta.instituicao.parser != extrato.parser.split("_")[0]:
        return f"o extrato é do {extrato.banco} e a conta do {conta.instituicao.nome}"
    erro, _ = servicos.escolher_subconta(extrato, conta)
    if erro:
        return erro
    if extrato.avisos:
        return extrato.avisos[0]
    return None


@dataclass
class ResultadoLote:
    validados: list = field(default_factory=list)     # (pendente, importacao)
    saltados: list = field(default_factory=list)      # (pendente, motivo)
    movimentos: int = 0


def validar_todos(db: Session, u: Utilizador, defs, token: str | None, ids: list[int] | None = None) -> ResultadoLote:
    """Valida, do mais antigo para o mais recente, os pendentes com conta detetada e sem avisos (os saldos do extrato
    batem certo). Os outros ficam por validar um a um."""
    from .investimentos import adaptar_extrato, e_investimento
    res = ResultadoLote()
    q = select(ImportacaoPendente).where(ImportacaoPendente.estado == "pendente", ImportacaoPendente.conta_id.is_not(None))
    if ids is not None:
        q = q.where(ImportacaoPendente.id.in_(ids))
    for p in db.scalars(q.order_by(ImportacaoPendente.data_ini, ImportacaoPendente.id)).all():
        conta = db.get(Conta, p.conta_id)
        try:
            extrato = ler_extrato(p.ficheiro, servicos.ler_upload(defs.data_dir, p.sha256))
        except (FileNotFoundError, FormatoDesconhecido) as e:
            res.saltados.append((p, f"não foi possível ler ({e})"))
            continue
        if (motivo := _problema(conta, extrato)):
            res.saltados.append((p, motivo))
            continue
        if e_investimento(conta):
            adaptar_extrato(extrato)
        try:
            imp, _ = servicos.confirmar(db, conta, extrato, p.ficheiro, p.sha256, u)
            finalizar(db, p, imp, defs, u, token)
            db.commit()               # um a um: um ficheiro problemático não desfaz os anteriores
        except IntegrityError:
            db.rollback()
            res.saltados.append((p, "tem movimentos que não foi possível distinguir entre si"))
            continue
        res.validados.append((p, imp))
        res.movimentos += imp.n_novos
    return res


def finalizar(db: Session, p: ImportacaoPendente | None, imp: Importacao, defs, u: Utilizador, token: str | None) -> str | None:
    """Depois de confirmar uma importação: regista o pendente como validado (o ficheiro da pasta passa para
    importados/) e guarda onde está o original. Devolve o aviso de não ter movido o ficheiro, se for o caso."""
    from . import pasta_importacao as pi
    aviso = None
    if p is not None:
        p.estado, p.conta_id, p.importacao_id = "validada", imp.conta_id, imp.id
        p.tratado_em, p.tratado_por = datetime.now(), u.id
        aviso = pi.mover_para_importados(defs.import_dir, p, date.today())
        p.nota = aviso
        imp.paperless_id = imp.paperless_id or p.paperless_id
        if not p.caminho.startswith("paperless:"):
            imp.origem = p.caminho
    try:
        procurar_no_paperless(db, imp, defs.data_dir, token)
    except paperless.ErroPaperless as e:     # sem Paperless não se perde a importação
        log.info("Paperless indisponível ao ligar a importação %s: %s", imp.id, e)
    return aviso


async def ciclo_paperless(SessionLocal, defs, prefs) -> None:
    """Lê os extratos do Paperless com o mesmo intervalo da pasta partilhada. Só com o token partilhado
    (GESTOR_PAPERLESS_TOKEN no paperless.env): sem ele, a leitura é só com «Ler do Paperless»."""
    import asyncio
    import os
    from . import pasta_importacao as pi
    token = (os.environ.get("GESTOR_PAPERLESS_TOKEN") or "").strip()
    if not paperless.configurado() or not token:
        return
    while True:
        try:
            def correr():
                with SessionLocal() as db:
                    r = ler_paperless(db, defs.data_dir, token, prefs().get("paperless_extratos", ""))
                    db.commit()
                    return r
            r = await asyncio.to_thread(correr)
            if r.novos or r.erros:
                log.info("Paperless: %d extratos novos por validar, %d com erro", len(r.novos), len(r.erros))
        except Exception:
            log.exception("Falha a ler os extratos do Paperless")
        await asyncio.sleep(pi.intervalo_minutos(prefs()) * 60)
