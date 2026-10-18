"""Layout da página Início, personalizado por cada utilizador diretamente na página (modo «Personalizar»).

Cada bloco tem uma posição, uma largura (em 12 colunas) e pode estar oculto. Guarda-se na tabela `definicao`, na chave
`inicio_layout.u<id>`, num texto compacto: "topo:12:1,composicao:4:1,…" (id:largura:visível).
"""
from sqlalchemy.orm import Session

from .models import Definicao

# id → (título, largura por omissão), pela ordem por omissão
BLOCOS = {
    "topo": ("Património líquido", 12),
    "composicao": ("Composição dos ativos", 4),
    "detalhe": ("Detalhe dos ativos", 8),
    "evolucao": ("Evolução", 6),
    "agenda": ("Agenda", 6),
    "entidades": ("Património por entidade", 6),
}
LARGURAS = {3: "¼", 4: "⅓", 6: "½", 8: "⅔", 12: "Inteira"}


def chave(utilizador_id: int) -> str:
    return f"inicio_layout.u{utilizador_id}"


def omissao() -> list[dict]:
    return [{"id": b, "w": w, "on": True} for b, (_, w) in BLOCOS.items()]


def normalizar(itens) -> list[dict]:
    """Aceita uma lista de {id, w, on}; ignora blocos desconhecidos e repetidos, junta no fim os que faltam."""
    out, vistos = [], set()
    for i in itens or []:
        if not isinstance(i, dict):
            continue
        b = str(i.get("id", ""))
        if b not in BLOCOS or b in vistos:
            continue
        try:
            w = int(i.get("w"))
        except (TypeError, ValueError):
            w = BLOCOS[b][1]
        out.append({"id": b, "w": w if w in LARGURAS else BLOCOS[b][1], "on": bool(i.get("on", True))})
        vistos.add(b)
    out += [{"id": b, "w": w, "on": True} for b, (_, w) in BLOCOS.items() if b not in vistos]
    return out


def _de_texto(txt: str) -> list[dict]:
    itens = []
    for parte in (txt or "").split(","):
        campos = parte.split(":")
        if len(campos) == 3:
            itens.append({"id": campos[0], "w": campos[1], "on": campos[2] == "1"})
    return normalizar(itens)


def _para_texto(itens: list[dict]) -> str:
    return ",".join(f"{i['id']}:{i['w']}:{1 if i['on'] else 0}" for i in normalizar(itens))


def ler(db: Session, utilizador_id: int) -> list[dict]:
    d = db.get(Definicao, chave(utilizador_id))
    return _de_texto(d.valor) if d else omissao()


def personalizado(db: Session, utilizador_id: int) -> bool:
    return (db.get(Definicao, chave(utilizador_id)) or db.get(Definicao, chave_colunas(utilizador_id))) is not None


def gravar(db: Session, utilizador_id: int, itens: list[dict] | None) -> None:
    """Grava o layout; None (ou igual ao de omissão) volta ao de omissão."""
    d = db.get(Definicao, chave(utilizador_id))
    texto = _para_texto(itens) if itens is not None else None
    if texto is None or texto == _para_texto(omissao()):
        if d:
            db.delete(d)
        return
    if d:
        d.valor = texto
    else:
        db.add(Definicao(chave=chave(utilizador_id), valor=texto))


# ------------------------------------------------------------------ colunas dos cartões em cada grupo do «Detalhe dos ativos»
GRUPOS_DETALHE = ("bancarios", "investimentos", "imoveis", "carros", "passivos")
COLUNAS = (1, 2, 3, 4)                       # sem valor = automático (cabem os que couberem)


def chave_colunas(utilizador_id: int) -> str:
    return f"inicio_colunas.u{utilizador_id}"


def normalizar_colunas(dados) -> dict[str, int]:
    out = {}
    for g, n in (dados or {}).items() if isinstance(dados, dict) else []:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if g in GRUPOS_DETALHE and n in COLUNAS:
            out[g] = n
    return out


def ler_colunas(db: Session, utilizador_id: int) -> dict[str, int]:
    d = db.get(Definicao, chave_colunas(utilizador_id))
    if not d:
        return {}
    return normalizar_colunas(dict(p.split(":", 1) for p in d.valor.split(",") if p.count(":") == 1))


def gravar_colunas(db: Session, utilizador_id: int, dados) -> None:
    """dados: {grupo: n.º de colunas}; vazio (ou None) = automático em todos."""
    cols = normalizar_colunas(dados)
    d = db.get(Definicao, chave_colunas(utilizador_id))
    if not cols:
        if d:
            db.delete(d)
        return
    texto = ",".join(f"{g}:{n}" for g, n in cols.items())
    if d:
        d.valor = texto
    else:
        db.add(Definicao(chave=chave_colunas(utilizador_id), valor=texto))
