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
3. **Ligar o Gestor** — no Paperless: o teu nome (canto superior direito) › O meu perfil › Token da API › gerar.
   Na NAS, criar `/share/Container/gestor-contas/paperless.env` (só legível pelo admin) com:
   ```
   GESTOR_PAPERLESS_URL=http://paperless:8000
   GESTOR_PAPERLESS_TOKEN=<o token>
   GESTOR_PAPERLESS_LINK=http://192.168.1.232:8300
   ```
   `chmod 600 /share/Container/gestor-contas/paperless.env` e correr outra vez o `update.sh` do Gestor (copiar a versão
   nova). Os dois containers ficam na rede Docker `nas-apps` — nesta NAS um container não alcança outro pelo IP da
   NAS, por isso o Gestor usa `http://paperless:8000`. O token nunca vai para o repositório nem para o chat.

Atualizar o Paperless: correr outra vez `instalar_paperless.sh` (os dados ficam em `/share/Container/paperless/`).

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
