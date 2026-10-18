"""Teste do consumo de tokens do agente (US-07) com um cliente da API simulado, sem rede e sem navegador.

Uso: python -m testes.teste_consumo
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from types import SimpleNamespace

from automacao.agente import Agente, AgentConfig


class Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self.message


class ClienteFalso:
    """Devolve as respostas da lista, uma por chamada."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_: Stream(self.respostas.pop(0))))


def resposta(texto: str, usage) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=texto)], stop_reason="end_turn", usage=usage)


def usage(entrada, saida, leitura=None, escrita=None) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=entrada, output_tokens=saida, cache_read_input_tokens=leitura,
                           cache_creation_input_tokens=escrita)


def main() -> int:
    failures: list[str] = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FALHA") + f" {label} {str(detail)[:200]!r}")
        if not ok:
            failures.append(label)

    def rodar(agente: Agente, pedido: str) -> str:
        err = io.StringIO()
        with redirect_stderr(err):
            agente.run(pedido)
        return err.getvalue()

    cliente = ClienteFalso([resposta("um", usage(1200, 300, 5000, 800)),
                            resposta("dois", usage(100, 50)),
                            resposta("três", None)])
    agente = Agente(AgentConfig(), client=cliente, out=io.StringIO())

    saida = rodar(agente, "primeiro")
    check("linha de consumo do pedido", "1.200 tokens de entrada, 300 de saída, 5.000 lidos do cache e 800 gravados "
          "no cache (1 chamada)" in saida, saida)
    saida = rodar(agente, "segundo")
    check("cache ausente conta como zero", "100 tokens de entrada, 50 de saída, 0 lidos" in saida, saida)
    check("/custo soma a conversa", "1.300 tokens de entrada, 350 de saída" in agente.consumo_conversa.texto()
          and "(2 chamadas)" in agente.consumo_conversa.texto(), agente.consumo_conversa.texto())
    saida = rodar(agente, "terceiro")
    check("sem usage: consumo indisponível, não zero", "Consumo indisponível" in saida, saida)
    check("conversa marca parcial", "parcial: 1 sem dados" in agente.consumo_conversa.texto(),
          agente.consumo_conversa.texto())
    agente.reset()
    check("nova conversa zera", agente.consumo_conversa.texto() == "Nenhum consumo ainda.")

    print(f"\n{len(failures)} falha(s): {failures}" if failures else "\nTodos os testes passaram.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
