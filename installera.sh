#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
# ════════════════════════════════════════════════════════════════════
#  Installerar Radiospanaren för den inloggade användaren.
#  Fungerar på Fedora 40+, Ubuntu 24.04+, Linux Mint 22+ och Debian 13+.
#
#    ./installera.sh                 installera (frågar innan paket installeras)
#    ./installera.sh --ja            installera utan frågor
#    ./installera.sh --utan-paket    hoppa över installation av systempaket
#    ./installera.sh --avinstallera  ta bort programmet (inställningar och loggar sparas)
#    ./installera.sh --avinstallera --allt   ta även bort inställningar och loggar
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_ID="se.eh.Radiospanaren"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/radiospanaren"
BIN="$HOME/.local/bin"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/radiospanaren"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/radiospanaren"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

JA=0; UTAN_PAKET=0; AVINSTALLERA=0; ALLT=0
for a in "$@"; do
  case "$a" in
    --ja|-y) JA=1 ;;
    --utan-paket) UTAN_PAKET=1 ;;
    --avinstallera) AVINSTALLERA=1 ;;
    --allt) ALLT=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Okänd flagga: $a (se --help)"; exit 1 ;;
  esac
done

gron() { printf '\033[32m%s\033[0m\n' "$*"; }
gul()  { printf '\033[33m%s\033[0m\n' "$*"; }
rod()  { printf '\033[31m%s\033[0m\n' "$*"; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

fraga() {  # fraga "text" → 0 om ja
  [ "$JA" = 1 ] && return 0
  read -r -p "$1 [J/n] " svar
  [[ -z "$svar" || "$svar" =~ ^[JjYy] ]]
}

# ── Avinstallation ──────────────────────────────────────────────────
if [ "$AVINSTALLERA" = 1 ]; then
  rm -rf "$DEST"
  rm -f "$BIN/radiospanaren" "$BIN/flygradar" "$APPS/$APP_ID.desktop" "$APPS/$APP_ID.Flygradar.desktop" \
        "$ICONS/$APP_ID.svg" "$DESKTOP_DIR/Radiospanaren.desktop"
  if [ "$ALLT" = 1 ]; then rm -rf "$CONF" "$STATE"; echo "Inställningar och loggar borttagna."; fi
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
  gron "Radiospanaren är avinstallerad."
  [ "$ALLT" = 1 ] || echo "Inställningar ($CONF) och loggar ($STATE) finns kvar. Ta bort dem med --avinstallera --allt"
  exit 0
fi

echo "═══ Installerar Radiospanaren ═══"

# ── 1. Systempaket ──────────────────────────────────────────────────
. /etc/os-release 2>/dev/null || true
DIST="${ID:-okänd} ${ID_LIKE:-}"
SAKNAS=()
if [[ "$DIST" =~ (fedora|rhel|centos) ]]; then
  PKGS=(python3 python3-gobject python3-cairo gtk4 libadwaita libshumate python3-numpy python3-scipy rtl-sdr
        pulseaudio-utils)
  for p in "${PKGS[@]}"; do rpm -q "$p" >/dev/null 2>&1 || SAKNAS+=("$p"); done
  INSTALL=($SUDO dnf install -y)
elif [[ "$DIST" =~ (debian|ubuntu|linuxmint) ]]; then
  PKGS=(python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-shumate-1.0 python3-numpy python3-scipy
        rtl-sdr pulseaudio-utils xdg-user-dirs)
  for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || SAKNAS+=("$p"); done
  INSTALL=($SUDO apt-get install -y)
else
  gul "Okänd distribution ($DIST). Installera själv: Python 3, PyGObject med GTK 4.12+ och libadwaita 1.5+,"
  gul "pycairo, numpy, scipy, rtl-sdr (librtlsdr), pacat (pulseaudio-utils) och libshumate (karta i Flygradar)."
  UTAN_PAKET=1
fi

if [ "$UTAN_PAKET" = 0 ] && [ "${#SAKNAS[@]}" -gt 0 ]; then
  echo "Följande paket behövs: ${SAKNAS[*]}"
  if fraga "Installera dem nu (kräver lösenord för sudo)?"; then
    if [[ "$DIST" =~ (debian|ubuntu|linuxmint) ]]; then $SUDO apt-get update; fi
    "${INSTALL[@]}" "${SAKNAS[@]}"
  else
    gul "Hoppar över paketinstallationen – programmet startar inte förrän paketen finns."
  fi
elif [ "$UTAN_PAKET" = 0 ]; then
  gron "Alla systempaket finns redan."
fi

# ── 2. Versionskontroll ─────────────────────────────────────────────
if ! python3 - <<'PY'
import sys
try:
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw
    import numpy, scipy, cairo
except Exception as e:
    print(f"  Saknas: {e}"); sys.exit(1)
g = (Gtk.get_major_version(), Gtk.get_minor_version()); a = (Adw.get_major_version(), Adw.get_minor_version())
print(f"  Python {sys.version.split()[0]}, GTK {g[0]}.{g[1]}, libadwaita {a[0]}.{a[1]}, numpy {numpy.__version__}, scipy {scipy.__version__}")
if g < (4, 12) or a < (1, 5):
    print("  För gamla GTK/libadwaita. Kräver GTK 4.12+ och libadwaita 1.5+ (Fedora 40+, Ubuntu 24.04+, Linux Mint 22+).")
    sys.exit(2)
PY
then
  rod "Systemet uppfyller inte kraven – se ovan. Installationen avbryts."
  exit 1
fi

# ── 3. Programfiler ─────────────────────────────────────────────────
mkdir -p "$DEST" "$BIN" "$APPS" "$ICONS" "$STATE"
install -m 755 "$SRC/radiospanaren.py" "$DEST/radiospanaren.py"
install -m 644 "$SRC/avkodare.py" "$SRC/adsb.py" "$SRC/kartvy.py" "$SRC/aktivitet.py" "$SRC/MANUAL.txt" "$DEST/"
install -m 755 "$SRC/adsb_karta.py" "$DEST/adsb_karta.py"
install -m 755 "$SRC/installera.sh" "$DEST/installera.sh"
install -m 644 "$SRC/$APP_ID.svg" "$ICONS/$APP_ID.svg"

cat > "$BIN/radiospanaren" <<'EOF'
#!/bin/sh
# Startskript för Radiospanaren. Felutskrifter från GTK m.m. hamnar i stderr.log.
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/radiospanaren"
mkdir -p "$STATE"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/radiospanaren/radiospanaren.py"
case " $* " in
  *" --rapport "*|*" --logg"*|*" --fel "*|*" --manual "*|*" --help "*|*" -h "*|*" --version "*|*" --sjalvtest "*|*" --sokvagar "*|*" --debug "*|*" --rensa-logg "*|*" --aterstall "*)
    exec python3 "$APP" "$@" ;;
esac
exec python3 "$APP" "$@" 2>>"$STATE/stderr.log"
EOF
chmod 755 "$BIN/radiospanaren"
cat > "$BIN/flygradar" <<'FLYG'
#!/bin/sh
# Startskript för Flygradar (ADS-B med karta), del av Radiospanaren.
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/radiospanaren"
mkdir -p "$STATE"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/radiospanaren/adsb_karta.py"
case " $* " in
  *" --help "*|*" -h "*|*" --version "*|*" --sjalvtest "*|*" --debug "*) exec python3 "$APP" "$@" ;;
esac
exec python3 "$APP" "$@" 2>>"$STATE/stderr.log"
FLYG
chmod 755 "$BIN/flygradar"

# ── 4. Programmeny och skrivbord ────────────────────────────────────
cat > "$APPS/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Radiospanaren
GenericName=SDR-mottagare och radioskanner
Comment=Spektrum, vattenfall, skanner, signallogg och avkodare för RTL-SDR
Exec=$BIN/radiospanaren
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Audio;HamRadio;
Keywords=SDR;RTL-SDR;radio;skanner;scanner;vattenfall;ACARS;AIS;APRS;
StartupNotify=true
StartupWMClass=$APP_ID
Actions=Flygradar;Simulator;Manual;

[Desktop Action Flygradar]
Name=Flygradar (ADS-B med karta)
Exec=$BIN/flygradar

[Desktop Action Simulator]
Name=Starta med simulator (utan SDR)
Exec=$BIN/radiospanaren --simulator --start

[Desktop Action Manual]
Name=Visa manualen
Exec=xdg-open $DEST/MANUAL.txt
EOF
chmod 755 "$APPS/$APP_ID.desktop"
cat > "$APPS/$APP_ID.Flygradar.desktop" <<FLYGDESK
[Desktop Entry]
Type=Application
Name=Flygradar
GenericName=ADS-B-mottagare med karta
Comment=Visar flygplan i närheten på en karta (ADS-B, 1090 MHz) – del av Radiospanaren
Exec=$BIN/flygradar
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;HamRadio;
Keywords=ADS-B;flygplan;radar;1090;karta;RTL-SDR;
StartupNotify=true
StartupWMClass=$APP_ID.Flygradar
Actions=Simulator;

[Desktop Action Simulator]
Name=Starta med simulator (utan SDR)
Exec=$BIN/flygradar --simulator --start
FLYGDESK
chmod 755 "$APPS/$APP_ID.Flygradar.desktop"
if [ -d "$DESKTOP_DIR" ]; then
  cp "$APPS/$APP_ID.desktop" "$DESKTOP_DIR/Radiospanaren.desktop"
  chmod 755 "$DESKTOP_DIR/Radiospanaren.desktop"
  gio set "$DESKTOP_DIR/Radiospanaren.desktop" metadata::trusted true 2>/dev/null || true
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$DATA/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
gron "Programmet är installerat i $DEST"

# ── 5. SDR-dongeln ──────────────────────────────────────────────────
if ! grep -rqs "blacklist dvb_usb_rtl28xxu" /etc/modprobe.d /usr/lib/modprobe.d /lib/modprobe.d; then
  gul "Kärnans DVB-TV-drivrutin är inte blockerad – den tar annars dongeln."
  if fraga "Blockera den (skapar /etc/modprobe.d/blacklist-rtlsdr.conf, kräver sudo)?"; then
    printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' | $SUDO tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null
    $SUDO modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
    gron "Blockerad. Dra ur och sätt i dongeln igen."
  fi
fi
# udev-regeln för RTL2838: ger den åtkomst till inloggad användare (uaccess / ID_SOFTWARE_RADIO),
# eller krävs en grupp som användaren inte är med i?
LINES="$(grep -hrs 'idProduct}=="2838"' /etc/udev/rules.d /usr/lib/udev/rules.d /lib/udev/rules.d 2>/dev/null || true)"
if [ -z "$LINES" ]; then
  gul "Hittar ingen udev-regel för RTL-SDR – dongeln kanske bara fungerar som root. Installera paketet rtl-sdr."
elif ! grep -qE 'uaccess|ID_SOFTWARE_RADIO|MODE="0666"' <<<"$LINES"; then
  GRP="$(sed -n 's/.*GROUP="\([^"]*\)".*/\1/p' <<<"$LINES" | head -1)"
  if [ -n "$GRP" ] && ! id -nG | grep -qw "$GRP"; then
    gul "Dongeln kräver gruppen $GRP, som du inte är med i. Kör:"
    echo "    sudo usermod -aG $GRP $USER     (och logga ut/in)"
  fi
fi

# ── 6. Självtest ────────────────────────────────────────────────────
echo "Kör självtest …"
if "$BIN/radiospanaren" --sjalvtest | tail -6; then :; fi
case ":$PATH:" in *":$BIN:"*) ;; *) gul "Obs: $BIN finns inte i PATH ännu – logga ut och in, så fungerar kommandot radiospanaren i terminalen.";; esac
echo
gron "Klart! Starta Radiospanaren från programmenyn eller skrivbordet, eller skriv: radiospanaren"
echo "Flygradar (ADS-B med karta): flygradar  – eller knappen i Radiospanaren (sidopanelen → Avkodare)"
echo "Manual: radiospanaren --manual    Felrapport: radiospanaren --rapport"
