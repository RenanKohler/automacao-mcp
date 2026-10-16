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

## Registrar

- claude.ai: Configurações > Conectores > Adicionar conector personalizado, colar a URL completa.
  Vale para o claude.ai na web, o Claude Desktop e o app de celular da mesma conta.
- Claude Code: `claude mcp add --transport http --scope user automacao-nuvem <URL>`

## Outro host (Cloud Run, Azure, VM)

    docker build -t automacao-mcp .
    docker run -p 8000:8000 -e MCP_TOKEN=<token de 32+ caracteres> automacao-mcp

Endpoint `/mcp` (token no cabeçalho `Authorization: Bearer` ou no caminho `/mcp/<token>`), saúde em `/saude`.
Sem estado de sessão: todos os clientes compartilham o mesmo navegador; serve a um único usuário.
