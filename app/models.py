"""Modelo de dados — Fase 1 (utilizadores, entidades, permissões, contas).

Montantes em cêntimos (inteiro), nunca float. Ver DESIGN.md §5 para o
modelo completo; as tabelas das fases seguintes entram por migração Alembic.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

PAPEIS = {"ver": 1, "editar": 2, "admin": 3}
PAPEIS_ROTULO = {"ver": "Ver", "editar": "Editar", "admin": "Administrar"}

TIPOS_CONTA = {
    "ORDEM": "Conta à ordem",
    "POUPANCA": "Poupança / depósito a prazo",
    "CARTAO_CREDITO": "Cartão de crédito",
    "INVESTIMENTO": "Investimento",
    "AFORRO": "Certificados de Aforro",
    "CARTEIRA_DIGITAL": "Carteira digital (Revolut)",
    "CARTAO_REFEICAO": "Cartão refeição",
    "EMPRESTIMO": "Empréstimo (habitação)",
}
# Contas cujo saldo é uma dívida (entram como passivo no património).
TIPOS_PASSIVO = {"CARTAO_CREDITO", "EMPRESTIMO"}


class Utilizador(Base):
    __tablename__ = "utilizador"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100))
    login: Mapped[str] = mapped_column(String(50), unique=True)
    email: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incrementar invalida todas as sessões abertas (ex.: troca de password).
    versao_sessao: Mapped[int] = mapped_column(Integer, default=1)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime)

    permissoes: Mapped[list["Permissao"]] = relationship(
        back_populates="utilizador", cascade="all, delete-orphan"
    )


class Entidade(Base):
    """Património cujas contas se somam entre si (Família, Mãe)."""
    __tablename__ = "entidade"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100), unique=True)
    descricao: Mapped[str | None] = mapped_column(Text)
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    # Início da análise: movimentos anteriores ficam fora dos relatórios, do fluxo de caixa e dos
    # gráficos (enquanto não houver extratos antigos de todas as contas). Os saldos não mudam.
    data_inicio: Mapped[date | None] = mapped_column(Date)

    contas: Mapped[list["Conta"]] = relationship(back_populates="entidade")


class Permissao(Base):
    __tablename__ = "permissao"

    utilizador_id: Mapped[int] = mapped_column(
        ForeignKey("utilizador.id", ondelete="CASCADE"), primary_key=True
    )
    entidade_id: Mapped[int] = mapped_column(
        ForeignKey("entidade.id", ondelete="CASCADE"), primary_key=True
    )
    papel: Mapped[str] = mapped_column(String(10))  # ver | editar | admin

    utilizador: Mapped[Utilizador] = relationship(back_populates="permissoes")
    entidade: Mapped[Entidade] = relationship()


class Instituicao(Base):
    __tablename__ = "instituicao"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(100), unique=True)
    bic: Mapped[str | None] = mapped_column(String(11))
    parser: Mapped[str | None] = mapped_column(String(30))  # chave do parser de importação
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)


conta_titular = Table(
    "conta_titular",
    Base.metadata,
    Column("conta_id", ForeignKey("conta.id", ondelete="CASCADE"), primary_key=True),
    Column("utilizador_id", ForeignKey("utilizador.id", ondelete="CASCADE"), primary_key=True),
)


class Conta(Base):
    __tablename__ = "conta"
    __table_args__ = (UniqueConstraint("entidade_id", "nome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id"))
    instituicao_id: Mapped[int] = mapped_column(ForeignKey("instituicao.id"))
    nome: Mapped[str] = mapped_column(String(100))
    tipo: Mapped[str] = mapped_column(String(20))
    moeda: Mapped[str] = mapped_column(String(3), default="EUR")
    iban: Mapped[str | None] = mapped_column(String(40))  # como foi escrito; a apresentação depende das preferências
    # Cartões de crédito: conta à ordem que paga o cartão + ciclo do extrato.
    conta_pagamento_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    dia_fecho: Mapped[int | None] = mapped_column(Integer)
    dia_pagamento: Mapped[int | None] = mapped_column(Integer)
    saldo_inicial: Mapped[int] = mapped_column(BigInteger, default=0)  # cêntimos
    data_saldo_inicial: Mapped[date | None] = mapped_column(Date)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    entidade: Mapped[Entidade] = relationship(back_populates="contas")
    instituicao: Mapped[Instituicao] = relationship()
    conta_pagamento: Mapped["Conta | None"] = relationship(remote_side="Conta.id")
    titulares: Mapped[list[Utilizador]] = relationship(secondary=conta_titular)
    emprestimo: Mapped["Emprestimo | None"] = relationship(back_populates="conta", uselist=False,
                                                           cascade="all, delete-orphan")

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_CONTA.get(self.tipo, self.tipo)


TIPOS_TAXA = {"variavel": "Variável", "fixa": "Fixa", "mista": "Mista"}


class Emprestimo(Base):
    """Dados de um empréstimo (1:1 com uma Conta do tipo EMPRESTIMO).

    O saldo da conta é o capital em dívida (negativo). A conta que paga a
    prestação é Conta.conta_pagamento_id e o dia da prestação Conta.dia_pagamento.
    Taxas em centésimas de milésima de ponto percentual (1,54895% → 154895)."""
    __tablename__ = "emprestimo"

    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), primary_key=True)
    montante_contratado: Mapped[int | None] = mapped_column(BigInteger)
    data_contrato: Mapped[date | None] = mapped_column(Date)
    prazo_meses: Mapped[int] = mapped_column(Integer)          # n.º total de prestações do contrato
    # N.º da última prestação paga à data do capital indicado (quando não se importa o extrato do empréstimo).
    ultima_prestacao_n: Mapped[int | None] = mapped_column(Integer)
    tipo_taxa: Mapped[str] = mapped_column(String(10), default="variavel")
    indexante: Mapped[str | None] = mapped_column(String(30))  # ex.: Euribor 12M
    spread_e5: Mapped[int | None] = mapped_column(Integer)
    taxa_e5: Mapped[int] = mapped_column(Integer)              # TAN em vigor
    padrao_prestacao: Mapped[str | None] = mapped_column(String(200))
    encargos: Mapped[int] = mapped_column(BigInteger, default=0)  # cêntimos por prestação (comissões, selo)

    conta: Mapped["Conta"] = relationship(back_populates="emprestimo")


class Importacao(Base):
    __tablename__ = "importacao"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"))
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    ficheiro_nome: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    parser: Mapped[str] = mapped_column(String(30))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    data_ini: Mapped[date | None] = mapped_column(Date)
    data_fim: Mapped[date | None] = mapped_column(Date)
    n_lidos: Mapped[int] = mapped_column(Integer, default=0)
    n_novos: Mapped[int] = mapped_column(Integer, default=0)
    n_duplicados: Mapped[int] = mapped_column(Integer, default=0)
    estado: Mapped[str] = mapped_column(String(12), default="confirmada")  # confirmada | anulada

    conta: Mapped[Conta] = relationship()
    utilizador: Mapped[Utilizador | None] = relationship()


class Movimento(Base):
    __tablename__ = "movimento"
    __table_args__ = (UniqueConstraint("conta_id", "hash_dedup"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    importacao_id: Mapped[int | None] = mapped_column(ForeignKey("importacao.id", ondelete="CASCADE"))
    data_mov: Mapped[date] = mapped_column(Date, index=True)
    data_valor: Mapped[date | None] = mapped_column(Date)
    # Posição cronológica dentro do mesmo dia (o extrato traz a ordem real).
    ordem: Mapped[int] = mapped_column(Integer, default=0)
    descricao_original: Mapped[str] = mapped_column(String(500))
    descricao_norm: Mapped[str] = mapped_column(String(500))
    montante: Mapped[int] = mapped_column(BigInteger)  # cêntimos, negativo = saída
    moeda: Mapped[str] = mapped_column(String(3), default="EUR")
    saldo_apos: Mapped[int | None] = mapped_column(BigInteger)  # saldo dado pelo banco
    notas: Mapped[str | None] = mapped_column(Text)
    hash_dedup: Mapped[str] = mapped_column(String(64))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    # Qualificação (relatórios). Transferências entre contas próprias e
    # movimentos excluídos não contam como receita nem despesa.
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"), index=True)
    transferencia: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    excluido: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 'manual' protege a qualificação de ser reescrita pelas regras.
    qualificado_por: Mapped[str | None] = mapped_column(String(10))  # manual | regra
    regra_id: Mapped[int | None] = mapped_column(ForeignKey("regra.id", ondelete="SET NULL"))
    # O outro lado de uma transferência (ex.: pagamento do cartão ↔ débito na conta pagadora).
    par_id: Mapped[int | None] = mapped_column(ForeignKey("movimento.id", ondelete="SET NULL"))
    # Bem (casa, carro) a que a despesa/receita diz respeito: separa casa principal, de férias, cada carro…
    bem_id: Mapped[int | None] = mapped_column(ForeignKey("bem.id", ondelete="SET NULL"), index=True)
    bem_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # escolhido à mão: a atribuição automática não mexe

    conta: Mapped[Conta] = relationship()
    par: Mapped["Movimento | None"] = relationship(remote_side="Movimento.id", foreign_keys=[par_id],
                                                   post_update=True)
    categoria: Mapped["Categoria | None"] = relationship()
    etiquetas: Mapped[list["Etiqueta"]] = relationship(secondary="movimento_etiqueta")
    bem: Mapped["Bem | None"] = relationship(lazy="selectin")


NATUREZAS = {"DESPESA": "Despesa", "RECEITA": "Receita"}

# Paleta categórica validada (8 cores, ordem fixa) — ver static/graficos.js.
CORES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


class Categoria(Base):
    __tablename__ = "categoria"
    __table_args__ = (UniqueConstraint("pai_id", "nome"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    pai_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="CASCADE"))
    natureza: Mapped[str] = mapped_column(String(10))  # DESPESA | RECEITA
    cor: Mapped[str | None] = mapped_column(String(7))
    ordem: Mapped[int] = mapped_column(Integer, default=0)

    pai: Mapped["Categoria | None"] = relationship(remote_side="Categoria.id", back_populates="filhas")
    filhas: Mapped[list["Categoria"]] = relationship(back_populates="pai", order_by="Categoria.nome")

    @property
    def nome_completo(self) -> str:
        return f"{self.pai.nome} › {self.nome}" if self.pai else self.nome

    @property
    def raiz(self) -> "Categoria":
        return self.pai or self

    @property
    def cor_efetiva(self) -> str:
        return self.cor or (self.pai.cor if self.pai else None) or "#8a8a85"


class Etiqueta(Base):
    __tablename__ = "etiqueta"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(50), unique=True)


movimento_etiqueta = Table(
    "movimento_etiqueta",
    Base.metadata,
    Column("movimento_id", ForeignKey("movimento.id", ondelete="CASCADE"), primary_key=True),
    Column("etiqueta_id", ForeignKey("etiqueta.id", ondelete="CASCADE"), primary_key=True),
)

MODOS_REGRA = {"contem": "contém", "comeca": "começa por", "regex": "expressão regular"}
SENTIDOS = {"": "entradas e saídas", "SAIDA": "só saídas", "ENTRADA": "só entradas"}


class Regra(Base):
    """Se a descrição corresponder ao padrão (e às condições opcionais),
    aplica as ações. Avaliadas por prioridade crescente; a 1.ª que
    corresponder ganha."""
    __tablename__ = "regra"

    id: Mapped[int] = mapped_column(primary_key=True)
    padrao: Mapped[str] = mapped_column(String(200))
    modo: Mapped[str] = mapped_column(String(10), default="contem")
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"))
    sentido: Mapped[str | None] = mapped_column(String(7))  # ENTRADA | SAIDA | None
    montante_min: Mapped[int | None] = mapped_column(BigInteger)  # cêntimos, valor absoluto
    montante_max: Mapped[int | None] = mapped_column(BigInteger)
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="CASCADE"))
    etiqueta_id: Mapped[int | None] = mapped_column(ForeignKey("etiqueta.id", ondelete="SET NULL"))
    marcar_transferencia: Mapped[bool] = mapped_column(Boolean, default=False)
    excluir: Mapped[bool] = mapped_column(Boolean, default=False)
    prioridade: Mapped[int] = mapped_column(Integer, default=100)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped[Conta | None] = relationship()
    categoria: Mapped[Categoria | None] = relationship()
    etiqueta: Mapped[Etiqueta | None] = relationship()


# Contas cujo saldo se pode retificar à data (valor real da carteira / dos certificados).
TIPOS_RETIFICAVEIS = {"AFORRO", "INVESTIMENTO", "CARTAO_REFEICAO"}


class RetificacaoSaldo(Base):
    """Saldo real de uma conta numa data. A diferença para o saldo calculado pelos
    movimentos vira um movimento de acerto (fora dos relatórios), recalculado
    sempre que os movimentos da conta mudam — os movimentos em falta podem ser
    inseridos depois e o acerto encolhe até zero."""
    __tablename__ = "retificacao_saldo"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    saldo: Mapped[int] = mapped_column(BigInteger)       # cêntimos
    # Valor de partida: sem histórico do capital investido, a diferença conta como capital (não como ganho).
    partida: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    nota: Mapped[str | None] = mapped_column(Text)
    criado_por: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped["Conta"] = relationship()


class ReceitaPrevista(Base):
    """Entrada recorrente esperada todos os meses numa conta (ordenado, carregamento
    do cartão refeição). Enquanto o crédito real desse mês não for importado,
    o fluxo de caixa do mês mostra-a como «prevista»."""
    __tablename__ = "receita_prevista"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id", ondelete="CASCADE"), index=True)
    descricao: Mapped[str] = mapped_column(String(100))
    dia: Mapped[int] = mapped_column(Integer)                      # dia do mês em que costuma entrar
    valor: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos; None = média dos últimos recebidos
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    padrao: Mapped[str | None] = mapped_column(String(200))        # texto do crédito real (ex.: VENCIMENTO)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped["Conta"] = relationship()
    categoria: Mapped["Categoria | None"] = relationship()


MOTIVOS_VIAGEM = {"lazer": "Lazer / férias", "trabalho": "Trabalho", "familia": "Família", "outro": "Outro"}


class ModeloViagem(Base):
    """Critérios para encontrar os gastos de uma viagem, reutilizáveis e afinados com o uso."""
    __tablename__ = "modelo_viagem"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80), unique=True)
    dias_antes: Mapped[int] = mapped_column(Integer, default=90)     # reservas (voos, alojamento) antes da partida
    dias_depois: Mapped[int] = mapped_column(Integer, default=3)
    tipos_conta: Mapped[str] = mapped_column(String(200), default="")  # CSV de TIPOS_CONTA usados em viagem
    contas: Mapped[str] = mapped_column(String(200), default="")       # CSV de ids de contas (além dos tipos)
    incluir: Mapped[str] = mapped_column(Text, default="")             # um padrão por linha (texto da descrição)
    excluir: Mapped[str] = mapped_column(Text, default="")
    categorias: Mapped[str] = mapped_column(String(300), default="")   # CSV de ids de categorias típicas
    valor_min: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos (valor absoluto)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    padroes: Mapped[list["ModeloPadrao"]] = relationship(back_populates="modelo", cascade="all, delete-orphan",
                                                         order_by="ModeloPadrao.padrao")


class ModeloPadrao(Base):
    """O que se aprendeu: quantas vezes um padrão de descrição foi confirmado / rejeitado como gasto de viagem."""
    __tablename__ = "modelo_padrao"
    __table_args__ = (UniqueConstraint("modelo_id", "padrao"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    modelo_id: Mapped[int] = mapped_column(ForeignKey("modelo_viagem.id", ondelete="CASCADE"), index=True)
    padrao: Mapped[str] = mapped_column(String(200))
    positivos: Mapped[int] = mapped_column(Integer, default=0)
    negativos: Mapped[int] = mapped_column(Integer, default=0)

    modelo: Mapped[ModeloViagem] = relationship(back_populates="padroes")


class Viagem(Base):
    __tablename__ = "viagem"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(100))
    destino: Mapped[str | None] = mapped_column(String(120))
    pais: Mapped[str | None] = mapped_column(String(80))
    data_ini: Mapped[date] = mapped_column(Date)
    data_fim: Mapped[date] = mapped_column(Date)
    pessoas: Mapped[int] = mapped_column(Integer, default=1)
    motivo: Mapped[str] = mapped_column(String(20), default="lazer")
    orcamento: Mapped[int | None] = mapped_column(BigInteger)          # cêntimos
    moeda: Mapped[str | None] = mapped_column(String(3))               # moeda local (informativa)
    notas: Mapped[str | None] = mapped_column(Text)
    modelo_id: Mapped[int | None] = mapped_column(ForeignKey("modelo_viagem.id", ondelete="SET NULL"))
    etiqueta_id: Mapped[int | None] = mapped_column(ForeignKey("etiqueta.id", ondelete="SET NULL"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    entidade: Mapped["Entidade"] = relationship()
    modelo: Mapped[ModeloViagem | None] = relationship()
    etiqueta: Mapped["Etiqueta | None"] = relationship()

    @property
    def dias(self) -> int:
        return (self.data_fim - self.data_ini).days + 1


class ViagemRejeitado(Base):
    """Movimento sugerido que o utilizador disse não ser desta viagem (não volta a ser sugerido)."""
    __tablename__ = "viagem_rejeitado"
    __table_args__ = (UniqueConstraint("viagem_id", "movimento_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    viagem_id: Mapped[int] = mapped_column(ForeignKey("viagem.id", ondelete="CASCADE"), index=True)
    movimento_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))


class ViagemConfirmado(Base):
    """Movimento de uma viagem que já foi ensinado (como confirmado) a um modelo — para não contar duas vezes."""
    __tablename__ = "viagem_confirmado"
    __table_args__ = (UniqueConstraint("viagem_id", "movimento_id", "modelo_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    viagem_id: Mapped[int] = mapped_column(ForeignKey("viagem.id", ondelete="CASCADE"), index=True)
    movimento_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))
    modelo_id: Mapped[int] = mapped_column(ForeignKey("modelo_viagem.id", ondelete="CASCADE"))


PERIODICIDADES = {"mensal": ("Mensal", 1), "bimestral": ("Bimestral", 2), "trimestral": ("Trimestral", 3),
                  "semestral": ("Semestral", 6), "anual": ("Anual", 12)}


class Subscricao(Base):
    """Subscrição / assinatura / contrato com pagamento periódico (streaming, telemóvel, ginásio, seguro…)."""
    __tablename__ = "subscricao"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(100))
    servico: Mapped[str | None] = mapped_column(String(40))          # chave do catálogo (netflix, meo, …)
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="SET NULL"))   # onde é debitada
    valor: Mapped[int] = mapped_column(BigInteger)                      # cêntimos, por pagamento
    periodicidade: Mapped[str] = mapped_column(String(12), default="mensal")
    dia_pagamento: Mapped[int | None] = mapped_column(Integer)          # dia do mês em que costuma ser debitada
    data_inicio: Mapped[date] = mapped_column(Date)
    fim_fidelizacao: Mapped[date | None] = mapped_column(Date)          # até quando há período de fidelização
    data_fim: Mapped[date | None] = mapped_column(Date)                 # cancelada / terminada
    padrao: Mapped[str | None] = mapped_column(String(200))             # texto do débito no extrato
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    conta: Mapped["Conta | None"] = relationship()
    categoria: Mapped["Categoria | None"] = relationship()

    @property
    def meses_periodo(self) -> int:
        return PERIODICIDADES.get(self.periodicidade, ("", 1))[1]

    @property
    def custo_mensal(self) -> float:
        return self.valor / self.meses_periodo

    def ativa_em(self, d: date) -> bool:
        return self.data_inicio <= d and (self.data_fim is None or self.data_fim >= d)


class SubscricaoIgnorada(Base):
    """Padrão recorrente que o utilizador disse não ser uma subscrição (não volta a ser sugerido)."""
    __tablename__ = "subscricao_ignorada"
    __table_args__ = (UniqueConstraint("entidade_id", "padrao"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"))
    padrao: Mapped[str] = mapped_column(String(200))


TIPOS_BEM = {"casa": "Casa", "carro": "Carro"}
USOS_BEM = {
    "casa": {"principal": "Habitação principal", "ferias": "Casa de férias", "arrendada": "Arrendada", "outra": "Outra"},
    "carro": {"principal": "Carro principal", "secundario": "Segundo carro", "outro": "Outro"},
}
ESTADOS_BEM = {"novo": "Novo / como novo", "bom": "Bom estado", "usado": "Precisa de obras / desgaste",
               "recuperar": "Para recuperar"}
COMBUSTIVEIS = {"gasolina": "Gasolina", "gasoleo": "Gasóleo", "hibrido": "Híbrido", "hibrido_plugin": "Híbrido plug-in",
                "eletrico": "Elétrico", "gpl": "GPL"}


class Bem(Base):
    """Património fora das contas: casas e carros, com atributos para avaliação e as despesas associadas."""
    __tablename__ = "bem"

    id: Mapped[int] = mapped_column(primary_key=True)
    entidade_id: Mapped[int] = mapped_column(ForeignKey("entidade.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(10))                      # casa | carro
    nome: Mapped[str] = mapped_column(String(100))
    uso: Mapped[str] = mapped_column(String(20), default="principal")  # ver USOS_BEM
    # Casa: assoalhadas (T2 = 2), área útil, localização (freguesia, concelho), ano de construção.
    tipologia: Mapped[int | None] = mapped_column(Integer)
    area_m2: Mapped[int | None] = mapped_column(Integer)
    localizacao: Mapped[str | None] = mapped_column(String(200))
    ano: Mapped[int | None] = mapped_column(Integer)                   # construção (casa) / matrícula (carro)
    estado: Mapped[str | None] = mapped_column(String(12))
    extras: Mapped[str | None] = mapped_column(String(200))           # garagem, piscina… / versão do carro
    # Carro
    marca: Mapped[str | None] = mapped_column(String(60))
    modelo: Mapped[str | None] = mapped_column(String(60))
    km: Mapped[int | None] = mapped_column(Integer)
    combustivel: Mapped[str | None] = mapped_column(String(16))
    # Aquisição, dívida e venda
    data_aquisicao: Mapped[date | None] = mapped_column(Date)
    valor_aquisicao: Mapped[int | None] = mapped_column(BigInteger)   # cêntimos
    emprestimo_conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="SET NULL"))
    data_venda: Mapped[date | None] = mapped_column(Date)
    valor_venda: Mapped[int | None] = mapped_column(BigInteger)
    # Revisão periódica do valor (meses entre avaliações; 0 = só à mão).
    revisao_meses: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    # Atribuição das despesas: textos do extrato (um por linha) e «por omissão» (fica com as despesas
    # da categoria Casa / Transportes que nenhum padrão atribuiu).
    padroes: Mapped[str | None] = mapped_column(Text)
    omissao: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    emprestimo_conta: Mapped["Conta | None"] = relationship()
    avaliacoes: Mapped[list["AvaliacaoBem"]] = relationship(back_populates="bem", cascade="all, delete-orphan",
                                                            order_by="AvaliacaoBem.data")

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_BEM.get(self.tipo, self.tipo)

    @property
    def uso_rotulo(self) -> str:
        return USOS_BEM.get(self.tipo, {}).get(self.uso, self.uso)

    @property
    def lista_padroes(self) -> list[str]:
        return [p.strip().upper() for p in (self.padroes or "").replace("|", "\n").splitlines() if p.strip()]

    @property
    def vendido(self) -> bool:
        return self.data_venda is not None


class AvaliacaoBem(Base):
    """Valor estimado de um bem numa data (IA ou indicado à mão) — o histórico das revisões."""
    __tablename__ = "avaliacao_bem"

    id: Mapped[int] = mapped_column(primary_key=True)
    bem_id: Mapped[int] = mapped_column(ForeignKey("bem.id", ondelete="CASCADE"), index=True)
    data: Mapped[date] = mapped_column(Date)
    valor: Mapped[int] = mapped_column(BigInteger)                    # cêntimos
    minimo: Mapped[int | None] = mapped_column(BigInteger)
    maximo: Mapped[int | None] = mapped_column(BigInteger)
    fonte: Mapped[str] = mapped_column(String(10), default="manual")  # ia | manual
    justificacao: Mapped[str | None] = mapped_column(Text)
    referencias: Mapped[str | None] = mapped_column(Text)             # URLs consultadas pela IA (uma por linha)
    confianca: Mapped[float | None] = mapped_column(Float)
    modelo: Mapped[str | None] = mapped_column(String(60))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    bem: Mapped[Bem] = relationship(back_populates="avaliacoes")


class ParIgnorado(Base):
    """Sugestão de ligação (débito ↔ crédito) que o utilizador disse não ser o mesmo dinheiro."""
    __tablename__ = "par_ignorado"
    __table_args__ = (UniqueConstraint("debito_id", "credito_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    debito_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))
    credito_id: Mapped[int] = mapped_column(ForeignKey("movimento.id", ondelete="CASCADE"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class SugestaoIA(Base):
    """Resposta da IA para um padrão de descrição (cache: cada padrão só é perguntado uma vez)."""
    __tablename__ = "sugestao_ia"

    id: Mapped[int] = mapped_column(primary_key=True)
    chave: Mapped[str] = mapped_column(String(200), unique=True)   # padrão + sentido, ex.: "ROYAL VERSAILLES|SAIDA"
    comerciante: Mapped[str | None] = mapped_column(String(120))
    atividade: Mapped[str | None] = mapped_column(String(200))
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    confianca: Mapped[float | None] = mapped_column(Float)
    modelo: Mapped[str | None] = mapped_column(String(60))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    categoria: Mapped["Categoria | None"] = relationship()


class Definicao(Base):
    """Preferências da app (chave → valor em texto), editáveis em Administração."""
    __tablename__ = "definicao"

    chave: Mapped[str] = mapped_column(String(50), primary_key=True)
    valor: Mapped[str] = mapped_column(String(500))


class Auditoria(Base):
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    acao: Mapped[str] = mapped_column(String(50))
    detalhe: Mapped[str | None] = mapped_column(Text)
