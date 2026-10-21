# Deploy na QNAP

Passos genéricos da NAS (SSH, GHCR público, porque é que `docker compose`
falha por SSH): `C:\Projetos\QNAP-Container-Apps\SETUP.md`. Aqui só o que é
desta app.

| | |
|---|---|
| Imagem | `ghcr.io/seriocha/gestor-contas` |
| Porta | **8200** (interna 8000) |
| Dados | `/share/Container/gestor-contas/data` → `/data` (`gestor.db`, `secret.key`) |
| Backups | `/share/Container/gestor-contas/backup` → `/backup` |

## Primeira instalação

1. No PC: `./release.sh 0.1.0` e esperar pelo build no GitHub Actions.
2. Tornar o pacote GHCR público (SETUP.md §5) — só expõe o código, nunca dados.
3. Na NAS (File Station ou SSH): criar `/share/Container/gestor-contas/data` e `.../backup`.
4. Container Station → **Create Application** → colar `qnap/docker-compose.yml`.
5. Abrir `http://<IP_NAS>:8200` → página de configuração inicial → criar o administrador.
6. Administração → Utilizadores → criar a conta da tua mulher (por omissão: *Editar* na Família, sem acesso à Mãe).

## Atualizar

```bash
ssh snunes_adm@192.168.1.232
sh /share/Container/gestor-contas/update.sh
```
(copiar `qnap/update.sh` para essa pasta na 1.ª vez.)

## Backups para a cloud (pacote diário cifrado)

A app faz, sozinha:
- **backup da base** de 24 em 24 h para `/share/Container/gestor-contas/backup` (30 dias), cada cópia verificada;
- **cópia antes de cada migração** da base (atualização com mudanças de estrutura): `backup/antes-migracao-…db`;
- **pacote diário para a cloud** em `/share/Container/gestor-contas/backup/cloud`: um ficheiro
  `gestor-pacote-AAAAMMDD-HHMMSS.gcpk` com a base, `secret.key`, os extratos originais e um manifesto (sha256),
  **cifrado** com AES-256-GCM. Ficam 14 dias (`GESTOR_PACOTE_RETENCAO_DIAS`).

Em Administração › Backups vê-se onde está cada coisa (no container e na NAS), os tamanhos, o último backup e
pacote, a verificação de integridade e os avisos (backup atrasado, falhas, pouco espaço).

1. **Senha do pacote** — por SSH, como admin (escolhe uma senha longa e guarda-a também fora da NAS, num gestor de
   passwords: sem ela o pacote não se abre):
   ```bash
   vi /share/Container/gestor-contas/pacote.env      # uma linha: GESTOR_PACOTE_SENHA=...
   chmod 600 /share/Container/gestor-contas/pacote.env
   sh /share/Container/gestor-contas/update.sh       # copiar antes a versão nova de qnap/update.sh
   ```
   Em Administração › Backups, «Gerar pacote agora» para confirmar.
2. **Enviar para a cloud** — HBS3 (Hybrid Backup Sync) › Sincronização › **Sincronização unidirecional** (Um sentido):
   origem `/Container/gestor-contas/backup/cloud`, destino OneDrive / Google Drive / Dropbox (pasta, p. ex.,
   `Backups/GestorContas`), agendamento diário **depois das 04:00**. Desligar «eliminar ficheiros no destino» se
   quiseres que a cloud guarde mais do que os 14 dias da NAS. Não é preciso a cifra do HBS3: o pacote já vai cifrado.
3. **Testar o restauro** (de vez em quando) — num PC com o código:
   ```bash
   python -m app.backup verificar gestor-pacote-AAAAMMDD-HHMMSS.gcpk
   python -m app.backup restaurar gestor-pacote-AAAAMMDD-HHMMSS.gcpk /tmp/restauro
   ```
   (pede a senha, ou lê `GESTOR_PACOTE_SENHA`). Para repor na NAS: parar o container, trocar o conteúdo de
   `/share/Container/gestor-contas/data` pelo da pasta restaurada, arrancar.

## Sugestões por IA (Claude API) — opcional

Em **Categorias e regras › Por classificar**, o botão «Pedir sugestões à IA»
pergunta à Claude API que comerciante/serviço está por trás de cada padrão de
descrição sem categoria. Só vai o texto do padrão e se é entrada ou saída (sem
valores, datas, contas nem transferências/MB WAY); cada padrão é perguntado uma
vez e a resposta fica guardada.

Criar a chave em console.anthropic.com e, na NAS (SSH, como admin):

```bash
vi /share/Container/gestor-contas/ia.env      # uma linha: ANTHROPIC_API_KEY=sk-ant-...
chmod 600 /share/Container/gestor-contas/ia.env
sh /share/Container/gestor-contas/update.sh
```

Opcional, no mesmo ficheiro: `GESTOR_IA_MODELO=claude-haiku-5-5` (o modelo por
omissão). Copiar a versão nova de `qnap/update.sh` para a NAS (é ela que lê o
`ia.env`). A chave nunca vai para o repositório nem para o chat.

## Extratos bancários numa pasta partilhada (leitura automática)

1. Criar a pasta partilhada `Extratos` no QTS (Painel de Controlo › Pastas partilhadas) — fica em `/share/Extratos`.
2. Dentro, uma subpasta por banco: `Santander/`, `ActivoBank/`, `Revolut/`, `BiG/`, `Credito Agricola/`…
   (o nome ajuda a reconhecer o banco; a conta é detetada pelo formato, n.º de conta e movimentos já importados).
3. Correr outra vez o `update.sh` (monta `/share/Extratos` em `/importar`). Noutra pasta:
   `EXTRATOS_DIR=/share/Outra sh update.sh`.

A app lê a pasta de hora a hora (configurável em Importar › Por validar, onde também há «Ler a pasta agora»).
Nada é importado sozinho: cada ficheiro novo fica **por validar** em Importar › Por validar; «Ver e validar» mostra a
pré-visualização habitual (com a conta, que se pode mudar). Validado, é importado e o ficheiro passa para
`<banco>/importados/AAAA-MM-DD_<ficheiro>`. Rejeitado, fica na pasta e não volta a ser proposto.

## Paperless-ngx (documentos: faturas, recibos, garantias)

O Paperless-ngx corre ao lado do Gestor (porta **8300**), com OCR em português; o Gestor procura lá os documentos e
liga-os aos movimentos e aos itens do recheio. Os scripts estão em `qnap/paperless/`.

1. **Instalar** — copiar `qnap/paperless/*.sh` para `/share/Container/paperless/` e, por SSH, como `admin`:
   ```bash
   sh /share/Container/paperless/instalar_paperless.sh
   ```
   O 1.º arranque demora uns minutos (instala o OCR em português). Depois, criar o teu utilizador (a password é
   pedida no terminal, não fica em ficheiro nenhum):
   ```bash
   docker exec -it paperless python3 manage.py createsuperuser
   ```
   Abrir `http://192.168.1.232:8300` e entrar.
2. **Trazer os documentos que já tens** — o script **copia** (os originais não são mexidos; o Paperless apaga as
   cópias da sua pasta de entrada depois de as importar). As subpastas passam a etiquetas:
   ```bash
   DOCS_DIR=/share/Documentos sh /share/Container/paperless/copiar_documentos.sh
   ```
   (trocar `/share/Documentos` pela tua pasta; caminho sem espaços). PDF e imagens; Word/Excel precisam do Tika.
3. **Ligar o Gestor** — na NAS, criar `/share/Container/gestor-contas/paperless.env` (só legível pelo admin) com:
   ```
   GESTOR_PAPERLESS_URL=http://paperless:8000
   GESTOR_PAPERLESS_LINK=http://192.168.1.232:8300
   ```
   `chmod 600 …/paperless.env` e correr outra vez o `update.sh` do Gestor. Os dois containers ficam na rede Docker
   `nas-apps` — nesta NAS um container não alcança outro pelo IP da NAS, por isso o Gestor usa `http://paperless:8000`.
4. **Um token por pessoa** — cada utilizador do Gestor tem conta no Paperless e guarda o SEU token em
   **Gestor › Perfil › Paperless** (no Paperless: o teu nome › O meu perfil › Token da API › gerar). O token é testado
   antes de ser guardado e fica cifrado. O Gestor pesquisa e mostra miniaturas com o token de quem está a usar: cada
   um só vê os documentos a que tem acesso no Paperless.
   - No Paperless: criar um utilizador por pessoa e um **grupo por entidade** (ex.: «Família», «Mãe»); nos documentos
     (ou em lote: selecionar › Permissões) dar «ver» ao grupo da entidade.
   - Opcional: `GESTOR_PAPERLESS_TOKEN=…` no `paperless.env` — token partilhado, usado **só por administradores** que
     ainda não guardaram o seu.
   O token nunca vai para o repositório nem para o chat.

Atualizar o Paperless: correr outra vez `instalar_paperless.sh` (os dados ficam em `/share/Container/paperless/`).

## Immich (fotografias das viagens)

O Gestor lê no Immich que já corre na NAS as fotos tiradas nas datas de cada viagem (com a cidade/país do EXIF) e pode
criar lá o álbum da viagem. Nada é copiado: as fotos ficam no Immich.

1. **Pôr o Immich na rede `nas-apps`** — por SSH, como `admin` (o nome do container vê-se com `docker ps | grep immich`;
   normalmente `immich_server`):
   ```
   docker network connect nas-apps immich_server
   ```
   Se o Immich for recriado (atualização no Container Station), repetir o comando — ou juntar a rede `nas-apps` como
   rede externa no docker-compose do Immich.
2. **Ligar o Gestor** — criar `/share/Container/gestor-contas/immich.env` (só legível pelo admin):
   ```
   GESTOR_IMMICH_URL=http://immich_server:2283
   GESTOR_IMMICH_LINK=http://192.168.1.232:2283
   ```
   `chmod 600 …/immich.env` e correr outra vez o `update.sh` do Gestor.
3. **Uma chave por pessoa** — no Immich: avatar › Definições da conta › Chaves de API › Nova chave (leitura de fotos e
   álbuns, criar/editar álbuns) › copiar; no Gestor: **Perfil › Immich** › colar › Guardar (é testada antes).
   Opcional: `GESTOR_IMMICH_TOKEN=…` no `immich.env` — chave partilhada, só para administradores sem chave pessoal.

## Segurança

- **Não abrir a porta 8200 no router.** Acesso de fora só por VPN (QVPN/Tailscale).
- Com HTTPS (reverse proxy do QTS ou Tailscale), pôr `GESTOR_COOKIE_SECURE=1`.
- `secret.key` assina as sessões: apagá-lo termina todas as sessões (não perde dados).
- Incluir `/share/Container/gestor-contas/backup` num job HBS3 para fora da NAS.

## Restaurar um backup

```bash
docker stop gestor-contas
cp /share/Container/gestor-contas/backup/gestor-AAAAMMDD-HHMMSS.db /share/Container/gestor-contas/data/gestor.db
rm -f /share/Container/gestor-contas/data/gestor.db-wal /share/Container/gestor-contas/data/gestor.db-shm
docker start gestor-contas
```
