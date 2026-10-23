"""Manutenção dos documentos do Paperless-ngx pela app (em vez dos scripts PowerShell):

  valores     ← Extrair_Valor_Paperless.ps1   total da fatura pelo texto (OCR) → campo «Valor»; incertos/sem valor
                                              ficam com a etiqueta «Valor por verificar»
  datas       ← Corrigir_Datas_Paperless.ps1  data de criação pela data do próprio texto
  títulos     ← Atualizar_Titulos_Paperless.ps1  títulos em lote a partir de um modelo
  duplicados  ← Analisar_Duplicados_Paperless.ps1  prováveis duplicados de um correspondente (não apaga nada)
  diagnóstico ← Diagnostico_Valor.ps1         porque é que um documento ficou sem valor

Cada ferramenta tem dois passos: «propor» lê e calcula sem alterar nada; «aplicar» altera só os documentos escolhidos
(relendo cada um, para não perder campos nem etiquetas entretanto mudados).
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import paperless
from .formatos import fmt_montante
from .paperless_gestao import listar
from .recibos import de_monetario, monetario

ETIQUETA_REVER = "Valor por verificar"
ETIQUETA_DUPLICADO = "Possível duplicado"
PERIODO, VALOR = "Período", "Valor"
LIMITE = 1000                 # documentos lidos de cada vez (o texto OCR pesa)


@dataclass
class Proposta:
    id: int
    titulo: str
    atual: str
    novo: str = ""            # o que fica (texto para mostrar)
    valor: str = ""           # o que se envia ao aplicar ("" = sem alteração possível)
    detalhe: str = ""
    aviso: bool = False       # incerto: rever antes de aplicar

    @property
    def link(self) -> str:
        return f"{paperless.url_browser()}/documents/{self.id}/details"


@dataclass
class Resultado:
    propostas: list = field(default_factory=list)
    ignorados: int = 0        # já certos / já tinham valor
    lidos: int = 0
    truncado: bool = False


# ------------------------------------------------------------------ ajudas
def _id_por_nome(endpoint: str, nome: str, token: str) -> int | None:
    alvo = nome.strip().lower()
    r = paperless._pedido(f"/api/{endpoint}/", {"name__iexact": nome, "page_size": 100}, token=token)
    return next((x["id"] for x in r.get("results", []) if (x.get("name") or "").strip().lower() == alvo), None)


def _ids_tipos(nomes: list[str], token: str) -> tuple[list[int], list[str]]:
    ids, faltam = [], []
    for n in nomes:
        i = _id_por_nome("document_types", n, token)
        (ids.append(i) if i else faltam.append(n))
    return ids, faltam


def _etiqueta(nome: str, token: str, criar: bool = True) -> int | None:
    i = _id_por_nome("tags", nome, token)
    if i or not criar:
        return i
    return paperless.enviar("POST", "/api/tags/", {"name": nome, "matching_algorithm": 0, "match": ""}, token=token)["id"]


def _documentos(token: str, campos: str, limite: int = LIMITE, **filtros) -> tuple[list[dict], bool]:
    filtros = {k: v for k, v in filtros.items() if v not in (None, "", [])}
    docs = listar("documents", token, fields=campos, ordering="-added", maximo=limite + 1, **filtros)
    return docs[:limite], len(docs) > limite


def _campo(doc: dict, fid: int | None):
    if not fid:
        return None
    return next((c.get("value") for c in doc.get("custom_fields") or [] if c.get("field") == fid), None)


def _doc(pid: int, campos: str, token: str) -> dict:
    return paperless._pedido(f"/api/documents/{pid}/", {"fields": campos}, token=token)


# ------------------------------------------------------------------ valor (total da fatura)
_IMPORTE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.\s ]\d{3})+|\d+),(\d{2})(?!\d)")
_IMPORTE_PONTO = re.compile(r"(?<![\d.,/-])(\d{1,6})\.(\d{2})(?![\d.,])")      # 7.20 — só se a linha não tiver vírgulas
_SO_IMPORTE = re.compile(r"^\s*-?[\d.,\s]+\s*(EUR|€)?\s*$", re.I)
# do mais fiável para o menos fiável; ganha o 1.º nível com resultado (a última ocorrência desse nível)
NIVEIS = [
    (r"total\s+a\s+pagar|total\s+por\s+pagar|valor\s+a\s+pagar|montante\s+a\s+pagar|import[aâ]ncia\s+a\s+pagar|total\s+a\s+d[eé]bit",
     None, "alta", True),
    (r"total\s+(da\s+)?fatura|valor\s+(da\s+)?fatura|total\s+faturado|total\s+geral|valor\s+total|total\s+do\s+documento",
     None, "alta", False),
    (r"total\s+com\s+iva|total\s+c/\s*iva|total\s+iva\s+inclu[ií]do", None, "media", False),
    (r"\btotal\b", r"sem\s+iva|s/\s*iva|sub\s*-?total|\biva\b|\bbase\b|desconto|isento", "baixa", False),
]


def importes(texto: str) -> list[int]:
    """Importes (cêntimos) em formato português (1.234,56 · 1 234,56 · 123,45); com ponto decimal só se não houver outros."""
    out = [int(re.sub(r"[.\s ]", "", m.group(1))) * 100 + int(m.group(2)) for m in _IMPORTE.finditer(texto)]
    if not out:
        out = [int(m.group(1)) * 100 + int(m.group(2)) for m in _IMPORTE_PONTO.finditer(texto)]
    return out


def encontrar_total(conteudo: str | None) -> dict | None:
    """{valor (cêntimos), regra, confianca (alta|media|baixa), linha} ou None."""
    if not conteudo:
        return None
    linhas = conteudo.splitlines()
    for padrao, excluir, conf, para_tras in NIVEIS:
        achado = None
        for i, linha in enumerate(linhas):
            m = re.search(padrao, linha, re.I)
            if not m or (excluir and re.search(excluir, linha, re.I)):
                continue
            valor, c, regra = None, conf, m.group(0).strip()
            na_linha = importes(linha[m.start():])
            if na_linha:
                valor = na_linha[-1]                                   # mesma linha: o último importe
            else:
                for j in (1, 2):                                       # linhas seguintes: o primeiro
                    if i + j < len(linhas) and (seg := importes(linhas[i + j])):
                        valor = seg[0]
                        break
            if valor is None and para_tras:
                # programas de faturação que imprimem a etiqueta e o importe longe (texto fora de ordem):
                # até 25 linhas atrás, uma linha só com UM importe
                for k in range(i - 1, max(-1, i - 26), -1):
                    if _SO_IMPORTE.match(linhas[k]) and len(a := importes(linhas[k])) == 1:
                        valor, c, regra = a[0], "media", f"{regra} (importe noutra linha)"
                        break
            if valor is not None:
                achado = {"valor": valor, "regra": regra, "confianca": c, "linha": linha.strip()}
        if achado:
            return achado
    return None


def propor_valores(token: str, tipos: list[str], desde: date | None = None, reprocessar: bool = False,
                   limite: int = LIMITE) -> Resultado:
    fid = _id_por_nome("custom_fields", VALOR, token)
    if not fid:
        raise paperless.ErroPaperless("O campo «Valor» não existe no Paperless (cria-o com o circuito «Lar» ou «Fatura de fornecedor»).")
    ids, faltam = _ids_tipos(tipos, token)
    if not ids:
        raise paperless.ErroPaperless("Nenhum tipo de documento válido" + (f" ({', '.join(faltam)} não existe)" if faltam else "") + ".")
    docs, truncado = _documentos(token, "id,title,content,custom_fields,tags", limite,
                                 document_type__id__in=",".join(map(str, ids)),
                                 added__date__gte=desde.isoformat() if desde else None)
    res = Resultado(lidos=len(docs), truncado=truncado)
    for d in docs:
        atual = _campo(d, fid)
        if atual and not reprocessar:
            res.ignorados += 1
            continue
        atual_txt = fmt_montante(de_monetario(atual)) if de_monetario(atual) is not None else ""
        t = encontrar_total(d.get("content"))
        if not t:
            res.propostas.append(Proposta(d["id"], d.get("title") or "", atual_txt, "—", "rever",
                                          "valor não encontrado: fica com a etiqueta «Valor por verificar»", True))
            continue
        if atual and de_monetario(atual) == t["valor"]:
            res.ignorados += 1                                         # recalculado e igual: nada a fazer
            continue
        incerto = t["confianca"] != "alta"
        res.propostas.append(Proposta(d["id"], d.get("title") or "", atual_txt, fmt_montante(t["valor"]),
                                      monetario(t["valor"]) + ("|rever" if incerto else ""),
                                      f"confiança {t['confianca']} · «{t['regra']}» · {t['linha'][:90]}", incerto))
    return res


def aplicar_valores(token: str, escolhidos: dict[int, str]) -> tuple[int, list[str]]:
    """escolhidos: id → "EUR12.34", "EUR12.34|rever" ou "rever" (só a etiqueta). PATCH substitui a lista inteira de
    campos: preservam-se os outros."""
    fid = _id_por_nome("custom_fields", VALOR, token)
    tag = _etiqueta(ETIQUETA_REVER, token)
    ok, erros = 0, []
    for pid, v in escolhidos.items():
        try:
            d = _doc(pid, "id,custom_fields,tags", token)
            valor, _, rever = v.partition("|")
            if valor == "rever":
                valor, rever = "", "rever"
            corpo = {}
            if valor:
                corpo["custom_fields"] = [{"field": c["field"], "value": c.get("value")} for c in d.get("custom_fields") or []
                                          if c.get("field") != fid] + [{"field": fid, "value": valor}]
            tags = list(d.get("tags") or [])
            if rever and tag not in tags:
                corpo["tags"] = tags + [tag]
            if corpo:
                paperless.enviar("PATCH", f"/api/documents/{pid}/", corpo, token=token)
            ok += 1
        except paperless.ErroPaperless as e:
            erros.append(f"documento {pid}: {e}")
    return ok, erros


# ------------------------------------------------------------------ datas
_DMY = re.compile(r"(?<!\d)(\d{2})[-/.](\d{2})[-/.](\d{4})(?!\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_ROTULO_DATA = re.compile(r"\bdata(\s+(do\s+documento|de\s+emiss[aã]o|da\s+fatura|doc\.?))?\s*:", re.I)
_IGNORAR = re.compile(r"limite|vencimento|pagar|at[eé]\s|nascimento|d[eé]bito|validade|prazo|reading|leitura", re.I)


def _datas(linha: str, ano_min: int, ate: date) -> list[date]:
    out = []
    for m in _DMY.finditer(linha):
        try:
            out.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    for m in _ISO.finditer(linha):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return [d for d in out if d.year >= ano_min and d <= ate]


def detetar_data(conteudo: str | None, ano_min: int = 2015, hoje: date | None = None) -> tuple[date, str] | None:
    """1) linha «Data:» / «Data do documento» / «Data de emissão» / «Data da fatura»; 2) primeira data plausível,
    ignorando linhas de limite/vencimento/pagamento/débito."""
    if not conteudo:
        return None
    ate = (hoje or date.today()) + timedelta(days=2)
    linhas = conteudo.splitlines()
    for l in linhas:
        if _ROTULO_DATA.search(l) and (d := _datas(l, ano_min, ate)):
            return d[0], "etiqueta «Data:»"
    for l in linhas:
        if not _IGNORAR.search(l) and (d := _datas(l, ano_min, ate)):
            return d[0], "primeira data do texto"
    return None


def propor_datas(token: str, tipos: list[str], desde: date | None = None, ano_min: int = 2015, todos: bool = False,
                 limite: int = LIMITE, hoje: date | None = None) -> Resultado:
    hoje = hoje or date.today()
    filtros = {"added__date__gte": desde.isoformat() if desde else None}
    if tipos:
        ids, faltam = _ids_tipos(tipos, token)
        if not ids:
            raise paperless.ErroPaperless(f"Nenhum tipo de documento válido ({', '.join(faltam)} não existe).")
        filtros["document_type__id__in"] = ",".join(map(str, ids))
    docs, truncado = _documentos(token, "id,title,created,content", limite, **filtros)
    res = Resultado(lidos=len(docs), truncado=truncado)
    for d in docs:
        criado = str(d.get("created") or "")[:10]
        try:
            atual = date.fromisoformat(criado)
        except ValueError:
            atual = None
        invalida = atual is None or atual.year < ano_min or atual > hoje + timedelta(days=2)
        if not invalida and not todos:
            res.ignorados += 1
            continue
        r = detetar_data(d.get("content"), ano_min, hoje)
        if not r:
            res.propostas.append(Proposta(d["id"], d.get("title") or "", criado or "(inválida)", "—", "",
                                          "não encontrei nenhuma data no texto", True))
            continue
        nova, metodo = r
        if atual == nova:
            res.ignorados += 1
            continue
        res.propostas.append(Proposta(d["id"], d.get("title") or "", criado or "(inválida)", nova.strftime("%d/%m/%Y"),
                                      nova.isoformat(), metodo, metodo != "etiqueta «Data:»"))
    return res


def aplicar_datas(token: str, escolhidos: dict[int, str]) -> tuple[int, list[str]]:
    ok, erros = 0, []
    for pid, iso in escolhidos.items():
        try:
            date.fromisoformat(iso)
            d = _doc(pid, "id,created", token)
            # versões recentes: «created» é só a data; antigas: data e hora
            valor = iso if len(str(d.get("created") or "")) <= 10 else f"{iso}T12:00:00"
            paperless.enviar("PATCH", f"/api/documents/{pid}/", {"created": valor}, token=token)
            ok += 1
        except ValueError:
            erros.append(f"documento {pid}: data inválida")
        except paperless.ErroPaperless as e:
            erros.append(f"documento {pid}: {e}")
    return ok, erros


# ------------------------------------------------------------------ títulos
MODELO_TITULO = "{periodo} {tipo} {correspondente}"


def titulo_de(modelo: str, criado: str, periodo: str, tipo: str, correspondente: str) -> str:
    """Marcadores {periodo} {tipo} {correspondente} {ano} {mes}; {periodo} = campo «Período» ou AAAA-MM da data."""
    ano, mes = (criado[:4], criado[5:7]) if len(criado) >= 7 else ("", "")
    periodo = periodo or (f"{ano}-{mes}" if ano else "")
    t = (modelo.replace("{periodo}", periodo).replace("{tipo}", tipo).replace("{correspondente}", correspondente)
         .replace("{ano}", ano).replace("{mes}", mes))
    return re.sub(r"\s+", " ", t).strip()[:128]


def propor_titulos(token: str, modelo: str = MODELO_TITULO, tipos: list[str] | None = None, correspondente: int | None = None,
                   desde: date | None = None, limite: int = LIMITE) -> Resultado:
    from .paperless_gestao import nomes
    filtros = {"added__date__gte": desde.isoformat() if desde else None, "correspondent__id": correspondente}
    if tipos:
        ids, faltam = _ids_tipos(tipos, token)
        if not ids:
            raise paperless.ErroPaperless(f"Nenhum tipo de documento válido ({', '.join(faltam)} não existe).")
        filtros["document_type__id__in"] = ",".join(map(str, ids))
    n_tipos, n_corr = nomes("document_types", token), nomes("correspondents", token)
    fper = _id_por_nome("custom_fields", PERIODO, token)
    docs, truncado = _documentos(token, "id,title,created,document_type,correspondent,custom_fields", limite, **filtros)
    res = Resultado(lidos=len(docs), truncado=truncado)
    for d in docs:
        novo = titulo_de(modelo, str(d.get("created") or ""), str(_campo(d, fper) or ""),
                         n_tipos.get(d.get("document_type"), ""), n_corr.get(d.get("correspondent"), ""))
        atual = d.get("title") or ""
        if not novo or novo == atual:
            res.ignorados += 1
            continue
        res.propostas.append(Proposta(d["id"], atual, atual, novo, novo))
    return res


def aplicar_titulos(token: str, escolhidos: dict[int, str]) -> tuple[int, list[str]]:
    ok, erros = 0, []
    for pid, titulo in escolhidos.items():
        titulo = re.sub(r"\s+", " ", titulo).strip()[:128]
        if not titulo:
            continue
        try:
            paperless.enviar("PATCH", f"/api/documents/{pid}/", {"title": titulo}, token=token)
            ok += 1
        except paperless.ErroPaperless as e:
            erros.append(f"documento {pid}: {e}")
    return ok, erros


# ------------------------------------------------------------------ duplicados
def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", (texto or "").lower())).strip()


def agrupar_duplicados(docs: list[dict], valor_fid: int | None = None, limiar: float = 0.90) -> list[list[dict]]:
    """Grupos de prováveis duplicados (o mais antigo primeiro). Critérios: 1) texto idêntico (normalizado);
    2) mesma data de criação + mesmo Valor; 3) mesma data + texto muito semelhante (Jaccard ≥ limiar) e Valor não
    contraditório. Cada documento leva «motivos»."""
    itens = []
    for d in docs:
        norm = _normalizar(d.get("content") or "")
        itens.append({"id": d["id"], "titulo": d.get("title") or "", "criado": str(d.get("created") or "")[:10],
                      "adicionado": str(d.get("added") or ""), "valor": str(_campo(d, valor_fid) or ""),
                      "ficheiro": d.get("original_file_name") or "", "tags": list(d.get("tags") or []),
                      "hash": hashlib.sha256(norm.encode()).hexdigest() if len(norm) > 100 else "",
                      "palavras": {w for w in norm.split(" ") if len(w) >= 3}, "motivos": []})
    pai = {x["id"]: x["id"] for x in itens}

    def raiz(i):
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    def juntar(grupo: list[dict], motivo: str):
        for x in grupo[1:]:
            a, b = raiz(grupo[0]["id"]), raiz(x["id"])
            if a != b:
                pai[b] = a
        for x in grupo:
            if motivo not in x["motivos"]:
                x["motivos"].append(motivo)

    def por(chave):
        g = {}
        for x in itens:
            k = chave(x)
            if k:
                g.setdefault(k, []).append(x)
        return [v for v in g.values() if len(v) > 1]

    for g in por(lambda x: x["hash"]):
        juntar(g, "texto idêntico")
    for g in por(lambda x: f"{x['criado']}|{x['valor']}" if x["valor"] and x["criado"] else None):
        juntar(g, "mesma data e mesmo valor")
    for g in por(lambda x: x["criado"]):
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                if (a["valor"] and b["valor"] and a["valor"] != b["valor"]) or len(a["palavras"]) < 20 or len(b["palavras"]) < 20:
                    continue
                sim = len(a["palavras"] & b["palavras"]) / len(a["palavras"] | b["palavras"])
                if sim >= limiar:
                    juntar([a, b], f"texto semelhante ({sim:.0%})")
    grupos = {}
    for x in itens:
        grupos.setdefault(raiz(x["id"]), []).append(x)
    out = []
    for g in grupos.values():
        if len(g) > 1:
            g.sort(key=lambda x: x["adicionado"])
            for k, x in enumerate(g):
                x["manter"] = k == 0
                x["link"] = f"{paperless.url_browser()}/documents/{x['id']}/details"
                x["valor_txt"] = fmt_montante(de_monetario(x["valor"])) if de_monetario(x["valor"]) is not None else ""
            out.append(g)
    out.sort(key=lambda g: g[0]["adicionado"])
    return out


def duplicados(token: str, correspondente: int, limiar: float = 0.90) -> tuple[list[list[dict]], int]:
    """(grupos, n.º de documentos analisados) de um correspondente."""
    fid = _id_por_nome("custom_fields", VALOR, token)
    docs = listar("documents", token, correspondent__id=correspondente, ordering="added",
                  fields="id,title,created,added,content,custom_fields,tags,original_file_name")
    return agrupar_duplicados(docs, fid, limiar), len(docs)


def marcar_duplicados(token: str, ids: list[int]) -> tuple[int, list[str]]:
    tag = _etiqueta(ETIQUETA_DUPLICADO, token)
    ok, erros = 0, []
    for pid in ids:
        try:
            tags = list(_doc(pid, "id,tags", token).get("tags") or [])
            if tag not in tags:
                paperless.enviar("PATCH", f"/api/documents/{pid}/", {"tags": tags + [tag]}, token=token)
            ok += 1
        except paperless.ErroPaperless as e:
            erros.append(f"documento {pid}: {e}")
    return ok, erros


# ------------------------------------------------------------------ diagnóstico
def diagnostico(token: str, pid: int) -> dict:
    d = _doc(pid, "id,title,document_type,custom_fields,tags,content,created", token)
    from .paperless_gestao import nomes
    n_campos = nomes("custom_fields", token)
    tipo = nomes("document_types", token).get(d.get("document_type"), "(sem tipo)") if d.get("document_type") else "(sem tipo)"
    texto = d.get("content") or ""
    return {
        "id": d.get("id"), "titulo": d.get("title") or "", "tipo": tipo, "criado": str(d.get("created") or "")[:10],
        "campo_valor": _id_por_nome("custom_fields", VALOR, token),
        "campos": [(n_campos.get(c.get("field"), f"#{c.get('field')}"), c.get("value")) for c in d.get("custom_fields") or []],
        "n_texto": len(texto),
        "linhas": [l for l in texto.splitlines() if re.search(r"total|pagar|valor|montante|d[eé]bit", l, re.I)][:25],
        "inicio": texto[:400], "total": encontrar_total(texto), "data": detetar_data(texto),
        "link": f"{paperless.url_browser()}/documents/{pid}/details",
    }
