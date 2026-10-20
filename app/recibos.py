"""Recibos de vencimento no Paperless → valores nos campos personalizados → ligação aos créditos do vencimento.

Para cada documento do tipo «Recibo de vencimento» (com o token do dono — os recibos só são visíveis para ele):
  1. lê o texto (o campo «content» do Paperless; os recibos chegam sem password, com a camada de texto do PDF);
  2. extrai Período, Bruto, Descontos, Cartão refeição, Líquido e Abonos (formato português 1.234,56);
  3. valida (0,01 €): Bruto − Descontos − Cartão = Líquido e Abonos = Bruto − Cartão;
  4. grava os campos personalizados (PATCH com os campos que o documento já tinha + os novos) ou, se falhar, junta a
     etiqueta «Valor por verificar» e diz porquê;
  5. procura o crédito do Líquido na conta (valor exato, perto do fim do mês do Período) e liga o recibo ao movimento.
Idempotente: os documentos que já têm «Líquido» ficam como estão (a não ser com «forçar»). Há modo ensaio.

Regras de extração (iguais nos dois formatos: «Aviso de Crédito / Débito» de 2025 e «Aviso de Crédito» de 2024):
  - Período: «<mês> de <aaaa>» (ex.: «dezembro de 2025») → 2025-12; senão o AAAAMM mais frequente nas tabelas.
  - Abonos, Descontos, Líquido: no bloco Acumulados, a palavra seguida do importe NA MESMA LINHA, depois da palavra
    («CDEF 30.571,80 458,62 Abonos 3.803,98»); ignora «2.490,40€ Rendimento Líquido Colaborador» (importe antes).
  - Cartão refeição: a linha com «CARTAO REFEICAO» e «TRANSFERIR», o último importe, em valor absoluto. Sem essa linha
    (meses de férias, recibos de prémio) o cartão é 0 — aceite só se as contas baterem certo.
  - Bruto: soma dos Montantes (último importe de cada linha) da tabela Abonos, entre o cabeçalho «Abonos» e o
    cabeçalho «Descontos», SEM a linha do cartão. Há linhas negativas que não são o cartão (acertos do subsídio de
    almoço nas férias, regularizações): por isso não é «a soma dos positivos» — é Abonos + Cartão.
Privacidade: o registo só tem id, período, valores e o resultado; nunca o texto, nome, NIF, NISS ou IBAN.
"""
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from . import paperless

log = logging.getLogger(__name__)

TIPO = "Recibo de vencimento"
ETIQUETA_REVER = "Valor por verificar"
CAMPOS = {"periodo": "Período", "bruto": "Bruto", "descontos": "Descontos", "liquido": "Líquido", "cartao": "Cartão refeição"}
IMPORTE = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
MESES = {m: i + 1 for i, m in enumerate(["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
                                         "setembro", "outubro", "novembro", "dezembro"])}
TOLERANCIA = 1        # cêntimo
TEXTO_MINIMO = 200


def normalizar(texto: str) -> list[str]:
    """Minúsculas, sem acentos, espaços colapsados — linha a linha."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return [re.sub(r"[ \t]+", " ", l).strip() for l in t.splitlines()]


def cent(txt: str) -> int:
    return int((Decimal(txt.replace(".", "").replace(",", ".")) * 100).to_integral_value())


@dataclass
class Recibo:
    periodo: str | None = None          # «2025-12»
    bruto: int | None = None            # cêntimos
    descontos: int | None = None
    cartao: int | None = None
    liquido: int | None = None
    abonos: int | None = None
    sem_cartao: bool = False
    erros: list = field(default_factory=list)

    @property
    def valido(self) -> bool:
        return not self.erros


def _depois_da_palavra(linhas: list[str], palavra: str) -> int | None:
    """«… Líquido 2.290,37» — o importe tem de vir DEPOIS da palavra, na mesma linha."""
    rx = re.compile(rf"\b{palavra}\s+({IMPORTE})(?![\d,])")
    for l in linhas:
        m = rx.search(l)
        if m:
            return cent(m.group(1))
    return None


def extrair(texto: str) -> Recibo:
    r = Recibo()
    linhas = normalizar(texto)
    if len("".join(linhas)) < TEXTO_MINIMO:
        r.erros.append("texto curto ou vazio (PDF protegido ou sem OCR)")
        return r
    # Período
    for l in linhas:
        m = re.search(r"\b(" + "|".join(MESES) + r") de (\d{4})\b", l)
        if m:
            r.periodo = f"{m.group(2)}-{MESES[m.group(1)]:02d}"
            break
    if not r.periodo:
        aaaamm = Counter(t for l in linhas for t in re.findall(r"\b(20\d{2})(0[1-9]|1[0-2])\b", l))
        if aaaamm:
            a, m = aaaamm.most_common(1)[0][0]
            r.periodo = f"{a}-{m}"
    # Acumulados
    r.abonos = _depois_da_palavra(linhas, "abonos")
    r.descontos = _depois_da_palavra(linhas, "descontos")
    r.liquido = _depois_da_palavra(linhas, "liquido")
    # Tabela Abonos: entre os cabeçalhos «Abonos» e «Descontos» (linhas só com a palavra)
    try:
        ini = next(i for i, l in enumerate(linhas) if l == "abonos")
        fim = next(i for i, l in enumerate(linhas) if i > ini and l == "descontos")
    except StopIteration:
        ini = fim = None
    if ini is None:
        r.erros.append("não encontrei a tabela de Abonos")
    else:
        bruto, cartao = 0, None
        for l in linhas[ini + 1:fim]:
            importes = re.findall(IMPORTE, l)
            if not importes:
                continue
            montante = cent(importes[-1])
            if "cartao refeicao" in l and "transferir" in l:
                cartao = abs(montante)
            else:
                bruto += montante
        r.sem_cartao = cartao is None
        r.cartao = cartao or 0
        r.bruto = bruto
    for nome, v in (("Período", r.periodo), ("Abonos", r.abonos), ("Descontos", r.descontos), ("Líquido", r.liquido),
                    ("Bruto", r.bruto)):
        if v is None:
            r.erros.append(f"falta {nome}")
    if not r.erros:
        if abs(r.bruto - r.descontos - r.cartao - r.liquido) > TOLERANCIA:
            r.erros.append("Bruto − Descontos − Cartão ≠ Líquido")
        if abs(r.abonos - (r.bruto - r.cartao)) > TOLERANCIA:
            r.erros.append("Abonos ≠ Bruto − Cartão")
    return r


# ------------------------------------------------------------------ Paperless
def monetario(c: int) -> str:
    return f"EUR{c / 100:.2f}"


def de_monetario(v) -> int | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return round(float(m.group(0)) * 100) if m else None


@dataclass
class Linha:
    id: int
    titulo: str
    recibo: Recibo | None = None
    resultado: str = ""            # gravado | ja_tinha | por_verificar | ensaio
    motivo: str = ""
    movimento: object = None       # Movimento ligado
    ligacao: str = ""              # ligado | sem movimento | ambíguo | já ligado
    candidatos: int = 0


def _ids(endpoint: str, token: str) -> dict[str, int]:
    r = paperless._pedido(f"/api/{endpoint}/", {"page_size": 1000}, token=token)
    return {x["name"]: x["id"] for x in r.get("results", [])}


def _documentos(tipo_id: int, token: str) -> list[dict]:
    out, pagina = [], 1
    while True:
        r = paperless._pedido("/api/documents/", {"document_type__id": tipo_id, "page_size": 50, "page": pagina,
                                                  "fields": "id,title,content,custom_fields,tags,created"}, token=token)
        out += r.get("results", [])
        if not r.get("next"):
            return out
        pagina += 1


def _etiqueta_rever(token: str, ensaio: bool) -> int | None:
    tags = _ids("tags", token)
    if ETIQUETA_REVER in tags:
        return tags[ETIQUETA_REVER]
    if ensaio:
        return None
    return paperless.enviar("POST", "/api/tags/", {"name": ETIQUETA_REVER, "matching_algorithm": 0, "match": ""}, token=token)["id"]


def processar(token: str, *, ensaio: bool = False, forcar: bool = False) -> list[Linha]:
    """Extrai e grava os valores de todos os recibos visíveis com este token. Devolve uma linha por documento."""
    tipos = _ids("document_types", token)
    if TIPO not in tipos:
        raise paperless.ErroPaperless(f"Não há o tipo de documento «{TIPO}» no Paperless (Administração › Paperless).")
    campos = _ids("custom_fields", token)
    em_falta = [n for n in CAMPOS.values() if n not in campos]
    if em_falta:
        raise paperless.ErroPaperless(f"Faltam campos personalizados no Paperless: {', '.join(em_falta)}.")
    id_campo = {k: campos[n] for k, n in CAMPOS.items()}
    rever = None
    out = []
    for d in _documentos(tipos[TIPO], token):
        linha = Linha(d["id"], d.get("title") or "")
        out.append(linha)
        atuais = {c["field"]: c.get("value") for c in d.get("custom_fields") or []}
        r = linha.recibo = extrair(d.get("content") or "")
        if atuais.get(id_campo["liquido"]) not in (None, "") and not forcar:
            linha.resultado = "ja_tinha"
            continue
        if ensaio:
            linha.resultado = "ensaio"
            linha.motivo = "; ".join(r.erros)
            continue
        if r.valido:
            novos = {id_campo["periodo"]: r.periodo, id_campo["bruto"]: monetario(r.bruto),
                     id_campo["descontos"]: monetario(r.descontos), id_campo["liquido"]: monetario(r.liquido),
                     id_campo["cartao"]: monetario(r.cartao)}
            # o PATCH substitui a lista toda: os campos que o documento já tinha + os novos
            lista = [{"field": f, "value": v} for f, v in atuais.items() if f not in novos]
            lista += [{"field": f, "value": v} for f, v in novos.items()]
            corpo = {"custom_fields": lista}
            if rever is None:
                rever = _ids("tags", token).get(ETIQUETA_REVER)
            if rever and rever in (d.get("tags") or []):
                corpo["tags"] = [t for t in d["tags"] if t != rever]
            paperless.enviar("PATCH", f"/api/documents/{d['id']}/", corpo, token=token)
            linha.resultado = "gravado"
        else:
            rever = rever or _etiqueta_rever(token, ensaio)
            if rever and rever not in (d.get("tags") or []):
                paperless.enviar("PATCH", f"/api/documents/{d['id']}/", {"tags": (d.get("tags") or []) + [rever]}, token=token)
            linha.resultado, linha.motivo = "por_verificar", "; ".join(r.erros)
        log.info("recibo %s %s bruto=%s descontos=%s cartao=%s liquido=%s → %s", d["id"], r.periodo, r.bruto, r.descontos,
                 r.cartao, r.liquido, linha.resultado)
    return out


# ------------------------------------------------------------------ ligação aos créditos do vencimento
def _fim_do_mes(periodo: str) -> date:
    a, m = map(int, periodo.split("-"))
    return date(a + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def ligar(db, entidade_ids: list[int], linhas: list[Linha], u, *, dias_antes: int = 12, dias_depois: int = 12,
          ensaio: bool = False) -> None:
    """Para cada recibo com valores: o crédito de valor igual ao Líquido entre (fim do mês − dias_antes) e
    (fim do mês + dias_depois). Um só → ligado (DocumentoLigado); nenhum → sem movimento; vários → ambíguo."""
    from sqlalchemy import select
    from .models import Conta, DocumentoLigado, Movimento
    for l in linhas:
        r = l.recibo
        if not r or not r.valido or not r.periodo:
            continue
        ja = db.scalar(select(DocumentoLigado).where(DocumentoLigado.paperless_id == l.id,
                                                     DocumentoLigado.movimento_id.is_not(None)))
        if ja:
            l.movimento, l.ligacao = db.get(Movimento, ja.movimento_id), "já ligado"
            continue
        fim = _fim_do_mes(r.periodo)
        cands = db.scalars(select(Movimento).join(Conta).where(
            Conta.entidade_id.in_(entidade_ids), Movimento.montante == r.liquido,
            Movimento.data_mov.between(fim - timedelta(days=dias_antes), fim + timedelta(days=dias_depois)))).all()
        l.candidatos = len(cands)
        if len(cands) == 1:
            l.movimento, l.ligacao = cands[0], "ligado"
            if not ensaio:
                db.add(DocumentoLigado(paperless_id=l.id, titulo=(l.titulo or f"Recibo {r.periodo}")[:200],
                                       correspondente=None, data=fim, movimento_id=cands[0].id, utilizador_id=u.id if u else None))
        else:
            l.ligacao = "sem movimento" if not cands else "ambíguo"


# ------------------------------------------------------------------ linha de comandos
def main(argv=None) -> int:
    """python -m app.recibos [--dry-run] [--force] [--sem-ligacao] [--dias N]
    O token vem de GESTOR_PAPERLESS_TOKEN (variável de ambiente, nunca no código); a ligação usa a base de dados do Gestor."""
    import argparse
    import os
    p = argparse.ArgumentParser(prog="python -m app.recibos", description="Valores dos recibos de vencimento no Paperless.")
    p.add_argument("--dry-run", action="store_true", help="ensaio: mostra o que faria, não grava nada")
    p.add_argument("--force", action="store_true", help="volta a ler os recibos que já têm Líquido")
    p.add_argument("--sem-ligacao", action="store_true", help="não liga os recibos aos movimentos")
    p.add_argument("--dias", type=int, default=12, help="janela (dias) à volta do fim do mês, para a ligação")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    token = os.environ.get("GESTOR_PAPERLESS_TOKEN", "")
    if not paperless.configurado() or not token:
        print("Falta GESTOR_PAPERLESS_URL ou GESTOR_PAPERLESS_TOKEN.")
        return 2
    linhas = processar(token, ensaio=a.dry_run, forcar=a.force)
    if not a.sem_ligacao:
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from .config import carregar_definicoes
        from .db import criar_engine
        from .models import Entidade
        with Session(criar_engine(carregar_definicoes().db_path)) as db:
            ligar(db, list(db.scalars(select(Entidade.id))), linhas, None, dias_antes=a.dias, dias_depois=a.dias,
                  ensaio=a.dry_run)
            db.commit()
    for l in sorted(linhas, key=lambda x: (x.recibo.periodo or "", x.id)):
        r = l.recibo
        print(f"#{l.id} {r.periodo or '?'} bruto={r.bruto} descontos={r.descontos} cartao={r.cartao} liquido={r.liquido}"
              f" → {l.resultado}{' (' + l.motivo + ')' if l.motivo else ''}{' · ' + l.ligacao if l.ligacao else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
