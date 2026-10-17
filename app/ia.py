"""Sugestões de categoria por IA (Claude API) para padrões de descrição sem categoria.

Só se envia o texto do padrão (ex.: 'ROYAL VERSAILLES') e se é entrada ou saída —
nunca valores, datas, contas, IBAN, nem descrições de transferências/MB WAY (que
têm nomes de pessoas). Cada padrão é perguntado uma vez: a resposta fica na
tabela sugestao_ia.

A chave vem de ANTHROPIC_API_KEY (na NAS: ficheiro ia.env lido pelo update.sh);
o modelo de GESTOR_IA_MODELO (por omissão claude-haiku-5-5).
"""
import json
import os
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Categoria, SugestaoIA

URL = "https://api.anthropic.com/v1/messages"
MODELO_OMISSAO = "claude-haiku-5-5"
LOTE = 60


class ErroIA(RuntimeError):
    pass


def chave_api() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def modelo() -> str:
    return os.environ.get("GESTOR_IA_MODELO") or MODELO_OMISSAO


def disponivel() -> bool:
    return bool(chave_api())


def _chamar(sistema: str, mensagem: str, ferramentas: list | None = None, modelo_ia: str | None = None) -> str:
    """Uma chamada à Messages API; devolve o texto da resposta (junta os blocos de texto)."""
    pedido_json = {"model": modelo_ia or modelo(), "max_tokens": 8000, "system": sistema,
                   "messages": [{"role": "user", "content": mensagem}]}
    if ferramentas:
        pedido_json["tools"] = ferramentas
    corpo = json.dumps(pedido_json).encode()
    pedido = urllib.request.Request(URL, data=corpo, method="POST", headers={
        "x-api-key": chave_api() or "", "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=180 if ferramentas else 90) as r:
            dados = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode(errors="ignore")[:300]
        raise ErroIA(f"A API respondeu {e.code}: {detalhe}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ErroIA(f"Sem ligação à API ({e}).") from e
    return "".join(b.get("text", "") for b in dados.get("content", []) if b.get("type") == "text")


SISTEMA = """És um assistente que identifica comerciantes e serviços a partir das descrições curtas que \
aparecem nos extratos bancários portugueses (ex.: «ROYAL VERSAILLES» é uma pastelaria em Lisboa, \
«GERTAL» é uma empresa de refeições/cantinas, «G ENERGY» é um comercializador de energia).
Para cada descrição dizes o nome provável do comerciante, a atividade (curta, em português) e escolhes \
a categoria mais adequada da lista dada para esse sentido (despesa ou receita). Se não souberes, \
usa categoria_id null e confiança baixa — não inventes.
Responde APENAS com um array JSON, um objeto por descrição, com as chaves: \
i (o número da descrição), comerciante, atividade, categoria_id, confianca (0 a 1)."""


def _lista_categorias(cats: list[Categoria], natureza: str) -> str:
    return "\n".join(f"{c.id}: {c.nome_completo}" for c in cats if c.natureza == natureza)


def _extrair_json(texto: str) -> list:
    ini, fim = texto.find("["), texto.rfind("]")
    if ini < 0 or fim < ini:
        raise ErroIA("A resposta da IA não trouxe JSON.")
    try:
        dados = json.loads(texto[ini:fim + 1])
    except json.JSONDecodeError as e:
        raise ErroIA(f"JSON inválido na resposta da IA ({e}).") from e
    return [d for d in dados if isinstance(d, dict)]


def sugerir(db: Session, grupos: list) -> int:
    """Pergunta à IA pelos grupos (não pessoais) ainda sem resposta em cache. Devolve quantos novos."""
    if not disponivel():
        raise ErroIA("A chave da Claude API não está configurada (ANTHROPIC_API_KEY).")
    em_cache = set(db.scalars(select(SugestaoIA.chave)))
    pendentes = [g for g in grupos if not g.pessoal and g.chave not in em_cache]
    if not pendentes:
        return 0
    cats = db.scalars(select(Categoria)).all()
    validas = {c.id: c.natureza for c in cats}
    novos = 0
    for k in range(0, len(pendentes), LOTE):
        lote = pendentes[k:k + LOTE]
        linhas = "\n".join(f"{i}. [{'despesa' if g.sentido == 'SAIDA' else 'receita'}] {g.padrao}"
                           for i, g in enumerate(lote, start=1))
        mensagem = (f"Categorias de DESPESA (id: nome):\n{_lista_categorias(cats, 'DESPESA')}\n\n"
                    f"Categorias de RECEITA (id: nome):\n{_lista_categorias(cats, 'RECEITA')}\n\n"
                    f"Descrições:\n{linhas}")
        for d in _extrair_json(_chamar(SISTEMA, mensagem)):
            try:
                g = lote[int(d.get("i")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            natureza = "DESPESA" if g.sentido == "SAIDA" else "RECEITA"
            cat = d.get("categoria_id")
            cat = cat if isinstance(cat, int) and validas.get(cat) == natureza else None
            try:
                conf = max(0.0, min(1.0, float(d.get("confianca"))))
            except (TypeError, ValueError):
                conf = None
            db.add(SugestaoIA(chave=g.chave, comerciante=str(d.get("comerciante") or "")[:120] or None,
                              atividade=str(d.get("atividade") or "")[:200] or None,
                              categoria_id=cat, confianca=conf, modelo=modelo()))
            em_cache.add(g.chave)
            novos += 1
        db.flush()
    return novos


def aplicar_cache(db: Session, grupos: list) -> None:
    """Junta aos grupos as respostas já guardadas (a IA só manda se o histórico não sugerir nada)."""
    cache = {s.chave: s for s in db.scalars(select(SugestaoIA).where(SugestaoIA.chave.in_([g.chave for g in grupos])))}
    for g in grupos:
        s = cache.get(g.chave)
        if not s:
            continue
        g.comerciante, g.atividade, g.confianca = s.comerciante, s.atividade, s.confianca
        if s.categoria_id and not g.sugestao_id:
            g.sugestao_id, g.origem = s.categoria_id, "IA"
