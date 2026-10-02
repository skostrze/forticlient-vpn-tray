#!/usr/bin/python3
import os
import pty
import errno
import json
import select
import subprocess
import signal
import threading
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
from gi.repository import Gtk, AppIndicator3, GLib

APPINDICATOR_ID = 'forti_vpn_indicator'

# Słowa kluczowe świadczące o tym, że forticlient czeka na token 2FA
TOKEN_PROMPTS = ['token:', 'totp:', 'otp:', 'code:', 'two-factor', 'authentication code']

# Sentinel – anulowanie dialogu tokenu przez użytkownika
_CANCELLED = object()

# ---------------------------------------------------------------------------
# Plik z zapisanymi hasłami (uprawnienia 600 – tylko właściciel)
# ---------------------------------------------------------------------------
CREDENTIALS_FILE = os.path.expanduser("~/.config/vpn_tray_credentials.json")


def load_credentials() -> dict:
    """Wczytuje słownik {profil: hasło} z pliku JSON."""
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_credential(profile: str, password: str) -> None:
    """Zapisuje hasło dla profilu i ustawia uprawnienia 600."""
    creds = load_credentials()
    creds[profile] = password
    # Upewnij się, że katalog istnieje
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    # Zapisz do pliku tymczasowego, potem zastąp atomowo
    tmp = CREDENTIALS_FILE + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(creds, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDENTIALS_FILE)


def get_vpn_profiles():
    """Pobiera listę profili VPN z forticlient vpn list."""
    try:
        result = subprocess.run(
            ['forticlient', 'vpn', 'list'],
            capture_output=True, text=True, timeout=5
        )
        profiles = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.endswith(':'):
                profiles.append(line)
        return profiles
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Dialog hasła (krok 1)
# ---------------------------------------------------------------------------
class PasswordDialog(Gtk.Dialog):
    def __init__(self, parent, profile, saved_password: str = ""):
        super().__init__(
            title=f"Połącz VPN: {profile}",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(360, -1)
        self.set_border_width(8)

        content = self.get_content_area()
        content.set_spacing(4)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.set_margin_top(8)
        grid.set_margin_bottom(8)

        pw_label = Gtk.Label(label="Hasło:", halign=Gtk.Align.END)
        self.pw_entry = Gtk.Entry()
        self.pw_entry.set_visibility(False)
        self.pw_entry.set_invisible_char('\u25cf')
        self.pw_entry.set_hexpand(True)
        self.pw_entry.set_activates_default(True)
        # Pre-wypełnij jeśli hasło jest zapisane
        if saved_password:
            self.pw_entry.set_text(saved_password)
        grid.attach(pw_label, 0, 0, 1, 1)
        grid.attach(self.pw_entry, 1, 0, 1, 1)

        # Informacja o zapisanym haśle
        if saved_password:
            hint = Gtk.Label()
            hint.set_markup('<small><i>Hasło zapisane — zmień jeśli wygasło</i></small>')
            hint.set_halign(Gtk.Align.START)
            grid.attach(hint, 1, 1, 1, 1)

        content.add(grid)

        self.add_button("Anuluj", Gtk.ResponseType.CANCEL)
        btn = self.add_button("\U0001f517 Połącz", Gtk.ResponseType.OK)
        btn.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.show_all()

    def get_password(self):
        return self.pw_entry.get_text()




# ---------------------------------------------------------------------------
# Dialog tokenu 2FA (krok 2 – pojawia się tylko gdy forticlient pyta)
# ---------------------------------------------------------------------------
class TokenDialog(Gtk.Dialog):
    def __init__(self, parent, profile, prompt_text=""):
        super().__init__(
            title=f"Token 2FA: {profile}",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(360, -1)
        self.set_border_width(8)

        content = self.get_content_area()
        content.set_spacing(6)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        if prompt_text:
            hint = Gtk.Label(label=f"<i>{GLib.markup_escape_text(prompt_text)}</i>")
            hint.set_use_markup(True)
            hint.set_xalign(0)
            box.add(hint)

        token_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        token_label = Gtk.Label(label="Token:")
        self.token_entry = Gtk.Entry()
        self.token_entry.set_placeholder_text("SMS / e-mail / TOTP")
        self.token_entry.set_hexpand(True)
        self.token_entry.set_activates_default(True)
        token_box.pack_start(token_label, False, False, 0)
        token_box.pack_start(self.token_entry, True, True, 0)
        box.add(token_box)

        content.add(box)

        self.add_button("Anuluj", Gtk.ResponseType.CANCEL)
        btn = self.add_button("\U0001f511 Wyślij token", Gtk.ResponseType.OK)
        btn.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        self.show_all()

    def get_token(self):
        return self.token_entry.get_text().strip()


# ---------------------------------------------------------------------------
# Dialog "Łączenie w toku..."
# ---------------------------------------------------------------------------
class ProgressDialog(Gtk.Dialog):
    def __init__(self, parent, profile):
        super().__init__(
            title="Łączenie...",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(320, -1)
        self.set_border_width(8)
        self.set_deletable(False)

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(12)
        content.set_margin_bottom(12)

        label = Gtk.Label()
        label.set_markup(
            f"Łączenie z profilem <b>{GLib.markup_escape_text(profile)}</b>..."
        )
        content.add(label)

        self.spinner = Gtk.Spinner()
        self.spinner.start()
        content.add(self.spinner)

        self.show_all()


# ---------------------------------------------------------------------------
# Główna klasa wskaźnika
# ---------------------------------------------------------------------------
class VPNIndicator:
    def __init__(self):
        # Ukryte okno – rodzic dla modali
        self._root = Gtk.Window()
        self._root.set_skip_taskbar_hint(True)
        self._root.set_skip_pager_hint(True)
        self._root.realize()

        self.indicator = AppIndicator3.Indicator.new(
            APPINDICATOR_ID,
            "security-low",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.last_state = None

        self._build_menu()

        GLib.timeout_add_seconds(3, self.check_vpn)
        self.check_vpn()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def _build_menu(self):
        self.menu = Gtk.Menu()

        self.status_item = Gtk.MenuItem(label="Sprawdzanie stanu...")
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        connect_item = Gtk.MenuItem(label="\U0001f50c Połącz z VPN")
        self.connect_submenu = Gtk.Menu()
        connect_item.set_submenu(self.connect_submenu)
        self.menu.append(connect_item)

        self._populate_profiles()

        self.menu.append(Gtk.SeparatorMenuItem())

        item_disconnect = Gtk.MenuItem(label="\u26d4 Rozłącz VPN")
        item_disconnect.connect('activate', self.disconnect_vpn)
        self.menu.append(item_disconnect)

        item_refresh = Gtk.MenuItem(label="\U0001f504 Odśwież listę profili")
        item_refresh.connect('activate', lambda _: self._populate_profiles())
        self.menu.append(item_refresh)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="\u2716 Zamknij wskaźnik")
        item_quit.connect('activate', self.quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def _populate_profiles(self):
        for child in self.connect_submenu.get_children():
            self.connect_submenu.remove(child)

        profiles = get_vpn_profiles()
        if profiles:
            for profile in profiles:
                item = Gtk.MenuItem(label=profile)
                item.connect('activate', self._on_connect_clicked, profile)
                self.connect_submenu.append(item)
        else:
            no_item = Gtk.MenuItem(label="(brak profili)")
            no_item.set_sensitive(False)
            self.connect_submenu.append(no_item)

        self.connect_submenu.show_all()

    # ------------------------------------------------------------------
    # Łączenie – krok 1: hasło
    # ------------------------------------------------------------------
    def _on_connect_clicked(self, _, profile):
        # Załaduj zapisane hasło (puste jeśli profil jeszcze nie istnieje w pliku)
        saved_pw = load_credentials().get(profile, "")

        dlg = PasswordDialog(self._root, profile, saved_password=saved_pw)
        response = dlg.run()
        password = dlg.get_password()
        dlg.destroy()

        if response != Gtk.ResponseType.OK:
            return

        # Zapisz hasło (nowe lub zaktualizowane) do pliku
        if password:
            save_credential(profile, password)

        progress = ProgressDialog(self._root, profile)

        t = threading.Thread(
            target=self._connect_worker,
            args=(profile, password, progress),
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # Łączenie – wątek roboczy z pseudoterminałem (pty)
    # ------------------------------------------------------------------
    def _connect_worker(self, profile, password, progress_dlg):
        """
        Uruchamia forticlient vpn connect przez pseudoterminal (pty).
        Dzięki temu forticlient "myśli", że działa w prawdziwym terminalu
        i komunikuje się z działającym fctsched (zamiast startować nową instancję).

        Sekwencja:
          1. Czyta output do napotkania promptu hasła → wysyła hasło
          2. Jeśli pojawi się prompt tokenu → pokazuje TokenDialog → wysyła token
          3. Czeka na zakończenie procesu → raportuje wynik
        """
        # Otwórz pseudo-terminal
        master_fd, slave_fd = pty.openpty()

        try:
            proc = subprocess.Popen(
                ['forticlient', 'vpn', 'connect', profile],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        except Exception as e:
            os.close(master_fd)
            os.close(slave_fd)
            GLib.idle_add(self._connect_done, profile, False, str(e), progress_dlg)
            return

        # Slave fd jest teraz używany przez dziecko – zamykamy w rodzicu
        os.close(slave_fd)

        accumulated = ""
        password_sent = False
        token_asked = False

        try:
            while True:
                try:
                    ready, _, _ = select.select([master_fd], [], [], 0.2)
                except (ValueError, select.error):
                    break

                if ready:
                    try:
                        chunk = os.read(master_fd, 1024)
                    except OSError as e:
                        if e.errno in (errno.EIO, errno.EBADF):
                            # PTY zamknięty – proces zakończony
                            break
                        raise
                    if not chunk:
                        break

                    text = chunk.decode('utf-8', errors='replace')
                    accumulated += text

                    lower = accumulated.lower()

                    # Prompt hasła – wyślij hasło
                    if not password_sent and 'password:' in lower:
                        password_sent = True
                        try:
                            os.write(master_fd, (password + '\r').encode())
                        except OSError:
                            break
                        accumulated = ""
                        continue

                    # Pytanie o certyfikat – automatyczna akceptacja
                    if 'confirm (y/n)' in lower or 'confirm(y/n)' in lower:
                        try:
                            os.write(master_fd, b'y\r')
                        except OSError:
                            break
                        accumulated = ""
                        continue

                    # Prompt tokenu – pokaż dialog i wyślij token
                    if password_sent and not token_asked:
                        for kw in TOKEN_PROMPTS:
                            if kw in lower:
                                token_asked = True
                                hint = accumulated.rstrip().split('\n')[-1].strip()

                                token_event = threading.Event()
                                token_holder = [_CANCELLED]

                                GLib.idle_add(
                                    self._ask_for_token,
                                    profile, hint, token_holder, token_event, progress_dlg
                                )
                                token_event.wait(timeout=120)

                                if token_holder[0] is _CANCELLED:
                                    proc.terminate()
                                    os.close(master_fd)
                                    GLib.idle_add(progress_dlg.destroy)
                                    return

                                try:
                                    os.write(master_fd, (token_holder[0] + '\r').encode())
                                except OSError:
                                    pass

                                accumulated = ""
                                break

                elif proc.poll() is not None:
                    # Brak danych i proces zakończony
                    break

        except Exception:
            pass

        try:
            os.close(master_fd)
        except OSError:
            pass

        proc.wait()
        connected = self.is_vpn_connected()
        GLib.idle_add(
            self._connect_done, profile, connected, accumulated.strip(), progress_dlg
        )

    # ------------------------------------------------------------------
    # Łączenie – krok 2: token (GTK thread)
    # ------------------------------------------------------------------
    def _ask_for_token(self, profile, hint, token_holder, token_event, progress_dlg):
        """Wyświetla TokenDialog. Wywoływany zawsze w wątku GTK."""
        progress_dlg.hide()
        dlg = TokenDialog(self._root, profile, hint)
        response = dlg.run()
        if response == Gtk.ResponseType.OK:
            token_holder[0] = dlg.get_token()
        else:
            token_holder[0] = _CANCELLED
        dlg.destroy()
        progress_dlg.show()
        token_event.set()
        return False

    # ------------------------------------------------------------------
    # Łączenie – wynik
    # ------------------------------------------------------------------
    def _connect_done(self, profile, connected, output, progress_dlg):
        progress_dlg.destroy()

        if connected:
            self.send_notification(
                "VPN Połączony",
                f"Profil: {profile}\nPamiętaj o rozłączeniu przed wyłączeniem!"
            )
        else:
            self._show_error_dialog(
                f"Nie udało się połączyć z profilem \"{profile}\".\n\n"
                f"Ostatni output forticlienta:\n{output}"
            )

        self.check_vpn()
        return False

    # ------------------------------------------------------------------
    # Rozłączanie
    # ------------------------------------------------------------------
    def disconnect_vpn(self, _):
        subprocess.run(['forticlient', 'vpn', 'disconnect'])
        self.check_vpn()

    # ------------------------------------------------------------------
    # Monitorowanie stanu
    # ------------------------------------------------------------------
    def is_vpn_connected(self):
        """Sprawdzanie obecności interfejsu sieciowego w sysfs (0% CPU)."""
        try:
            return any(dev.startswith("fctvpn") for dev in os.listdir("/sys/class/net"))
        except OSError:
            return False

    def send_notification(self, title, message, urgency="normal"):
        subprocess.run(["notify-send", "-u", urgency, "-a", "FortiClient VPN", title, message])

    def check_vpn(self):
        connected = self.is_vpn_connected()
        if connected:
            self.indicator.set_icon_full("security-high", "VPN Połączony")
            self.status_item.set_label("\U0001f7e2 VPN: POŁĄCZONY")
            if self.last_state is False:
                self.send_notification("VPN Aktywny", "Pamiętaj o rozłączeniu przed wyłączeniem!")
        else:
            self.indicator.set_icon_full("security-low", "VPN Rozłączony")
            self.status_item.set_label("\u26aa VPN: Rozłączony")
            if self.last_state is True:
                self.send_notification("VPN Rozłączony", "VPN został pomyślnie rozłączony.", "low")

        self.last_state = connected
        return True

    # ------------------------------------------------------------------
    # Pomocnicze
    # ------------------------------------------------------------------
    def _show_error_dialog(self, message):
        dlg = Gtk.MessageDialog(
            transient_for=self._root,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Błąd połączenia VPN",
        )
        dlg.format_secondary_text(message)
        dlg.run()
        dlg.destroy()

    def quit(self, _):
        Gtk.main_quit()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    VPNIndicator()
    Gtk.main()