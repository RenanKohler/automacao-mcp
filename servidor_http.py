"""Atalho: python servidor_http.py [opções]  (equivale a python -m automacao.mcp_http)."""

import sys

from automacao.mcp_http import main

if __name__ == "__main__":
    sys.exit(main())
