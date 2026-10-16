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
| Arquivo de extratos (opcional) | pasta partilhada indicada em `arquivo.conf` → `/arquivo` |

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

## Arquivo de extratos numa pasta partilhada

Cada importação guarda uma cópia do extrato em
`<arquivo>/<Entidade>/<Banco>/<Conta>/<Ano>/AAAA-MM-DD_a_AAAA-MM-DD_<Conta>.xls`.
Com uma pasta partilhada montada, essa árvore fica visível na rede (SMB) e entra
nos backups da NAS; a subpasta `_Entrada` funciona como caixa de entrada: os
extratos lá gravados aparecem em **Importar** com a conta sugerida e, depois de
importados, passam para a árvore.

```bash
echo /share/Documentos/Gestor-Contas > /share/Container/gestor-contas/arquivo.conf
sh /share/Container/gestor-contas/update.sh
```

O caminho não pode ter espaços. Copiar também a versão nova de `qnap/update.sh` para a NAS (é ela que lê o
`arquivo.conf`). Verificar em **Administração › Importadores › Arquivo de
extratos** («Pasta da NAS montada», «acessível») e carregar em «Arquivar as
importações anteriores». O contentor corre como root: os ficheiros e pastas que
cria ficam com permissões abertas (666/777) para se poderem mover e apagar por
SMB — o acesso fica controlado pelas permissões da partilha no QTS.

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
