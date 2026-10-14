# Gestor de Contas

Gestor financeiro doméstico, multiutilizador e self-hosted, para a NAS QNAP. Este projeto
importa movimentos de bancos portugueses e gere contas à ordem, cartões, investimentos e
Certificados de Aforro. O design completo e as fases estão em [DESIGN.md](DESIGN.md).

## Estado: v0.4.0

- Login com várias pessoas: passwords em argon2, bloqueio ao fim de 5 tentativas falhadas,
  proteção CSRF e invalidação das sessões quando a password muda.
- Entidades **Família** e **Mãe** separadas, com permissões por entidade (Ver / Editar / Administrar).
- Contas: criar, editar e arquivar. Tipos: à ordem, poupança, cartão de crédito (com a conta
  que o paga e o ciclo do extrato), investimento, Certificados de Aforro e Revolut. Cada conta
  pode ter titulares.
- Backup automático diário da base SQLite (ficam guardados 30 dias), backup manual e registo de auditoria.
- Migrações Alembic aplicadas automaticamente no arranque.
- Deploy: GitHub Actions → GHCR → Container Station (ver [qnap/README.md](qnap/README.md)).
- **Importação de extratos**: Santander (.xls de «Saldos e movimentos»), ActivoBank e Crédito Agrícola
  (.xlsx). Há pré-visualização antes de gravar e deteção de movimentos repetidos, por isso podes
  reimportar períodos sobrepostos. Os saldos do banco são verificados linha a linha. Na 1.ª
  importação o saldo inicial é calculado a partir do extrato. Cada importação pode ser anulada.
  Os ficheiros originais ficam guardados em `data/uploads/`.
- Lista de movimentos por conta, com pesquisa e filtro por datas. O saldo atual é o indicado pelo
  banco, com um alerta quando difere do saldo calculado.
- **Qualificação de movimentos** (v0.4.0): categorias em 2 níveis (receita/despesa, com cor),
  etiquetas, «transferência entre contas próprias» e «excluir dos relatórios». A categoria muda-se
  diretamente na linha; há edição em massa e uma página de detalhe com notas. Regras automáticas
  correm em cada importação. As condições são: descrição contém / começa por / expressão regular,
  sentido, conta e intervalo de montante. As ações são: categoria, etiqueta, transferência,
  excluir. Há teste antes de gravar e «criar regra a partir deste movimento». As qualificações
  feitas à mão nunca são reescritas pelas regras. Vêm cerca de 50 categorias e 23 regras
  iniciais para descrições de bancos portugueses.
- **Painel** (inspirado no [Sure](https://sure.am)): património líquido com variação a 30 dias e
  evolução em 12 meses, ativos/passivos por tipo de conta, fluxo de caixa (sankey receitas →
  despesas/poupança) e despesas por categoria.
- **Relatórios**: mês, trimestre, 6 e 12 meses, ano até hoje ou período personalizado, com
  filtros por conta e etiqueta. Inclui comparação com o período anterior, taxa de poupança,
  receitas e despesas por categoria (com subcategorias), tendência mensal, estatísticas por
  categoria (média, mediana, mínimo e máximo) e exportação CSV (categorias × meses).

Próximo passo: orçamentos por categoria, deteção automática de transferências entre contas e
Revolut (precisa de um CSV de exemplo).
Para juntar um banco novo: [app/parsers/](app/parsers/).

## Desenvolvimento local

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m uvicorn --factory app.main:create_app --port 8200 --reload
```

Os dados locais ficam em `./data/` (ignorado pelo git). Para guardá-los noutra pasta, define `GESTOR_DATA_DIR`.

Para gerar uma migração depois de alterar `app/models.py`:
```bash
python -m alembic revision --autogenerate -m "descricao"
```

## Variáveis de ambiente

| Variável | Omissão | |
|---|---|---|
| `GESTOR_DATA_DIR` | `./data` (container: `/data`) | `gestor.db` + `secret.key` |
| `GESTOR_BACKUP_DIR` | `$GESTOR_DATA_DIR/backup` (container: `/backup`) | |
| `GESTOR_BACKUP_INTERVALO_HORAS` | `24` | `0` desativa os backups automáticos |
| `GESTOR_BACKUP_RETENCAO_DIAS` | `30` | |
| `GESTOR_COOKIE_SECURE` | `0` | `1` atrás de HTTPS |
| `GESTOR_SECRET_KEY` | gerada em `secret.key` | |
