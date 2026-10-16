package br.automacao.celular;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.View;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

/** Tela de configuração: URL do conector, atalhos para a acessibilidade e para a bateria, e o estado. */
public class MainActivity extends Activity {

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView estado;
    private TextView servico;
    private Button botaoBateria;

    private final Runnable atualizar = new Runnable() {
        @Override
        public void run() {
            boolean ligado = AutomacaoService.instancia != null;
            servico.setText(ligado
                    ? "Serviço de acessibilidade: ligado"
                    : "Serviço de acessibilidade: desligado. Toque em \"Abrir acessibilidade\" e ligue \"Automação\".");
            estado.setText(ligado ? "Conexão: " + Conexao.estado : "Conexão: aguardando o serviço de acessibilidade");
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            botaoBateria.setVisibility(pm.isIgnoringBatteryOptimizations(getPackageName()) ? View.GONE : View.VISIBLE);
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        SharedPreferences prefs = getSharedPreferences(AutomacaoService.PREFS, Context.MODE_PRIVATE);

        LinearLayout raiz = new LinearLayout(this);
        raiz.setOrientation(LinearLayout.VERTICAL);
        int m = dp(20);

        TextView titulo = new TextView(this);
        titulo.setText("Automação");
        titulo.setTextSize(TypedValue.COMPLEX_UNIT_SP, 26);
        raiz.addView(titulo);

        TextView ajuda = new TextView(this);
        ajuda.setText("Deixa o Claude operar este celular pelo conector Automação do claude.ai. "
                + "Cole a URL completa do conector (https://.../mcp/<token>) e salve.");
        ajuda.setPadding(0, dp(8), 0, dp(16));
        raiz.addView(ajuda);

        EditText url = new EditText(this);
        url.setHint("https://automacao-mcp.onrender.com/mcp/<token>");
        url.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        url.setSingleLine(true);
        url.setText(prefs.getString(AutomacaoService.PREF_URL, ""));
        raiz.addView(url);

        Button salvar = new Button(this);
        salvar.setText("Salvar URL");
        salvar.setOnClickListener(v -> {
            String valor = url.getText().toString().trim();
            try {
                Conexao.enderecoCelular(valor);
            } catch (IllegalArgumentException e) {
                Toast.makeText(this, e.getMessage(), Toast.LENGTH_LONG).show();
                return;
            }
            prefs.edit().putString(AutomacaoService.PREF_URL, valor).apply();
            Toast.makeText(this, "URL salva.", Toast.LENGTH_SHORT).show();
        });
        raiz.addView(salvar);

        Switch ativo = new Switch(this);
        ativo.setText("Conexão ativa");
        ativo.setChecked(prefs.getBoolean(AutomacaoService.PREF_ATIVO, true));
        ativo.setPadding(0, dp(12), 0, dp(12));
        ativo.setOnCheckedChangeListener((b, marcado) -> prefs.edit().putBoolean(AutomacaoService.PREF_ATIVO, marcado).apply());
        raiz.addView(ativo);

        Button acessibilidade = new Button(this);
        acessibilidade.setText("Abrir acessibilidade");
        acessibilidade.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        raiz.addView(acessibilidade);

        botaoBateria = new Button(this);
        botaoBateria.setText("Permitir rodar em segundo plano");
        botaoBateria.setOnClickListener(v -> {
            Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName()));
            startActivity(i);
        });
        raiz.addView(botaoBateria);

        Button detalhes = new Button(this);
        detalhes.setText("Informações do app (configurações restritas)");
        detalhes.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + getPackageName()))));
        raiz.addView(detalhes);

        servico = new TextView(this);
        servico.setPadding(0, dp(16), 0, 0);
        raiz.addView(servico);

        estado = new TextView(this);
        estado.setPadding(0, dp(8), 0, 0);
        raiz.addView(estado);

        TextView nota = new TextView(this);
        nota.setText("Se o Android bloquear a ativação do serviço (\"configuração restrita\"), abra Informações do app, "
                + "toque no menu ⋮ e escolha \"Permitir configurações restritas\". Depois ligue o serviço de novo.");
        nota.setPadding(0, dp(16), 0, 0);
        nota.setAlpha(0.7f);
        raiz.addView(nota);

        ScrollView rolagem = new ScrollView(this);
        rolagem.addView(raiz);
        rolagem.setOnApplyWindowInsetsListener((v, insets) -> {
            Insets barras = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
            raiz.setPadding(m + barras.left, m + barras.top, m + barras.right, m + barras.bottom);
            return WindowInsets.CONSUMED;
        });
        setContentView(rolagem);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(atualizar);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(atualizar);
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics()));
    }
}
