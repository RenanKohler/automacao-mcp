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

    private Conexao conexao;
    private Thread threadConexao;

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
            case "screenshot":
                return captura();
            case "ui":
                return texto(arvore());
            case "tap":
                return texto(toque(a.getInt("x"), a.getInt("y"), 60));
            case "long_press":
                return texto(toque(a.getInt("x"), a.getInt("y"), a.optInt("duration_ms", 800)));
            case "swipe":
                return texto(deslizar(a.getInt("x1"), a.getInt("y1"), a.getInt("x2"), a.getInt("y2"),
                        a.optInt("duration_ms", 300)));
            case "type":
                return texto(escrever(a.getString("text")));
            case "key":
                return texto(tecla(a.getString("key")));
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

    // ---- texto e teclas

    private AccessibilityNodeInfo campoEmFoco() {
        AccessibilityNodeInfo n = findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (n == null || !n.isEditable()) {
            throw new IllegalStateException("Nenhum campo de texto em foco. Toque no campo antes de escrever.");
        }
        return n;
    }

    private String escrever(String texto) {
        boolean enviar = texto.endsWith("\n");
        String novo = enviar ? texto.substring(0, texto.length() - 1) : texto;
        AccessibilityNodeInfo campo = campoEmFoco();
        String atual = campo.getText() != null && !campo.isShowingHintText() ? campo.getText().toString() : "";
        String completo = atual + novo;
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, completo);
        if (!campo.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
            throw new IllegalStateException("O campo em foco não aceitou o texto.");
        }
        Bundle sel = new Bundle();
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, completo.length());
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, completo.length());
        campo.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel);
        if (enviar) {
            campo.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId());
        }
        assentar();
        return "Escreveu " + novo.codePointCount(0, novo.length()) + " caracteres" + (enviar ? " e enviou." : ".");
    }

    private String tecla(String nome) {
        String k = nome.trim().toLowerCase(Locale.ROOT);
        int acao;
        switch (k) {
            case "back":
            case "voltar":
                acao = GLOBAL_ACTION_BACK;
                break;
            case "home":
            case "inicio":
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
                if (!campoEmFoco().performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId())) {
                    throw new IllegalStateException("O campo em foco não aceitou o Enter.");
                }
                assentar();
                return "Enter enviado.";
            default:
                throw new IllegalArgumentException("Tecla não suportada: " + nome
                        + ". Use back, home, recents, notifications, quick_settings, enter, power_dialog ou lock_screen.");
        }
        if (!performGlobalAction(acao)) {
            throw new IllegalStateException("O sistema recusou a ação " + k + ".");
        }
        assentar();
        return "Acionou " + k + ".";
    }

    // ---- apps

    private String abrirApp(String pacote) {
        Intent i = getPackageManager().getLaunchIntentForPackage(pacote);
        if (i == null) {
            throw new IllegalArgumentException("App não encontrado ou sem tela inicial: " + pacote + ". Veja android_list_apps.");
        }
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        startActivity(i);
        try {
            Thread.sleep(1200);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        return "Abriu " + pacote + ".";
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
