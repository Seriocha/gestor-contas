"""Gestão do Paperless-ngx pela Administração do Gestor: correspondentes, fluxos de trabalho e email.

Substitui o que se fazia à mão no Paperless ou com os scripts PowerShell (Rever_Mail_Paperless.ps1). Tudo pela API REST
com o token escolhido na página; o Paperless continua a decidir o que esse token pode ver e alterar.
"""
from dataclasses import dataclass, field

from . import paperless

ALGORITMOS = {0: "Nenhum", 1: "Qualquer palavra", 2: "Todas as palavras", 3: "Texto exato", 4: "Expressão regular",
              5: "Aproximado", 6: "Automático"}
GATILHOS = {1: "Consumo iniciado", 2: "Documento adicionado", 3: "Documento atualizado", 4: "Agendado"}
ORIGENS = {1: "pasta", 2: "API", 3: "email", 4: "interface web"}
TIPOS_ACAO = {1: "Atribuição", 2: "Remoção", 3: "Email", 4: "Webhook"}
TIPOS_CONTA = {1: "IMAP", 2: "Gmail OAuth", 3: "Outlook OAuth"}
SEGURANCA = {0: "sem", 1: "SSL", 2: "STARTTLS"}
ACOES_EMAIL = {1: "apagar", 2: "mover para pasta", 3: "marcar como lido", 4: "marcar com bandeira", 5: "atribuir etiqueta"}
ESCOPOS = {1: "só anexos", 2: "só o email", 3: "email e anexos"}


# ------------------------------------------------------------------ API
def listar(endpoint: str, token: str, maximo: int | None = None, **params) -> list[dict]:
    """Todos os resultados (até `maximo`), página a página (o «next» do Paperless traz o endereço interno; usa-se o
    n.º da página)."""
    out, pagina = [], 1
    while True:
        r = paperless._pedido(f"/api/{endpoint}/", {"page_size": 100, **params, "page": pagina}, token=token)
        out += r.get("results", [])
        if not r.get("next") or pagina >= 200 or (maximo and len(out) >= maximo):
            return out[:maximo] if maximo else out
        pagina += 1


def nomes(endpoint: str, token: str) -> dict[int, str]:
    return {x["id"]: x.get("name") or "" for x in listar(endpoint, token)}


def apagar(endpoint: str, oid: int, token: str) -> None:
    paperless.enviar("DELETE", f"/api/{endpoint}/{oid}/", {}, token=token)


def alterar(endpoint: str, oid: int, corpo: dict, token: str) -> dict:
    return paperless.enviar("PATCH", f"/api/{endpoint}/{oid}/", corpo, token=token)


# ------------------------------------------------------------------ correspondentes
def correspondentes(token: str) -> list[dict]:
    lista = listar("correspondents", token, ordering="name")
    for c in lista:
        c["algoritmo"] = ALGORITMOS.get(c.get("matching_algorithm"), str(c.get("matching_algorithm")))
        c["n"] = c.get("document_count") or 0
        c["ultimo"] = (c.get("last_correspondence") or "")[:10]
    return lista


def guardar_correspondente(token: str, cid: int | None, nome: str, algoritmo: int, texto: str, insensivel: bool) -> dict:
    corpo = {"name": nome.strip(), "matching_algorithm": algoritmo, "match": texto.strip(), "is_insensitive": insensivel}
    if cid:
        return alterar("correspondents", cid, corpo, token)
    return paperless.enviar("POST", "/api/correspondents/", corpo, token=token)


def fundir_correspondentes(token: str, origem: int, destino: int) -> int:
    """Passa os documentos de `origem` para `destino` e apaga `origem`. Devolve o n.º de documentos mudados."""
    if origem == destino:
        raise paperless.ErroPaperless("Escolhe dois correspondentes diferentes.")
    ids = [d["id"] for d in listar("documents", token, correspondent__id=origem, fields="id")]
    if ids:
        paperless.enviar("POST", "/api/documents/bulk_edit/", {
            "documents": ids, "method": "set_correspondent", "parameters": {"correspondent": destino}}, token=token)
    apagar("correspondents", origem, token)
    return len(ids)


# ------------------------------------------------------------------ fluxos de trabalho
@dataclass
class Fluxo:
    id: int
    nome: str
    ordem: int
    ativo: bool
    gatilhos: list = field(default_factory=list)     # [dict] com «resumo» e «curto»
    acoes: list = field(default_factory=list)        # [dict] com «resumo» e «partes» [(rótulo, valor, em falta)]
    avisos: list = field(default_factory=list)

    @property
    def do_gestor(self) -> bool:
        """Publicado a partir de uma regra do motor de classificação (Administração › Paperless › Regras)."""
        return self.nome.startswith("Gestor · ")

    @property
    def partes(self) -> list:
        return [x for a in self.acoes for x in a["partes"]]


def _nome(mapa: dict, oid) -> tuple[str, bool]:
    """(nome, em falta): um id que já não existe no Paperless aparece como «#id» e é um aviso."""
    return (mapa[oid], False) if oid in mapa else (f"#{oid}", True)


def fluxos(token: str) -> list[Fluxo]:
    n = {ep: nomes(ep, token) for ep in ("tags", "correspondents", "document_types", "storage_paths", "custom_fields")}
    try:
        utilizadores = {u["id"]: u.get("username") or "" for u in listar("users", token)}
    except paperless.ErroPaperless:      # o token pode não ter permissão para ver os utilizadores
        utilizadores = None
    out = []
    for w in sorted(listar("workflows", token), key=lambda w: (w.get("order") or 0, w.get("name") or "")):
        f = Fluxo(w["id"], w.get("name") or "", w.get("order") or 0, bool(w.get("enabled")))
        for t in w.get("triggers") or []:
            partes = [GATILHOS.get(t.get("type"), f"tipo {t.get('type')}")]
            if t.get("sources"):
                partes.append("origem: " + ", ".join(ORIGENS.get(s, str(s)) for s in t["sources"]))
            if t.get("matching_algorithm"):
                partes.append(f"{ALGORITMOS.get(t['matching_algorithm'], '?').lower()}: «{t.get('match') or ''}»")
            if t.get("filter_filename"):
                partes.append(f"ficheiro «{t['filter_filename']}»")
            if t.get("filter_path"):
                partes.append(f"caminho «{t['filter_path']}»")
            if t.get("filter_has_tags"):
                partes.append("com etiquetas " + ", ".join(_nome(n["tags"], i)[0] for i in t["filter_has_tags"]))
            filtro = (f"«{t.get('match')}»" if t.get("matching_algorithm") and t.get("match") else
                      f"ficheiro «{t['filter_filename']}»" if t.get("filter_filename") else
                      f"caminho «{t['filter_path']}»" if t.get("filter_path") else
                      "com etiquetas" if t.get("filter_has_tags") else "qualquer documento")
            f.gatilhos.append({**t, "resumo": " · ".join(partes), "curto": filtro,
                               "tipo_txt": GATILHOS.get(t.get("type"), f"tipo {t.get('type')}"),
                               "tudo": filtro == "qualquer documento" and t.get("type") in (1, 2, 3)})
        for a in w.get("actions") or []:
            partes = []

            def junta(rotulo, ep, ids):
                ids = ids if isinstance(ids, list) else [ids]
                for i in ids:
                    nome, falta = _nome(n[ep], i)
                    partes.append((rotulo, nome, falta))
            if a.get("assign_title"):
                partes.append(("título", a["assign_title"], False))
            if a.get("assign_correspondent"):
                junta("correspondente", "correspondents", a["assign_correspondent"])
            if a.get("assign_document_type"):
                junta("tipo", "document_types", a["assign_document_type"])
            if a.get("assign_tags"):
                junta("etiqueta", "tags", a["assign_tags"])
            if a.get("assign_storage_path"):
                junta("caminho", "storage_paths", a["assign_storage_path"])
            if a.get("assign_custom_fields"):
                junta("campo", "custom_fields", a["assign_custom_fields"])
            if a.get("assign_owner"):
                dono = (utilizadores or {}).get(a["assign_owner"])
                partes.append(("dono", dono or f"#{a['assign_owner']}", utilizadores is not None and dono is None))
            if a.get("remove_tags"):
                junta("tira etiqueta", "tags", a["remove_tags"])
            resumo = "; ".join(f"{r} {v}" for r, v, _ in partes) or "—"
            f.acoes.append({**a, "partes": partes, "tipo_txt": TIPOS_ACAO.get(a.get("type"), "?"),
                            "resumo": f"{TIPOS_ACAO.get(a.get('type'), '?')}: {resumo}"})
        out.append(f)
    _avisos(out)
    return out


def _avisos(lista: list[Fluxo]) -> None:
    """Problemas prováveis em cada fluxo (só os ligados contam para sobreposições e ordem repetida)."""
    textos: dict[str, list[Fluxo]] = {}
    ordens: dict[int, list[Fluxo]] = {}
    for f in lista:
        if not f.gatilhos:
            f.avisos.append("sem gatilho: nunca corre")
        if not f.acoes:
            f.avisos.append("sem ações: não faz nada")
        if any(t["tudo"] for t in f.gatilhos):
            f.avisos.append("o gatilho não tem filtro: apanha todos os documentos")
        for r, v, falta in f.partes:
            if falta:
                f.avisos.append(f"{r} {v} já não existe no Paperless")
        if f.ativo:
            for t in f.gatilhos:
                if t.get("matching_algorithm") and (t.get("match") or "").strip():
                    textos.setdefault((t.get("match") or "").strip().lower(), []).append(f)
            ordens.setdefault(f.ordem, []).append(f)
    for texto, fs in textos.items():
        if len({f.id for f in fs}) > 1:
            outros = sorted({f.nome for f in fs})
            for f in fs:
                f.avisos.append(f"o mesmo texto «{texto}» noutro fluxo ligado ({', '.join(x for x in outros if x != f.nome)}): "
                                "o que corre depois sobrepõe-se")
    for ordem, fs in ordens.items():
        if len(fs) > 1:
            for f in fs:
                f.avisos.append(f"ordem {ordem} repetida noutro fluxo ligado: a ordem entre eles não é garantida")


def mover_fluxo(token: str, fid: int, direcao: int) -> None:
    """Sobe (−1) ou desce (+1) um fluxo: renumera todos de 10 em 10 pela ordem atual e troca-o com o vizinho."""
    lista = sorted(listar("workflows", token), key=lambda w: (w.get("order") or 0, w.get("name") or ""))
    ids = [w["id"] for w in lista]
    if fid not in ids:
        raise paperless.ErroPaperless("Fluxo não encontrado.")
    i = ids.index(fid)
    j = i + direcao
    if not 0 <= j < len(ids):
        return
    ids[i], ids[j] = ids[j], ids[i]
    atuais = {w["id"]: w.get("order") or 0 for w in lista}
    for k, wid in enumerate(ids):
        nova = (k + 1) * 10
        if atuais[wid] != nova:
            alterar("workflows", wid, {"order": nova}, token)


def alterar_fluxo(token: str, fid: int, *, ativo: bool | None = None, ordem: int | None = None, nome: str | None = None) -> None:
    corpo = {k: v for k, v in (("enabled", ativo), ("order", ordem), ("name", nome)) if v is not None}
    if corpo:
        alterar("workflows", fid, corpo, token)


def alterar_gatilho(token: str, tid: int, algoritmo: int, texto: str, insensivel: bool) -> None:
    alterar("workflow_triggers", tid, {"matching_algorithm": algoritmo, "match": texto.strip(), "is_insensitive": insensivel}, token)


# ------------------------------------------------------------------ email (Rever_Mail_Paperless.ps1)
def _problemas_regra(r: dict, contas: set) -> tuple[list[str], bool]:
    """(problemas, corrigível). Corrigível = action_parameter "" numa ação que não o usa (erro 400 ao gravar no menu)."""
    problemas, corrigir = [], False
    precisa = r.get("action") in (2, 5)
    vazio = r.get("action_parameter") in ("", None)
    if precisa and vazio:
        problemas.append(f"a ação «{ACOES_EMAIL.get(r.get('action'))}» precisa de parâmetro (pasta/etiqueta) e está vazio")
    if not precisa and r.get("action_parameter") == "":
        problemas.append("parâmetro da ação vazio ('') numa ação que não o usa: dá o erro 400 ao gravar no Paperless")
        corrigir = True
    if not r.get("enabled", True):
        problemas.append("regra desativada")
    if r.get("account") not in contas:
        problemas.append(f"a conta {r.get('account')} não existe")
    if (r.get("maximum_age") or 0) == 0:
        problemas.append("idade máxima 0: só os emails de hoje (confirma que é o pretendido)")
    if not any(r.get(k) for k in ("filter_from", "filter_subject", "filter_body", "filter_to")):
        problemas.append("sem filtros de remetente/assunto/corpo: apanha todos os emails da pasta")
    return problemas, corrigir


def rever_email(token: str) -> dict:
    contas = listar("mail_accounts", token)
    ids_contas = {c["id"] for c in contas}
    nomes_contas = {c["id"]: c.get("name") or "" for c in contas}
    for c in contas:
        c["tipo"] = TIPOS_CONTA.get(c.get("account_type"), "?")
        c["seg"] = SEGURANCA.get(c.get("imap_security"), "?")
    regras = sorted(listar("mail_rules", token), key=lambda r: (r.get("order") or 0, r.get("name") or ""))
    for r in regras:
        r["problemas"], r["corrigivel"] = _problemas_regra(r, ids_contas)
        r["conta_nome"] = nomes_contas.get(r.get("account"), f"#{r.get('account')}")
        r["acao_txt"] = ACOES_EMAIL.get(r.get("action"), "?")
        r["escopo_txt"] = ESCOPOS.get(r.get("consumption_scope"), "?")
    try:
        processados = paperless._pedido("/api/processed_mail/", {"ordering": "-processed", "page_size": 15},
                                        token=token).get("results", [])
    except paperless.ErroPaperless:
        processados = None           # versão sem /api/processed_mail/
    nomes_regras = {r["id"]: r.get("name") or "" for r in regras}
    for x in processados or []:
        x["regra_nome"] = nomes_regras.get(x.get("rule"), f"#{x.get('rule')}")
    return {"contas": contas, "regras": regras, "processados": processados}


def corrigir_regra(token: str, rid: int) -> None:
    alterar("mail_rules", rid, {"action_parameter": None}, token)
