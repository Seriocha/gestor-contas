"""Garagem / Espaços: onde está cada coisa (espaços, zonas no mapa 2D/3D, localizações em árvore, artigos, pré-registo).

É a lógica da app autónoma «Gestão de Garagem» (server.js) passada para aqui, com o mesmo contrato JSON, para que a
mesma interface (app/static/garagem/) funcione sem alterações. Tudo é por entidade (módulo «garagem») e respeita as
permissões do Gestor: ver = ler, editar = alterar, administrar = importar a substituir e apagar tudo.

O histórico de desfazer/refazer guarda-se em memória, por entidade (até 50 passos; perde-se ao reiniciar).
"""
from __future__ import annotations

import base64
import io
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from .models import PAPEIS, Bem, GarArtigo, GarEspaco, GarLocal, GarRascunho, ItemRecheio

ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAREDES = ("fundo", "esquerda", "direita", "frente")
FACES = ("entrada", "fundo", "esquerda", "direita")        # para onde olha a frente quando não há parede
FOTO_RE = re.compile(r"^[a-f0-9]{24}\.jpg$")
FORMATO = "garagem-export"                                   # ficheiro de exportação/importação (versão 1)
MAX_HISTORICO = 50

LOC_COLS = ("id", "parent_id", "name", "type", "code", "notes", "photo", "grid_rows", "grid_cols", "grid_row", "grid_col",
            "plan_x", "plan_y", "plan_w", "plan_h", "plan_z", "plan_alt", "plan_wall", "plan_face", "space_id")
LOC_CAMPOS = tuple(c for c in LOC_COLS if c not in ("id", "code"))
LOC_INT = ("parent_id", "grid_rows", "grid_cols", "grid_row", "grid_col", "space_id", "recheio_id")
LOC_FLOAT = ("plan_x", "plan_y", "plan_w", "plan_h", "plan_z", "plan_alt")
ITEM_COLS = ("id", "name", "quantity", "category", "notes", "photo", "location_id", "updated", "valuable", "recheio_id")
ITEM_CAMPOS = ("name", "quantity", "category", "notes", "photo", "location_id", "valuable", "recheio_id")
DRAFT_COLS = ("id", "name", "quantity", "category", "notes", "location_id", "created")
DRAFT_CAMPOS = ("name", "quantity", "category", "notes", "location_id")
ESP_COLS = ("id", "name", "w", "d", "h", "bem_id", "imovel_nome", "imovel_ref")


class GaragemErro(Exception):
    """Erro com mensagem para o utilizador; a interface mostra-a como está (JSON {"error": ...})."""

    def __init__(self, mensagem: str, status: int = 400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status


# ---------------------------------------------------------------------------------------------------------------------
# contexto, histórico e utilitários
# ---------------------------------------------------------------------------------------------------------------------
class Historico:
    def __init__(self) -> None:
        self.undo: list[tuple[dict, str]] = []
        self.redo: list[tuple[dict, str]] = []


@dataclass
class Ctx:
    db: Session
    ent: int                 # entidade_id
    papel: str               # ver | editar | admin
    fotos: Path
    hist: Historico

    def exigir(self, minimo: str) -> None:
        if PAPEIS[self.papel] < PAPEIS[minimo]:
            raise GaragemErro("Sem permissão para esta operação.", 403)


def agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _txt(v, limite: int | None = None) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:limite] if limite else s


def _pick(body: dict, campos) -> dict:
    """Só os campos presentes; texto aparado; vazio → None; números convertidos."""
    o = {}
    for f in campos:
        if f not in body:
            continue
        v = body[f]
        if isinstance(v, str):
            v = v.strip() or None
        if v is not None and f in LOC_INT:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise GaragemErro(f"Valor inválido em {f}.")
        elif f == "valuable":
            v = bool(v) and str(v).lower() not in ("0", "false", "")
        elif v is not None and f in LOC_FLOAT:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise GaragemErro(f"Valor inválido em {f}.")
        o[f] = v
    return o


def _exige_nome(o: dict, criar: bool) -> None:
    if (criar or "name" in o) and not o.get("name"):
        raise GaragemErro("O nome é obrigatório.")
    if o.get("name"):
        o["name"] = str(o["name"])[:100]


def _verifica_foto(o: dict) -> None:
    if o.get("photo") and not FOTO_RE.match(str(o["photo"])):
        raise GaragemErro("Foto inválida.")


def _verifica_parede(o: dict) -> None:
    if o.get("plan_wall") is not None and o["plan_wall"] not in PAREDES:
        raise GaragemErro("Parede inválida.")
    if o.get("plan_face") is not None and o["plan_face"] not in FACES:
        raise GaragemErro("Orientação inválida.")


def _linhas(db: Session, modelo, cols, ent: int) -> list[dict]:
    q = select(*[getattr(modelo, c) for c in cols]).where(modelo.entidade_id == ent).order_by(modelo.id)
    return [dict(zip(cols, r)) for r in db.execute(q).all()]


def _loc(ctx: Ctx, id_) -> GarLocal | None:
    try:
        l = ctx.db.get(GarLocal, int(id_))
    except (TypeError, ValueError):
        return None
    return l if l and l.entidade_id == ctx.ent else None


def _espaco(ctx: Ctx, id_) -> GarEspaco | None:
    try:
        e = ctx.db.get(GarEspaco, int(id_))
    except (TypeError, ValueError):
        return None
    return e if e and e.entidade_id == ctx.ent else None


def _artigo(ctx: Ctx, id_) -> GarArtigo | None:
    try:
        a = ctx.db.get(GarArtigo, int(id_))
    except (TypeError, ValueError):
        return None
    return a if a and a.entidade_id == ctx.ent else None


def primeiro_espaco(ctx: Ctx) -> int | None:
    return ctx.db.scalar(select(func.min(GarEspaco.id)).where(GarEspaco.entidade_id == ctx.ent))


def garantir_espaco(ctx: Ctx) -> None:
    """Uma entidade nova começa com o espaço «Garagem» (6 × 5 × 2,5 m)."""
    if primeiro_espaco(ctx) is None and PAPEIS[ctx.papel] >= PAPEIS["editar"]:
        ctx.db.add(GarEspaco(entidade_id=ctx.ent, name="Garagem", w=6, d=5, h=2.5))
        ctx.db.commit()


def novo_codigo(db: Session) -> str:
    while True:
        codigo = "".join(secrets.choice(ALFABETO) for _ in range(5))
        if not db.scalar(select(GarLocal.id).where(GarLocal.code == codigo)):
            return codigo


# --- fotos -----------------------------------------------------------------------------------------------------------
def guardar_foto(ctx: Ctx, dados: bytes) -> str:
    ctx.exigir("editar")
    if len(dados) < 100 or dados[:2] != b"\xff\xd8":
        raise GaragemErro("Envie uma imagem JPEG.")
    if len(dados) > 12 * 1024 * 1024:
        raise GaragemErro("Imagem demasiado grande (máx. 12 MB).", 413)
    nome = secrets.token_hex(12) + ".jpg"
    ctx.fotos.mkdir(parents=True, exist_ok=True)
    (ctx.fotos / nome).write_bytes(dados)
    return nome


def caminho_foto(fotos: Path, nome: str) -> Path | None:
    if not FOTO_RE.match(nome):
        return None
    p = fotos / nome
    return p if p.is_file() else None


def limpar_fotos(db: Session, fotos: Path, historicos: dict[int, Historico]) -> int:
    """Apaga as fotos (com mais de 1 hora) que já não estão em nada — nem no histórico de desfazer."""
    if not fotos.is_dir():
        return 0
    usadas = {r for r in db.scalars(select(GarArtigo.photo).where(GarArtigo.photo.is_not(None)))}
    usadas |= {r for r in db.scalars(select(GarLocal.photo).where(GarLocal.photo.is_not(None)))}
    for h in historicos.values():
        for snap, _ in [*h.undo, *h.redo]:
            usadas |= {x["photo"] for x in snap["items"] + snap["locations"] if x.get("photo")}
    n = 0
    for p in fotos.iterdir():
        if p.name not in usadas and time.time() - p.stat().st_mtime > 3600:
            p.unlink(missing_ok=True)
            n += 1
    return n


# --- histórico (desfazer / refazer) ----------------------------------------------------------------------------------
def instantaneo(db: Session, ent: int) -> dict:
    return {"spaces": _linhas(db, GarEspaco, ESP_COLS, ent), "locations": _linhas(db, GarLocal, LOC_COLS, ent),
            "items": _linhas(db, GarArtigo, ITEM_COLS, ent), "drafts": _linhas(db, GarRascunho, DRAFT_COLS, ent)}


def _pais_primeiro(rows: list[dict]) -> list[dict]:
    ids = {r["id"] for r in rows}
    feitos, saida, pendentes = set(), [], list(rows)
    while pendentes:
        seguintes = []
        for r in pendentes:
            if r["parent_id"] is None or r["parent_id"] not in ids or r["parent_id"] in feitos:
                saida.append(r)
                feitos.add(r["id"])
            else:
                seguintes.append(r)
        if len(seguintes) == len(pendentes):          # ciclo (não devia acontecer): insere o resto como está
            saida.extend(seguintes)
            break
        pendentes = seguintes
    return saida


def _apagar_tudo(db: Session, ent: int) -> None:
    db.execute(text("PRAGMA defer_foreign_keys = ON"))
    for modelo in (GarRascunho, GarArtigo, GarLocal, GarEspaco):
        db.execute(delete(modelo).where(modelo.entidade_id == ent))


def repor(db: Session, ent: int, snap: dict) -> None:
    _apagar_tudo(db, ent)
    bens = set(db.scalars(select(Bem.id)))
    if snap["spaces"]:
        db.execute(insert(GarEspaco), [{**r, "entidade_id": ent, "bem_id": r["bem_id"] if r["bem_id"] in bens else None} for r in snap["spaces"]])
    if snap["locations"]:
        db.execute(insert(GarLocal), [{**r, "entidade_id": ent} for r in _pais_primeiro(snap["locations"])])
    if snap["items"]:
        rech = set(db.scalars(select(ItemRecheio.id)))
        db.execute(insert(GarArtigo), [{**r, "entidade_id": ent, "recheio_id": r.get("recheio_id") if r.get("recheio_id") in rech else None}
                                       for r in snap["items"]])
    if snap["drafts"]:
        db.execute(insert(GarRascunho), [{**r, "entidade_id": ent} for r in snap["drafts"]])


@contextmanager
def mutacao(ctx: Ctx, rotulo: str, minimo: str = "editar", historico: bool = True):
    """Envolve uma alteração: exige o papel, guarda o estado anterior (para desfazer) e grava ou desfaz tudo."""
    ctx.exigir(minimo)
    antes = instantaneo(ctx.db, ctx.ent) if historico else None
    try:
        yield
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    if antes is not None:
        h = ctx.hist
        h.undo.append((antes, rotulo))
        del h.undo[:-MAX_HISTORICO]
        h.redo.clear()


def desfazer(ctx: Ctx, refazer: bool = False) -> str:
    ctx.exigir("editar")
    de, para = (ctx.hist.redo, ctx.hist.undo) if refazer else (ctx.hist.undo, ctx.hist.redo)
    if not de:
        raise GaragemErro("Nada para refazer." if refazer else "Nada para desfazer.", 409)
    snap, rotulo = de[-1]
    atual = instantaneo(ctx.db, ctx.ent)
    try:
        repor(ctx.db, ctx.ent, snap)
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    de.pop()
    para.append((atual, rotulo))
    return rotulo


# ---------------------------------------------------------------------------------------------------------------------
# estado e espaços
# ---------------------------------------------------------------------------------------------------------------------
def imoveis(db: Session, ent: int) -> list[Bem]:
    return list(db.scalars(select(Bem).where(Bem.entidade_id == ent, Bem.tipo.in_(("casa", "rustico"))).order_by(Bem.nome)))


def _d_espaco(e: GarEspaco, bens: dict[int, Bem]) -> dict:
    bem = bens.get(e.bem_id) if e.bem_id else None
    return {"id": e.id, "name": e.name, "w": e.w, "d": e.d, "h": e.h,
            "property_id": str(e.bem_id) if bem else e.imovel_ref,
            "property_name": bem.nome if bem else e.imovel_nome}


def espacos(ctx: Ctx, property_id: str | None = None) -> list[dict]:
    bens = {b.id: b for b in imoveis(ctx.db, ctx.ent)}
    out = [_d_espaco(e, bens) for e in ctx.db.scalars(select(GarEspaco).where(GarEspaco.entidade_id == ctx.ent).order_by(GarEspaco.id))]
    return [s for s in out if property_id is None or str(s["property_id"] or "") == str(property_id)]


def estado(ctx: Ctx) -> dict:
    db, ent = ctx.db, ctx.ent
    h = ctx.hist
    return {
        "spaces": espacos(ctx),
        "locations": _linhas(db, GarLocal, LOC_COLS, ent),
        "items": sorted(_linhas(db, GarArtigo, ITEM_COLS, ent), key=lambda i: (i["updated"], i["id"]), reverse=True),
        "drafts": _linhas(db, GarRascunho, DRAFT_COLS, ent),
        "bens": [{"id": b.id, "nome": b.nome, "tipo": b.tipo} for b in imoveis(db, ent)],
        "recheio": recheio_da_entidade(db, ent),
        "history": {"undo": len(h.undo), "redo": len(h.redo),
                    "undoLabel": h.undo[-1][1] if h.undo else None, "redoLabel": h.redo[-1][1] if h.redo else None},
    }


def _ligar_imovel(db: Session, ent: int, pid, pnome, preferir_nome: bool = False) -> tuple[int | None, str | None, str | None]:
    """(bem_id, imovel_nome, imovel_ref). Liga a um imóvel (casa/rústico) da entidade por id ou, nas importações, por nome."""
    pid, pnome = _txt(pid, 120), _txt(pnome, 120)
    bens = imoveis(db, ent)

    def por_id():
        if pid and pid.isdigit():
            return next((b for b in bens if b.id == int(pid)), None)

    def por_nome():
        if pnome:
            return next((b for b in bens if b.nome.strip().lower() == pnome.lower()), None)

    bem = (por_nome() or por_id()) if preferir_nome else (por_id() or None)
    if bem:
        return bem.id, None, None
    return None, pnome, pid


def _corpo_espaco(ctx: Ctx, b: dict, preferir_nome: bool = False) -> dict:
    nome = _txt(b.get("name"), 100)
    if not nome:
        raise GaragemErro("O nome do espaço é obrigatório.")
    try:
        w, d, h = float(b.get("w")), float(b.get("d")), float(b.get("h") or 2.5)
    except (TypeError, ValueError):
        raise GaragemErro("Dimensões inválidas (1 a 100 m).")
    if not (1 <= w <= 100 and 1 <= d <= 100):
        raise GaragemErro("Dimensões inválidas (1 a 100 m).")
    if not (1 <= h <= 20):
        raise GaragemErro("Altura inválida (1 a 20 m).")
    bem_id, nome_im, ref = _ligar_imovel(ctx.db, ctx.ent, b.get("property_id"), b.get("property_name"), preferir_nome)
    return {"name": nome, "w": w, "d": d, "h": h, "bem_id": bem_id, "imovel_nome": nome_im, "imovel_ref": ref}


def criar_espaco(ctx: Ctx, body: dict) -> int:
    with mutacao(ctx, "criar espaço"):
        e = GarEspaco(entidade_id=ctx.ent, **_corpo_espaco(ctx, body))
        ctx.db.add(e)
        ctx.db.flush()
        return e.id


def atualizar_espaco(ctx: Ctx, id_, body: dict) -> None:
    with mutacao(ctx, "alterar espaço"):
        e = _espaco(ctx, id_)
        if not e:
            raise GaragemErro("Espaço inexistente.", 404)
        for k, v in _corpo_espaco(ctx, body).items():
            setattr(e, k, v)


def apagar_espaco(ctx: Ctx, id_) -> None:
    with mutacao(ctx, "apagar espaço"):
        e = _espaco(ctx, id_)
        if not e:
            raise GaragemErro("Espaço inexistente.", 404)
        if ctx.db.scalar(select(func.count()).select_from(GarEspaco).where(GarEspaco.entidade_id == ctx.ent)) <= 1:
            raise GaragemErro("É preciso manter pelo menos um espaço.", 409)
        if ctx.db.scalar(select(GarLocal.id).where(GarLocal.entidade_id == ctx.ent, GarLocal.parent_id.is_(None), GarLocal.space_id == e.id)):
            raise GaragemErro("Este espaço ainda tem zonas. Mova-as para outro espaço ou apague-as primeiro.", 409)
        ctx.db.delete(e)


def reposicao_total(ctx: Ctx) -> None:
    """Apaga tudo desta entidade (fica só a Garagem por defeito). Só administradores."""
    with mutacao(ctx, "apagar tudo", minimo="admin"):
        _apagar_tudo(ctx.db, ctx.ent)
        ctx.db.add(GarEspaco(entidade_id=ctx.ent, name="Garagem", w=6, d=5, h=2.5))


# ---------------------------------------------------------------------------------------------------------------------
# localizações
# ---------------------------------------------------------------------------------------------------------------------
def d_loc(l: GarLocal) -> dict:
    return {c: getattr(l, c) for c in LOC_COLS}


def _checar_pai(ctx: Ctx, id_, pai_id) -> None:
    """Impede de mover uma localização para dentro de si própria."""
    if pai_id is None:
        return
    p = _loc(ctx, pai_id)
    if not p:
        raise GaragemErro("Localização-mãe inexistente.")
    while p:
        if id_ is not None and p.id == int(id_):
            raise GaragemErro("Não pode mover uma localização para dentro de si própria.")
        if p.parent_id is None:
            return
        p = _loc(ctx, p.parent_id)


def _checar_espaco(ctx: Ctx, o: dict) -> None:
    if o.get("space_id") is not None and not _espaco(ctx, o["space_id"]):
        raise GaragemErro("Espaço inexistente.")


def criar_local(ctx: Ctx, body: dict) -> int:
    with mutacao(ctx, "criar localização"):
        o = _pick(body, LOC_CAMPOS)
        _exige_nome(o, True)
        _verifica_foto(o)
        _verifica_parede(o)
        _checar_pai(ctx, None, o.get("parent_id"))
        if o.get("parent_id") is None:
            o["space_id"] = o.get("space_id") or primeiro_espaco(ctx)
            _checar_espaco(ctx, o)
        else:
            o["space_id"] = None
        o.setdefault("type", "zona")
        l = GarLocal(entidade_id=ctx.ent, code=novo_codigo(ctx.db), **o)
        ctx.db.add(l)
        ctx.db.flush()
        return l.id

# Alturas por omissão (iguais às da interface) e verificação de sobreposição no mapa.
ALT_PADRAO = {"estante": 2, "armario": 2, "bancada": 0.9, "prateleira": 0.4, "caixa": 0.4, "gaveta": 0.2, "mezanine": 0.15,
              "bike_suspensa": 1.8, "bike_piso": 0.02}
SEM_COLISAO = ("bike_piso",)      # demarcação no piso: não ocupa volume, por isso pode estar por baixo de outras zonas
_FACE_PAREDE = {"entrada": "fundo", "fundo": "frente", "esquerda": "direita", "direita": "esquerda"}
_PLAN = ("plan_x", "plan_y", "plan_w", "plan_h", "plan_z", "plan_alt", "plan_wall", "plan_face")


def _caixa(l, pai) -> tuple | None:
    """(x0, x1, y0, y1, z0, z1) absolutos de uma zona no mapa, ou None se não estiver posicionada."""
    if not l.plan_w or l.plan_w <= 0 or not l.plan_h or l.plan_h <= 0:
        return None
    lado = (l.plan_wall or _FACE_PAREDE.get(l.plan_face) or "fundo") in ("esquerda", "direita")
    ex, ey = (l.plan_h, l.plan_w) if lado else (l.plan_w, l.plan_h)
    x, y, z = l.plan_x or 0, l.plan_y or 0, l.plan_z or 0
    if pai is not None:
        if not pai.plan_w or pai.plan_w <= 0:
            return None
        x, y = x + (pai.plan_x or 0), y + (pai.plan_y or 0)
        z += (pai.plan_z or 0) + (pai.plan_alt or ALT_PADRAO["mezanine"])
    return (x, x + ex, y, y + ey, z, z + (l.plan_alt or ALT_PADRAO.get(l.type, 1)))


def _verifica_sobreposicao(ctx: Ctx, cur: GarLocal, o: dict) -> None:
    """Recusa pôr a zona onde choca com outra do mesmo espaço ao mesmo nível (chão e altura em comum)."""
    if not any(k in o for k in _PLAN):
        return
    from types import SimpleNamespace
    novo = SimpleNamespace(**{c.name: getattr(cur, c.name) for c in GarLocal.__table__.columns})
    for k, v in o.items():
        if k in novo.__dict__:
            setattr(novo, k, v)
    if novo.type in SEM_COLISAO:
        return
    pai = ctx.db.get(GarLocal, novo.parent_id) if novo.parent_id else None
    if pai is not None and pai.type != "mezanine":
        return                                     # só as zonas do mapa (topo ou sobre uma mezanine) têm planta
    if novo.grid_row is not None:
        return
    a = _caixa(novo, pai)
    if a is None:
        return
    espaco = novo.space_id if pai is None else pai.space_id
    outras = ctx.db.scalars(select(GarLocal).where(GarLocal.entidade_id == ctx.ent, GarLocal.id != cur.id)).all()
    por_id = {x.id: x for x in outras}
    e = 1e-6
    for x in outras:
        xp = por_id.get(x.parent_id)
        if x.type in SEM_COLISAO or x.grid_row is not None or (x.parent_id is not None and (xp is None or xp.type != "mezanine")):
            continue
        if (xp.space_id if xp is not None else x.space_id) != espaco:
            continue
        b = _caixa(x, xp)
        if b and a[0] < b[1] - e and b[0] < a[1] - e and a[2] < b[3] - e and b[2] < a[3] - e and a[4] < b[5] - e and b[4] < a[5] - e:
            raise GaragemErro(f"Sobrepõe-se a «{x.name}» ao mesmo nível.", 409)


def atualizar_local(ctx: Ctx, id_, body: dict, rotulo: str = "alterar localização") -> None:
    with mutacao(ctx, rotulo):
        cur = _loc(ctx, id_)
        if not cur:
            raise GaragemErro("Localização inexistente.", 404)
        o = _pick(body, LOC_CAMPOS)
        _exige_nome(o, False)
        _verifica_foto(o)
        _verifica_parede(o)
        if "parent_id" in o:
            _checar_pai(ctx, cur.id, o["parent_id"])
            if o["parent_id"] is not None and _dentro_de_matriz(ctx, o["parent_id"]) and _tem_matriz(ctx, cur.id):
                raise GaragemErro(MSG_MATRIZ + " Não pode pôr aqui uma localização que tem matriz.", 409)
            if o["parent_id"] is not None:
                o["space_id"] = None                       # só as zonas de topo pertencem a um espaço
            elif o.get("space_id") is None:
                o["space_id"] = cur.space_id or primeiro_espaco(ctx)
        if o.get("space_id") is not None:
            _checar_espaco(ctx, o)
            raiz = (o["parent_id"] is None) if "parent_id" in o else cur.parent_id is None
            if not raiz:
                del o["space_id"]
            elif o["space_id"] != cur.space_id:           # mudou de espaço: tem de ser posicionada de novo no mapa
                o.update(plan_x=None, plan_y=None, plan_w=None, plan_h=None, plan_wall=None)
        _verifica_sobreposicao(ctx, cur, o)
        for k, v in o.items():
            setattr(cur, k, v)


def _tem_conteudo(ctx: Ctx, id_: int) -> bool:
    return bool(ctx.db.scalar(select(GarLocal.id).where(GarLocal.parent_id == id_)) or
                ctx.db.scalar(select(GarArtigo.id).where(GarArtigo.location_id == id_)))


def apagar_local(ctx: Ctx, id_) -> None:
    with mutacao(ctx, "apagar localização"):
        l = _loc(ctx, id_)
        if not l:
            return
        if _tem_conteudo(ctx, l.id):
            raise GaragemErro("Esta localização não está vazia. Mova ou apague primeiro o conteúdo.", 409)
        ctx.db.delete(l)


MSG_MATRIZ = "Uma matriz não pode conter outra matriz."


def _dentro_de_matriz(ctx: Ctx, id_) -> bool:
    """True se a localização `id_` é uma posição de matriz ou está dentro de uma (a qualquer nível)."""
    p = _loc(ctx, id_) if id_ else None
    while p:
        if p.grid_row is not None:
            return True
        p = _loc(ctx, p.parent_id) if p.parent_id else None
    return False


def _tem_matriz(ctx: Ctx, id_: int) -> bool:
    """True se a localização ou algo do que tem dentro tem matriz."""
    filhos: dict[int | None, list[GarLocal]] = {}
    for l in ctx.db.scalars(select(GarLocal).where(GarLocal.entidade_id == ctx.ent)):
        filhos.setdefault(l.parent_id, []).append(l)
    pilha = [id_]
    while pilha:
        atual = pilha.pop()
        l = _loc(ctx, atual)
        if l is not None and l.grid_rows:
            return True
        pilha.extend(f.id for f in filhos.get(atual, []))
    return False


def grelha_criar(ctx: Ctx, id_, body: dict) -> None:
    """Cria (ou altera) a matriz de sub-localizações, ex.: caixa de arquivo com 5 × 4 gavetas. Reduzir só apaga posições vazias."""
    with mutacao(ctx, "alterar matriz"):
        loc = _loc(ctx, id_)
        if not loc:
            raise GaragemErro("Localização inexistente.", 404)
        if _dentro_de_matriz(ctx, loc.id):
            raise GaragemErro(MSG_MATRIZ + " Esta localização já está dentro de uma matriz.", 409)
        try:
            linhas, colunas = int(body.get("rows")), int(body.get("cols"))
        except (TypeError, ValueError):
            raise GaragemErro("Matriz inválida (máx. 26 linhas x 40 colunas).")
        if not (1 <= linhas <= 26 and 1 <= colunas <= 40):
            raise GaragemErro("Matriz inválida (máx. 26 linhas x 40 colunas).")
        tipo = str(body.get("type") or "gaveta")[:20]
        prefixo = str(body.get("prefix") or "").strip()
        db = ctx.db
        for c in db.scalars(select(GarLocal).where(GarLocal.parent_id == loc.id, GarLocal.grid_row.is_not(None),
                                                   (GarLocal.grid_row >= linhas) | (GarLocal.grid_col >= colunas))):
            if _tem_conteudo(ctx, c.id):
                raise GaragemErro(f"A posição {c.name} tem conteúdo; esvazie-a antes de reduzir a matriz.", 409)
            db.delete(c)
        existentes = {(r, c) for r, c in db.execute(select(GarLocal.grid_row, GarLocal.grid_col).where(
            GarLocal.parent_id == loc.id, GarLocal.grid_row.is_not(None)))}
        for r in range(linhas):
            for c in range(colunas):
                if (r, c) in existentes:
                    continue
                celula = chr(65 + r) + str(c + 1)
                db.add(GarLocal(entidade_id=ctx.ent, parent_id=loc.id, name=f"{prefixo} {celula}" if prefixo else celula, type=tipo,
                                code=novo_codigo(db), grid_row=r, grid_col=c))
                db.flush()
        loc.grid_rows, loc.grid_cols = linhas, colunas


def grelha_apagar(ctx: Ctx, id_, mover_artigos: bool) -> None:
    """Apaga a matriz. Os artigos das gavetas passam para a localização-mãe (mover_artigos) ou a operação recusa-se."""
    with mutacao(ctx, "apagar matriz"):
        loc = _loc(ctx, id_)
        if not loc:
            raise GaragemErro("Localização inexistente.", 404)
        db = ctx.db
        celulas = list(db.scalars(select(GarLocal).where(GarLocal.parent_id == loc.id, GarLocal.grid_row.is_not(None))))
        for c in celulas:
            if db.scalar(select(GarLocal.id).where(GarLocal.parent_id == c.id)):
                raise GaragemErro(f"A posição {c.name} tem sub-localizações; apague-as primeiro.", 409)
            if not mover_artigos and db.scalar(select(GarArtigo.id).where(GarArtigo.location_id == c.id)):
                raise GaragemErro(f"A posição {c.name} tem artigos.", 409)
        for c in celulas:
            for a in db.scalars(select(GarArtigo).where(GarArtigo.location_id == c.id)):
                a.location_id, a.updated = loc.id, agora()
            db.flush()
            db.delete(c)
        loc.grid_rows = loc.grid_cols = None


def _proximo_nome(ctx: Ctx, nome: str, pai_id) -> str:
    q = select(GarLocal.name).where(GarLocal.entidade_id == ctx.ent)
    q = q.where(GarLocal.parent_id.is_(None) if pai_id is None else GarLocal.parent_id == pai_id)
    ocupados = set(ctx.db.scalars(q))
    m = re.match(r"^(.*?)(\d+)(\s*)$", nome)
    n, out = (int(m.group(2)) if m else 1), nome
    while True:
        n += 1
        out = f"{m.group(1)}{n}{m.group(3)}" if m else f"{nome} {n}"
        if out not in ocupados:
            return out


def _copiar_arvore(ctx: Ctx, src_id: int, pai_id, nome, modo: str, sobre: dict | None) -> int:
    s = _loc(ctx, src_id)
    dados = {c: getattr(s, c) for c in LOC_COLS if c not in ("id", "code")}
    dados.update(parent_id=pai_id, name=nome if nome is not None else s.name)
    if modo == "self":
        dados.update(grid_rows=None, grid_cols=None)
    if sobre is not None:                                 # cópia de topo: deixa de ser uma posição da matriz
        dados.update(grid_row=None, grid_col=None, **sobre)
    novo = GarLocal(entidade_id=ctx.ent, code=novo_codigo(ctx.db), **dados)
    ctx.db.add(novo)
    ctx.db.flush()
    if modo != "self":
        for filho in list(ctx.db.scalars(select(GarLocal).where(GarLocal.parent_id == src_id).order_by(GarLocal.id))):
            _copiar_arvore(ctx, filho.id, novo.id, None, modo, None)
    if modo == "full":
        for a in list(ctx.db.scalars(select(GarArtigo).where(GarArtigo.location_id == src_id))):
            ctx.db.add(GarArtigo(entidade_id=ctx.ent, name=a.name, quantity=a.quantity, category=a.category, notes=a.notes,
                                 photo=a.photo, location_id=novo.id, updated=agora(), valuable=a.valuable))
    return novo.id


def duplicar(ctx: Ctx, id_, body: dict) -> list[int]:
    """Duplica uma localização (ex.: prateleira) uma ou várias vezes; modo: self | structure | full (com artigos)."""
    with mutacao(ctx, "duplicar localização"):
        src = _loc(ctx, id_)
        if not src:
            raise GaragemErro("Localização inexistente.", 404)
        try:
            n = int(body.get("count") or 1)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= 50:
            raise GaragemErro("Número de cópias inválido (1 a 50).")
        modo = body.get("mode") if body.get("mode") in ("self", "structure", "full") else "structure"
        try:
            dx, dy = float(body.get("dx") or 0), float(body.get("dy") or 0)
        except (TypeError, ValueError):
            dx = dy = 0.0
        nome = _txt(body.get("name"), 100) or _proximo_nome(ctx, src.name, src.parent_id)
        ids = []
        for k in range(1, n + 1):
            sobre = {}
            if src.plan_w and src.plan_w > 0 and (dx or dy):
                sobre = {"plan_x": round(src.plan_x + k * dx, 2), "plan_y": round(src.plan_y + k * dy, 2)}
            ids.append(_copiar_arvore(ctx, src.id, src.parent_id, nome, modo, sobre))
            nome = _proximo_nome(ctx, nome, src.parent_id)
        return ids


# ---------------------------------------------------------------------------------------------------------------------
# artigos e pré-registo
# ---------------------------------------------------------------------------------------------------------------------
def _local_valido(ctx: Ctx, lid) -> int | None:
    if lid is None or lid == "":
        return None
    if not _loc(ctx, lid):
        raise GaragemErro("Localização inexistente.")
    return int(lid)


def recheio_da_entidade(db: Session, ent: int) -> list[dict]:
    """Os itens do recheio das casas da entidade (os que ainda existem), para ligar os artigos de valor."""
    q = select(ItemRecheio).where(ItemRecheio.entidade_id == ent, ItemRecheio.estado != "ignorado").order_by(ItemRecheio.nome)
    return [{"id": r.id, "nome": r.nome, "marca": r.marca, "modelo": r.modelo, "valor": r.valor, "bem_id": r.bem_id,
             "estado": r.estado} for r in db.scalars(q)]


def _casa_do_local(ctx: Ctx, location_id) -> int | None:
    """A casa (imóvel) a que pertence uma localização: a do espaço (garagem, arrecadação…) onde está."""
    l = _loc(ctx, location_id) if location_id else None
    while l is not None and l.parent_id is not None:
        l = _loc(ctx, l.parent_id)
    e = _espaco(ctx, l.space_id) if l is not None and l.space_id else None
    return e.bem_id if e is not None else None


def _verifica_recheio(ctx: Ctx, o: dict, atual: GarArtigo | None) -> None:
    """Só um artigo marcado «de valor» liga ao recheio, e a um item da mesma casa da garagem/arrecadação onde está."""
    valuable = o["valuable"] if "valuable" in o else (atual.valuable if atual else False)
    rid = o["recheio_id"] if "recheio_id" in o else (atual.recheio_id if atual else None)
    if not valuable and "valuable" in o:
        o["recheio_id"] = None                                    # tirou a marca: desliga
        return
    if rid is None:
        return
    if not valuable:
        raise GaragemErro("Marque o artigo como «de valor» para o ligar ao recheio da casa.")
    r = ctx.db.get(ItemRecheio, rid)
    if r is None or r.entidade_id != ctx.ent:
        raise GaragemErro("Item do recheio inexistente.", 404)
    loc = o["location_id"] if "location_id" in o else (atual.location_id if atual else None)
    casa = _casa_do_local(ctx, loc)
    if casa is not None and r.bem_id is not None and r.bem_id != casa:
        raise GaragemErro("Esse item do recheio é de outra casa (a garagem/arrecadação onde o artigo está pertence a outra).", 409)


def criar_artigo(ctx: Ctx, body: dict) -> int:
    with mutacao(ctx, "criar artigo"):
        o = _pick(body, ITEM_CAMPOS)
        _exige_nome(o, True)
        _verifica_foto(o)
        o["location_id"] = _local_valido(ctx, o.get("location_id"))
        _verifica_recheio(ctx, o, None)
        a = GarArtigo(entidade_id=ctx.ent, updated=agora(), **o)
        ctx.db.add(a)
        ctx.db.flush()
        return a.id


def atualizar_artigo(ctx: Ctx, id_, body: dict) -> None:
    with mutacao(ctx, "alterar artigo"):
        a = _artigo(ctx, id_)
        if not a:
            return
        o = _pick(body, ITEM_CAMPOS)
        _exige_nome(o, False)
        _verifica_foto(o)
        if "location_id" in o:
            o["location_id"] = _local_valido(ctx, o["location_id"])
        _verifica_recheio(ctx, o, a)
        for k, v in o.items():
            setattr(a, k, v)
        a.updated = agora()


def apagar_artigo(ctx: Ctx, id_) -> None:
    with mutacao(ctx, "apagar artigo"):
        a = _artigo(ctx, id_)
        if a:
            ctx.db.delete(a)


def corrigir_categoria(ctx: Ctx, body: dict) -> int:
    """Renomeia (ou junta) categorias em todos os artigos: `from` (uma ou várias) passa a `to`; `to` vazio = sem categoria."""
    with mutacao(ctx, "corrigir categoria"):
        origem = body.get("from")
        origem = [origem] if isinstance(origem, str) else origem
        origem = [str(o).strip() for o in origem or [] if str(o).strip()]
        if not origem:
            raise GaragemErro("Indique a categoria a corrigir.")
        destino = _txt(body.get("to"), 80)
        n = 0
        for a in ctx.db.scalars(select(GarArtigo).where(GarArtigo.entidade_id == ctx.ent, GarArtigo.category.in_(origem))):
            if a.category != destino:
                a.category, a.updated = destino, agora()
                n += 1
        return n


def _numero(v) -> float | None:
    try:
        return float(str(v).strip().replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None


def juntar_duplicados(ctx: Ctx, body: dict) -> dict:
    """Junta artigos repetidos: fica `keep` e os `remove` são apagados. Quantidades numéricas somam-se; senão ficam juntas
    (ex.: «10 + 1 caixa»). A foto, categoria e notas em falta no que fica são herdadas."""
    with mutacao(ctx, "juntar duplicados"):
        fica = _artigo(ctx, body.get("keep"))
        if not fica:
            raise GaragemErro("Artigo a manter inexistente.", 404)
        ids = [i for i in (body.get("remove") or []) if str(i).lstrip("-").isdigit() and int(i) != fica.id]
        outros = [x for x in (_artigo(ctx, i) for i in ids) if x]
        if not outros:
            raise GaragemErro("Nenhum artigo para juntar.")
        qtds = [q for q in [fica.quantity, *[x.quantity for x in outros]] if q not in (None, "")]
        nums = [_numero(q) for q in qtds]
        if qtds and all(n is not None for n in nums):
            total = sum(nums)
            fica.quantity = str(int(total)) if total == int(total) else str(round(total, 3)).replace(".", ",")
        elif qtds:
            fica.quantity = " + ".join(dict.fromkeys(str(q) for q in qtds))[:60]
        for x in outros:
            fica.photo = fica.photo or x.photo
            fica.category = fica.category or x.category
            fica.notes = fica.notes or x.notes
            fica.valuable = fica.valuable or x.valuable
            fica.recheio_id = fica.recheio_id or x.recheio_id
            ctx.db.delete(x)
        fica.updated = agora()
        return {"removed": len(outros), "quantity": fica.quantity}


def mover_artigos(ctx: Ctx, body: dict) -> int:
    """Move vários artigos de uma vez; sem destino = área temporária."""
    with mutacao(ctx, "mover artigos"):
        ids = [int(i) for i in (body.get("ids") or []) if str(i).lstrip("-").isdigit()]
        if not ids:
            raise GaragemErro("Nenhum artigo escolhido.")
        destino = _local_valido(ctx, body.get("location_id"))
        n = 0
        for i in ids:
            a = _artigo(ctx, i)
            if a:
                a.location_id, a.updated = destino, agora()
                n += 1
        return n


def criar_rascunhos(ctx: Ctx, body: dict) -> int:
    ctx.exigir("editar")
    linhas = body.get("rows") if isinstance(body.get("rows"), list) else [body]
    if len(linhas) > 500:
        raise GaragemErro("Máximo de 500 linhas de cada vez.")
    for r in linhas:
        o = _pick(r or {}, DRAFT_CAMPOS)
        lid = o.get("location_id")
        o["location_id"] = int(lid) if lid not in (None, "") and _loc(ctx, lid) else None   # desconhecida: fica na área temporária
        ctx.db.add(GarRascunho(entidade_id=ctx.ent, created=agora(), **{k: (str(v)[:200] if isinstance(v, str) else v) for k, v in o.items()}))
    ctx.db.commit()
    return len(linhas)


def atualizar_rascunho(ctx: Ctx, id_, body: dict) -> None:
    ctx.exigir("editar")
    r = ctx.db.get(GarRascunho, int(id_)) if str(id_).isdigit() else None
    if r and r.entidade_id == ctx.ent:                   # linha já apagada: sem efeito
        o = _pick(body, DRAFT_CAMPOS)
        if "location_id" in o:
            o["location_id"] = int(o["location_id"]) if o["location_id"] not in (None, "") and _loc(ctx, o["location_id"]) else None
        for k, v in o.items():
            setattr(r, k, v)
        ctx.db.commit()


def apagar_rascunho(ctx: Ctx, id_=None) -> None:
    ctx.exigir("editar")
    q = delete(GarRascunho).where(GarRascunho.entidade_id == ctx.ent)
    if id_ is not None:
        q = q.where(GarRascunho.id == int(id_))
    ctx.db.execute(q)
    ctx.db.commit()


def confirmar_rascunhos(ctx: Ctx, body: dict) -> dict:
    """Cria os artigos a partir das linhas; sem localização (ou desconhecida) ficam na área temporária."""
    with mutacao(ctx, "confirmar pré-registo"):
        ids = [int(i) for i in body["ids"]] if isinstance(body.get("ids"), list) else None
        linhas = [r for r in ctx.db.scalars(select(GarRascunho).where(GarRascunho.entidade_id == ctx.ent).order_by(GarRascunho.id))
                  if ids is None or r.id in ids]
        if not linhas:
            raise GaragemErro("Não há linhas para confirmar.")
        sem_nome = sum(1 for r in linhas if not (r.name or "").strip())
        if sem_nome:
            raise GaragemErro(f"Há {sem_nome} {'linha' if sem_nome == 1 else 'linhas'} sem nome. Preencha-o ou remova a linha.")
        temporarios = 0
        for r in linhas:
            loc = r.location_id if r.location_id and _loc(ctx, r.location_id) else None
            temporarios += loc is None
            ctx.db.add(GarArtigo(entidade_id=ctx.ent, name=r.name.strip(), quantity=r.quantity, category=r.category, notes=r.notes,
                                 location_id=loc, updated=agora()))
            ctx.db.delete(r)
        return {"created": len(linhas), "temporary": temporarios}


# ---------------------------------------------------------------------------------------------------------------------
# etiquetas QR
# ---------------------------------------------------------------------------------------------------------------------
def qr_svg(texto: str) -> str:
    import segno
    buf = io.BytesIO()
    segno.make(texto[:300], error="m").save(buf, kind="svg", scale=4, border=1, xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode("utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# exportar / importar (formato «garagem-export» v1, o mesmo da app autónoma)
# ---------------------------------------------------------------------------------------------------------------------
def exportar(ctx: Ctx, com_fotos: bool = True) -> dict:
    snap = instantaneo(ctx.db, ctx.ent)
    bens = {b.id: b for b in imoveis(ctx.db, ctx.ent)}
    spaces = [{"id": e.id, "name": e.name, "w": e.w, "d": e.d, "h": e.h, **{k: v for k, v in _d_espaco(e, bens).items() if k.startswith("property")}}
              for e in ctx.db.scalars(select(GarEspaco).where(GarEspaco.entidade_id == ctx.ent).order_by(GarEspaco.id))]
    fotos = {}
    if com_fotos:
        for nome in {r["photo"] for r in snap["items"] + snap["locations"] if r.get("photo")}:
            p = caminho_foto(ctx.fotos, nome)
            if p:
                fotos[nome] = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"format": FORMATO, "version": 1, "exported": datetime.now(timezone.utc).isoformat(), "source": "gestor",
            "spaces": spaces, "locations": snap["locations"], "items": snap["items"], "drafts": snap["drafts"], "photos": fotos}


def importar(ctx: Ctx, b: dict, modo: str) -> dict:
    """Importa um ficheiro exportado (da app autónoma ou deste módulo). 'merge' acrescenta (os ids são renumerados);
    'replace' apaga antes tudo o que esta entidade tem (só administradores). Os códigos das etiquetas mantêm-se, salvo
    se já existirem. Os espaços ligam-se aos imóveis (Bens) da entidade pelo nome ou, se for um número, pelo id."""
    if (not isinstance(b, dict) or b.get("format") != FORMATO or b.get("version") != 1
            or not isinstance(b.get("locations"), list) or not isinstance(b.get("items"), list)):
        raise GaragemErro("Ficheiro inválido: não é uma exportação da Garagem (versão 1).")
    substituir = modo == "replace"
    ctx.exigir("admin" if substituir else "editar")
    def num(v):
        try:
            return None if v is None or v == "" else float(v)
        except (TypeError, ValueError):
            return None

    def inteiro(v):
        n = num(v)
        return None if n is None else int(n)

    def foto(v):
        return v if isinstance(v, str) and FOTO_RE.match(v) else None

    # fotos primeiro (ficheiros; se algo falhar adiante ficam órfãs e a limpeza apaga-as)
    ctx.fotos.mkdir(parents=True, exist_ok=True)
    for nome, b64 in (b.get("photos") or {}).items():
        if not FOTO_RE.match(str(nome)) or (ctx.fotos / nome).exists():
            continue
        try:
            dados = base64.b64decode(str(b64), validate=False)
        except Exception:
            continue
        if len(dados) > 100 and dados[:2] == b"\xff\xd8":
            (ctx.fotos / nome).write_bytes(dados)

    contagem = {"spaces": 0, "locations": 0, "items": 0, "drafts": 0}
    sem_imovel: list[str] = []
    codigos_novos = 0
    mapa_esp: dict = {}
    mapa_loc: dict = {}
    with mutacao(ctx, "importar dados", minimo="admin" if substituir else "editar"):
        db = ctx.db
        if substituir:
            _apagar_tudo(db, ctx.ent)
        elif not (db.scalar(select(GarLocal.id).where(GarLocal.entidade_id == ctx.ent)) or
                  db.scalar(select(GarArtigo.id).where(GarArtigo.entidade_id == ctx.ent)) or
                  db.scalar(select(GarRascunho.id).where(GarRascunho.entidade_id == ctx.ent))):
            # entidade ainda vazia: o espaço «Garagem» inicial dá lugar aos espaços do ficheiro
            db.execute(delete(GarEspaco).where(GarEspaco.entidade_id == ctx.ent))
        for s in b.get("spaces") or []:
            if not isinstance(s, dict):
                continue
            corpo = _corpo_espaco(ctx, s, preferir_nome=True)
            if (s.get("property_id") or s.get("property_name")) and corpo["bem_id"] is None:
                sem_imovel.append(corpo["name"])
            e = GarEspaco(entidade_id=ctx.ent, **corpo)
            db.add(e)
            db.flush()
            mapa_esp[s.get("id")] = e.id
            contagem["spaces"] += 1
        garantir = primeiro_espaco(ctx)
        if garantir is None:
            e = GarEspaco(entidade_id=ctx.ent, name="Garagem", w=6, d=5, h=2.5)
            db.add(e)
            db.flush()
            garantir = e.id
        espaco_omissao = next(iter(mapa_esp.values()), garantir)
        pendentes = [l for l in b["locations"] if isinstance(l, dict) and _txt(l.get("name"))]
        while pendentes:
            seguintes = []
            for l in pendentes:
                pai = l.get("parent_id")
                if pai is not None and pai not in mapa_loc:
                    seguintes.append(l)
                    continue
                codigo = (_txt(l.get("code"), 12) or "").upper()
                if not codigo or db.scalar(select(GarLocal.id).where(GarLocal.code == codigo)):
                    codigo, codigos_novos = novo_codigo(db), codigos_novos + (1 if codigo else 0)
                raiz = pai is None
                novo = GarLocal(
                    entidade_id=ctx.ent, parent_id=None if raiz else mapa_loc[pai], name=_txt(l.get("name"), 100),
                    type=_txt(l.get("type"), 20) or "zona", code=codigo, notes=_txt(l.get("notes")), photo=foto(l.get("photo")),
                    grid_rows=inteiro(l.get("grid_rows")), grid_cols=inteiro(l.get("grid_cols")),
                    grid_row=inteiro(l.get("grid_row")), grid_col=inteiro(l.get("grid_col")),
                    plan_x=num(l.get("plan_x")), plan_y=num(l.get("plan_y")), plan_w=num(l.get("plan_w")), plan_h=num(l.get("plan_h")),
                    plan_z=num(l.get("plan_z")), plan_alt=num(l.get("plan_alt")),
                    plan_wall=l.get("plan_wall") if l.get("plan_wall") in PAREDES else None,
                    plan_face=l.get("plan_face") if l.get("plan_face") in FACES else None,
                    space_id=mapa_esp.get(l.get("space_id"), espaco_omissao) if raiz else None)
                db.add(novo)
                db.flush()
                mapa_loc[l.get("id")] = novo.id
                contagem["locations"] += 1
            if len(seguintes) == len(pendentes):
                raise GaragemErro("Hierarquia de localizações inválida (pai em falta ou ciclo).")
            pendentes = seguintes
        for it in b["items"]:
            if not isinstance(it, dict) or not _txt(it.get("name")):
                continue
            lid = it.get("location_id")
            db.add(GarArtigo(entidade_id=ctx.ent, name=_txt(it.get("name"), 200), quantity=_txt(it.get("quantity"), 60),
                             category=_txt(it.get("category"), 80), notes=_txt(it.get("notes")), photo=foto(it.get("photo")),
                             location_id=mapa_loc.get(lid) if lid is not None else None,
                             updated=_txt(it.get("updated"), 19) or agora(), valuable=bool(it.get("valuable")),
                             recheio_id=(inteiro(it.get("recheio_id")) if db.scalar(select(ItemRecheio.id).where(
                                 ItemRecheio.id == inteiro(it.get("recheio_id")), ItemRecheio.entidade_id == ctx.ent)) else None)))
            contagem["items"] += 1
        for d in b.get("drafts") or []:
            if not isinstance(d, dict):
                continue
            lid = d.get("location_id")
            db.add(GarRascunho(entidade_id=ctx.ent, name=_txt(d.get("name"), 200), quantity=_txt(d.get("quantity"), 60),
                               category=_txt(d.get("category"), 80), notes=_txt(d.get("notes")),
                               location_id=mapa_loc.get(lid) if lid is not None else None, created=agora()))
            contagem["drafts"] += 1
    return {"ok": True, "mode": "replace" if substituir else "merge", **contagem,
            "codigos_novos": codigos_novos, "espacos_sem_imovel": sem_imovel}


# ---------------------------------------------------------------------------------------------------------------------
# menu (o mesmo de public/menu-def.js: espaços › zonas, Consulta, Registo, Mapas, Dados)
# ---------------------------------------------------------------------------------------------------------------------
ICONE_TIPO = {"estante": "🗄️", "armario": "🚪", "bancada": "🛠️", "prateleira": "🪜", "caixa": "📦", "gaveta": "🗃️",
              "mezanine": "🏗️", "bike_suspensa": "🚲", "bike_piso": "🅿️", "zona": "📍", "outro": "📌"}


def construir_menu(espacos_: list[dict], locais: list[dict], n_temp: int, n_rascunhos: int, prefixo: str = "",
                   espaco_atual: int | None = None, imovel: str | None = None) -> dict:
    """Menu em dados (espaços › zonas e as secções da app). Os endereços levam `prefixo` antes de «#/…»."""
    def href(h: str) -> str:
        return prefixo + h

    def por_nome(l):
        return sorted(l, key=lambda x: [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", x["name"])])

    esp = [s for s in espacos_ if imovel is None or str(s.get("property_id") or "") == str(imovel)]
    primeiro = espacos_[0]["id"] if espacos_ else None

    def filhos(id_):
        return por_nome([l for l in locais if l["parent_id"] == id_ and l["grid_row"] is None])

    def zona(l):
        return {"key": f"loc:{l['id']}", "title": l["name"], "icon": ICONE_TIPO.get(l["type"], ICONE_TIPO["outro"]),
                "href": href(f"#/loc/{l['id']}"), "type": l["type"],
                "children": [zona(c) for c in filhos(l["id"])] if l["type"] == "mezanine" else []}

    itens_esp = []
    for s in esp:
        raizes = por_nome([l for l in locais if l["parent_id"] is None and (l["space_id"] if l["space_id"] is not None else primeiro) == s["id"]])
        prop = {"id": s.get("property_id"), "name": s.get("property_name")} if s.get("property_id") or s.get("property_name") else None
        itens_esp.append({"key": f"space:{s['id']}", "title": s["name"], "icon": "🏠", "href": href(f"#/espaco/{s['id']}"),
                          "current": s["id"] == espaco_atual, "property": prop,
                          "size": {"w": s["w"], "d": s["d"], "h": s["h"]}, "children": [zona(r) for r in raizes]})
    itens_esp.append({"key": "action:new-space", "title": "Novo espaço", "icon": "＋", "href": href("#/mapa3d"), "action": "new-space", "children": []})
    grupos = [
        {"id": "espacos", "title": "Espaços", "items": itens_esp},
        {"id": "consulta", "title": "Consulta", "items": [
            {"key": "view:home", "title": "Procurar", "icon": "🔍", "href": href("#/"), "children": []},
            {"key": "view:inventario", "title": "Inventário completo", "icon": "📦", "href": href("#/inventario"), "children": []}]},
        {"id": "registo", "title": "Registo", "items": [
            {"key": "view:pre", "title": "Pré-registo", "icon": "📝", "href": href("#/pre"), "badge": n_rascunhos or None, "children": []},
            {"key": "view:temp", "title": "Área temporária", "icon": "📥", "href": href("#/temporaria"), "badge": n_temp or None, "children": []}]},
        {"id": "mapas", "title": "Edição de Mapas", "items": [
            {"key": "view:mapa3d", "title": "Mapa 3D", "icon": "🧊", "href": href("#/mapa3d"), "children": []},
            {"key": "view:mapa2d", "title": "Mapa 2D", "icon": "🗺️", "href": href("#/mapa2d"), "children": []}]},
        {"id": "dados", "title": "Dados", "items": [
            {"key": "view:dados", "title": "Importar / Exportar", "icon": "💾", "href": href("#/dados"), "children": []}]},
    ]
    return {"groups": grupos}


def menu_da_entidade(ctx: Ctx, prefixo: str = "/garagem/", espaco_atual: int | None = None, imovel: str | None = None) -> dict:
    esp = espacos(ctx)
    locais = [{c: getattr(l, c) for c in ("id", "parent_id", "name", "type", "grid_row", "space_id")}
              for l in ctx.db.scalars(select(GarLocal).where(GarLocal.entidade_id == ctx.ent))]
    n_temp = ctx.db.scalar(select(func.count()).select_from(GarArtigo).where(GarArtigo.entidade_id == ctx.ent, GarArtigo.location_id.is_(None)))
    n_rasc = ctx.db.scalar(select(func.count()).select_from(GarRascunho).where(GarRascunho.entidade_id == ctx.ent))
    return construir_menu(esp, locais, n_temp or 0, n_rasc or 0, prefixo, espaco_atual, imovel)


def entidades_garagem(db: Session, u) -> list:
    """As entidades onde o utilizador pode usar a Garagem: só a conta administradora (e só onde o módulo está ligado)."""
    if not u.is_admin:
        return []
    from .deps import entidades_visiveis
    return [(e, p) for e, p in entidades_visiveis(db, u) if e.tem("garagem")]


def menu_para_utilizador(db: Session, u, request) -> dict | None:
    """O grupo «Garagem» do menu do Gestor: None se o utilizador não vê nenhuma entidade com o módulo ligado."""
    visiveis = entidades_garagem(db, u)
    if not visiveis:
        return None
    escolha = request.session.get("garagem_entidade")
    ent, papel = next(((e, p) for e, p in visiveis if e.id == escolha), visiveis[0])
    ctx = Ctx(db=db, ent=ent.id, papel=papel, fotos=Path("."), hist=Historico())
    return {"entidade": ent, "outras": [e for e, _ in visiveis if e.id != ent.id], "menu": menu_da_entidade(ctx, "/garagem/")}
