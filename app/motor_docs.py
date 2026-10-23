"""Motor de classificação dos documentos do Paperless-ngx (Administração › Paperless › Regras, Revisão, Reclassificar,
Auditoria).

Modelo híbrido:
- no Paperless (nativo) ficam os casos simples e estáveis — correspondência automática dos correspondentes e fluxos de
  entrada (dono, caminho). Uma regra do Gestor sem extração pode ser «publicada» como fluxo nativo;
- no Gestor fica o que o Paperless não faz: extrair atributos do texto (valor, IVA, datas, período, NIF, n.º e ATCUD,
  referência MB) para campos personalizados, condições, confiança, IA de apoio, versões das regras, simulação e
  auditoria (cada campo alterado: antes → depois, regra e versão).

Fluxo de um documento: 1.ª regra ativa (por ordem) cuja condição apanha o texto → alterações propostas. Confiança alta
(regra «alta», atributos todos encontrados com confiança alta, sem conflito com o que o Paperless já atribuiu) → aplica
sozinho no ciclo automático; senão → fila de Revisão. Sem regra → a IA (se ligada) propõe classificação e uma regra nova,
sempre para revisão. Com valor e data, procura o movimento bancário e liga-o (DocumentoLigado) se houver um só.
"""
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import paperless
from .formatos import fmt_montante
from .models import (AlteracaoDocumento, ClassificacaoDocumento, Conta, DocumentoLigado, Movimento, RegraDocumento,
                     RegraDocumentoVersao)
from .paperless_gestao import listar
from .paperless_manutencao import _datas, detetar_data, encontrar_total, importes
from .recibos import de_monetario, monetario

log = logging.getLogger(__name__)

ALGORITMOS = {"qualquer": "Qualquer palavra", "todas": "Todas as palavras", "exato": "Texto exato", "regex": "Expressão regular"}
ALG_PAPERLESS = {"qualquer": 1, "todas": 2, "exato": 3, "regex": 4}
CONFIANCAS = {"alta": "Alta — aplica sozinha", "media": "Média — vai para revisão"}
ESTADOS = {"aplicado": "Aplicado", "revisao": "Por rever", "sem_regra": "Sem regra", "ignorado": "Ignorado", "erro": "Erro"}
CAMPOS_DOC = "id,title,content,created,added,correspondent,document_type,tags,custom_fields"
NIVEL = {"baixa": 0, "media": 1, "alta": 2}


# ------------------------------------------------------------------ atributos
@dataclass
class Atributo:
    chave: str
    valor: str                # o que vai para o Paperless (EUR12.34, AAAA-MM-DD, texto)
    mostrar: str
    confianca: str = "alta"
    detalhe: str = ""


# chave → (rótulo, campo personalizado no Paperless (None = data do documento), tipo do campo)
ATRIBUTOS = {
    "valor": ("Valor total", "Valor", "monetary"),
    "iva": ("IVA", "IVA", "monetary"),
    "data_emissao": ("Data do documento", None, "date"),
    "vencimento": ("Data de vencimento", "Data de vencimento", "date"),
    "periodo": ("Período", "Período", "string"),
    "nif": ("NIF do emitente", "NIF do emitente", "string"),
    "numero": ("N.º do documento", "N.º do documento", "string"),
    "atcud": ("ATCUD", "ATCUD", "string"),
    "referencia_mb": ("Referência MB", "Referência MB", "string"),
}
MESES = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
         "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}


def nif_valido(n: str) -> bool:
    if not re.fullmatch(r"\d{9}", n) or n[0] not in "1235689" and n[:2] not in ("45", "70", "71", "72", "74", "75", "77", "79", "90", "91", "98", "99"):
        return False
    r = sum(int(n[i]) * (9 - i) for i in range(8)) % 11
    return int(n[8]) == (0 if r < 2 else 11 - r)


_RE_IVA = re.compile(r"total\s+(do\s+)?iva|valor\s+(do\s+)?iva|\biva\s*\(?\d{1,2}([.,]\d+)?\s*%", re.I)
_RE_IVA_EXCL = re.compile(r"sem\s+iva|s/\s*iva|c/\s*iva|com\s+iva|inclu", re.I)
_RE_VENC = re.compile(r"data\s+(limite|de\s+vencimento)|vencimento|pagar\s+at[eé]|data\s+de\s+d[eé]bito|prazo\s+de\s+pagamento", re.I)
_RE_PERIODO = re.compile(r"per[ií]odo(\s+de\s+(fatura[cç][aã]o|consumo|refer[eê]ncia))?\s*:?", re.I)
_RE_NIF_ROTULO = re.compile(r"\b(n\.?\s?i\.?\s?f\.?|nipc|contribuinte|vat)\b", re.I)
_RE_NUMERO = re.compile(r"\b((?:FT|FR|FS|NC|ND|RC|RG|FA)\s+[\w.-]+/\d+)\b")
_RE_NUMERO_ROTULO = re.compile(r"fatura(?:-recibo)?\s*(?:n\.?\s?º|n\.?\s?o|nº|n\.|número|numero)\s*:?\s*([A-Z0-9][\w/.-]{3,})", re.I)
_RE_ATCUD = re.compile(r"ATCUD\s*:?\s*([A-Z0-9]{6,}-\d+)", re.I)
_RE_ENT = re.compile(r"entidade\s*:?\s*(\d{5})\b", re.I)
_RE_REF = re.compile(r"refer[eê]ncia\s*:?\s*(\d{3}\s?\d{3}\s?\d{3})\b", re.I)


def _periodo(linhas: list[str], hoje: date) -> Atributo | None:
    ate = hoje + timedelta(days=400)
    for i, l in enumerate(linhas):
        if not _RE_PERIODO.search(l):
            continue
        trecho = " ".join(linhas[i:i + 2])
        ds = _datas(trecho, 2000, ate)
        if len(ds) >= 2:
            return Atributo("periodo", f"{ds[0]:%Y-%m-%d} a {ds[1]:%Y-%m-%d}", f"{ds[0]:%d/%m/%Y} a {ds[1]:%d/%m/%Y}")
        m = re.search(r"(janeiro|fevereiro|mar[cç]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\D{0,4}(\d{4})",
                      trecho, re.I)
        if m:
            mes = MESES[m.group(1).lower()]
            return Atributo("periodo", f"{m.group(2)}-{mes:02d}", f"{m.group(2)}-{mes:02d}")
    for l in linhas:
        m = re.search(r"\bde\s+(\d{2}[-/.]\d{2}[-/.]\d{4})\s+a\s+(\d{2}[-/.]\d{2}[-/.]\d{4})", l, re.I)
        if m and len(ds := _datas(m.group(0), 2000, ate)) == 2:
            return Atributo("periodo", f"{ds[0]:%Y-%m-%d} a {ds[1]:%Y-%m-%d}", f"{ds[0]:%d/%m/%Y} a {ds[1]:%d/%m/%Y}", "media")
    return None


def extrair(texto: str | None, chaves: list[str] | None = None, hoje: date | None = None) -> dict[str, Atributo]:
    """Os atributos pedidos (todos, sem `chaves`) que se encontram no texto."""
    hoje = hoje or date.today()
    texto = texto or ""
    linhas = texto.splitlines()
    chaves = chaves or list(ATRIBUTOS)
    out: dict[str, Atributo] = {}
    if "valor" in chaves and (t := encontrar_total(texto)):
        out["valor"] = Atributo("valor", monetario(t["valor"]), fmt_montante(t["valor"]), t["confianca"], f"«{t['regra']}»")
    if "iva" in chaves:
        for l in reversed(linhas):
            if _RE_IVA.search(l) and not _RE_IVA_EXCL.search(l) and (a := importes(l[_RE_IVA.search(l).end():])):
                out["iva"] = Atributo("iva", monetario(a[-1]), fmt_montante(a[-1]), "media")
                break
    if "data_emissao" in chaves and (d := detetar_data(texto, 2000, hoje)):
        out["data_emissao"] = Atributo("data_emissao", d[0].isoformat(), d[0].strftime("%d/%m/%Y"),
                                       "alta" if d[1].startswith("etiqueta") else "media", d[1])
    if "vencimento" in chaves:
        for i, l in enumerate(linhas):
            if _RE_VENC.search(l):
                ds = _datas(l, 2000, hoje + timedelta(days=730)) or \
                    (_datas(linhas[i + 1], 2000, hoje + timedelta(days=730)) if i + 1 < len(linhas) else [])
                if ds:
                    out["vencimento"] = Atributo("vencimento", ds[-1].isoformat(), ds[-1].strftime("%d/%m/%Y"))
                    break
    if "periodo" in chaves and (p := _periodo(linhas, hoje)):
        out["periodo"] = p
    if "nif" in chaves:
        for l in linhas:
            if _RE_NIF_ROTULO.search(l) or re.search(r"\bPT\s?\d{9}\b", l):
                n = next((x for x in re.findall(r"(?<!\d)(\d{9})(?!\d)", l.replace(" ", "")) if nif_valido(x)), None)
                if n:
                    out["nif"] = Atributo("nif", n, n)
                    break
    if "numero" in chaves:
        m = _RE_NUMERO.search(texto) or _RE_NUMERO_ROTULO.search(texto)
        if m:
            n = re.sub(r"\s+", " ", m.group(1)).strip()
            out["numero"] = Atributo("numero", n, n, "alta" if m.re is _RE_NUMERO else "media")
    if "atcud" in chaves and (m := _RE_ATCUD.search(texto)):
        out["atcud"] = Atributo("atcud", m.group(1).upper(), m.group(1).upper())
    if "referencia_mb" in chaves:
        e, r = _RE_ENT.search(texto), _RE_REF.search(texto)
        if e and r:
            ref = re.sub(r"\D", "", r.group(1))
            v = f"{e.group(1)} {ref[:3]} {ref[3:6]} {ref[6:]}"
            out["referencia_mb"] = Atributo("referencia_mb", v, v)
    return out


# ------------------------------------------------------------------ regras
def _simples(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t)


def condicao(algoritmo: str, padrao: str, texto: str) -> bool:
    padrao = (padrao or "").strip()
    if not padrao:
        return False
    if algoritmo == "regex":
        try:
            return re.search(padrao, texto or "", re.I | re.M) is not None
        except re.error:
            return False
    t, p = _simples(texto), _simples(padrao)
    if algoritmo == "exato":
        return p in t
    palavras = [w for w in p.split(" ") if w]
    if algoritmo == "qualquer":
        return any(re.search(rf"\b{re.escape(w)}\b", t) for w in palavras)
    return all(re.search(rf"\b{re.escape(w)}\b", t) for w in palavras)


def regra_aplica(r: RegraDocumento, doc: dict, ctx: "Contexto") -> bool:
    if not r.ativa or not condicao(r.algoritmo, r.texto, doc.get("content") or ""):
        return False
    if r.so_sem_correspondente and doc.get("correspondent"):
        return False
    if r.so_tipo and (ctx.nome("document_types", doc.get("document_type")) or "").lower() != r.so_tipo.strip().lower():
        return False
    return True


def regra_dados(r: RegraDocumento) -> dict:
    return {k: getattr(r, k) for k in ("nome", "ativa", "ordem", "algoritmo", "texto", "so_tipo", "so_sem_correspondente",
                                       "correspondente", "tipo", "etiquetas", "extrair", "confianca", "nota")}


def guardar_regra(db: Session, r: RegraDocumento, dados: dict, u=None, nota: str = "") -> RegraDocumento:
    """Grava (nova ou alterada): sobe a versão e guarda a cópia no histórico — só se algo mudou."""
    novo = r.id is None
    antes = None if novo else regra_dados(r)
    for k, v in dados.items():
        setattr(r, k, v)
    if not novo and regra_dados(r) == antes:
        return r
    if novo:
        r.versao = 1
        db.add(r)
        db.flush()
    else:
        r.versao = (r.versao or 1) + 1
    r.alterado_em = datetime.now()
    db.add(RegraDocumentoVersao(regra_id=r.id, versao=r.versao, dados=json.dumps(regra_dados(r), ensure_ascii=False),
                                utilizador_id=u.id if u else None, nota=(nota or ("criada" if novo else ""))[:200]))
    return r


def regras_ativas(db: Session) -> list[RegraDocumento]:
    return db.scalars(select(RegraDocumento).where(RegraDocumento.ativa).order_by(RegraDocumento.ordem, RegraDocumento.id)).all()


# ------------------------------------------------------------------ contexto (nomes e ids do Paperless)
class Contexto:
    """Nomes ↔ ids do Paperless, lidos uma vez por lote; cria correspondentes/tipos/etiquetas/campos em falta ao aplicar."""
    ENDPOINTS = ("correspondents", "document_types", "tags", "custom_fields")

    def __init__(self, token: str):
        self.token = token
        self.itens = {ep: {x["id"]: x for x in listar(ep, token)} for ep in self.ENDPOINTS}

    def nome(self, ep: str, oid) -> str | None:
        x = self.itens[ep].get(oid)
        return x.get("name") if x else None

    def id(self, ep: str, nome: str | None) -> int | None:
        alvo = (nome or "").strip().lower()
        return next((i for i, x in self.itens[ep].items() if (x.get("name") or "").strip().lower() == alvo), None) if alvo else None

    def garantir(self, ep: str, nome: str, **extra) -> int:
        i = self.id(ep, nome)
        if i:
            return i
        corpo = {"name": nome.strip(), **({"matching_algorithm": 0, "match": ""} if ep != "custom_fields" else {}), **extra}
        novo = paperless.enviar("POST", f"/api/{ep}/", corpo, token=self.token)
        self.itens[ep][novo["id"]] = novo
        return novo["id"]


# ------------------------------------------------------------------ classificar
@dataclass
class Alteracao:
    campo: str                # correspondente | tipo | etiquetas | data | campo:<nome>
    antes: str
    depois: str
    valor: object             # nome / lista de nomes / AAAA-MM-DD / valor do campo


@dataclass
class Proposta:
    id: int
    titulo: str
    adicionado: str = ""
    origem: str | None = None            # regra | ia | None
    regra_id: int | None = None
    regra_nome: str = ""
    versao: int | None = None
    confianca: str = "baixa"
    alteracoes: list = field(default_factory=list)
    atributos: dict = field(default_factory=dict)      # chave → Atributo (asdict)
    em_falta: list = field(default_factory=list)       # atributos pedidos e não encontrados
    conflitos: list = field(default_factory=list)
    motivo: str = ""
    regra_sugerida: dict | None = None

    @property
    def link(self) -> str:
        return f"{paperless.url_browser()}/documents/{self.id}/details"

    @property
    def automatica(self) -> bool:
        return self.origem == "regra" and self.confianca == "alta" and bool(self.alteracoes)


def _campo_atual(doc: dict, fid: int | None):
    return next((c.get("value") for c in doc.get("custom_fields") or [] if c.get("field") == fid), None) if fid else None


def _mostrar_campo(v, tipo: str) -> str:
    if v in (None, ""):
        return ""
    if tipo == "monetary":
        c = de_monetario(v)
        return fmt_montante(c) if c is not None else str(v)
    if tipo == "date":
        try:
            return date.fromisoformat(str(v)[:10]).strftime("%d/%m/%Y")
        except ValueError:
            return str(v)
    return str(v)


def classificar(doc: dict, regras: list[RegraDocumento], ctx: Contexto, substituir: bool = False,
                hoje: date | None = None) -> Proposta:
    """O que a 1.ª regra que se aplica faria a este documento (nada é alterado). `substituir`: muda também o
    correspondente/tipo/campos já preenchidos (reclassificação forçada); sem ele, um valor diferente já atribuído é um
    conflito e o documento vai para revisão."""
    p = Proposta(doc["id"], doc.get("title") or "", str(doc.get("added") or "")[:10])
    r = next((x for x in regras if regra_aplica(x, doc, ctx)), None)
    if not r:
        p.motivo = "nenhuma regra se aplica"
        p.atributos = {k: asdict(v) for k, v in extrair(doc.get("content"), ["nif"]).items()}   # para as sugestões
        return p
    p.origem, p.regra_id, p.regra_nome, p.versao = "regra", r.id, r.nome, r.versao
    niveis = [NIVEL.get(r.confianca, 1)]

    def propor(campo, ep, nome):
        atual = ctx.nome(ep, doc.get("correspondent" if ep == "correspondents" else "document_type"))
        if not nome or (atual or "").lower() == nome.strip().lower():
            return
        if atual and not substituir:
            p.conflitos.append(f"{campo}: o Paperless já tem «{atual}», a regra diz «{nome}»")
            return
        p.alteracoes.append(Alteracao(campo, atual or "", nome.strip(), nome.strip()))

    propor("correspondente", "correspondents", r.correspondente)
    propor("tipo", "document_types", r.tipo)
    atuais = [ctx.nome("tags", t) or str(t) for t in doc.get("tags") or []]
    novas = [e for e in r.lista_etiquetas if e.lower() not in {a.lower() for a in atuais}]
    if novas:
        p.alteracoes.append(Alteracao("etiquetas", ", ".join(atuais), ", ".join(atuais + novas), novas))
    attrs = extrair(doc.get("content"), r.lista_extrair, hoje)
    p.atributos = {k: asdict(v) for k, v in attrs.items()}
    for chave in r.lista_extrair:
        if chave not in ATRIBUTOS:
            continue
        a = attrs.get(chave)
        if not a:
            p.em_falta.append(ATRIBUTOS[chave][0])
            continue
        niveis.append(NIVEL[a.confianca])
        rotulo, nome_campo, tipo = ATRIBUTOS[chave]
        if nome_campo is None:
            # data do documento: o Paperless preenche-a sempre (muitas vezes com a data de entrada), por isso, se a
            # regra a pede, a do texto ganha
            atual = str(doc.get("created") or "")[:10]
            if atual != a.valor:
                p.alteracoes.append(Alteracao("data", _mostrar_campo(atual, "date"), a.mostrar, a.valor))
            continue
        atual = _campo_atual(doc, ctx.id("custom_fields", nome_campo))
        if atual in (None, "") or substituir:
            if str(atual or "") != a.valor:
                p.alteracoes.append(Alteracao(f"campo:{nome_campo}", _mostrar_campo(atual, tipo), a.mostrar, a.valor))
        elif tipo == "monetary" and de_monetario(atual) != de_monetario(a.valor):
            p.conflitos.append(f"{rotulo}: o documento já tem {_mostrar_campo(atual, tipo)}, o texto diz {a.mostrar}")
    nivel = min(niveis)
    if p.em_falta or p.conflitos:
        nivel = min(nivel, NIVEL["media"])
    p.confianca = {2: "alta", 1: "media", 0: "baixa"}[nivel]
    motivos = []
    if p.em_falta:
        motivos.append("não encontrei: " + ", ".join(p.em_falta))
    motivos += p.conflitos
    if not p.alteracoes and not motivos:
        motivos.append("já está como a regra diz")
    p.motivo = "; ".join(motivos)
    return p


# ------------------------------------------------------------------ aplicar (com auditoria)
def _tipo_campo(nome_campo: str) -> str:
    return next((t for _, n, t in ATRIBUTOS.values() if n == nome_campo), "string")


def aplicar(db: Session, token: str, p: Proposta | dict, ctx: Contexto, lote: str, u=None) -> int:
    """Escreve no Paperless as alterações da proposta (relendo o documento: só junta etiquetas e campos, não apaga
    nenhum) e regista cada campo em AlteracaoDocumento. Devolve o n.º de campos alterados."""
    if isinstance(p, dict):
        p = proposta_de_json(p)
    if not p.alteracoes:
        return 0
    doc = paperless._pedido(f"/api/documents/{p.id}/", {"fields": "id,created,tags,custom_fields"}, token=token)
    corpo: dict = {}
    campos = {c["field"]: c.get("value") for c in doc.get("custom_fields") or []}
    tags = list(doc.get("tags") or [])
    for a in p.alteracoes:
        a = a if isinstance(a, Alteracao) else Alteracao(**a)
        if a.campo == "correspondente":
            corpo["correspondent"] = ctx.garantir("correspondents", a.valor)
        elif a.campo == "tipo":
            corpo["document_type"] = ctx.garantir("document_types", a.valor)
        elif a.campo == "etiquetas":
            for nome in a.valor:
                t = ctx.garantir("tags", nome)
                if t not in tags:
                    tags.append(t)
            corpo["tags"] = tags
        elif a.campo == "data":
            corpo["created"] = a.valor if len(str(doc.get("created") or "")) <= 10 else f"{a.valor}T12:00:00"
        elif a.campo.startswith("campo:"):
            nome = a.campo[6:]
            campos[ctx.garantir("custom_fields", nome, data_type=_tipo_campo(nome))] = a.valor
            corpo["custom_fields"] = [{"field": k, "value": v} for k, v in campos.items()]
    paperless.enviar("PATCH", f"/api/documents/{p.id}/", corpo, token=token)
    for a in p.alteracoes:
        a = a if isinstance(a, Alteracao) else Alteracao(**a)
        db.add(AlteracaoDocumento(paperless_id=p.id, lote=lote, campo=a.campo, antes=a.antes or None, depois=a.depois,
                                  origem=p.origem or "manual", regra_id=p.regra_id, versao_regra=p.versao,
                                  utilizador_id=u.id if u else None))
    return len(p.alteracoes)


def proposta_json(p: Proposta) -> str:
    d = asdict(p)
    return json.dumps(d, ensure_ascii=False, default=str)


def proposta_de_json(d: dict | str) -> Proposta:
    d = json.loads(d) if isinstance(d, str) else dict(d)
    d["alteracoes"] = [Alteracao(**a) if isinstance(a, dict) else a for a in d.get("alteracoes") or []]
    return Proposta(**{k: v for k, v in d.items() if k in Proposta.__dataclass_fields__})


def novo_lote(prefixo: str = "") -> str:
    return f"{prefixo}{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------ ligar ao Gestor (movimento bancário)
def ligar_movimento(db: Session, p: Proposta, titulo: str = "", correspondente: str | None = None) -> tuple[str, Movimento | None]:
    """Com valor e data (do documento ou vencimento): o débito de valor igual entre 3 dias antes da data do documento
    e 40 dias depois da última das datas. Um só → DocumentoLigado. ('ligado'|'já ligado'|'ambíguo'|'sem movimento'|'')."""
    attrs = p.atributos or {}
    valor = de_monetario((attrs.get("valor") or {}).get("valor"))
    datas = [date.fromisoformat(attrs[k]["valor"]) for k in ("data_emissao", "vencimento") if attrs.get(k)]
    if not valor or not datas:
        return "", None
    ja = db.scalar(select(DocumentoLigado).where(DocumentoLigado.paperless_id == p.id, DocumentoLigado.movimento_id.is_not(None)))
    if ja:
        return "já ligado", db.get(Movimento, ja.movimento_id)
    cands = db.scalars(select(Movimento).join(Conta).where(
        Movimento.montante == -valor, Movimento.data_mov.between(min(datas) - timedelta(days=3), max(datas) + timedelta(days=40)))).all()
    if len(cands) != 1:
        return ("ambíguo" if cands else "sem movimento"), None
    m = cands[0]
    db.add(DocumentoLigado(paperless_id=p.id, titulo=(titulo or p.titulo or f"Documento {p.id}")[:200],
                           correspondente=(correspondente or "")[:120] or None, data=min(datas), movimento_id=m.id))
    return "ligado", m


# ------------------------------------------------------------------ IA de apoio
SISTEMA_IA = """Classificas UM documento português digitalizado (texto OCR) para um arquivo Paperless-ngx doméstico.
Dizes quem o emitiu (correspondente: usa EXATAMENTE um nome da lista se for o mesmo, senão um nome curto novo), o tipo
(um da lista ou null), até 3 etiquetas curtas úteis, e os atributos que encontrares: valor (total a pagar, «1234,56»),
data_emissao e vencimento (AAAA-MM-DD), nif (do emitente), numero (n.º da fatura). Propões também uma regra para
reconhecer documentos deste emitente: o texto mais específico e estável (de preferência o NIF), com algoritmo
«exato». Não inventes: o que não souberes fica null. Confiança de 0 a 1.
Responde APENAS com um objeto JSON com as chaves: correspondente, tipo, etiquetas, valor, data_emissao, vencimento, nif,
numero, confianca, motivo, regra (objeto com algoritmo, texto)."""


def ia_propor(doc: dict, ctx: Contexto) -> Proposta:
    from . import ia
    p = Proposta(doc["id"], doc.get("title") or "", str(doc.get("added") or "")[:10], origem="ia", confianca="media")
    corr = sorted(x.get("name") for x in ctx.itens["correspondents"].values())[:400]
    tipos = sorted(x.get("name") for x in ctx.itens["document_types"].values())
    msg = (f"Correspondentes existentes: {', '.join(corr)}\nTipos existentes: {', '.join(tipos)}\n\n"
           f"Título: {doc.get('title')}\nTexto:\n{(doc.get('content') or '')[:6000]}")
    txt = ia._chamar(SISTEMA_IA, msg)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ia.ErroIA("A IA não devolveu JSON.")
    d = json.loads(m.group(0))
    atual_c = ctx.nome("correspondents", doc.get("correspondent"))
    if d.get("correspondente") and (atual_c or "").lower() != d["correspondente"].lower():
        p.alteracoes.append(Alteracao("correspondente", atual_c or "", d["correspondente"], d["correspondente"]))
    atual_t = ctx.nome("document_types", doc.get("document_type"))
    if d.get("tipo") and (atual_t or "").lower() != d["tipo"].lower():
        p.alteracoes.append(Alteracao("tipo", atual_t or "", d["tipo"], d["tipo"]))
    atuais = [ctx.nome("tags", t) or str(t) for t in doc.get("tags") or []]
    novas = [e for e in (d.get("etiquetas") or [])[:3] if e and e.lower() not in {a.lower() for a in atuais}]
    if novas:
        p.alteracoes.append(Alteracao("etiquetas", ", ".join(atuais), ", ".join(atuais + novas), novas))
    v = de_monetario(str(d.get("valor") or "").replace(".", "").replace(",", ".")) if d.get("valor") else None
    attrs = {}
    if v:
        attrs["valor"] = Atributo("valor", monetario(v), fmt_montante(v), "media", "IA")
    for k in ("data_emissao", "vencimento"):
        try:
            if d.get(k):
                dd = date.fromisoformat(str(d[k])[:10])
                attrs[k] = Atributo(k, dd.isoformat(), dd.strftime("%d/%m/%Y"), "media", "IA")
        except ValueError:
            pass
    for k in ("nif", "numero"):
        if d.get(k):
            attrs[k] = Atributo(k, str(d[k]).strip(), str(d[k]).strip(), "media", "IA")
    if attrs.get("nif") and not nif_valido(attrs["nif"].valor):
        del attrs["nif"]
    for k, a in attrs.items():
        _, nome_campo, tipo = ATRIBUTOS[k]
        if nome_campo is None:
            if str(doc.get("created") or "")[:10] != a.valor:
                p.alteracoes.append(Alteracao("data", _mostrar_campo(doc.get("created"), "date"), a.mostrar, a.valor))
        elif _campo_atual(doc, ctx.id("custom_fields", nome_campo)) in (None, ""):
            p.alteracoes.append(Alteracao(f"campo:{nome_campo}", "", a.mostrar, a.valor))
    p.atributos = {k: asdict(a) for k, a in attrs.items()}
    p.motivo = f"IA (confiança {float(d.get('confianca') or 0):.0%}): {d.get('motivo') or ''}".strip()
    regra = d.get("regra") or {}
    if regra.get("texto"):
        p.regra_sugerida = {"nome": d.get("correspondente") or f"Regra do documento {doc['id']}",
                            "algoritmo": regra.get("algoritmo") if regra.get("algoritmo") in ALGORITMOS else "exato",
                            "texto": str(regra["texto"])[:500], "correspondente": d.get("correspondente"),
                            "tipo": d.get("tipo"), "extrair": ",".join(k for k in ("valor", "data_emissao") if k in attrs)}
    return p


# ------------------------------------------------------------------ processar (ciclo e «correr agora»)
@dataclass
class Resumo:
    lidos: int = 0
    aplicados: int = 0
    revisao: int = 0
    sem_regra: int = 0
    ligados: int = 0
    erros: list = field(default_factory=list)


def _registar(db: Session, p: Proposta, estado: str, mov: Movimento | None = None) -> ClassificacaoDocumento:
    c = db.scalar(select(ClassificacaoDocumento).where(ClassificacaoDocumento.paperless_id == p.id)) or \
        ClassificacaoDocumento(paperless_id=p.id)
    c.titulo, c.estado, c.origem, c.regra_id, c.versao_regra = p.titulo[:200], estado, p.origem, p.regra_id, p.versao
    c.confianca, c.motivo, c.proposta, c.processado_em = p.confianca, p.motivo, proposta_json(p), datetime.now()
    try:
        c.adicionado = date.fromisoformat(p.adicionado) if p.adicionado else None
    except ValueError:
        c.adicionado = None
    if mov:
        c.movimento_id = mov.id
    db.add(c)
    return c


def processar(db: Session, token: str, prefs: dict, limite: int = 50, u=None, ids: list[int] | None = None) -> Resumo:
    """Documentos novos (ainda sem classificação no Gestor), adicionados desde «motor_desde» (ou nos últimos 30 dias):
    aplica os de confiança alta, o resto vai para revisão; sem regra → IA (se ligada) ou «sem regra»."""
    from . import ia, preferencias
    res = Resumo()
    regras = regras_ativas(db)
    ctx = Contexto(token)
    if ids:
        docs = [paperless._pedido(f"/api/documents/{i}/", {"fields": CAMPOS_DOC}, token=token) for i in ids]
    else:
        desde = prefs.get("motor_desde") or (date.today() - timedelta(days=30)).isoformat()
        vistos = set(db.scalars(select(ClassificacaoDocumento.paperless_id)))
        docs = [d for d in listar("documents", token, fields=CAMPOS_DOC, ordering="added", added__date__gte=desde,
                                  maximo=limite + len(vistos)) if d["id"] not in vistos][:limite]
    usar_ia = preferencias.ligada(prefs, "motor_ia") and ia.disponivel()
    ligar = preferencias.ligada(prefs, "motor_ligar")
    lote = novo_lote("auto-")
    for d in docs:
        res.lidos += 1
        try:
            p = classificar(d, regras, ctx)
            if not p.origem and usar_ia:
                try:
                    p = ia_propor(d, ctx)
                except Exception as e:          # a IA falhar não pára o ciclo
                    p.motivo = f"nenhuma regra se aplica; IA indisponível ({e})"
            mov = None
            if p.automatica:
                aplicar(db, token, p, ctx, lote, u)
                estado = "aplicado"
                res.aplicados += 1
            elif p.origem and not p.alteracoes and not p.conflitos and not p.em_falta:
                estado = "aplicado"                                  # já estava certo
            elif p.origem:
                estado = "revisao"
                res.revisao += 1
            else:
                estado = "sem_regra"
                res.sem_regra += 1
            if ligar and estado == "aplicado":
                r, mov = ligar_movimento(db, p, d.get("title") or "", ctx.nome("correspondents", d.get("correspondent")))
                res.ligados += r == "ligado"
            _registar(db, p, estado, mov)
            db.commit()
        except paperless.ErroPaperless as e:
            db.rollback()
            res.erros.append(f"documento {d.get('id')}: {e}")
            _registar(db, Proposta(d["id"], d.get("title") or "", motivo=str(e)), "erro")
            db.commit()
    return res


def aprovar(db: Session, token: str, c: ClassificacaoDocumento, prefs: dict, u=None) -> int:
    """Aplica a proposta guardada de um documento da fila de revisão."""
    from . import preferencias
    ctx = Contexto(token)
    p = proposta_de_json(c.proposta or "{}")
    n = aplicar(db, token, p, ctx, novo_lote("revisao-"), u)
    c.estado, c.processado_em = "aplicado", datetime.now()
    if preferencias.ligada(prefs, "motor_ligar"):
        r, mov = ligar_movimento(db, p, c.titulo)
        if mov:
            c.movimento_id = mov.id
    return n


async def ciclo(SessionLocal, defs, prefs) -> None:
    """Corre «processar» de X em X minutos se o motor estiver ligado. Só com o token partilhado
    (GESTOR_PAPERLESS_TOKEN): sem ele, o motor corre com «Correr agora»."""
    import asyncio
    import os
    from . import preferencias
    token = (os.environ.get("GESTOR_PAPERLESS_TOKEN") or "").strip()
    if not paperless.configurado() or not token:
        return
    while True:
        try:
            if preferencias.ligada(prefs(), "motor_ativo"):
                def correr():
                    with SessionLocal() as db:
                        return processar(db, token, prefs())
                r = await asyncio.to_thread(correr)
                if r.lidos:
                    log.info("Motor de documentos: %d lidos, %d aplicados, %d para rever, %d sem regra, %d ligados",
                             r.lidos, r.aplicados, r.revisao, r.sem_regra, r.ligados)
        except Exception:
            log.exception("Falha no motor de classificação dos documentos")
        try:
            minutos = max(5, int(prefs().get("motor_intervalo_minutos") or 30))
        except ValueError:
            minutos = 30
        await asyncio.sleep(minutos * 60)


# ------------------------------------------------------------------ reclassificação em massa
def simular(db: Session, token: str, filtros: dict, substituir: bool = False, regra_id: int | None = None,
            limite: int = 500) -> tuple[list[Proposta], int, bool]:
    """(propostas com alterações ou problemas, n.º de documentos lidos, truncado). Nada é alterado."""
    regras = [r for r in regras_ativas(db) if not regra_id or r.id == regra_id]
    ctx = Contexto(token)
    f = {k: v for k, v in filtros.items() if v not in (None, "")}
    docs = listar("documents", token, fields=CAMPOS_DOC, ordering="-added", maximo=limite + 1, **f)
    out = []
    for d in docs[:limite]:
        p = classificar(d, regras, ctx, substituir)
        if p.origem and (p.alteracoes or p.conflitos or p.em_falta):
            out.append(p)
    return out, min(len(docs), limite), len(docs) > limite


def aplicar_lote(db: Session, token: str, ids: list[int], substituir: bool, regra_id: int | None, u, prefs: dict) -> tuple[int, int, str, list[str]]:
    """Volta a classificar cada documento escolhido (com as regras de agora) e aplica. (documentos, campos, lote, erros)."""
    from . import preferencias
    regras = [r for r in regras_ativas(db) if not regra_id or r.id == regra_id]
    ctx = Contexto(token)
    lote = novo_lote("lote-")
    n_docs = n_campos = 0
    erros = []
    for pid in ids:
        try:
            d = paperless._pedido(f"/api/documents/{pid}/", {"fields": CAMPOS_DOC}, token=token)
            p = classificar(d, regras, ctx, substituir)
            if not p.alteracoes:
                continue
            n_campos += aplicar(db, token, p, ctx, lote, u)
            n_docs += 1
            mov = None
            if preferencias.ligada(prefs, "motor_ligar"):
                _, mov = ligar_movimento(db, p, d.get("title") or "", ctx.nome("correspondents", d.get("correspondent")))
            _registar(db, p, "aplicado", mov)
            db.commit()
        except paperless.ErroPaperless as e:
            db.rollback()
            erros.append(f"documento {pid}: {e}")
    return n_docs, n_campos, lote, erros


# ------------------------------------------------------------------ publicar no Paperless (fluxo nativo)
def publicavel(r: RegraDocumento) -> str | None:
    """None se a regra pode ser um fluxo nativo; senão o motivo."""
    if r.lista_extrair:
        return "extrai atributos (só o Gestor sabe fazer)"
    if r.so_tipo or r.so_sem_correspondente:
        return "tem condições sobre o tipo/correspondente atual"
    if not (r.correspondente or r.tipo or r.lista_etiquetas):
        return "não atribui nada"
    return None


def publicar(r: RegraDocumento, token: str) -> int:
    """Cria (ou recria) o fluxo «Gestor · <nome>» no Paperless: documento adicionado com o texto → atribuições."""
    motivo = publicavel(r)
    if motivo:
        raise paperless.ErroPaperless(f"Esta regra não pode ser um fluxo nativo: {motivo}.")
    ctx = Contexto(token)
    if r.paperless_fluxo_id:
        despublicar(r, token)
    acao = {"type": 1}
    if r.correspondente:
        acao["assign_correspondent"] = ctx.garantir("correspondents", r.correspondente)
    if r.tipo:
        acao["assign_document_type"] = ctx.garantir("document_types", r.tipo)
    if r.lista_etiquetas:
        acao["assign_tags"] = [ctx.garantir("tags", e) for e in r.lista_etiquetas]
    novo = paperless.enviar("POST", "/api/workflows/", {
        "name": f"Gestor · {r.nome}"[:256], "order": r.ordem, "enabled": r.ativa,
        "triggers": [{"type": 2, "sources": [1, 2, 3, 4], "matching_algorithm": ALG_PAPERLESS[r.algoritmo], "match": r.texto,
                      "is_insensitive": True, "filter_filename": None, "filter_path": None, "filter_has_tags": []}],
        "actions": [acao]}, token=token)
    r.paperless_fluxo_id = novo["id"]
    return novo["id"]


def despublicar(r: RegraDocumento, token: str) -> None:
    if r.paperless_fluxo_id:
        try:
            paperless.enviar("DELETE", f"/api/workflows/{r.paperless_fluxo_id}/", {}, token=token)
        except paperless.ErroPaperless as e:
            if "404" not in str(e):
                raise
    r.paperless_fluxo_id = None


# ------------------------------------------------------------------ auditoria (desvio das regras) e sugestões
def _contar(token: str, **filtros) -> int | None:
    try:
        return paperless._pedido("/api/documents/", {"page_size": 1, "fields": "id", **filtros}, token=token).get("count")
    except paperless.ErroPaperless:
        return None


def auditoria(db: Session, token: str | None, dias: int = 90) -> dict:
    hoje = datetime.now()
    estados = dict(db.execute(select(ClassificacaoDocumento.estado, func.count()).group_by(ClassificacaoDocumento.estado)).all())
    por_regra = []
    for r in db.scalars(select(RegraDocumento).order_by(RegraDocumento.ordem, RegraDocumento.id)):
        cs = db.scalars(select(ClassificacaoDocumento).where(ClassificacaoDocumento.regra_id == r.id)).all()
        n = len(cs)
        rever = sum(1 for c in cs if c.estado == "revisao")
        falta = sum(1 for c in cs if c.motivo and "não encontrei" in c.motivo)
        ultima = max((c.processado_em for c in cs), default=None)
        alertas = []
        if n >= 3 and falta / n >= 0.3:
            alertas.append(f"{falta} de {n} sem algum atributo — o layout pode ter mudado; rever a extração")
        if n >= 3 and rever / n >= 0.5:
            alertas.append(f"{rever} de {n} ficaram para rever — conflitos com o Paperless ou confiança média")
        if r.ativa and (hoje - r.criado_em).days > dias and (not ultima or (hoje - ultima).days > dias):
            alertas.append(f"não apanhou nenhum documento nos últimos {dias} dias")
        por_regra.append({"r": r, "n": n, "rever": rever, "falta": falta, "ultima": ultima, "alertas": alertas})
    paperless_n = {}
    if token:
        paperless_n = {"total": _contar(token), "sem_correspondente": _contar(token, correspondent__isnull=1),
                       "sem_tipo": _contar(token, document_type__isnull=1), "inbox": _contar(token, is_in_inbox=1)}
    return {"estados": estados, "por_regra": por_regra, "paperless": paperless_n, "sugestoes": sugestoes(db)}


def sugestoes(db: Session, minimo: int = 2) -> list[dict]:
    """Regras novas propostas a partir dos documentos sem regra: os que partilham o NIF do emitente (e o que a IA
    sugeriu para eles)."""
    grupos: dict[str, list] = {}
    sugeridas: dict[str, dict] = {}
    for c in db.scalars(select(ClassificacaoDocumento).where(ClassificacaoDocumento.estado.in_(("sem_regra", "revisao")),
                                                             ClassificacaoDocumento.regra_id.is_(None))):
        try:
            d = json.loads(c.proposta or "{}")
        except ValueError:
            continue
        nif = ((d.get("atributos") or {}).get("nif") or {}).get("valor")
        if not nif:
            nif = None
        chave = nif or ((d.get("regra_sugerida") or {}).get("texto"))
        if not chave:
            continue
        grupos.setdefault(chave, []).append(c)
        if d.get("regra_sugerida"):
            sugeridas.setdefault(chave, d["regra_sugerida"])
    existentes = {(r.texto or "").strip().lower() for r in db.scalars(select(RegraDocumento))}
    out = []
    for chave, cs in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
        if chave.lower() in existentes or (len(cs) < minimo and chave not in sugeridas):
            continue
        s = sugeridas.get(chave, {})
        out.append({"texto": chave, "algoritmo": s.get("algoritmo", "exato"), "nome": s.get("nome") or f"Emitente {chave}",
                    "correspondente": s.get("correspondente") or "", "tipo": s.get("tipo") or "",
                    "extrair": s.get("extrair") or "valor,data_emissao", "n": len(cs),
                    "exemplos": [c.titulo for c in cs[:3]], "ids": [c.paperless_id for c in cs]})
    return out[:30]
