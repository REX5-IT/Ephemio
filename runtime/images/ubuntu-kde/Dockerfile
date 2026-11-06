FROM lscr.io/linuxserver/webtop@sha256:22a6eb6e798abbbac3faed236dfb140b1451051b59fc58fd21eea097d1497ab9
ENV DEBIAN_FRONTEND=noninteractive WINEPREFIX=/config/.wine
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends wine wine64 wine32:i386 fonts-wine shared-mime-info desktop-file-utils \
    && apt-get clean
COPY ephemio-wine /usr/local/bin/ephemio-wine
COPY ephemio-wine.desktop ephemio-wine-settings.desktop /usr/share/applications/
COPY kwalletrc /etc/xdg/kwalletrc
RUN chmod 755 /usr/local/bin/ephemio-wine \
    && update-desktop-database /usr/share/applications
