"""Circuitos de documentos no Paperless-ngx, criados pela app (em vez dos scripts PowerShell).

Um circuito é o conjunto de elementos que o Paperless precisa para tratar um tipo de documento sozinho: tipo de
documento, correspondente, etiquetas, campos personalizados, caminho de armazenamento, fluxo de entrada (com dono) e,
opcionalmente, a regra de email. Cada modelo gera um plano de passos; «pré-visualizar» só diz o que já existe e o que
seria criado, «aplicar» cria o que falta (idempotente: nada se duplica, procura-se pelo nome sem diferenciar maiúsculas).

O mesmo modelo serve outra pessoa/entidade mudando os parâmetros (empregador, pasta, filtro do fluxo) e o utilizador
cujo token se usa: o fluxo de entrada atribui como dono o utilizador do token (só ele e os superutilizadores veem os
documentos — privacidade dos recibos de vencimento).

Modelos (equivalentes aos scripts):
  recibo_vencimento  ← Adicionar_Recibo_Vencimento.ps1
  fatura_fornecedor  ← Adicionar_Caso_Paperless.ps1
  conta_email        ← Configurar_Paperless_Mail.ps1 (a palavra-passe de aplicação vai ao Paperless e não fica na app)
"""
from dataclasses import dataclass, field

from . import paperless

NENHUM, QUALQUER, TODAS, LITERAL = 0, 1, 2, 3        # algoritmos de correspondência do Paperless


@dataclass
class Passo:
    endpoint: str            # document_types, tags, …
    nome: str
    corpo: dict = field(default_factory=dict)
    estado: str = "por ver"  # existe | criar | criado | erro | falta
    id: int | None = None
    detalhe: str = ""

    @property
    def rotulo(self) -> str:
        return ROTULOS.get(self.endpoint, self.endpoint)


ROTULOS = {"document_types": "Tipo de documento", "correspondents": "Correspondente", "tags": "Etiqueta",
           "custom_fields": "Campo personalizado", "storage_paths": "Caminho de armazenamento", "workflows": "Fluxo de entrada",
           "mail_accounts": "Conta de email", "mail_rules": "Regra de email"}


@dataclass
class Modelo:
    chave: str
    nome: str
    descricao: str
    parametros: list          # [(nome, rótulo, omissão, ajuda)]


MODELOS = {
    "recibo_vencimento": Modelo(
        "recibo_vencimento", "Recibo de vencimento",
        "Tipo «Recibo de vencimento», o empregador como correspondente, etiqueta «Vencimento», campos Período, Bruto, "
        "Descontos, Líquido e Cartão refeição, caminho e o fluxo de entrada que reconhece o recibo e o atribui ao dono.",
        [("empregador", "Empregador (correspondente)", "Banco Santander Totta", ""),
         ("pasta", "Pasta no arquivo", "Pessoal/Vencimentos", "o caminho fica <pasta>/{{ created_year }}/{{ title }}"),
         ("filtro", "Texto do recibo (todas as palavras)", "abonos descontos acumulados",
          "para outra pessoa junte uma palavra só dos recibos dela (ex.: o NIF), para os fluxos não se cruzarem"),
         ("sufixo", "Nome (outra pessoa)", "", "vazio = o seu; ex.: «Ana» cria «Entrada Vencimento - Ana» e «Pessoal - Vencimentos - Ana»")]),
    "fatura_fornecedor": Modelo(
        "fatura_fornecedor", "Fatura de fornecedor",
        "Um fornecedor novo: correspondente, etiquetas (o nome, extras e «Débito direto»), caminho, o fluxo de entrada pelo "
        "conteúdo e, com remetente, a regra de email. Reaproveita o tipo «Fatura» e os campos «Período» e «Valor».",
        [("nome", "Fornecedor", "", "ex.: Vodafone"),
         ("filtro", "Texto no documento", "", "vazio = o nome; mais exato: o NIF do emitente"),
         ("etiquetas", "Etiquetas extra", "", "separadas por vírgulas"),
         ("debito_direto", "Débito direto (sim/não)", "não", ""),
         ("tipo", "Tipo de documento", "Fatura", ""),
         ("pasta", "Pasta (1.º nível)", "Casa", "o caminho fica <pasta>/<fornecedor>/{{ created_year }}/{{ title }}"),
         ("remetente", "Remetente do email", "", "vazio = sem regra de email; ex.: vodafone.pt"),
         ("idade", "Idade máxima do email (dias)", "0", "0 = só os emails de hoje"),
         ("conta", "Conta de email", "Gmail Lar", "")]),
    "conta_email": Modelo(
        "conta_email", "Conta de email (IMAP)",
        "A conta de onde o Paperless recolhe os anexos (ex.: Gmail com palavra-passe de aplicação). A palavra-passe é "
        "enviada ao Paperless no momento e não fica guardada na app.",
        [("nome", "Nome da conta", "Gmail Lar", ""), ("utilizador", "Utilizador (email)", "", ""),
         ("servidor", "Servidor IMAP", "imap.gmail.com", ""), ("porta", "Porta", "993", "")]),
}

PERIODO, VALOR, LIQUIDO, CARTAO, DEBITO = "Período", "Valor", "Líquido", "Cartão refeição", "Débito direto"


def _sim(v: str) -> bool:
    return str(v or "").strip().lower() in ("sim", "s", "1", "true", "yes", "on")


def _procurar(endpoint: str, nome: str, token: str) -> dict | None:
    if endpoint in ("mail_accounts", "mail_rules"):        # estes não filtram pelo nome
        r = paperless._pedido(f"/api/{endpoint}/", {"page_size": 1000}, token=token)
        return next((x for x in r.get("results", []) if (x.get("name") or "").lower() == nome.lower()), None)
    r = paperless._pedido(f"/api/{endpoint}/", {"name__iexact": nome, "page_size": 5}, token=token)
    return next(iter(r.get("results", [])), None)


def utilizador_do_token(token: str) -> dict:
    """{id, username} do dono do token (o fluxo de entrada atribui-lhe os documentos)."""
    ui = paperless._pedido("/api/ui_settings/", token=token)
    u = ui.get("user") or {}
    if not u.get("id"):
        raise paperless.ErroPaperless("Não consegui identificar o utilizador do token (ui_settings).")
    return {"id": int(u["id"]), "username": u.get("username") or ""}


@dataclass
class Plano:
    modelo: Modelo
    params: dict
    passos: list = field(default_factory=list)
    dono: dict | None = None
    aplicado: bool = False

    @property
    def n_criar(self) -> int:
        return sum(1 for p in self.passos if p.estado in ("criar", "criado"))


def _passos(modelo: str, p: dict, dono_id: int | None, ids: dict) -> list[Passo]:
    """Os passos do modelo. `ids` (nome do passo → id) serve para o fluxo apontar para os elementos criados antes."""
    nenhum = {"matching_algorithm": NENHUM, "match": ""}
    if modelo == "recibo_vencimento":
        suf = f" - {p['sufixo'].strip()}" if p.get("sufixo", "").strip() else ""
        pasta = p.get("pasta", "").strip().strip("/") or "Pessoal/Vencimentos"
        campos = [PERIODO, "Bruto", "Descontos", LIQUIDO, CARTAO]
        return ([Passo("document_types", "Recibo de vencimento", dict(nenhum)),
                 Passo("correspondents", p.get("empregador") or "Empregador", dict(nenhum)),
                 Passo("tags", "Vencimento", dict(nenhum))]
                + [Passo("custom_fields", c, {"data_type": "string" if c == PERIODO else "monetary"}) for c in campos]
                + [Passo("storage_paths", f"Pessoal - Vencimentos{suf}",
                         {**nenhum, "path": f"{pasta}{('/' + p['sufixo'].strip()) if suf else ''}/{{{{ created_year }}}}/{{{{ title }}}}"}),
                   Passo("workflows", f"Entrada Vencimento{suf}", _fluxo(
                       20, TODAS, p.get("filtro") or "abonos descontos acumulados", {
                           "assign_correspondent": ids.get(("correspondents", p.get("empregador") or "Empregador")),
                           "assign_document_type": ids.get(("document_types", "Recibo de vencimento")),
                           "assign_tags": [ids.get(("tags", "Vencimento"))],
                           "assign_storage_path": ids.get(("storage_paths", f"Pessoal - Vencimentos{suf}")),
                           "assign_owner": dono_id,
                           "assign_custom_fields": [ids.get(("custom_fields", c)) for c in campos]}))])
    if modelo == "fatura_fornecedor":
        nome = p.get("nome", "").strip()
        etiquetas = [nome] + [e.strip() for e in (p.get("etiquetas") or "").split(",") if e.strip()]
        if _sim(p.get("debito_direto")):
            etiquetas.append(DEBITO)
        pasta = (p.get("pasta") or "Casa").strip().strip("/")
        tipo = (p.get("tipo") or "Fatura").strip()
        passos = ([Passo("correspondents", nome, dict(nenhum))]
                  + [Passo("tags", e, dict(nenhum)) for e in etiquetas]
                  + [Passo("storage_paths", f"{pasta} - {nome}", {**nenhum, "path": f"{pasta}/{nome}/{{{{ created_year }}}}/{{{{ title }}}}"}),
                     Passo("document_types", tipo, dict(nenhum)),
                     Passo("custom_fields", PERIODO, {"data_type": "string"}),
                     Passo("custom_fields", VALOR, {"data_type": "monetary"}),
                     Passo("workflows", f"Entrada {nome}", _fluxo(10, LITERAL, p.get("filtro") or nome, {
                         "assign_correspondent": ids.get(("correspondents", nome)),
                         "assign_document_type": ids.get(("document_types", tipo)),
                         "assign_tags": [ids.get(("tags", e)) for e in etiquetas],
                         "assign_storage_path": ids.get(("storage_paths", f"{pasta} - {nome}")),
                         "assign_custom_fields": [ids.get(("custom_fields", PERIODO)), ids.get(("custom_fields", VALOR))]}))])
        remetente = (p.get("remetente") or "").strip()
        if remetente:
            conta = (p.get("conta") or "Gmail Lar").strip()
            passos.append(Passo("mail_rules", f"{nome} - {remetente}", {
                "account": ids.get(("mail_accounts", conta)), "enabled": True, "folder": "INBOX", "filter_from": remetente,
                "maximum_age": int(p.get("idade") or 0) if str(p.get("idade") or "0").isdigit() else 0,
                "action": 3, "assign_title_from": 2, "assign_correspondent_from": 1, "attachment_type": 1,
                "consumption_scope": 1, "order": 10, "_conta": conta}))
        return passos
    if modelo == "conta_email":
        return [Passo("mail_accounts", (p.get("nome") or "Gmail Lar").strip(), {
            "imap_server": (p.get("servidor") or "imap.gmail.com").strip(),
            "imap_port": int(p.get("porta") or 993) if str(p.get("porta") or "993").isdigit() else 993,
            "imap_security": 1, "username": (p.get("utilizador") or "").strip(), "character_set": "UTF-8", "account_type": 1})]
    raise ValueError(modelo)


def _fluxo(ordem: int, algoritmo: int, filtro: str, acao: dict) -> dict:
    return {"order": ordem, "enabled": True,
            "triggers": [{"type": 2, "sources": [1, 2, 3, 4], "matching_algorithm": algoritmo, "match": filtro.strip(),
                          "is_insensitive": True, "filter_filename": None, "filter_path": None, "filter_has_tags": []}],
            "actions": [{"type": 1, **acao}]}


def validar(modelo: str, p: dict) -> list[str]:
    erros = []
    if modelo == "fatura_fornecedor" and not (p.get("nome") or "").strip():
        erros.append("Indica o nome do fornecedor.")
    if modelo == "recibo_vencimento" and not (p.get("empregador") or "").strip():
        erros.append("Indica o empregador.")
    if modelo == "conta_email" and not (p.get("utilizador") or "").strip():
        erros.append("Indica o utilizador (email) da conta.")
    return erros


def planear(modelo: str, params: dict, token: str) -> Plano:
    """O que já existe e o que seria criado (nada é alterado)."""
    m = MODELOS[modelo]
    plano = Plano(m, params)
    plano.dono = utilizador_do_token(token)
    ids = {}
    for passo in _passos(modelo, params, plano.dono["id"], ids):
        achado = _procurar(passo.endpoint, passo.nome, token)
        passo.estado, passo.id = ("existe", achado["id"]) if achado else ("criar", None)
        if achado:
            ids[(passo.endpoint, passo.nome)] = achado["id"]
        if passo.endpoint == "mail_rules":
            conta = passo.corpo.get("_conta")
            existe = _procurar("mail_accounts", conta, token)
            if not achado and not existe:
                passo.estado, passo.detalhe = "falta", f"a conta de email «{conta}» não existe — cria-a primeiro (modelo «Conta de email»)"
        plano.passos.append(passo)
    return plano


def aplicar(modelo: str, params: dict, token: str, senha_email: str | None = None) -> Plano:
    """Cria o que falta, pela ordem do plano. Um passo que falhe não impede os seguintes (o fluxo tenta sem campos)."""
    m = MODELOS[modelo]
    plano = Plano(m, params, aplicado=True)
    plano.dono = utilizador_do_token(token)
    ids: dict = {}
    for passo in _passos(modelo, params, plano.dono["id"], ids):
        # o corpo é refeito a cada passo para usar os ids dos elementos criados nos passos anteriores
        passo = next(x for x in _passos(modelo, params, plano.dono["id"], ids) if (x.endpoint, x.nome) == (passo.endpoint, passo.nome))
        achado = _procurar(passo.endpoint, passo.nome, token)
        if achado:
            passo.estado, passo.id = "existe", achado["id"]
            ids[(passo.endpoint, passo.nome)] = achado["id"]
            plano.passos.append(passo)
            continue
        corpo = {k: v for k, v in passo.corpo.items() if not k.startswith("_")}
        if passo.endpoint == "mail_rules":
            conta = _procurar("mail_accounts", passo.corpo.get("_conta"), token)
            if not conta:
                passo.estado, passo.detalhe = "falta", f"a conta de email «{passo.corpo.get('_conta')}» não existe"
                plano.passos.append(passo)
                continue
            corpo["account"] = conta["id"]
        if passo.endpoint == "mail_accounts":
            if not senha_email:
                passo.estado, passo.detalhe = "erro", "falta a palavra-passe de aplicação"
                plano.passos.append(passo)
                continue
            corpo["password"] = senha_email
        if passo.endpoint == "workflows":
            for a in corpo["actions"]:
                for k in list(a):
                    if isinstance(a[k], list):
                        a[k] = [x for x in a[k] if x]
                    elif a[k] is None:
                        del a[k]
        try:
            novo = paperless.enviar("POST", f"/api/{passo.endpoint}/", {"name": passo.nome, **corpo}, token=token)
            passo.estado, passo.id = "criado", novo.get("id")
        except paperless.ErroPaperless as e:
            if passo.endpoint == "workflows" and "assign_custom_fields" in corpo["actions"][0]:
                corpo["actions"][0].pop("assign_custom_fields")      # versões que tratam os campos de outra forma
                try:
                    novo = paperless.enviar("POST", f"/api/{passo.endpoint}/", {"name": passo.nome, **corpo}, token=token)
                    passo.estado, passo.id, passo.detalhe = "criado", novo.get("id"), "sem campos personalizados"
                except paperless.ErroPaperless as e2:
                    passo.estado, passo.detalhe = "erro", str(e2)
            else:
                passo.estado, passo.detalhe = "erro", str(e)
        if passo.id:
            ids[(passo.endpoint, passo.nome)] = passo.id
        plano.passos.append(passo)
    return plano


def link(passo: Passo) -> str | None:
    if not passo.id:
        return None
    caminhos = {"document_types": "documenttypes", "correspondents": "correspondents", "tags": "tags",
                "custom_fields": "customfields", "storage_paths": "storagepaths", "workflows": "workflows", "mail_rules": "mail",
                "mail_accounts": "mail"}
    return f"{paperless.url_browser()}/{caminhos.get(passo.endpoint, '')}" if passo.endpoint in caminhos else None

