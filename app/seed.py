"""Dados iniciais: instituições e entidades por omissão (só se as tabelas
estiverem vazias — nunca sobrepõe o que o utilizador alterou)."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Entidade, Instituicao

INSTITUICOES = [
    ("Santander", "santander"),
    ("ActivoBank", "activobank"),
    ("Banco BiG", "big"),
    ("Certificados de Aforro (IGCP/CTT)", "igcp"),
    ("Revolut", "revolut"),
    ("Crédito Agrícola", "ca"),
    ("Caixa Geral de Depósitos", None),
]

ENTIDADES = [
    ("Família", "Contas da família — base dos totais e relatórios.", 1),
    ("Mãe", "Conta da Mãe, gerida à parte — nunca entra nos totais da Família.", 2),
]


# Categorias de despesa principais: as 8 primeiras recebem as 8 cores da
# paleta, por esta ordem; as restantes ficam a cinzento (nos gráficos caem
# em "Outras" quando não estão entre as maiores).
DESPESAS = [
    ("Casa", ["Renda / prestação", "Condomínio", "Eletricidade", "Água", "Gás",
              "Internet e telemóvel", "Manutenção e reparações"]),
    ("Alimentação", ["Supermercado", "Restaurantes e cafés", "Take-away"]),
    ("Transportes", ["Combustível", "Portagens e estacionamento", "Transportes públicos",
                     "Manutenção automóvel"]),
    ("Saúde", ["Farmácia", "Consultas e exames", "Lar / apoio sénior"]),
    ("Lazer", ["Viagens", "Cultura e espetáculos", "Desporto e ginásio", "Subscrições"]),
    ("Compras", ["Vestuário", "Casa e decoração", "Tecnologia", "Online"]),
    ("Educação", ["Propinas", "Livros e material", "Explicações e cursos"]),
    ("Seguros", []),
    ("Impostos e taxas", []),
    ("Comissões bancárias", []),
    ("Prendas e donativos", []),
    ("Levantamentos", []),
    ("Outras despesas", []),
]
RECEITAS = [
    ("Salário", []),
    ("Subsídio de alimentação", []),
    ("Pensão", []),
    ("Juros e dividendos", []),
    ("Reembolsos", []),
    ("Outras receitas", []),
]

# Regras iniciais para descrições típicas de bancos portugueses:
# (padrão "contém", alternativas separadas por |, categoria "Pai › Filha", sentido)
REGRA_REFEICAO = "SUB REFEICAO|SUBSIDIO REFEICAO|SUBS REFEICAO|SUBSIDIO ALIMENTACAO"
REGRAS = [
    ("LEVANTAMENTO|LEV ATM|LEV", "Levantamentos", "SAIDA"),
    ("EDP|ENDESA|IBERDROLA|GOLDENERGY", "Casa › Eletricidade", "SAIDA"),
    ("SMAS|EPAL|AGUAS DE|SERVICOS MUNICIP", "Casa › Água", "SAIDA"),
    ("NOS COMUNI|MEO|VODAFONE|NOWO|DIGI", "Casa › Internet e telemóvel", "SAIDA"),
    ("CONDOMIN|LOJA DO CONDOM", "Casa › Condomínio", "SAIDA"),
    ("CONTINENTE|PINGO DOCE|ALDI|LIDL|INTERMARCHE|MINIPRECO|AUCHAN|MERCADONA|CELEIRO|"
     "SPAR|COVIRAN|E.LECLERC", "Alimentação › Supermercado", "SAIDA"),
    ("UBER EATS|GLOVO|BOLT FOOD|SUSHI AT HOME", "Alimentação › Take-away", "SAIDA"),
    ("GALP|REPSOL|PRIO|CEPSA|BP|MOEVE", "Transportes › Combustível", "SAIDA"),
    ("VIA VERDE|BRISA|ASCENDI|EMEL|PARQUE", "Transportes › Portagens e estacionamento", "SAIDA"),
    ("CP CP|CARRIS|METRO|NAVEGANTE|FERTAGUS|UBER|BOLT", "Transportes › Transportes públicos", "SAIDA"),
    ("FARMACIA|FARMÁCIA|WELLS", "Saúde › Farmácia", "SAIDA"),
    ("SANTA CASA DA MISERICORDIA", "Saúde › Lar / apoio sénior", "SAIDA"),
    ("CYBERGYM|GINAS|FITNESS|HOLMES PLACE|SOLINCA", "Lazer › Desporto e ginásio", "SAIDA"),
    ("NETFLIX|SPOTIFY|DISNEY|HBO|APPLE.COM|GOOGLE|AMAZON PRIME|YOUTUBE", "Lazer › Subscrições", "SAIDA"),
    ("SHEIN|AMAZON|ALIEXPRESS|TEMU", "Compras › Online", "SAIDA"),
    ("FNAC|WORTEN|MEDIA MARKT|RADIO POPULAR", "Compras › Tecnologia", "SAIDA"),
    ("IKEA|LEROY MERLIN|AKI|BRICOMARCHE|MAXMAT", "Compras › Casa e decoração", "SAIDA"),
    ("SEGUROS|FIDELIDADE|ALLIANZ|TRANQUILIDADE|GENERALI|AGEAS", "Seguros", "SAIDA"),
    ("AT - AUTORIDADE|AUTORIDADE TRIBUTARIA|PAG ESTADO|IMI|IUC", "Impostos e taxas", "SAIDA"),
    ("COMISSAO|COMISSÃO|IMPOSTO DO SELO|IMP SELO|MANUTENCAO DE CONTA", "Comissões bancárias", "SAIDA"),
    ("JUROS", "Juros e dividendos", "ENTRADA"),
    ("PENSAO|PENSÃO|CENTRO NACIONAL DE PENSOES|CNP", "Pensão", "ENTRADA"),
    ("VENCIMENTO|ORDENADO|SALARIO|SALÁRIO", "Salário", "ENTRADA"),
    (REGRA_REFEICAO, "Subsídio de alimentação", "ENTRADA"),
]


def _semear_categorias(db: Session) -> None:
    from .models import CORES, Categoria, Regra

    def criar(lista, natureza, com_cor):
        for i, (nome, filhas) in enumerate(lista):
            cor = CORES[i] if com_cor and i < len(CORES) else None
            raiz = Categoria(nome=nome, natureza=natureza, cor=cor, ordem=i)
            db.add(raiz)
            for j, f in enumerate(filhas):
                db.add(Categoria(nome=f, natureza=natureza, pai=raiz, ordem=j))

    criar(DESPESAS, "DESPESA", True)
    criar(RECEITAS, "RECEITA", False)
    db.flush()
    por_nome = {c.nome_completo: c for c in db.scalars(select(Categoria))}
    for i, (padrao, categoria, sentido) in enumerate(REGRAS):
        db.add(Regra(padrao=padrao, modo="contem", sentido=sentido, categoria_id=por_nome[categoria].id,
                     prioridade=100 + i))


def semear(db: Session) -> None:
    from .models import Categoria
    existentes = set(db.scalars(select(Instituicao.nome)))
    db.add_all(Instituicao(nome=n, parser=p) for n, p in INSTITUICOES if n not in existentes)
    if not db.scalar(select(func.count()).select_from(Entidade)):
        db.add_all(Entidade(nome=n, descricao=d, ordem=o) for n, d, o in ENTIDADES)
    if not db.scalar(select(func.count()).select_from(Categoria)):
        _semear_categorias(db)
    else:
        _semear_refeicao(db)
    db.commit()


def _semear_refeicao(db: Session) -> None:
    """Bases criadas antes da v0.18: categoria e regra do subsídio de alimentação (uma só vez,
    para não voltar a criá-las se forem apagadas)."""
    from .models import Categoria, Definicao, Regra
    if db.get(Definicao, "seed_refeicao"):
        return
    cat = db.scalar(select(Categoria).where(Categoria.nome == "Subsídio de alimentação", Categoria.pai_id.is_(None)))
    if not cat:
        cat = Categoria(nome="Subsídio de alimentação", natureza="RECEITA")
        db.add(cat)
        db.flush()
    if not db.scalar(select(Regra).where(Regra.padrao == REGRA_REFEICAO)):
        db.add(Regra(padrao=REGRA_REFEICAO, modo="contem", sentido="ENTRADA", categoria_id=cat.id, prioridade=99))
    db.add(Definicao(chave="seed_refeicao", valor="1"))
