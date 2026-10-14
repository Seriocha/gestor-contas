"""Modelo de dados — Fase 1 (utilizadores, entidades, permissões, contas).

Montantes em cêntimos (inteiro), nunca float. Ver DESIGN.md §5 para o
modelo completo; as tabelas das fases seguintes entram por migração Alembic.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Table, Text,
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
}


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
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
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
    iban_mascarado: Mapped[str | None] = mapped_column(String(40))
    # Cartões de crédito: conta à ordem que paga o cartão + ciclo do extrato.
    conta_pagamento_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    dia_fecho: Mapped[int | None] = mapped_column(Integer)
    dia_pagamento: Mapped[int | None] = mapped_column(Integer)
    saldo_inicial: Mapped[int] = mapped_column(BigInteger, default=0)  # cêntimos
    data_saldo_inicial: Mapped[date | None] = mapped_column(Date)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    notas: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    entidade: Mapped[Entidade] = relationship(back_populates="contas")
    instituicao: Mapped[Instituicao] = relationship()
    conta_pagamento: Mapped["Conta | None"] = relationship(remote_side="Conta.id")
    titulares: Mapped[list[Utilizador]] = relationship(secondary=conta_titular)

    @property
    def tipo_rotulo(self) -> str:
        return TIPOS_CONTA.get(self.tipo, self.tipo)


class Auditoria(Base):
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    utilizador_id: Mapped[int | None] = mapped_column(ForeignKey("utilizador.id", ondelete="SET NULL"))
    acao: Mapped[str] = mapped_column(String(50))
    detalhe: Mapped[str | None] = mapped_column(Text)
