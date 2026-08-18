"""
Widget "estilo Task Bar Hero": janela sem borda, sempre no topo, que você
arrasta pra qualquer lugar da tela e solta.

Layout horizontal e baixo, pensado pra encaixar do lado da barra de tarefas
do Windows: blocos lado a lado, um por métrica configurada no ai-usagebar
(ex.: Claude · 5h, Claude · semana, saldo do DeepSeek), sem barra de
progresso — só % + tempo até resetar, ou um saldo em texto. Cada bloco
pode ser "destacado" (clique direito nele -> Destacar) e virar sua própria
janelinha independente, arrastável pra qualquer canto da tela — útil se
você tem muitos provedores configurados e quer espalhar em vez de manter
tudo grudado num cartão só. Também dá pra "Ocultar" um bloco que você não
quer ver agora (some de vez, sem virar janela); reaparece pelo submenu
"Reexibir bloco oculto" na janela principal. Pelo menos um bloco sempre
fica visível — não dá pra ocultar todos, senão não sobraria onde clicar
pra reverter.

O conteúdo fica dentro de um cartão com fundo sólido (não 100%
transparente) — um fundo totalmente transparente faz o anti-aliasing das
fontes criar um halo colorido em volta do texto (mistura pixel a pixel com
a cor-chave de transparência). Só a moldura fininha fora do cartão usa a
cor-chave, então os cantos da janela retangular ficam invisíveis mas o
cartão em si é sólido e legível.

SÓ FUNCIONA NO WINDOWS. A transparência de cantos usa `-transparentcolor`,
um recurso específico do Tk no Windows (não existe no Tk do Linux/macOS).
O reforço nativo de "sempre no topo" e a exclusão de capturas de tela
(o widget nunca aparece em Print Screen/Snipping Tool/gravação) também são
Windows-only (ctypes contra user32.dll, exige Windows 10 build 19041+ pra
a exclusão de captura) — em outro SO ou versão mais antiga o widget ainda
abre e funciona, só sem essas coisas.

Como rodar:
    python widget.py                # modo normal, consulta o ai-usagebar de verdade
    python widget.py --mock         # usa dados falsos, pra testar a interface sem
                                     #   precisar ter o ai-usagebar instalado/rodando

Clique com o botão direito em qualquer janela pra abrir o menu (atualizar
agora, destacar/ocultar/reagrupar bloco, ver erro se houver, sair). Clique
e arraste com o botão esquerdo pra mover cada janela. As
posições são salvas em layout.json, na mesma pasta deste arquivo, e
restauradas da próxima vez que você abrir.
"""

import argparse
import ctypes
import json
import logging
import os
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import font as tkfont
from tkinter import messagebox

import usage_data

HERE = os.path.dirname(os.path.abspath(__file__))
POSITION_FILE = os.path.join(HERE, "position.json")  # formato antigo (janela única) — só lido p/ migração
LAYOUT_FILE = os.path.join(HERE, "layout.json")


def _setup_logging():
    # Sob pythonw.exe (o jeito normal de rodar via atalho) não existe
    # console nenhum, então o log em arquivo é o único jeito de diagnosticar
    # um problema. Fica em %LOCALAPPDATA% (não do lado do .py, que pode
    # estar numa pasta somente-leitura dependendo de como foi instalado).
    appdata = os.environ.get("LOCALAPPDATA")
    log_dir = os.path.join(appdata, "ai-usagebar-widget") if appdata else HERE
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = HERE  # fallback: melhor logar ao lado do script que não logar nada
    log_file = os.path.join(log_dir, "widget.log")
    log = logging.getLogger("ai_usagebar_widget")
    log.setLevel(logging.INFO)
    try:
        handler = RotatingFileHandler(log_file, maxBytes=512_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())  # nem o log consegue abrir — não trava o widget por isso
    return log, log_file


logger, LOG_FILE = _setup_logging()

# API nativa do Windows pra reforçar "sempre no topo" (mais confiável que o
# -topmost do Tk sozinho). Os argtypes precisam ser declarados explicitamente
# — sem isso, em Windows 64-bit o ctypes pode truncar o ponteiro HWND_TOPMOST
# e a chamada falha silenciosamente em transições mais "agressivas" de
# janela (ex.: maximizar outro programa).
_user32 = None
if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _HWND_TOPMOST = wintypes.HWND(-1)
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOACTIVATE = 0x0010

    # Exclui a janela de QUALQUER captura de tela (Print Screen, Snipping
    # Tool, gravação, compartilhamento de tela) — resolvido pelo próprio
    # compositor do Windows (DWM): você continua vendo o widget ao vivo
    # normalmente, ele só nunca aparece em nenhuma imagem/vídeo capturado.
    # Exige Windows 10 versão 2004 (build 19041) ou mais novo; em versões
    # mais antigas a chamada simplesmente retorna falso — sem crash, só sem
    # esse benefício.
    _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _WDA_EXCLUDEFROMCAPTURE = 0x00000011

TRANSPARENT_COLOR = "#ff00fe"  # magenta quase puro; raro aparecer em UI real
BG = "#1e1e2e"       # fundo sólido do "cartão" — evita halo de anti-aliasing
                     # de fonte se misturando com a cor-chave de transparência
BORDER = "#3a3a52"
FG = "#e6e6f0"
FG_DIM = "#9090a5"


def bar_color(percent):
    if percent is None:
        return "#7a7a90"
    if percent < 50:
        return "#4caf50"
    if percent < 80:
        return "#ffb300"
    return "#e53935"


def _truncate(text, max_chars=20):
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


class _FloatingCard:
    """Comportamento comum a QUALQUER janela flutuante do widget (a
    principal ou um bloco destacado): sem borda, cantos transparentes com
    cartão sólido por trás, arrastável com o botão esquerdo, posição
    persistida por chave própria, excluída de qualquer captura de tela. O
    reforço de "sempre no topo" em si fica fora daqui — o App chama
    `enforce_topmost()` numa única lista com todas as janelas a cada tick."""

    def __init__(self, win, app, position_key, default_pos):
        self.win = win
        self.app = app
        self.position_key = position_key

        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TRANSPARENT_COLOR)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            print(
                "Aviso: -transparentcolor não é suportado neste sistema "
                "(só funciona no Windows). A janela vai aparecer com fundo sólido.",
                file=sys.stderr,
            )

        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT_COLOR, highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._do_drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        # Abre no RELEASE do botão direito, não no press: se o menu abrisse
        # com o botão ainda fisicamente pressionado, arrastar até um item e
        # soltar ali seria lido pelo Windows como confirmação por arrasto —
        # só o botão esquerdo deve confirmar itens do menu.
        self.canvas.bind("<ButtonRelease-3>", self._show_menu)
        self._drag_offset = (0, 0)

        x, y = self.app.layout_position(position_key, default_pos)
        self.win.geometry(f"+{x}+{y}")

        # O HWND é recalculado a cada chamada (em `_current_hwnd`), não
        # cacheado: se `winfo_id()` for lido antes do Tk terminar de mapear
        # a janela no SO, pode devolver um HWND provisório — cacheado cedo
        # demais, todo reforço futuro de topo (e a exclusão de captura)
        # ficaria preso nesse valor ruim.
        self._exclude_from_screen_capture()

    def _current_hwnd(self):
        self.win.update_idletasks()
        return wintypes.HWND(self.win.winfo_id())

    def _exclude_from_screen_capture(self):
        if _user32 is None:
            return
        try:
            ok = _user32.SetWindowDisplayAffinity(self._current_hwnd(), _WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            logger.exception("SetWindowDisplayAffinity levantou exceção em %s", self.position_key)
            return
        if not ok:
            # Feature de privacidade: se não pegou, o widget volta a
            # aparecer em capturas de tela — melhor registrar isso do que
            # engolir silenciosamente. Causas prováveis: Windows < build
            # 19041, ou alguma peculiaridade do ambiente gráfico.
            logger.warning("SetWindowDisplayAffinity retornou falso em %s.", self.position_key)

    def enforce_topmost(self):
        if _user32 is not None:
            try:
                _user32.SetWindowPos(
                    self._current_hwnd(), _HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
                )
            except Exception:
                logger.debug("SetWindowPos falhou em %s", self.position_key, exc_info=True)
        # Reforça também pelo lado do Tk, além da API nativa acima.
        try:
            self.win.attributes("-topmost", True)
            self.win.lift()
        except Exception:
            pass

    # ---------- arrastar ----------

    def _start_drag(self, event):
        self._drag_offset = (event.x, event.y)

    def _do_drag(self, event):
        x = self.win.winfo_pointerx() - self._drag_offset[0]
        y = self.win.winfo_pointery() - self._drag_offset[1]
        self.win.geometry(f"+{x}+{y}")

    def _end_drag(self, _event):
        self.app.save_position(self.position_key, self.win.winfo_x(), self.win.winfo_y())

    # ---------- menu de contexto ----------

    def base_menu_items(self, menu):
        menu.add_command(label="Atualizar agora", command=self.app.refresh_now)
        menu.add_command(label="Abrir log", command=self.app.open_log)

    def _popup_menu(self, menu, event):
        # O reforço de "sempre no topo" (a cada tick) precisa parar enquanto
        # o menu está aberto — senão ele reafirma o topo do widget por cima
        # do próprio menu recém-aberto, escondendo os itens de baixo (ex.:
        # "Sair"). `tk_popup` é bloqueante, então dá pra pausar/retomar de
        # forma exata.
        self.app.set_menu_open(True)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.app.set_menu_open(False)

    def _show_menu(self, event):
        raise NotImplementedError


def _measure_block_width(font_label, font_value, label, value, pad_x, min_width):
    text_w = max(
        font_label.measure(_truncate(label, 26)),
        font_value.measure(_truncate(value, 16)),
    )
    return max(min_width, text_w + pad_x * 2)


class MainWindow(_FloatingCard):
    HEIGHT = 40
    BLOCK_MIN_WIDTH = 40
    BLOCK_PAD_X = 4  # respiro interno de cada lado do texto mais largo do bloco
    PAD = 5

    def __init__(self, win, app):
        super().__init__(win, app, position_key="main", default_pos=(80, 80))
        self.font_label = tkfont.Font(family="Segoe UI", size=8)
        self.font_value = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.blocks = []
        self.error = None
        self._block_bounds = []  # [(x0, x1, block), ...] do último desenho — usado pra achar o bloco sob o clique direito
        self.redraw()

    def set_blocks(self, blocks, error):
        self.blocks = blocks
        self.error = error
        self.redraw()

    def redraw(self):
        c = self.canvas
        blocks = self.blocks

        # Cada bloco só ocupa a largura que o texto dele (label ou valor, o
        # que for mais largo) realmente precisa — não uma largura fixa igual
        # pra todos.
        block_widths = [
            _measure_block_width(self.font_label, self.font_value, b["label"], b["value_text"], self.BLOCK_PAD_X, self.BLOCK_MIN_WIDTH)
            for b in blocks
        ] or [self.BLOCK_MIN_WIDTH]
        width = self.PAD * 2 + sum(block_widths)

        c.delete("all")
        c.configure(width=width, height=self.HEIGHT)
        c.create_rectangle(0, 0, width, self.HEIGHT, fill=BG, outline=BORDER, width=1)

        x = self.PAD
        self._block_bounds = []
        if not blocks:
            cx = x + (width - self.PAD - x) / 2
            if self.app.detached or self.app.hidden:
                msg = "todos os blocos destacados/ocultos"
            else:
                msg = "sem provedor configurado" if not self.error else "erro — ver menu"
            c.create_text(cx, self.HEIGHT / 2, text=msg, font=self.font_label, fill=FG_DIM, anchor="c")
        else:
            for i, block in enumerate(blocks):
                bw = block_widths[i]
                self._draw_block(c, x, bw, block)
                self._block_bounds.append((x, x + bw, block))
                x += bw
                if i < len(blocks) - 1:
                    c.create_line(x, 6, x, self.HEIGHT - 6, fill=BORDER)

        if self.error:
            # indicador discreto de erro no canto — detalhe completo fica no
            # menu de contexto (botão direito -> "Ver erro"), pra não estourar
            # a altura compacta do widget.
            c.create_text(width - 6, 5, text="⚠", font=self.font_label, fill="#e57373", anchor="ne")
        if self.app.is_data_stale():
            # indica que os dados na tela estão desatualizados (o
            # ai-usagebar pode ter parado de responder) — ver também
            # `_draw_block`, que esmaece a cor do valor no mesmo caso.
            c.create_text(6, 5, text="🕐", font=self.font_label, fill=FG_DIM, anchor="nw")

    def _draw_block(self, c, x, block_width, block):
        cx = x + block_width / 2
        if self.app.is_data_stale():
            color = FG_DIM
        else:
            color = bar_color(block["percent"]) if block.get("percent") is not None else FG
        c.create_text(cx, 11, text=_truncate(block["label"], 26), font=self.font_label, fill=FG_DIM, anchor="c")
        c.create_text(cx, 27, text=_truncate(block["value_text"], 16), font=self.font_value, fill=color, anchor="c")

    def _block_at(self, event_x):
        for x0, x1, block in self._block_bounds:
            if x0 <= event_x < x1:
                return block
        return None

    def _show_menu(self, event):
        menu = tk.Menu(self.win, tearoff=0)
        self.base_menu_items(menu)
        block = self._block_at(event.x)
        if block is not None:
            menu.add_command(
                label=f'Destacar "{block["label"]}"',
                command=lambda b=block: self.app.request_detach(b["label"]),
            )
            menu.add_command(
                label=f'Ocultar "{block["label"]}"',
                command=lambda b=block: self.app.request_hide(b["label"]),
            )
        # Blocos ocultos que ainda existem de verdade (um vendor pode ter
        # sido removido do ai-usagebar enquanto estava oculto — nesse caso
        # não faz sentido oferecer "reexibir" um bloco que não existe mais).
        current_labels = {b["label"] for b in (self.app.data.get("blocks") or [])}
        hidden_now = sorted(self.app.hidden & current_labels)
        if hidden_now:
            submenu = tk.Menu(menu, tearoff=0)
            for key in hidden_now:
                submenu.add_command(label=key, command=lambda k=key: self.app.request_unhide(k))
            menu.add_cascade(label=f"Reexibir bloco oculto ({len(hidden_now)})", menu=submenu)
        if self.error:
            menu.add_separator()
            menu.add_command(label="⚠ Ver erro", command=lambda: messagebox.showwarning("ai-usagebar-widget", self.error))
        menu.add_separator()
        menu.add_command(label="Sair", command=self.app.quit_app)
        self._popup_menu(menu, event)


class DetachedWindow(_FloatingCard):
    """Um bloco puxado pra fora do cartão principal — sua própria janelinha,
    arrastável independente, do tamanho exato do próprio texto."""

    HEIGHT = 40
    PAD = 6

    def __init__(self, win, app, block_key, default_pos):
        super().__init__(win, app, position_key=f"detached:{block_key}", default_pos=default_pos)
        self.block_key = block_key
        self.font_label = tkfont.Font(family="Segoe UI", size=8)
        self.font_value = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.last_block = None  # mantém o último dado bom se o vendor sumir de uma atualização
        self.missing = True
        self.redraw()

    def set_block(self, block):
        if block is not None:
            self.last_block = block
            self.missing = False
        else:
            self.missing = True
        self.redraw()

    def redraw(self):
        c = self.canvas
        block = self.last_block
        label = block["label"] if block else self.block_key
        value = block["value_text"] if block else "sem dados"
        percent = block.get("percent") if block else None

        width = self.PAD * 2 + _measure_block_width(
            self.font_label, self.font_value, label, value, pad_x=0, min_width=0
        )

        c.delete("all")
        c.configure(width=width, height=self.HEIGHT)
        c.create_rectangle(0, 0, width, self.HEIGHT, fill=BG, outline=BORDER, width=1)

        cx = width / 2
        if self.missing or self.app.is_data_stale():
            color = FG_DIM
        else:
            color = bar_color(percent) if percent is not None else FG
        c.create_text(cx, 11, text=_truncate(label, 26), font=self.font_label, fill=FG_DIM, anchor="c")
        c.create_text(cx, 27, text=_truncate(value, 16), font=self.font_value, fill=color, anchor="c")

    def _show_menu(self, event):
        menu = tk.Menu(self.win, tearoff=0)
        self.base_menu_items(menu)
        menu.add_command(label="Reagrupar", command=lambda: self.app.request_reattach(self.block_key))
        menu.add_separator()
        menu.add_command(label="Sair", command=self.app.quit_app)
        self._popup_menu(menu, event)


class App:
    """Orquestrador: busca os dados uma vez por ciclo e distribui pra
    janela principal + qualquer bloco destacado; guarda o layout (quais
    blocos estão destacados e onde cada janela está) em disco; e reforça
    "sempre no topo" de todas as janelas de uma vez só, num único timer."""

    # Se passar disso sem um fetch bem-sucedido, os dados na tela são
    # considerados desatualizados (ver `is_data_stale`) — o widget passa a
    # esmaecer os valores em vez de mostrá-los como se fossem de agora.
    STALE_AFTER_SECONDS = usage_data.POLL_INTERVAL_SECONDS * 3

    def __init__(self, root, mock=False):
        self.root = root
        self.mock = mock
        self._fetching = False
        self._menu_open = False  # pausa o reforço de topo enquanto um menu de contexto está aberto
        self.layout = self._load_layout()
        self.hidden = set(self.layout.get("hidden", []))  # block_key (= block["label"]) ocultos, não viram janela nenhuma
        self.data = usage_data.get_usage(mock=self.mock)
        self.data_fetched_at = time.time()

        self.detached = {}  # block_key (= block["label"]) -> DetachedWindow
        self.cards = []      # todas as _FloatingCard ativas, p/ reforço de topo em bloco

        self.main = MainWindow(self.root, self)
        self.cards.append(self.main)

        for block_key, pos in dict(self.layout.get("detached", {})).items():
            self._open_detached(block_key, pos)

        self._redraw_all()
        self._schedule_poll()
        self._keep_on_top_tick()

    # ---------- destacar / reagrupar ----------

    def request_detach(self, block_key):
        if block_key in self.detached:
            return
        # janela nova nasce encostada à direita da principal, não sobreposta
        mx = self.main.win.winfo_x() + self.main.win.winfo_width() + 12
        my = self.main.win.winfo_y()
        pos = {"x": mx, "y": my}
        self.layout.setdefault("detached", {})[block_key] = pos
        self._save_layout()
        self._open_detached(block_key, pos)
        self._redraw_all()

    def request_reattach(self, block_key):
        card = self.detached.pop(block_key, None)
        if card is None:
            return
        self.cards.remove(card)
        card.win.destroy()
        self.layout.get("detached", {}).pop(block_key, None)
        self._save_layout()
        self._redraw_all()

    def _open_detached(self, block_key, pos):
        win = tk.Toplevel(self.root)
        card = DetachedWindow(win, self, block_key, (pos.get("x", 80), pos.get("y", 80)))
        self.detached[block_key] = card
        self.cards.append(card)

    # ---------- ocultar / reexibir ----------

    def request_hide(self, block_key):
        if block_key in self.hidden:
            return
        current_labels = {b["label"] for b in (self.data.get("blocks") or [])}
        if block_key not in current_labels:
            return
        # Nunca oculta o último bloco visível — senão não sobraria nenhum
        # lugar pra clicar e reverter.
        remaining_visible = len(current_labels - self.hidden) - 1
        if remaining_visible < 1:
            messagebox.showinfo(
                "ai-usagebar-widget",
                "Pelo menos um bloco precisa continuar visível — reagrupe ou "
                "reexiba outro antes de ocultar este.",
            )
            return
        if block_key in self.detached:
            self.request_reattach(block_key)  # oculto e destacado são mutuamente exclusivos
        self.hidden.add(block_key)
        self._save_hidden()
        self._redraw_all()

    def request_unhide(self, block_key):
        if block_key not in self.hidden:
            return
        self.hidden.discard(block_key)
        self._save_hidden()
        self._redraw_all()

    def _save_hidden(self):
        self.layout["hidden"] = sorted(self.hidden)
        self._save_layout()

    # ---------- layout (posições + quais blocos estão destacados) ----------

    @staticmethod
    def _valid_pos(pos):
        return (
            isinstance(pos, dict)
            and isinstance(pos.get("x"), (int, float))
            and isinstance(pos.get("y"), (int, float))
        )

    @classmethod
    def _validate_layout(cls, saved):
        """Valida o FORMATO esperado do layout salvo (não só se é JSON
        válido) — um `layout.json` malformado de um jeito ainda válido como
        JSON (ex.: `"detached": []` em vez de `{}`) cai pro padrão em vez de
        derrubar o `__init__` inteiro."""
        if not isinstance(saved, dict):
            return None
        main = saved.get("main")
        main = main if cls._valid_pos(main) else {"x": 80, "y": 80}

        detached_raw = saved.get("detached")
        detached = {}
        if isinstance(detached_raw, dict):
            for key, pos in detached_raw.items():
                if isinstance(key, str) and cls._valid_pos(pos):
                    detached[key] = {"x": int(pos["x"]), "y": int(pos["y"])}

        hidden_raw = saved.get("hidden")
        hidden = [k for k in hidden_raw if isinstance(k, str)] if isinstance(hidden_raw, list) else []

        return {"main": {"x": int(main["x"]), "y": int(main["y"])}, "detached": detached, "hidden": hidden}

    def _load_layout(self):
        default = {"main": {"x": 80, "y": 80}, "detached": {}, "hidden": []}
        if os.path.exists(LAYOUT_FILE):
            try:
                with open(LAYOUT_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.exception("falha ao ler layout.json — usando padrão")
            else:
                validated = self._validate_layout(saved)
                if validated is not None:
                    return validated
                logger.warning("layout.json com formato inesperado, ignorando e usando padrão")
        # Migração de versão anterior (janela única, position.json só com
        # {"x", "y"}) — reaproveita como posição inicial da principal.
        if os.path.exists(POSITION_FILE):
            try:
                with open(POSITION_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if self._valid_pos(old):
                    default["main"] = {"x": int(old["x"]), "y": int(old["y"])}
            except (json.JSONDecodeError, OSError):
                logger.exception("falha ao ler position.json (migração) — usando padrão")
        return default

    def _save_layout(self):
        # Escrita atômica: grava num arquivo temporário e só troca pelo
        # definitivo com os.replace (atômico no Windows/NTFS e no Linux) —
        # evita um layout.json truncado se o processo for encerrado no meio
        # da escrita.
        tmp_path = LAYOUT_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.layout, f)
            os.replace(tmp_path, LAYOUT_FILE)
        except OSError:
            logger.exception("falha ao salvar layout.json")

    def layout_position(self, position_key, default_pos):
        if position_key == "main":
            pos = self.layout.get("main") or {}
        else:
            block_key = position_key.split(":", 1)[1]
            pos = self.layout.get("detached", {}).get(block_key) or {}
        x = pos.get("x", default_pos[0])
        y = pos.get("y", default_pos[1])
        return self._clamp_to_screen(x, y)

    def _clamp_to_screen(self, x, y):
        # Uma posição salva perto demais da borda (monitor desconectado,
        # resolução mudou, etc) pode acabar fora da área visível — sem
        # ícone de bandeja, uma janela assim é praticamente irrecuperável.
        # Garante uma faixa mínima sempre visível.
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            return x, y
        margin = 60  # folga acima da altura típica da barra de tarefas (~40-48px)
        x = max(0, min(x, sw - margin))
        y = max(0, min(y, sh - margin))
        return x, y

    def save_position(self, position_key, x, y):
        if position_key == "main":
            self.layout["main"] = {"x": x, "y": y}
        else:
            block_key = position_key.split(":", 1)[1]
            self.layout.setdefault("detached", {})[block_key] = {"x": x, "y": y}
        self._save_layout()

    # ---------- sair / diagnóstico ----------

    def quit_app(self):
        self.root.destroy()

    def open_log(self):
        try:
            if sys.platform == "win32" and os.path.exists(LOG_FILE):
                os.startfile(LOG_FILE)  # noqa: S606 - abre no editor padrão do usuário, é só um .log local
                return
        except Exception:
            logger.exception("falha ao tentar abrir o log pelo SO")
        messagebox.showinfo("ai-usagebar-widget", f"Log em:\n{LOG_FILE}")

    # ---------- sempre no topo ----------

    def set_menu_open(self, is_open):
        self._menu_open = is_open

    def _keep_on_top_tick(self):
        # O reagendamento (`root.after`) fica no `finally` — garante que o
        # loop nunca para de vez, mesmo se algo dentro dele falhar.
        try:
            if not self._menu_open:
                # Enquanto um menu de contexto está aberto, reforçar o topo
                # jogaria o widget por cima do próprio menu — só reforça
                # sem menu aberto.
                for card in list(self.cards):
                    try:
                        card.enforce_topmost()
                    except Exception:
                        pass
        finally:
            self.root.after(50, self._keep_on_top_tick)

    # ---------- dados ----------

    def refresh_now(self):
        # Nunca deixa duas consultas ao ai-usagebar rodarem ao mesmo tempo —
        # invocações concorrentes do binário podem colidir no cache dele.
        if self._fetching:
            return
        self._fetching = True
        threading.Thread(target=self._fetch_in_background, daemon=True).start()

    def _fetch_in_background(self):
        try:
            data = usage_data.get_usage(mock=self.mock)
        except Exception:
            # get_usage() já captura os erros esperados; algo escapando
            # daqui é inesperado o bastante pra valer log.
            logger.exception("get_usage() levantou uma exceção não esperada")
            data = {"blocks": [], "error": "erro interno ao buscar dados (ver log)"}
        try:
            self.root.after(0, lambda: self._apply_data(data))
        except Exception:
            # Se o agendamento falhar (ex.: root já destruído durante o
            # fetch), `_apply_data` nunca roda — e é ela quem zera
            # `_fetching`. Reseta aqui pra não travar `refresh_now` pra sempre.
            logger.exception("root.after falhou ao aplicar dados buscados")
            self._fetching = False

    def _apply_data(self, data):
        self._fetching = False
        if data.get("blocks"):
            self.data = data
            self.data_fetched_at = time.time()
        else:
            # Fetch veio vazio/com erro — mantém os últimos dados bons na
            # tela em vez de piscar pra vazio numa falha transitória.
            # `data_fetched_at` não avança, o que deixa `is_data_stale()`
            # perceber que os números pararam de ser atuais.
            self.data["error"] = data.get("error") or self.data.get("error")
        self._redraw_all()

    def is_data_stale(self):
        return (time.time() - self.data_fetched_at) > self.STALE_AFTER_SECONDS

    def _schedule_poll(self):
        # A primeira busca já foi feita (síncrona) no __init__ — aqui só
        # agenda as próximas, sem disparar uma nova imediatamente.
        self.root.after(usage_data.POLL_INTERVAL_SECONDS * 1000, self._poll_tick)

    def _poll_tick(self):
        try:
            self.refresh_now()
        except Exception:
            logger.exception("refresh_now() falhou no poll periódico")
        finally:
            self.root.after(usage_data.POLL_INTERVAL_SECONDS * 1000, self._poll_tick)

    def _redraw_all(self):
        blocks = self.data.get("blocks") or []
        by_key = {b["label"]: b for b in blocks}

        main_blocks = [b for b in blocks if b["label"] not in self.detached and b["label"] not in self.hidden]
        self.main.set_blocks(main_blocks, self.data.get("error"))

        for block_key, card in self.detached.items():
            card.set_block(by_key.get(block_key))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="usa dados falsos, sem chamar o ai-usagebar")
    args = parser.parse_args()

    root = tk.Tk()
    App(root, mock=args.mock)
    root.mainloop()


if __name__ == "__main__":
    main()
