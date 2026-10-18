"""Gera a chave de assinatura do app Android e a grava nos segredos do GitHub Actions.

Roda no seu PC, uma vez. A chave fica só com você e nos segredos do repositório; nunca no código.

    python android/gerar_chave.py

O que faz:
1. Cria ~/.automacao/assinatura-android.p12 (PKCS12, RSA 3072, alias "automacao") e uma senha
   aleatória em ~/.automacao/assinatura-android.senha. Se os arquivos já existem, reaproveita.
2. Grava os segredos ANDROID_KEYSTORE_B64 e ANDROID_KEYSTORE_SENHA no repositório:
   com o GitHub CLI (gh) logado, direto; sem ele, copia cada valor para a área de transferência
   e abre a página de segredos no navegador para você colar.
Nenhum valor secreto é impresso na tela.

Guarde a pasta ~/.automacao: sem a chave, a próxima troca obriga a reinstalar o app no celular.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

REPO = "RenanKohler/automacao-mcp"
ALIAS = "automacao"
PASTA = Path.home() / ".automacao"
CHAVE = PASTA / "assinatura-android.p12"
SENHA = PASTA / "assinatura-android.senha"


def gerar_com_cryptography(destino: Path, senha: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Automacao"),
                      x509.NameAttribute(NameOID.COUNTRY_NAME, "BR")])
    agora = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome).public_key(chave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(agora - datetime.timedelta(days=1))
            .not_valid_after(agora + datetime.timedelta(days=365 * 30))
            .sign(chave, hashes.SHA256()))
    dados = pkcs12.serialize_key_and_certificates(ALIAS.encode(), chave, cert, None,
                                                  serialization.BestAvailableEncryption(senha.encode()))
    destino.write_bytes(dados)


def gerar_com_keytool(destino: Path, senha: str) -> None:
    subprocess.run(["keytool", "-genkeypair", "-keystore", str(destino), "-storetype", "PKCS12",
                    "-alias", ALIAS, "-keyalg", "RSA", "-keysize", "3072", "-validity", "10950",
                    "-dname", "CN=Automacao, C=BR", "-storepass:env", "AUTOMACAO_SENHA"],
                   check=True, env={**os.environ, "AUTOMACAO_SENHA": senha}, capture_output=True)


def gerar() -> None:
    PASTA.mkdir(mode=0o700, exist_ok=True)
    senha = secrets.token_urlsafe(24)
    try:
        gerar_com_cryptography(CHAVE, senha)
    except ImportError:
        if not shutil.which("keytool"):
            sys.exit("Precisa do pacote cryptography (python -m pip install cryptography) ou do keytool do Java.")
        gerar_com_keytool(CHAVE, senha)
    SENHA.write_text(senha, encoding="ascii")
    for p in (CHAVE, SENHA):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    print(f"Chave nova criada em {CHAVE}.")


def gh_pronto() -> bool:
    if not shutil.which("gh"):
        return False
    return subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0


def gravar_com_gh(nome: str, valor: str) -> None:
    subprocess.run(["gh", "secret", "set", nome, "--repo", REPO], input=valor.encode(), check=True,
                   capture_output=True)
    print(f"Segredo {nome} gravado.")


def copiar(valor: str) -> bool:
    if sys.platform == "win32":
        comando = ["clip"]
    elif sys.platform == "darwin":
        comando = ["pbcopy"]
    elif shutil.which("xclip"):
        comando = ["xclip", "-selection", "clipboard"]
    else:
        return False
    subprocess.run(comando, input=valor.encode("ascii"), check=True)
    return True


def gravar_manual(nome: str, valor: str) -> None:
    if not copiar(valor):
        arquivo = Path(tempfile.gettempdir()) / f"{nome}.txt"
        arquivo.write_text(valor, encoding="ascii")
        print(f"Abra {arquivo}, copie o conteúdo e apague o arquivo depois.")
    else:
        print(f"O valor de {nome} está na área de transferência.")
    webbrowser.open(f"https://github.com/{REPO}/settings/secrets/actions/new")
    input(f"Na página aberta, em Name escreva {nome}, cole o valor em Secret e clique em Add secret. "
          "Depois tecle Enter aqui. ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sem-gh", action="store_true", help="não usar o GitHub CLI, mesmo se instalado")
    args = parser.parse_args()

    if CHAVE.exists() and SENHA.exists():
        print(f"Usando a chave existente em {CHAVE}.")
    else:
        gerar()
    valores = {
        "ANDROID_KEYSTORE_B64": base64.b64encode(CHAVE.read_bytes()).decode("ascii"),
        "ANDROID_KEYSTORE_SENHA": SENHA.read_text(encoding="ascii").strip(),
    }
    usar_gh = not args.sem_gh and gh_pronto()
    for nome, valor in valores.items():
        gravar_com_gh(nome, valor) if usar_gh else gravar_manual(nome, valor)
    if sys.platform == "win32":
        copiar(" ")  # não deixa a senha na área de transferência
    print("\nPronto. Rode o workflow \"App Android\" no GitHub (Actions > App Android > Run workflow) ou faça um push "
          "em android/. Como a chave mudou, desinstale o app antigo do celular antes de instalar o novo APK, "
          f"e guarde a pasta {PASTA}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
