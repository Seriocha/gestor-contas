# Gestor de Contas

Gestor financeiro doméstico, multiutilizador e self-hosted, para a NAS QNAP. Este projeto
importa movimentos de bancos portugueses e gere contas à ordem, cartões, investimentos e
Certificados de Aforro. O design completo e as fases estão em [DESIGN.md](DESIGN.md).

## Estado: v0.15.0

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
- **Cartão de crédito** (v0.6.0): importação do extrato do cartão Santander («Consulta Movimentos
  Extracto», .xls). Compras ficam negativas e pagamentos/devoluções positivos; os movimentos ainda
  em curso ficam de fora até serem extratados. O pagamento do cartão é marcado automaticamente como
  transferência nos dois lados: o crédito «PAG…» no cartão e o débito do mesmo valor (±7 dias) na
  conta que o paga. Assim a despesa conta só uma vez (as compras). A página do cartão mostra a
  dívida, as compras desde o último fecho, o próximo fecho e a data de pagamento. Um extrato de
  cartão importado numa conta à ordem é recusado.
- **v0.15.0 — retificação de saldo e ordenação de contas:** nas contas de Certificados de Aforro
  e de Investimento, «Retificar saldo» acerta a conta ao valor real numa data. A diferença vira
  um acerto fora dos relatórios. O acerto é recalculado sempre que os movimentos da conta mudam,
  por isso os movimentos em falta podem ser inseridos depois e a diferença encolhe até
  desaparecer. Enquanto houver diferença por explicar, a conta mostra um aviso na sua página, na
  lista de contas e no painel. A lista de contas ordena-se clicando no título das colunas (Conta,
  Tipo, Instituição, Saldo), e a escolha fica memorizada.
- **v0.14.0 — pares visíveis:** na lista de movimentos, os dois lados de uma ligação têm a mesma
  cor (barra lateral) e a mesma letra (etiqueta «↔ A»). Passar o rato sobre a etiqueta realça os
  dois; clicar salta para o outro lado, ou abre-o se estiver noutra conta (a etiqueta indica qual).
  Corrigido um corte à direita dos cartões de movimentos no telemóvel.
- **v0.13.0 — Banco BiG e Importadores:** importação do CSV de movimentos da conta à ordem do
  BiG. A descrição duplicada fica só com a versão completa. «EUR» é o valor com sinal e
  «Contabilistico*» o saldo. As aplicações em fundos e o «Levantamento SuperConta» contam como
  transferência. *Administração › Importadores* mostra uma tabela com os atributos de cada
  importador: extrato, conta de destino, formato, onde exportar, colunas, ordem, saldo,
  particularidades e utilização. A tabela é gerada a partir da ficha (`FICHA`) de cada parser.
- **v0.12.0 — «O que mudou»:** clicar (ou tocar) num ponto do gráfico do património, no Painel
  ou nos Relatórios, mostra o que mudou desde o ponto anterior: a variação, as receitas, as
  despesas e as transferências internas; a variação por conta (saldo inicial e final, quanto os
  movimentos explicam e a parte **não explicada**, que é sinal de um extrato em falta); e os
  movimentos do período, do maior para o menor, com ligação para a lista completa. Os scripts
  passaram a levar a versão no endereço, para o browser não usar versões antigas em cache.
- **v0.11.0:** os ativos e passivos do painel podem ser agrupados por tipo, por banco ou sem
  grupos, e ordenados por nome, saldo ou banco; a escolha fica memorizada. Nos movimentos há
  pesquisa por valor (escrever «40,05» encontra entradas e saídas desse montante), intervalo de
  valor e ordenação clicando no título de cada coluna. Administração › Preferências tem a opção
  «Mostrar NIB/IBAN completo»; o NIB passa a ser guardado tal como é escrito. Movimentos manuais:
  «+ Movimento» em qualquer conta, a pensar nos depósitos e fundos que não se importam. Podem ser
  editados e apagados, e mostram os candidatos de ligação (valor simétrico noutra conta, até 10
  dias de diferença).
- **Novo visual e telemóvel** (v0.10.0), inspirado no [votoavoto.pt](https://votoavoto.pt):
  tipografia Bricolage Grotesque e Geist (incluídas na app, sem CDN), fundo cinza-claro,
  cartões brancos de cantos largos e amarelo #FFD23F nas ações, com modo escuro. No computador
  há uma barra lateral e o conteúdo ocupa a largura disponível (até 1600px). No telemóvel
  (referência: Galaxy S24, 360×780) há barra de topo, menu deslizante e abas em baixo; os
  movimentos aparecem como cartões, os filtros são recolhíveis e as ações em massa só aparecem
  quando são precisas. Nenhuma página excede a largura do ecrã. Com o manifesto e os ícones, a
  app pode ser instalada no ecrã principal do telemóvel. Para abrir como aplicação, sem a barra
  do browser, o acesso tem de ser por HTTPS.
- **Revolut** (v0.9.0): extrato CSV, em português ou inglês. Cada subconta (produto e moeda) é
  importada na conta certa: «Atual» na conta Revolut, «Depósito»/Conta reforçada numa conta
  poupança Revolut. As comissões passam a movimentos à parte, em «Comissões bancárias», e os
  movimentos revertidos ficam de fora. Carregamentos, câmbios, transferências entre subcontas e
  transferências para investimento contam como transferência. **Ligação automática de
  transferências entre contas:** valor simétrico, até 5 dias de diferença, e o outro lado com
  «REVOLUT» na descrição ou já marcado como transferência. A leitura de CSV passou a aceitar aspas
  e vírgulas dentro das descrições.
- **Empréstimo à habitação** (v0.8.0): novo tipo de conta, que conta como passivo. Guarda a TAN
  (5 casas decimais), o tipo de taxa, o indexante, o spread, o total de prestações e a conta de
  débito. O extrato do empréstimo Santander («Consulta Movimentos Empréstimo», .xls) importa cada
  prestação com os valores exatos do banco: a prestação é transferência e fica ligada ao débito
  «Cred. Habitacao…» na conta à ordem; os juros são despesa («Casa › Juros do empréstimo»); o saldo
  é o saldo em dívida do banco. Se o débito chegar antes do extrato, a prestação é estimada
  (capital × TAN × dias/360) e depois substituída pelos valores reais. A página do empréstimo mostra
  o capital em dívida, a próxima prestação, o fim previsto, os juros e o capital do ano, as
  prestações registadas e um quadro previsional de 12 meses, com «Atualizar taxa» e «Acertar capital».
- **Pagamentos de cartão ligados** (v0.7.0): os dois lados de cada pagamento ficam ligados.
  A página de detalhe tem uma secção «Ligado a», com link para o outro lado e o botão
  «Desligar». O relatório *Relatórios › Pagamentos de cartão* mostra os pares encontrados e os
  pagamentos sem par, com os débitos candidatos (mesmo valor, ±30 dias) e um botão «Ligar».
  O botão «Procurar pares agora» liga também os pagamentos marcados pela v0.6.0.
- **Repor qualificações** (v0.5.0): os filtros «Qualificados à mão» e «Qualificados por regra»
  e a indicação «à mão»/«auto» em cada linha. As ações «Repor e reaplicar as regras» e «Repor e
  deixar sem categoria» desfazem categoria, transferência e excluído, mantendo etiquetas e notas.
  Todas as ações em massa podem aplicar-se aos movimentos selecionados ou a todos os do filtro
  (todas as páginas), com confirmação. Acrescentar ou retirar etiquetas já não marca o movimento
  como «à mão».
- **Painel** (inspirado no [Sure](https://sure.am)): património líquido com variação a 30 dias e
  evolução em 12 meses, ativos/passivos por tipo de conta, fluxo de caixa (sankey receitas →
  despesas/poupança) e despesas por categoria.
- **Relatórios**: mês, trimestre, 6 e 12 meses, ano até hoje ou período personalizado, com
  filtros por conta e etiqueta. Inclui comparação com o período anterior, taxa de poupança,
  receitas e despesas por categoria (com subcategorias), tendência mensal, estatísticas por
  categoria (média, mediana, mínimo e máximo) e exportação CSV (categorias × meses).

Próximo passo: orçamentos por categoria, deteção automática de transferências entre contas e
Revolut (precisa de um CSV de exemplo).
Para juntar um banco novo: criar `app/parsers/<banco>.py` com `Parser` (chave, banco, `FICHA`, `detetar`, `ler`) e acrescentá-lo a `todos()` em [app/parsers/__init__.py](app/parsers/__init__.py).

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
