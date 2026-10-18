# Servidor MCP na nuvem (só navegador)

Pasta independente: tem a própria cópia do pacote `automacao`, então não altera nada no projeto do PC.
Roda um Chromium headless num container e expõe as ferramentas `browser_*` por HTTP (Streamable HTTP),
para usar como conector personalizado no claude.ai (web, desktop e celular) sem depender do PC.

Limites: o navegador da nuvem não tem os seus logins, cookies nem acesso à sua rede local.
As ferramentas `pc_*` e `android_*` continuam só no PC.

## Testar localmente (no PC, dentro desta pasta)

    ..\.venv\Scripts\python -m testes.teste_http

## Publicar no Hugging Face Space

Uma vez: `..\.venv\Scripts\python -m pip install huggingface_hub` e `..\.venv\Scripts\hf auth login`
(token de escrita, criado em https://huggingface.co/settings/tokens).

    ..\.venv\Scripts\python publicar_space.py

O script cria o Space público `<seu usuário>/automacao-mcp`, grava o token de acesso como segredo
`MCP_TOKEN`, envia os arquivos, acompanha o build e imprime a URL do conector:
`https://<usuario>-automacao-mcp.hf.space/mcp/<token>`. Rodar de novo atualiza o código e mantém a URL;
`--novo-token` troca o token (use se a URL vazar). O token fica em `.mcp_token` nesta pasta.
O código do Space fica público; o token não. No plano gratuito o Space dorme após 48 h sem uso e
acorda na primeira chamada, que pode demorar cerca de um minuto.

## Trocar o token (se a URL do conector vazar)

No PC, com uma chave da API do Render (https://dashboard.render.com/u/settings#api-keys):

    $env:RENDER_API_KEY = "<chave>"     # PowerShell; não cole a chave em chats
    python trocar_token.py

O script grava um token novo em `MCP_TOKEN`, publica, confere que o token novo entra e o antigo recebe
401, guarda o token em `.mcp_token` e imprime a nova URL. Se a API ou o deploy falharem, o token antigo
continua valendo. Depois, troque a URL no conector do claude.ai e no app do celular.
Teste sem rede: `python -m testes.teste_trocar_token`.

## Registrar

- claude.ai: Configurações > Conectores > Adicionar conector personalizado, colar a URL completa.
  Vale para o claude.ai na web, o Claude Desktop e o app de celular da mesma conta.
- Claude Code: `claude mcp add --transport http --scope user automacao-nuvem <URL>`

## Outro host (Cloud Run, Azure, VM)

    docker build -t automacao-mcp .
    docker run -p 8000:8000 -e MCP_TOKEN=<token de 32+ caracteres> automacao-mcp

Endpoint `/mcp` (token no cabeçalho `Authorization: Bearer` ou no caminho `/mcp/<token>`), saúde em `/saude`.
Sem estado de sessão: todos os clientes compartilham o mesmo navegador; serve a um único usuário.

## Celular Android sem PC (app Automação)

O servidor também expõe as ferramentas `android_*`, repassadas a um app que roda no próprio celular
(pasta `android/`, Java sem bibliotecas externas, serviço de acessibilidade). O app abre um WebSocket
para `wss://<servidor>/celular/<token>` e executa os comandos: árvore da tela, captura (Android 11+),
toque, toque longo, deslize, escrita com acentos, teclas do sistema, abrir e listar apps.

O GitHub Actions compila o APK a cada push em `android/` e publica em
https://github.com/RenanKohler/automacao-mcp/releases/download/android/automacao-celular.apk

No celular: instale o APK, abra o app, cole a URL do conector (`https://.../mcp/<token>`), salve e ligue
o serviço "Automação" em Configurações > Acessibilidade. Se o Android bloquear como configuração restrita,
abra Informações do app > ⋮ > Permitir configurações restritas. Permita também rodar em segundo plano.

Teste da ponte no servidor, sem celular: `python -m testes.teste_celular`.

### Assinatura do APK

A chave de assinatura fica nos segredos do repositório (`ANDROID_KEYSTORE_B64` e `ANDROID_KEYSTORE_SENHA`),
nunca no código. Para criá-la, uma vez, no PC: `python android/gerar_chave.py` (precisa do pacote
`cryptography` ou do `keytool` do Java). O script guarda a chave em `~/.automacao/` e grava os segredos
pelo GitHub CLI, se estiver logado, ou abre a página de segredos para você colar. Sem os segredos o
workflow falha e a release anterior continua no ar. A chave antiga, que esteve no repositório público até
18/10/2026, é recusada pelo workflow. Quem instalou o app antes dessa data precisa desinstalar uma vez antes
de instalar o APK assinado com a chave nova.
