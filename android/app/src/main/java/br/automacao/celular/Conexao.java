package br.automacao.celular;

import android.util.Base64;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Collections;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.SNIHostName;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * Cliente WebSocket próprio (RFC 6455) que liga o celular ao servidor MCP.
 *
 * O celular abre a conexão (wss://servidor/celular/token), diz "olá" e fica esperando comandos.
 * Cada comando roda numa thread própria e a resposta volta pela mesma conexão. Se a conexão cair,
 * tenta de novo com espera crescente (2 s a 30 s; o servidor gratuito pode estar acordando).
 */
final class Conexao implements Runnable {

    interface Tratador {
        JSONObject ola();

        JSONObject executar(String acao, JSONObject args) throws Exception;
    }

    private static final String TAG = "AutomacaoConexao";
    private static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int OP_CONT = 0x0, OP_TEXT = 0x1, OP_BINARY = 0x2, OP_CLOSE = 0x8, OP_PING = 0x9, OP_PONG = 0xA;
    private static final int READ_TIMEOUT_MS = 90_000; // o servidor manda ping a cada 20 s
    private static final int MAX_MESSAGE = 4 * 1024 * 1024;

    /** Versão do app, preenchida pelo serviço (vai no User-Agent). */
    static volatile String versaoApp = "?";

    /** Estado mostrado na tela do app. */
    static volatile String estado = "Desligado";

    private final URI uri;
    private final Tratador tratador;
    private final ExecutorService comandos = Executors.newSingleThreadExecutor();
    private final SecureRandom random = new SecureRandom();
    private final Object envio = new Object();
    private volatile boolean parar;
    private volatile Socket socket;
    private OutputStream out;

    Conexao(String endereco, Tratador tratador) throws IllegalArgumentException {
        this.uri = enderecoCelular(endereco);
        this.tratador = tratador;
    }

    /** Aceita a URL do conector (https://host/mcp/TOKEN) e devolve a do celular (wss://host/celular/TOKEN). */
    static URI enderecoCelular(String endereco) {
        String texto = endereco == null ? "" : endereco.trim();
        URI u = URI.create(texto);
        String esquema = u.getScheme() == null ? "" : u.getScheme().toLowerCase();
        String caminho = u.getRawPath() == null ? "" : u.getRawPath();
        int i = caminho.indexOf("/mcp/");
        if (i >= 0) {
            caminho = caminho.substring(0, i) + "/celular/" + caminho.substring(i + 5);
        }
        if (!caminho.contains("/celular/") || caminho.endsWith("/celular/")) {
            throw new IllegalArgumentException("Cole a URL completa do conector, terminada em /mcp/<token>.");
        }
        String novoEsquema;
        if (esquema.equals("https") || esquema.equals("wss")) {
            novoEsquema = "wss";
        } else if (esquema.equals("http") || esquema.equals("ws")) {
            novoEsquema = "ws";
        } else {
            throw new IllegalArgumentException("A URL precisa começar com https://");
        }
        if (u.getHost() == null) {
            throw new IllegalArgumentException("URL sem servidor.");
        }
        return URI.create(novoEsquema + "://" + u.getRawAuthority() + caminho);
    }

    void parar() {
        parar = true;
        estado = "Desligado";
        // Fechar um socket TLS envia dados: fora da thread principal (NetworkOnMainThreadException).
        new Thread(this::fecharSocket, "automacao-fechar").start();
        comandos.shutdownNow();
    }

    @Override
    public void run() {
        long espera = 2000;
        while (!parar) {
            try {
                estado = "Conectando a " + uri.getHost() + "...";
                conectar();
                espera = 2000;
                estado = "Conectado a " + uri.getHost();
                enviar(tratador.ola());
                ler();
            } catch (Exception e) {
                if (parar) {
                    break;
                }
                Log.w(TAG, "conexão perdida", e);
                estado = "Sem conexão (" + resumo(e) + "). Tentando de novo em " + espera / 1000 + " s.";
            } finally {
                fecharSocket();
            }
            if (parar) {
                break;
            }
            try {
                Thread.sleep(espera);
            } catch (InterruptedException e) {
                break;
            }
            espera = Math.min(espera * 2, 30_000);
        }
        estado = "Desligado";
    }

    private static String resumo(Exception e) {
        String m = e.getMessage();
        return m == null ? e.getClass().getSimpleName() : (m.length() > 80 ? m.substring(0, 80) : m);
    }

    // ------------------------------------------------------------------ conexão e handshake

    private void conectar() throws IOException {
        boolean tls = uri.getScheme().equals("wss");
        String host = uri.getHost();
        int porta = uri.getPort() > 0 ? uri.getPort() : (tls ? 443 : 80);
        Socket s;
        if (tls) {
            SSLSocket ssl = (SSLSocket) SSLSocketFactory.getDefault().createSocket();
            SSLParameters p = ssl.getSSLParameters();
            p.setServerNames(Collections.singletonList(new SNIHostName(host)));
            p.setEndpointIdentificationAlgorithm("HTTPS"); // confere o certificado contra o nome do servidor
            ssl.setSSLParameters(p);
            ssl.connect(new InetSocketAddress(host, porta), 15_000);
            ssl.setSoTimeout(30_000);
            ssl.startHandshake();
            s = ssl;
        } else {
            s = new Socket();
            s.connect(new InetSocketAddress(host, porta), 15_000);
            s.setSoTimeout(30_000);
        }
        s.setTcpNoDelay(true);
        socket = s;
        out = s.getOutputStream();

        byte[] nonce = new byte[16];
        random.nextBytes(nonce);
        String chave = Base64.encodeToString(nonce, Base64.NO_WRAP);
        String caminho = uri.getRawPath() + (uri.getRawQuery() != null ? "?" + uri.getRawQuery() : "");
        String hostHeader = host + (uri.getPort() > 0 ? ":" + uri.getPort() : "");
        String pedido = "GET " + caminho + " HTTP/1.1\r\n"
                + "Host: " + hostHeader + "\r\n"
                + "User-Agent: AutomacaoCelular/" + versaoApp + "\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + chave + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n\r\n";
        out.write(pedido.getBytes(StandardCharsets.US_ASCII));
        out.flush();

        InputStream in = new BufferedInputStream(s.getInputStream());
        String status = lerLinha(in);
        String aceite = null;
        String linha;
        while (!(linha = lerLinha(in)).isEmpty()) {
            int i = linha.indexOf(':');
            if (i > 0 && linha.substring(0, i).trim().equalsIgnoreCase("Sec-WebSocket-Accept")) {
                aceite = linha.substring(i + 1).trim();
            }
        }
        if (!status.contains(" 101")) {
            if (status.contains(" 401")) {
                throw new IOException("token recusado pelo servidor");
            }
            throw new IOException("servidor respondeu " + status);
        }
        if (!esperado(chave).equals(aceite)) {
            throw new IOException("resposta de handshake inválida");
        }
        s.setSoTimeout(READ_TIMEOUT_MS);
        this.entrada = in;
    }

    private InputStream entrada;

    private static String esperado(String chave) {
        try {
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            byte[] d = sha1.digest((chave + GUID).getBytes(StandardCharsets.US_ASCII));
            return Base64.encodeToString(d, Base64.NO_WRAP);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String lerLinha(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) != -1) {
            if (c == '\n') {
                break;
            }
            if (c != '\r') {
                b.write(c);
            }
            if (b.size() > 8192) {
                throw new IOException("cabeçalho grande demais");
            }
        }
        if (c == -1 && b.size() == 0) {
            throw new EOFException("conexão encerrada no handshake");
        }
        return b.toString("ISO-8859-1");
    }

    // ------------------------------------------------------------------ frames

    private void ler() throws IOException {
        ByteArrayOutputStream mensagem = new ByteArrayOutputStream();
        while (!parar) {
            int b0 = lerByte();
            int b1 = lerByte();
            boolean fin = (b0 & 0x80) != 0;
            int opcode = b0 & 0x0F;
            long tamanho = b1 & 0x7F;
            if (tamanho == 126) {
                tamanho = (lerByte() << 8) | lerByte();
            } else if (tamanho == 127) {
                tamanho = 0;
                for (int i = 0; i < 8; i++) {
                    tamanho = (tamanho << 8) | lerByte();
                }
            }
            if (tamanho > MAX_MESSAGE) {
                throw new IOException("mensagem grande demais");
            }
            byte[] mascara = null;
            if ((b1 & 0x80) != 0) {
                mascara = lerExato(4);
            }
            byte[] dados = lerExato((int) tamanho);
            if (mascara != null) {
                for (int i = 0; i < dados.length; i++) {
                    dados[i] ^= mascara[i & 3];
                }
            }
            switch (opcode) {
                case OP_PING:
                    enviarFrame(OP_PONG, dados);
                    break;
                case OP_PONG:
                    break;
                case OP_CLOSE:
                    try {
                        enviarFrame(OP_CLOSE, new byte[]{0x03, (byte) 0xE8});
                    } catch (IOException ignored) {
                        // a conexão já pode ter caído
                    }
                    throw new EOFException("servidor fechou a conexão");
                case OP_TEXT:
                case OP_BINARY:
                case OP_CONT:
                    mensagem.write(dados);
                    if (mensagem.size() > MAX_MESSAGE) {
                        throw new IOException("mensagem grande demais");
                    }
                    if (fin) {
                        String texto = mensagem.toString("UTF-8");
                        mensagem.reset();
                        despachar(texto);
                    }
                    break;
                default:
                    break;
            }
        }
    }

    private int lerByte() throws IOException {
        int c = entrada.read();
        if (c == -1) {
            throw new EOFException("conexão encerrada");
        }
        return c;
    }

    private byte[] lerExato(int n) throws IOException {
        byte[] b = new byte[n];
        int lido = 0;
        while (lido < n) {
            int r = entrada.read(b, lido, n - lido);
            if (r == -1) {
                throw new EOFException("conexão encerrada");
            }
            lido += r;
        }
        return b;
    }

    private void despachar(String texto) {
        final JSONObject pedido;
        try {
            pedido = new JSONObject(texto);
        } catch (Exception e) {
            Log.w(TAG, "mensagem inválida do servidor");
            return;
        }
        if (!pedido.has("id")) {
            return;
        }
        comandos.execute(() -> {
            JSONObject resposta;
            try {
                JSONObject args = pedido.optJSONObject("args");
                resposta = tratador.executar(pedido.optString("acao"), args == null ? new JSONObject() : args);
            } catch (Exception e) {
                resposta = new JSONObject();
                try {
                    resposta.put("ok", false);
                    resposta.put("erro", e.getMessage() == null ? e.toString() : e.getMessage());
                } catch (Exception ignored) {
                    // JSONObject.put só falha com chave nula
                }
            }
            try {
                resposta.put("id", pedido.opt("id"));
                enviar(resposta);
            } catch (Exception e) {
                Log.w(TAG, "falha ao responder", e);
            }
        });
    }

    private void enviar(JSONObject json) throws IOException {
        enviarFrame(OP_TEXT, json.toString().getBytes(StandardCharsets.UTF_8));
    }

    private void enviarFrame(int opcode, byte[] dados) throws IOException {
        byte[] mascara = new byte[4];
        random.nextBytes(mascara);
        ByteArrayOutputStream f = new ByteArrayOutputStream(dados.length + 14);
        f.write(0x80 | opcode);
        if (dados.length < 126) {
            f.write(0x80 | dados.length);
        } else if (dados.length < 65536) {
            f.write(0x80 | 126);
            f.write(dados.length >>> 8);
            f.write(dados.length & 0xFF);
        } else {
            f.write(0x80 | 127);
            long n = dados.length;
            for (int i = 7; i >= 0; i--) {
                f.write((int) (n >>> (8 * i)) & 0xFF);
            }
        }
        f.write(mascara);
        byte[] mascarado = dados.clone();
        for (int i = 0; i < mascarado.length; i++) {
            mascarado[i] ^= mascara[i & 3];
        }
        f.write(mascarado);
        synchronized (envio) {
            OutputStream o = out;
            if (o == null) {
                throw new IOException("sem conexão");
            }
            o.write(f.toByteArray());
            o.flush();
        }
    }

    private void fecharSocket() {
        Socket s = socket;
        socket = null;
        synchronized (envio) {
            out = null;
        }
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // já fechado
            }
        }
    }
}
