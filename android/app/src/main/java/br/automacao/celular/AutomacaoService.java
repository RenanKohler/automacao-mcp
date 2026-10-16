package br.automacao.celular;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.ColorSpace;
import android.graphics.Path;
import android.graphics.Point;
import android.graphics.Rect;
import android.hardware.HardwareBuffer;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Bundle;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Driver nativo do celular: serviço de acessibilidade que executa os comandos android_* recebidos
 * do servidor MCP. Coordenadas são pixels da captura reduzida (lado maior até 1280, menor até 720).
 */
public class AutomacaoService extends AccessibilityService implements Conexao.Tratador,
        SharedPreferences.OnSharedPreferenceChangeListener {

    static final String PREFS = "automacao";
    static final String PREF_URL = "url";
    static final String PREF_ATIVO = "ativo";

    static volatile AutomacaoService instancia;

    private static final int MAX_LONGO = 1280, MAX_CURTO = 720;
    private static final int MAX_ELEMENTOS = 300;
    private static final long ASSENTAR_MS = 400;
    private static final long TRAZER_MS = 4000;
    /** Apps que não são alvo da automação: se um deles vier para a frente, o app alvo é trazido de volta. */
    private static final String PACOTE_CLAUDE = "com.anthropic.claude";

    private Conexao conexao;
    private Thread threadConexao;
    /** App que a automação está operando (android_open_app ou o parâmetro app). */
    private volatile String appAlvo;
    /** Pacote e teclado na última leitura da tela, para saber se coordenadas antigas ainda valem. */
    private volatile String estadoVisto;

    // ------------------------------------------------------------------ ciclo de vida

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instancia = this;
        try {
            Conexao.versaoApp = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException ignored) {
            // mantém "?"
        }
        prefs().registerOnSharedPreferenceChangeListener(this);
        reiniciarConexao();
    }

    @Override
    public void onDestroy() {
        instancia = null;
        prefs().unregisterOnSharedPreferenceChangeListener(this);
        pararConexao();
        super.onDestroy();
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // Não reage a eventos: só atende comandos.
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public void onSharedPreferenceChanged(SharedPreferences p, String chave) {
        if (PREF_URL.equals(chave) || PREF_ATIVO.equals(chave)) {
            reiniciarConexao();
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private synchronized void reiniciarConexao() {
        pararConexao();
        SharedPreferences p = prefs();
        String url = p.getString(PREF_URL, "");
        if (!p.getBoolean(PREF_ATIVO, true) || url.isEmpty()) {
            Conexao.estado = url.isEmpty() ? "Cole a URL do conector e salve." : "Conexão desligada no app.";
            return;
        }
        try {
            conexao = new Conexao(url, this);
        } catch (IllegalArgumentException e) {
            Conexao.estado = "URL inválida: " + e.getMessage();
            return;
        }
        threadConexao = new Thread(conexao, "automacao-conexao");
        threadConexao.start();
    }

    private synchronized void pararConexao() {
        if (conexao != null) {
            conexao.parar();
            conexao = null;
        }
        if (threadConexao != null) {
            threadConexao.interrupt();
            threadConexao = null;
        }
    }

    // ------------------------------------------------------------------ escala da captura

    private Point telaReal() {
        DisplayManager dm = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
        Display d = dm.getDisplay(Display.DEFAULT_DISPLAY);
        Point p = new Point();
        d.getRealSize(p);
        return p;
    }

    private double escala() {
        Point p = telaReal();
        int longo = Math.max(p.x, p.y), curto = Math.min(p.x, p.y);
        return Math.min(1.0, Math.min((double) MAX_LONGO / longo, (double) MAX_CURTO / curto));
    }

    private int[] tamanhoCaptura() {
        Point p = telaReal();
        double s = escala();
        return new int[]{(int) Math.round(p.x * s), (int) Math.round(p.y * s)};
    }

    private float paraTela(int v) {
        return (float) (v / escala());
    }

    private int paraCaptura(int v) {
        return (int) Math.round(v * escala());
    }

    private void conferirPonto(int x, int y) {
        int[] t = tamanhoCaptura();
        if (x < 0 || y < 0 || x >= t[0] || y >= t[1]) {
            throw new IllegalArgumentException("[" + x + ", " + y + "] está fora da captura de " + t[0] + "x" + t[1]
                    + ". Use android_screenshot ou android_ui para ver a tela.");
        }
    }

    // ------------------------------------------------------------------ comandos

    @Override
    public JSONObject ola() {
        JSONObject o = new JSONObject();
        try {
            int[] t = tamanhoCaptura();
            o.put("tipo", "ola");
            o.put("modelo", Build.MANUFACTURER + " " + Build.MODEL);
            o.put("android", Build.VERSION.RELEASE);
            o.put("tela", new JSONArray().put(t[0]).put(t[1]));
            o.put("versao_app", Conexao.versaoApp);
        } catch (Exception ignored) {
            // put só falha com chave nula
        }
        return o;
    }

    @Override
    public JSONObject executar(String acao, JSONObject a) throws Exception {
        switch (acao) {
            case "screenshot": {
                String nota = garantirApp(a, false);
                estadoVisto = estadoTela();
                JSONObject o = captura();
                o.put("texto", nota + o.getString("texto"));
                return o;
            }
            case "ui": {
                String nota = garantirApp(a, false);
                estadoVisto = estadoTela();
                return texto(nota + arvore());
            }
            case "tap":
                return texto(tocar(a));
            case "long_press": {
                String nota = garantirApp(a, true);
                return texto(nota + toque(a.getInt("x"), a.getInt("y"), a.optInt("duration_ms", 800)));
            }
            case "swipe": {
                String nota = garantirApp(a, true);
                return texto(nota + deslizar(a.getInt("x1"), a.getInt("y1"), a.getInt("x2"), a.getInt("y2"),
                        a.optInt("duration_ms", 300)));
            }
            case "type":
                return texto(escrever(a));
            case "key":
                return texto(tecla(a.getString("key"), a));
            case "open_app":
                return texto(abrirApp(a.getString("package")));
            case "list_apps":
                return texto(listarApps(a.optString("filter", "")));
            default:
                throw new IllegalArgumentException("Ação não suportada pelo app: " + acao);
        }
    }

    private static JSONObject texto(String t) throws Exception {
        JSONObject o = new JSONObject();
        o.put("ok", true);
        o.put("texto", t);
        return o;
    }

    private static void assentar() {
        try {
            Thread.sleep(ASSENTAR_MS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }

    private static void pausa(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }

    // ---- app em primeiro plano

    /** Janelas de app (não teclado nem sistema), a ativa primeiro, depois de cima para baixo. */
    private List<AccessibilityWindowInfo> janelasDeApp() {
        List<AccessibilityWindowInfo> todas = getWindows();
        List<AccessibilityWindowInfo> apps = new ArrayList<>();
        if (todas == null) {
            return apps;
        }
        for (AccessibilityWindowInfo j : todas) {
            if (j.getType() == AccessibilityWindowInfo.TYPE_APPLICATION) {
                if (j.isActive() || j.isFocused()) {
                    apps.add(0, j);
                } else {
                    apps.add(j);
                }
            }
        }
        return apps;
    }

    private List<AccessibilityNodeInfo> raizesDeApp() {
        List<AccessibilityNodeInfo> raizes = new ArrayList<>();
        for (AccessibilityWindowInfo j : janelasDeApp()) {
            AccessibilityNodeInfo r = j.getRoot();
            if (r != null) {
                raizes.add(r);
            }
        }
        if (raizes.isEmpty()) {
            AccessibilityNodeInfo r = getRootInActiveWindow();
            if (r != null) {
                raizes.add(r);
            }
        }
        return raizes;
    }

    private String pacoteAtivo() {
        List<AccessibilityNodeInfo> raizes = raizesDeApp();
        return raizes.isEmpty() || raizes.get(0).getPackageName() == null ? ""
                : raizes.get(0).getPackageName().toString();
    }

    private boolean tecladoAberto() {
        List<AccessibilityWindowInfo> todas = getWindows();
        if (todas != null) {
            for (AccessibilityWindowInfo j : todas) {
                if (j.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) {
                    return true;
                }
            }
        }
        return false;
    }

    private String estadoTela() {
        return pacoteAtivo() + (tecladoAberto() ? " com teclado" : " sem teclado");
    }

    private boolean naoEhAlvo(String pacote) {
        return pacote.isEmpty() || PACOTE_CLAUDE.equals(pacote) || getPackageName().equals(pacote);
    }

    /**
     * Confere, antes de agir, se o app operado está na frente. Se o Claude (ou este app) tomou a frente,
     * traz o app alvo de volta. Ações por coordenada só seguem se a tela voltou como estava na última
     * leitura (mesmo app, teclado no mesmo estado); senão nada é tocado e a ação devolve um erro.
     * Devolve um aviso para o início da resposta, ou "".
     */
    private String garantirApp(JSONObject a, boolean porCoordenada) {
        String pedido = a.optString("app", "").trim();
        if (!pedido.isEmpty()) {
            appAlvo = pedido;
        }
        String alvo = appAlvo;
        String atual = pacoteAtivo();
        if (alvo == null || alvo.isEmpty()) {
            if (naoEhAlvo(atual)) {
                throw new IllegalStateException("O app " + (atual.isEmpty() ? "desconhecido" : atual)
                        + " está em primeiro plano e não é o app a operar; nada foi feito. Abra o app com "
                        + "android_open_app ou passe o parâmetro app (ex.: com.whatsapp).");
            }
            return "";
        }
        if (alvo.equals(atual)) {
            return "";
        }
        if (pedido.isEmpty() && !naoEhAlvo(atual)) {
            // A própria automação levou a outro app (um link, um seletor): passa a operar esse.
            appAlvo = atual;
            return "";
        }
        trazerParaFrente(alvo);
        String aviso = "O app " + atual + " estava na frente; " + alvo + " foi trazido de volta.";
        if (porCoordenada) {
            String agora = estadoTela();
            if (estadoVisto == null || !estadoVisto.equals(agora)) {
                throw new IllegalStateException(aviso + " A tela mudou desde a última leitura (" + agora
                        + "), então nada foi tocado. Leia a tela com android_ui e repita a ação, "
                        + "de preferência por texto ou id.");
            }
        }
        return aviso + "\n";
    }

    private void trazerParaFrente(String pacote) {
        Intent i = getPackageManager().getLaunchIntentForPackage(pacote);
        if (i == null) {
            throw new IllegalArgumentException("App não encontrado ou sem tela inicial: " + pacote + ". Veja android_list_apps.");
        }
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        startActivity(i);
        long fim = System.currentTimeMillis() + TRAZER_MS;
        while (System.currentTimeMillis() < fim) {
            pausa(150);
            if (pacote.equals(pacoteAtivo())) {
                assentar();
                return;
            }
        }
        throw new IllegalStateException("Não foi possível trazer " + pacote + " para a frente; está "
                + pacoteAtivo() + ". Nada foi feito.");
    }

    // ---- captura

    private JSONObject captura() throws Exception {
        CountDownLatch pronto = new CountDownLatch(1);
        AtomicReference<ScreenshotResult> resultado = new AtomicReference<>();
        AtomicReference<String> erro = new AtomicReference<>();
        Executor direto = Runnable::run;
        takeScreenshot(Display.DEFAULT_DISPLAY, direto, new TakeScreenshotCallback() {
            @Override
            public void onSuccess(ScreenshotResult r) {
                resultado.set(r);
                pronto.countDown();
            }

            @Override
            public void onFailure(int codigo) {
                erro.set(motivoCaptura(codigo));
                pronto.countDown();
            }
        });
        if (!pronto.await(10, TimeUnit.SECONDS)) {
            throw new IllegalStateException("A captura de tela não respondeu.");
        }
        if (erro.get() != null) {
            throw new IllegalStateException(erro.get());
        }
        ScreenshotResult r = resultado.get();
        HardwareBuffer buffer = r.getHardwareBuffer();
        Bitmap hw = Bitmap.wrapHardwareBuffer(buffer, r.getColorSpace() != null ? r.getColorSpace()
                : ColorSpace.get(ColorSpace.Named.SRGB));
        if (hw == null) {
            buffer.close();
            throw new IllegalStateException("Não foi possível ler a captura.");
        }
        Bitmap copia = hw.copy(Bitmap.Config.ARGB_8888, false);
        hw.recycle();
        buffer.close();
        int[] t = tamanhoCaptura();
        Bitmap reduzida = Bitmap.createScaledBitmap(copia, t[0], t[1], true);
        if (reduzida != copia) {
            copia.recycle();
        }
        ByteArrayOutputStream jpeg = new ByteArrayOutputStream();
        reduzida.compress(Bitmap.CompressFormat.JPEG, 80, jpeg);
        reduzida.recycle();

        JSONObject img = new JSONObject();
        img.put("data", Base64.encodeToString(jpeg.toByteArray(), Base64.NO_WRAP));
        img.put("mimeType", "image/jpeg");
        JSONObject o = texto("Captura " + t[0] + "x" + t[1] + ". Coordenadas das ferramentas android_* são pixels desta imagem.");
        o.put("imagem", img);
        return o;
    }

    private static String motivoCaptura(int codigo) {
        switch (codigo) {
            case ERROR_TAKE_SCREENSHOT_SECURE_WINDOW:
                return "A tela atual não permite captura (app protegido, como banco ou senha). Use android_ui.";
            case ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT:
                return "Capturas seguidas rápido demais. Tente de novo em um segundo.";
            case ERROR_TAKE_SCREENSHOT_NO_ACCESSIBILITY_ACCESS:
                return "O serviço de acessibilidade não tem permissão de captura.";
            default:
                return "Falha na captura de tela (código " + codigo + ").";
        }
    }

    // ---- árvore da tela

    private String arvore() {
        int[] t = tamanhoCaptura();
        List<String> linhas = new ArrayList<>();
        String pacote = "";
        List<AccessibilityWindowInfo> janelas = getWindows();
        if (janelas == null || janelas.isEmpty()) {
            AccessibilityNodeInfo raiz = getRootInActiveWindow();
            if (raiz != null) {
                pacote = String.valueOf(raiz.getPackageName());
                percorrer(raiz, linhas);
            }
        } else {
            List<AccessibilityWindowInfo> ordem = new ArrayList<>(janelas);
            Collections.reverse(ordem); // de baixo para cima: o app primeiro, sobreposições depois
            for (AccessibilityWindowInfo j : ordem) {
                AccessibilityNodeInfo raiz = j.getRoot();
                if (raiz == null) {
                    continue;
                }
                if (j.getType() == AccessibilityWindowInfo.TYPE_APPLICATION && j.isActive()) {
                    pacote = String.valueOf(raiz.getPackageName());
                }
                linhas.add("Janela: " + tipoJanela(j.getType())
                        + (j.getTitle() != null ? " \"" + j.getTitle() + "\"" : "")
                        + (j.isActive() ? " (ativa)" : ""));
                percorrer(raiz, linhas);
                if (linhas.size() >= MAX_ELEMENTOS) {
                    break;
                }
            }
        }
        if (linhas.size() >= MAX_ELEMENTOS) {
            linhas = linhas.subList(0, MAX_ELEMENTOS);
            linhas.add("[lista cortada em " + MAX_ELEMENTOS + " elementos]");
        }
        String cabecalho = "App em primeiro plano: " + (pacote.isEmpty() ? "desconhecido" : pacote)
                + ". Coordenadas em pixels da captura " + t[0] + "x" + t[1] + ".";
        return cabecalho + "\n" + (linhas.isEmpty() ? "Nenhum elemento com texto ou ação na tela." : String.join("\n", linhas));
    }

    private static String tipoJanela(int tipo) {
        switch (tipo) {
            case AccessibilityWindowInfo.TYPE_APPLICATION:
                return "app";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD:
                return "teclado";
            case AccessibilityWindowInfo.TYPE_SYSTEM:
                return "sistema";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER:
                return "divisor";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY:
                return "sobreposição";
            default:
                return "outra";
        }
    }

    private void percorrer(AccessibilityNodeInfo n, List<String> linhas) {
        if (n == null || linhas.size() >= MAX_ELEMENTOS) {
            return;
        }
        if (n.isVisibleToUser()) {
            String linha = descrever(n);
            if (linha != null) {
                linhas.add(linha);
            }
        }
        for (int i = 0; i < n.getChildCount(); i++) {
            percorrer(n.getChild(i), linhas);
        }
    }

    private String descrever(AccessibilityNodeInfo n) {
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        if (r.width() <= 0 || r.height() <= 0) {
            return null;
        }
        String texto = n.getText() != null ? n.getText().toString().trim() : "";
        String desc = n.getContentDescription() != null ? n.getContentDescription().toString().trim() : "";
        String id = n.getViewIdResourceName() != null ? n.getViewIdResourceName() : "";
        int barra = id.lastIndexOf('/');
        if (barra >= 0) {
            id = id.substring(barra + 1);
        }
        String classe = n.getClassName() != null ? n.getClassName().toString() : "View";
        classe = classe.substring(classe.lastIndexOf('.') + 1);
        List<String> marcas = new ArrayList<>();
        if (n.isClickable()) marcas.add("clicável");
        if (n.isLongClickable()) marcas.add("toque longo");
        if (n.isScrollable()) marcas.add("rolável");
        if (n.isCheckable()) marcas.add(n.isChecked() ? "marcado" : "desmarcado");
        if (n.isFocused()) marcas.add("em foco");
        if (!n.isEnabled()) marcas.add("desativado");
        if (n.isEditable()) marcas.add("campo de texto");
        if (n.isPassword()) marcas.add("senha");
        if (texto.isEmpty() && desc.isEmpty() && marcas.isEmpty()) {
            return null;
        }
        if (n.isShowingHintText() && !texto.isEmpty()) {
            texto = "dica: " + texto;
        }
        StringBuilder b = new StringBuilder("- ").append(classe);
        if (!texto.isEmpty()) b.append(" \"").append(corta(texto)).append('"');
        if (!desc.isEmpty() && !desc.equals(texto)) b.append(" desc=\"").append(corta(desc)).append('"');
        if (!id.isEmpty()) b.append(" id=").append(id);
        if (!marcas.isEmpty()) b.append(" [").append(String.join(", ", marcas)).append(']');
        b.append(" em (").append(paraCaptura(r.centerX())).append(", ").append(paraCaptura(r.centerY())).append(')');
        return b.toString();
    }

    private static String corta(String s) {
        s = s.replace('\n', ' ');
        return s.length() > 120 ? s.substring(0, 120) + "…" : s;
    }

    // ---- gestos

    private void gesto(Path caminho, long duracao) throws Exception {
        GestureDescription g = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(caminho, 0, Math.max(1, duracao)))
                .build();
        CountDownLatch fim = new CountDownLatch(1);
        AtomicReference<Boolean> ok = new AtomicReference<>(false);
        boolean aceito = dispatchGesture(g, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                ok.set(true);
                fim.countDown();
            }

            @Override
            public void onCancelled(GestureDescription d) {
                fim.countDown();
            }
        }, null);
        if (!aceito) {
            throw new IllegalStateException("O sistema recusou o gesto.");
        }
        if (!fim.await(duracao + 5000, TimeUnit.MILLISECONDS) || !ok.get()) {
            throw new IllegalStateException("O gesto foi cancelado pelo sistema.");
        }
        assentar();
    }

    private String toque(int x, int y, int duracao) throws Exception {
        conferirPonto(x, y);
        Path p = new Path();
        p.moveTo(paraTela(x), paraTela(y));
        gesto(p, duracao);
        return (duracao >= 500 ? "Toque longo em (" : "Tocou em (") + x + ", " + y + ").";
    }

    private String deslizar(int x1, int y1, int x2, int y2, int duracao) throws Exception {
        conferirPonto(x1, y1);
        conferirPonto(x2, y2);
        Path p = new Path();
        p.moveTo(paraTela(x1), paraTela(y1));
        p.lineTo(paraTela(x2), paraTela(y2));
        gesto(p, Math.max(50, duracao));
        return "Deslizou de (" + x1 + ", " + y1 + ") até (" + x2 + ", " + y2 + ").";
    }

    // ---- toque por elemento

    private String tocar(JSONObject a) throws Exception {
        String procurado = a.optString("texto", a.optString("text", "")).trim();
        String id = a.optString("id", "").trim();
        if (!procurado.isEmpty() || !id.isEmpty()) {
            String nota = garantirApp(a, false);
            return nota + clicar(acharElemento(procurado, id));
        }
        if (!a.has("x") || !a.has("y")) {
            throw new IllegalArgumentException("Informe texto, id, ou x e y.");
        }
        String nota = garantirApp(a, true);
        return nota + toque(a.getInt("x"), a.getInt("y"), 60);
    }

    private static String textoDe(AccessibilityNodeInfo n) {
        return n.getText() != null ? n.getText().toString().trim() : "";
    }

    private static String descDe(AccessibilityNodeInfo n) {
        return n.getContentDescription() != null ? n.getContentDescription().toString().trim() : "";
    }

    private static boolean idConfere(AccessibilityNodeInfo n, String id) {
        String v = n.getViewIdResourceName();
        return v != null && (v.equals(id) || v.endsWith("/" + id));
    }

    private void coletar(AccessibilityNodeInfo n, List<AccessibilityNodeInfo> saida) {
        if (n == null) {
            return;
        }
        if (n.isVisibleToUser()) {
            saida.add(n);
        }
        for (int i = 0; i < n.getChildCount(); i++) {
            coletar(n.getChild(i), saida);
        }
    }

    /** Nós visíveis das janelas de app e de sistema (diálogos), sem o teclado. */
    private List<AccessibilityNodeInfo> nosVisiveis(boolean soApp) {
        List<AccessibilityNodeInfo> nos = new ArrayList<>();
        if (soApp) {
            for (AccessibilityNodeInfo r : raizesDeApp()) {
                coletar(r, nos);
            }
            return nos;
        }
        List<AccessibilityWindowInfo> todas = getWindows();
        List<AccessibilityNodeInfo> raizes = raizesDeApp();
        if (todas != null) {
            for (AccessibilityWindowInfo j : todas) {
                if (j.getType() == AccessibilityWindowInfo.TYPE_SYSTEM && j.getRoot() != null) {
                    raizes.add(j.getRoot());
                }
            }
        }
        for (AccessibilityNodeInfo r : raizes) {
            coletar(r, nos);
        }
        return nos;
    }

    private AccessibilityNodeInfo acharElemento(String procurado, String id) {
        String p = procurado.toLowerCase(Locale.ROOT);
        List<AccessibilityNodeInfo> exatos = new ArrayList<>(), parciais = new ArrayList<>();
        for (AccessibilityNodeInfo n : nosVisiveis(false)) {
            if (!id.isEmpty() && !idConfere(n, id)) {
                continue;
            }
            if (p.isEmpty()) {
                exatos.add(n);
                continue;
            }
            String t = textoDe(n).toLowerCase(Locale.ROOT), d = descDe(n).toLowerCase(Locale.ROOT);
            if (t.equals(p) || d.equals(p)) {
                exatos.add(n);
            } else if (t.contains(p) || d.contains(p)) {
                parciais.add(n);
            }
        }
        List<AccessibilityNodeInfo> achados = exatos.isEmpty() ? parciais : exatos;
        String alvo = (procurado.isEmpty() ? "" : "\"" + procurado + "\"") + (id.isEmpty() ? "" : " id=" + id);
        if (achados.isEmpty()) {
            throw new IllegalArgumentException("Nenhum elemento visível com " + alvo.trim()
                    + ". Veja os textos e ids em android_ui.");
        }
        if (achados.size() > 1) {
            List<String> linhas = new ArrayList<>();
            for (int i = 0; i < Math.min(achados.size(), 8); i++) {
                String linha = descrever(achados.get(i));
                if (linha != null) {
                    linhas.add(linha);
                }
            }
            throw new IllegalArgumentException(achados.size() + " elementos com " + alvo.trim()
                    + "; nada foi tocado. Use um texto mais específico, o id, ou x e y:\n" + String.join("\n", linhas));
        }
        return achados.get(0);
    }

    private String clicar(AccessibilityNodeInfo n) throws Exception {
        String nome = !textoDe(n).isEmpty() ? textoDe(n) : descDe(n);
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        String onde = " em (" + paraCaptura(r.centerX()) + ", " + paraCaptura(r.centerY()) + ").";
        AccessibilityNodeInfo c = n;
        for (int i = 0; c != null && i < 8 && !c.isClickable(); i++) {
            c = c.getParent();
        }
        if (c != null && c.isClickable() && c.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            assentar();
            return "Tocou em \"" + corta(nome) + "\"" + onde;
        }
        Path p = new Path();
        p.moveTo(r.centerX(), r.centerY());
        gesto(p, 60);
        return "Tocou (gesto) em \"" + corta(nome) + "\"" + onde;
    }

    // ---- texto e teclas

    /**
     * Acha o campo onde escrever: pelo ponto (x, y) ou id, se dados; senão o campo com foco de entrada
     * nas janelas de app; senão o único campo de texto visível no app (ex.: "Mensagem" do WhatsApp).
     */
    private AccessibilityNodeInfo acharCampo(JSONObject a) throws Exception {
        List<AccessibilityNodeInfo> editaveis = new ArrayList<>();
        for (AccessibilityNodeInfo n : nosVisiveis(true)) {
            if (n.isEditable() && n.isEnabled()) {
                editaveis.add(n);
            }
        }
        if (a.has("x") && a.has("y")) {
            int x = a.getInt("x"), y = a.getInt("y");
            conferirPonto(x, y);
            int px = Math.round(paraTela(x)), py = Math.round(paraTela(y));
            AccessibilityNodeInfo melhor = null;
            long menor = Long.MAX_VALUE;
            for (AccessibilityNodeInfo n : editaveis) {
                Rect r = new Rect();
                n.getBoundsInScreen(r);
                long area = (long) r.width() * r.height();
                if (r.contains(px, py) && area < menor) {
                    melhor = n;
                    menor = area;
                }
            }
            if (melhor == null) {
                throw new IllegalArgumentException("Não há campo de texto em (" + x + ", " + y
                        + "). Os campos aparecem como \"campo de texto\" em android_ui.");
            }
            return melhor;
        }
        String id = a.optString("id", "").trim();
        if (!id.isEmpty()) {
            for (AccessibilityNodeInfo n : editaveis) {
                if (idConfere(n, id)) {
                    return n;
                }
            }
            throw new IllegalArgumentException("Nenhum campo de texto visível com id=" + id + ".");
        }
        for (AccessibilityNodeInfo r : raizesDeApp()) {
            AccessibilityNodeInfo f = r.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (f != null && f.isEditable()) {
                return f;
            }
        }
        AccessibilityNodeInfo f = findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (f != null && f.isEditable() && !naoEhAlvo(String.valueOf(f.getPackageName()))) {
            return f;
        }
        for (AccessibilityNodeInfo n : editaveis) {
            if (n.isFocused()) {
                return n;
            }
        }
        if (editaveis.size() == 1) {
            return editaveis.get(0);
        }
        if (editaveis.isEmpty()) {
            throw new IllegalStateException("Não há campo de texto visível em " + pacoteAtivo()
                    + ". Abra a tela com o campo e tente de novo.");
        }
        List<String> linhas = new ArrayList<>();
        for (AccessibilityNodeInfo n : editaveis) {
            String linha = descrever(n);
            if (linha != null) {
                linhas.add(linha);
            }
        }
        throw new IllegalStateException("Há " + editaveis.size() + " campos de texto e nenhum em foco; "
                + "passe x e y (ou id) do campo:\n" + String.join("\n", linhas));
    }

    private static String textoAtual(AccessibilityNodeInfo campo) {
        if (campo.getText() == null || campo.isShowingHintText()) {
            return "";
        }
        String t = campo.getText().toString();
        CharSequence dica = campo.getHintText();
        return dica != null && t.contentEquals(dica) ? "" : t;
    }

    private String escrever(JSONObject a) throws Exception {
        String nota = garantirApp(a, false);
        String texto = a.getString("text");
        boolean enviar = texto.endsWith("\n");
        String novo = enviar ? texto.substring(0, texto.length() - 1) : texto;
        AccessibilityNodeInfo campo = acharCampo(a);
        if (!campo.isFocused()) {
            campo.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
            campo.refresh();
        }
        String atual = textoAtual(campo);
        String completo = atual + novo;
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, completo);
        if (!campo.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
            throw new IllegalStateException("O campo de texto não aceitou o texto.");
        }
        Bundle sel = new Bundle();
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, completo.length());
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, completo.length());
        campo.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel);
        boolean enviou = enviar && campo.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId());
        assentar();
        campo.refresh();
        String depois = textoAtual(campo);
        Rect r = new Rect();
        campo.getBoundsInScreen(r);
        String onde = " no campo em (" + paraCaptura(r.centerX()) + ", " + paraCaptura(r.centerY()) + ")";
        String fim = !enviar ? "." : enviou ? " e acionou Enter." : ", mas o campo não aceitou Enter; toque no botão de enviar.";
        if (!enviou && !depois.contains(novo)) {
            fim += " Atenção: o campo não mostra o texto escrito; confira com android_ui.";
        }
        return nota + "Escreveu " + novo.codePointCount(0, novo.length()) + " caracteres" + onde + fim;
    }

    private String tecla(String nome, JSONObject a) throws Exception {
        String k = nome.trim().toLowerCase(Locale.ROOT);
        int acao;
        String nota = "";
        switch (k) {
            case "back":
            case "voltar":
                nota = garantirApp(a, false);
                acao = GLOBAL_ACTION_BACK;
                break;
            case "home":
            case "inicio":
                appAlvo = null;
                acao = GLOBAL_ACTION_HOME;
                break;
            case "recents":
            case "recentes":
                acao = GLOBAL_ACTION_RECENTS;
                break;
            case "notifications":
            case "notificacoes":
                acao = GLOBAL_ACTION_NOTIFICATIONS;
                break;
            case "quick_settings":
                acao = GLOBAL_ACTION_QUICK_SETTINGS;
                break;
            case "power_dialog":
                acao = GLOBAL_ACTION_POWER_DIALOG;
                break;
            case "lock_screen":
                acao = GLOBAL_ACTION_LOCK_SCREEN;
                break;
            case "enter":
                nota = garantirApp(a, false);
                if (!acharCampo(a).performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId())) {
                    throw new IllegalStateException("O campo de texto não aceitou o Enter. Em apps de mensagem, "
                            + "toque no botão de enviar (ex.: android_tap com texto \"Enviar\").");
                }
                assentar();
                return nota + "Enter enviado.";
            default:
                throw new IllegalArgumentException("Tecla não suportada: " + nome
                        + ". Use back, home, recents, notifications, quick_settings, enter, power_dialog ou lock_screen.");
        }
        if (!performGlobalAction(acao)) {
            throw new IllegalStateException("O sistema recusou a ação " + k + ".");
        }
        assentar();
        return nota + "Acionou " + k + ".";
    }

    // ---- apps

    private String abrirApp(String pacote) {
        appAlvo = pacote;
        if (pacote.equals(pacoteAtivo())) {
            return "O app " + pacote + " já está na frente.";
        }
        trazerParaFrente(pacote);
        pausa(600);
        return "Abriu " + pacote + ". As próximas ações conferem se ele continua na frente e o trazem de volta se outro app "
                + "(como o Claude) tomar a frente.";
    }

    private String listarApps(String filtro) {
        PackageManager pm = getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> lista = pm.queryIntentActivities(launcher, 0);
        String f = filtro.toLowerCase(Locale.ROOT);
        List<String> linhas = new ArrayList<>();
        for (ResolveInfo r : lista) {
            String nome = String.valueOf(r.loadLabel(pm));
            String pacote = r.activityInfo.packageName;
            if (f.isEmpty() || nome.toLowerCase(Locale.ROOT).contains(f) || pacote.toLowerCase(Locale.ROOT).contains(f)) {
                String linha = nome + " (" + pacote + ")";
                if (!linhas.contains(linha)) {
                    linhas.add(linha);
                }
            }
        }
        Collections.sort(linhas, String.CASE_INSENSITIVE_ORDER);
        return linhas.isEmpty() ? "Nenhum app encontrado." : linhas.size() + " apps:\n" + String.join("\n", linhas);
    }
}
