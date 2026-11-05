# Radiospanaren som Flatpak

Flatpak-paketet kör på i stort sett alla Linux-distributioner, oavsett systemets
GTK-version, eftersom det tar med egna bibliotek (GTK 4, libadwaita, libshumate,
librtlsdr, numpy, scipy). Bra för t.ex. äldre Ubuntu/Debian/Mint där
systempaketen är för gamla för det vanliga installationsskriptet.

## Bygga

```sh
# Verktyg och körtid (en gång)
flatpak install flathub org.flatpak.Builder org.gnome.Platform//51 org.gnome.Sdk//51

# Bygg och installera för den inloggade användaren
cd radiospanaren-beta-v2
flatpak run org.flatpak.Builder --user --install --force-clean \
    build-dir flatpak/se.eh.Radiospanaren.yaml

# Kör
flatpak run se.eh.Radiospanaren
```

Flygradar (ADS-B): `flatpak run --command=flygradar se.eh.Radiospanaren`
(eller knappen i programmet). Simulator utan dongel:
`flatpak run se.eh.Radiospanaren --simulator --start`.

## Ett delbart paket (en fil)

```sh
flatpak run org.flatpak.Builder --user --force-clean --repo=repo \
    build-dir flatpak/se.eh.Radiospanaren.yaml
flatpak build-bundle repo radiospanaren.flatpak se.eh.Radiospanaren
```

Filen `radiospanaren.flatpak` installeras på en annan dator med:
`flatpak install --user radiospanaren.flatpak`.

## Behörigheter

Paketet begär rå USB-åtkomst (`--device=all`) för dongeln, nätverk (kartrutor,
rtl_tcp, webbgränssnittet), ljud (PulseAudio/PipeWire) och skrivåtkomst till
musikmappen för inspelningar.

## Att tänka på

- **SDR-dongeln:** udev-reglerna som ger åtkomst till dongeln ligger på värdsystemet,
  inte i Flatpak. De installeras av paketet `rtl-sdr` (eller manuellt). Utan dem
  når bara root dongeln.
- **Körtid:** manifestet använder GNOME 51 (Python 3.14). Höj `runtime-version` när du byter till
  en nyare GNOME-körtid (och uppdatera Python-hjulens `cp3XX` samt `PYTHONPATH` vid behov).
- **Wayland utan GPU:** i en GPU-lös miljö (t.ex. vissa virtuella maskiner) kan GTK:s
  Wayland-läge krascha i dmabuf-förhandlingen. Starta då med `GDK_BACKEND=x11`
  (XWayland). På en vanlig skrivbordsdator med grafikdrivrutin behövs det inte.
