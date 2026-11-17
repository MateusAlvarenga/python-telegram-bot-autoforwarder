# Copiador agendado de mensagens do Telegram

Bot privado que mantém rotinas de cópia contínua entre grupos, canais e tópicos. A configuração, o histórico de execuções e o último ID verificado de cada rotina ficam no PostgreSQL.

## Como funciona

- O bot de controle recebe comandos do proprietário somente no chat privado.
- Uma sessão de usuário Telethon consulta o histórico da origem. A Bot API não oferece busca de histórico; por isso, a sessão Telethon é necessária para descobrir mensagens novas.
- A Bot API `copyMessages` copia as mensagens em lotes de até 100, sem adicionar o cabeçalho de encaminhamento. `COPIER_BOT_TOKEN` permite usar outro bot como remetente; se estiver vazio, o próprio `BOT_TOKEN` é usado.
- Ao criar uma rotina, o cursor começa na mensagem mais recente da origem ou do tópico selecionado; assim, somente mensagens posteriores à criação são copiadas. A cada execução são copiadas até 1.000 mensagens novas; o cursor é salvo após cada lote confirmado e retoma do ponto salvo na próxima execução.
- Cada rotina pode limitar a quantidade de posts enviados por execução; sem limite é o padrão. Um álbum conta como um post e é copiado como uma unidade. A migração adiciona o limite como `NULL`, então as rotinas já existentes continuam ilimitadas.
- Execuções automáticas e manuais usam lease persistido no PostgreSQL para impedir que a mesma rotina rode simultaneamente. A próxima execução automática é agendada após o término da atual.
- Pausar impede execuções futuras. Parar também solicita a interrupção após o lote em andamento. Excluir mantém o histórico.
- Um timeout depois de o Telegram aceitar uma cópia, mas antes de o serviço receber a resposta, pode causar repetição: a Bot API não oferece chave de idempotência.

O bot remetente precisa ser membro da origem e ter permissão para publicar no destino. A sessão Telethon também precisa ter acesso de leitura à origem. Mensagens que o Telegram não permite copiar são ignoradas e registradas nos logs; erros que interrompem a rotina ficam no histórico.

Use uma única réplica Railway deste serviço. Embora o lease evite execuções simultâneas entre réplicas, o comando para interromper uma execução envia o sinal ao processo que a iniciou.

## Configuração local

1. Crie o bot de controle com o [@BotFather](https://t.me/BotFather) e obtenha o token.
2. Obtenha seu ID numérico do Telegram para `OWNER_ID`.
3. Crie um app em [my.telegram.org](https://my.telegram.org) e obtenha `API_ID` e `API_HASH`.
4. Copie `.env.example` para `.env` e preencha `API_ID` e `API_HASH`.
5. Gere a sessão Telethon:

   ```powershell
   python -m bot.auth
   ```

   Informe o telefone, o código recebido e, se necessário, a senha de verificação em duas etapas. Copie a sessão exibida para `TELETHON_SESSION`. Ela equivale às credenciais da conta: mantenha-a secreta e não a compartilhe.
6. Preencha `BOT_TOKEN`, `OWNER_ID` e `DATABASE_URL` no `.env`. PostgreSQL é necessário.
7. Instale e execute:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python -m bot.main
   ```

## Comandos do proprietário

Todos os comandos são usados no chat privado do bot. Chats podem ser IDs numéricos ou `@username`; use `-` para indicar que não há tópico.

| Comando | Descrição |
| --- | --- |
| `/rotina_criar nome|chat_origem|topico_origem_ou_-|chat_destino|topico_destino_ou_-|intervalo_segundos[|limite_de_posts_ou_-]` | Criar uma rotina inicialmente pausada; omitir o limite ou usar `-` deixa ilimitada |
| `/rotina_editar id|nome|chat_origem|topico_origem_ou_-|chat_destino|topico_destino_ou_-|intervalo_segundos[|limite_de_posts_ou_-]` | Editar uma rotina; alterar a origem reinicia seu cursor |
| `/rotinas` | Listar rotinas |
| `/rotina id` | Mostrar configuração, checkpoint e próxima execução |
| `/rotina_cursor id|ultimo_id_de_mensagem` | Definir o último ID verificado; use `0` para recomeçar do histórico. Não funciona durante uma execução |
| `/rotina_ativar id` | Ativar; a primeira execução é iniciada imediatamente |
| `/rotina_pausar id` | Pausar execuções futuras |
| `/rotina_executar id` | Disparar manualmente, mesmo que a rotina esteja pausada |
| `/rotina_parar id` | Pausar e interromper após o lote atual |
| `/rotina_excluir id` | Excluir uma rotina parada, mantendo o histórico |
| `/historico id` | Mostrar as últimas execuções e erros |
| `/ajuda` | Listar comandos |

Exemplo:

```text
/rotina_criar novidades|-1001234567890|456|-1009876543210|-|300
/rotina_criar destaques|-1001234567890|-|-1009876543210|-|300|20
/rotina_cursor 1|5000
/rotina_ativar 1
/rotina_executar 1
```

O exemplo lê o tópico `456` da origem, copia para o chat de destino sem escolher tópico e agenda a rotina a cada 300 segundos. O comando de cursor faz a próxima execução buscar mensagens com ID maior que `5000`; use `0` para voltar ao início do histórico acessível.

## Implantação no Railway

1. Crie um serviço PostgreSQL e um serviço para este repositório.
2. Configure as variáveis no serviço do bot:

   ```text
   BOT_TOKEN=<token do bot de controle>
   OWNER_ID=<ID numérico do proprietário>
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   API_ID=<API ID do my.telegram.org>
   API_HASH=<API hash do my.telegram.org>
   TELETHON_SESSION=<sessão gerada por python -m bot.auth>
   COPIER_BOT_TOKEN=<opcional; vazio usa BOT_TOKEN>
   LOG_LEVEL=INFO
   ```

   Ajuste `Postgres` se o serviço de banco tiver outro nome.
3. O deploy usa o `Dockerfile` e a configuração existente em `railway.json`. As tabelas são criadas automaticamente ao iniciar.

## Testes

```powershell
python -m unittest discover -s tests
```
