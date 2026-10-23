"""Preferências da app (tabela `definicao`), com valores por omissão.

Ficam em memória em app.state.prefs (a app corre num só processo) e são
atualizadas sempre que se gravam em Administração › Preferências."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Definicao

OMISSAO = {
    "mostrar_iban_completo": "0",  # "1": mostra o NIB/IBAN inteiro; "0": PT50 •••• 1234
    # Lembrete das tarefas de importação (menu e painel): ligado e entre que dias do mês.
    "tarefas_ativas": "1",
    "tarefas_dia_ini": "25",
    "tarefas_dia_fim": "5",
    # Património: revisão automática (IA) do valor dos bens quando passa o prazo de cada um.
    "avaliacao_automatica": "1",
    # Heranças: mostrar a parte do utilizador (quota e 2.º cenário) junto dos valores e no Início da entidade pessoal.
    "mostrar_minha_parte": "1",
    # Recheio: garantia por omissão dos itens novos (meses).
    "recheio_garantia_meses": "36",
    "recheio_valor_minimo": "50",          # euros: compras abaixo disto não entram no recheio
    # Página de registo de abastecimentos sem login: código secreto do link (vazio = desligada).
    "abastecer_token": "",
    # Pasta partilhada da NAS com extratos: de quantos em quantos minutos é lida.
    "pasta_intervalo_minutos": "60",
    # Paperless: etiqueta ou tipo de documento dos extratos bancários a importar.
    "paperless_extratos": "Extrato bancário",
    # «1»: as chaves da API do Paperless e do Immich de cada pessoa são configuradas pelo administrador
    # (Administração › Utilizadores); no Perfil só se vê o estado. «0»: cada pessoa guarda a sua no Perfil.
    "tokens_pelo_admin": "0",
    # Motor de classificação dos documentos do Paperless (Administração › Paperless › Regras): ciclo automático,
    # de quantos em quantos minutos, IA de apoio para os que nenhuma regra apanha, ligar aos movimentos e a partir
    # de que data (adicionados ao Paperless) os documentos entram no ciclo.
    "motor_ativo": "0",
    "motor_intervalo_minutos": "30",
    "motor_ia": "1",
    "motor_ligar": "1",
    "motor_desde": "",
}


def carregar(db: Session) -> dict[str, str]:
    prefs = dict(OMISSAO)
    prefs.update({d.chave: d.valor for d in db.scalars(select(Definicao))})
    return prefs


def gravar(db: Session, chave: str, valor: str) -> None:
    if chave not in OMISSAO:
        raise KeyError(chave)
    d = db.get(Definicao, chave)
    if d:
        d.valor = valor
    else:
        db.add(Definicao(chave=chave, valor=valor))


def ligada(prefs: dict, chave: str) -> bool:
    return prefs.get(chave, OMISSAO.get(chave)) == "1"
