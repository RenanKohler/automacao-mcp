"""Linha de comando: python -m automacao [opções] ["pedido"]

Sem pedido na linha de comando, abre um prompt interativo em que cada linha é um pedido.
A chave da API vem de ANTHROPIC_API_KEY (variável de ambiente ou arquivo .env).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import ExitStack
from pathlib import Path

import anthropic


def load_dotenv(*paths: Path) -> None:
    """Carrega VAR=valor de arquivos .env sem sobrescrever variáveis já definidas."""
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if name and name not in os.environ:
                os.environ[name] = value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="automacao",
        description="Executa pedidos em linguagem natural no navegador e no PC usando a API da Anthropic.",
    )
    p.add_argument("pedido", nargs="*", help="pedido a executar; sem ele, abre o modo interativo")
    p.add_argument("--modo", choices=["browser", "pc", "ambos"], default="browser",
                   help="quais drivers oferecer ao modelo (padrão: browser)")
    p.add_argument("--modelo", default="claude-opus-5-5", help="modelo da API (padrão: claude-opus-5-5)")
    p.add_argument("--esforco", choices=["low", "medium", "high", "xhigh", "max"], default="high",
                   help="nível de esforço do modelo (padrão: high)")
    p.add_argument("--max-turnos", type=int, default=60, help="limite de turnos por pedido (padrão: 60)")

    b = p.add_argument_group("navegador")
    b.add_argument("--hosts-permitidos", default="",
                   help="lista de hosts permitidos, separados por vírgula (subdomínios incluídos). Vazio: qualquer host público")
    b.add_argument("--permitir-rede-local", action="store_true",
                   help="permite localhost e endereços privados (desligado por padrão)")
    b.add_argument("--headless", action="store_true", help="navegador sem janela")
    b.add_argument("--viewport", default="1280x720", help="tamanho da área da página (padrão: 1280x720)")
    b.add_argument("--chrome", dest="chrome_path", help="caminho do executável do Chrome/Chromium/Edge")
    b.add_argument("--cdp", help="conecta a um Chrome já aberto (ex.: http://127.0.0.1:9222) em vez de abrir um novo")
    b.add_argument("--downloads", help="pasta para downloads; sem ela, downloads são recusados")
    b.add_argument("--uploads", help="pasta com os arquivos que podem ser enviados; habilita file_upload (com confirmação)")
    b.add_argument("--javascript", action="store_true", help="habilita javascript_exec (com confirmação)")
    b.add_argument("--confirmar-browser", choices=["controlados", "tudo"], default="controlados",
                   help="no navegador, pedir aprovação só para javascript/upload (padrão) ou para toda ação")

    c = p.add_argument_group("PC")
    c.add_argument("--confirmar-pc", choices=["teclado", "tudo", "nada"], default="teclado",
                   help="no PC, pedir aprovação antes de digitar (padrão), antes de toda ação ou nunca")

    p.add_argument("--sem-limpeza-contexto", action="store_true",
                   help="não usa a edição de contexto do servidor para remover capturas antigas")
    p.add_argument("--sem-fallback", action="store_true", help="não usa o fallback de modelo do servidor")
    p.add_argument("-v", "--verbose", action="store_true", help="registra detalhes de depuração")
    return p


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(Path.cwd() / ".env", root / ".env")
    args = build_parser().parse_args(argv)
    if sys.platform == "win32":
        os.system("")  # ativa as sequências de cor ANSI no console do Windows
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Defina ANTHROPIC_API_KEY (variável de ambiente ou arquivo .env na pasta do projeto).", file=sys.stderr)
        return 2

    from .agente import Agente, AgentConfig
    from .confirmacao import browser_confirm, computer_confirm

    with ExitStack() as stack:
        toolsets = []
        if args.modo in ("browser", "ambos"):
            from anthropic.tools.browser import BetaLocalFilePolicy

            from .navegador import NavegadorCDP
            from .politica import URLPolicy

            width, height = (int(v) for v in args.viewport.lower().split("x"))
            policy = URLPolicy(
                allowed_hosts=[h for h in args.hosts_permitidos.split(",") if h.strip()],
                allow_private=args.permitir_rede_local,
            )
            configs = {}
            if args.javascript:
                configs["javascript_exec"] = {"enabled": True}
            file_policy = None
            if args.uploads:
                configs["file_upload"] = {"enabled": True}
                file_policy = BetaLocalFilePolicy(upload_roots=[args.uploads],
                                                  download_dir=args.downloads, expose_download_paths=False)
            print("Abrindo o navegador...", file=sys.stderr)
            toolsets.append(stack.enter_context(NavegadorCDP(
                url_policy=policy, headless=args.headless, viewport=(width, height),
                chrome_path=args.chrome_path, cdp_endpoint=args.cdp, download_dir=args.downloads,
                configs=configs, file_policy=file_policy, confirm=browser_confirm(args.confirmar_browser),
            )))
        if args.modo in ("pc", "ambos"):
            from .computador import ComputadorNativo

            if args.confirmar_pc == "nada":
                print("Aviso: o modelo vai digitar e clicar no seu PC sem pedir aprovação.", file=sys.stderr)
            toolsets.append(stack.enter_context(ComputadorNativo(confirm=computer_confirm(args.confirmar_pc))))

        agent = Agente(AgentConfig(model=args.modelo, effort=args.esforco, max_turns=args.max_turnos,
                                   context_editing=not args.sem_limpeza_contexto,
                                   fallbacks=not args.sem_fallback, toolsets=toolsets))
        if args.pedido:
            return _run_one(agent, " ".join(args.pedido))
        return _interactive(agent)


def _run_one(agent, request: str) -> int:
    try:
        agent.run(request)
    except anthropic.APIStatusError as exc:
        print(f"\nErro da API ({exc.status_code}): {exc.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("\nNão foi possível conectar à API da Anthropic.", file=sys.stderr)
        return 1
    return 0


def _interactive(agent) -> int:
    print("Digite um pedido. Comandos: /nova (nova conversa), /custo (tokens da conversa), /sair.", file=sys.stderr)
    while True:
        try:
            request = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not request:
            continue
        if request in ("/sair", "sair", "exit", "quit"):
            return 0
        if request == "/custo":
            print(agent.consumo_conversa.texto(), file=sys.stderr)
            continue
        if request == "/nova":
            agent.reset()
            print("Nova conversa iniciada.", file=sys.stderr)
            continue
        try:
            _run_one(agent, request)
        except KeyboardInterrupt:
            # O histórico pode ter um tool_use sem resposta: começa uma conversa nova.
            agent.reset()
            print("\nInterrompido. Nova conversa iniciada.", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
