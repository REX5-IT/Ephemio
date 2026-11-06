ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN if command -v apt-get >/dev/null; then \
      apt-get update && apt-get install -y --no-install-recommends python3-pyqt5 qtwayland5 \
      && apt-get clean && rm -rf /var/lib/apt/lists/*; \
    else \
      dnf install -y --setopt=install_weak_deps=False python3-qt5 qt5-qtwayland && dnf clean all; \
    fi
COPY ephemio-tools.py /usr/local/lib/ephemio-tools.py
COPY ephemio-status.py /usr/local/lib/ephemio-status.py
COPY ephemio-status.desktop /etc/xdg/autostart/ephemio-status.desktop
COPY ephemio-status.desktop /usr/share/applications/ephemio-status.desktop
# Ubuntu GNOME uses an explicit autostart directory.
RUN if test -d /defaults/gnome-autostart; then \
      cp /etc/xdg/autostart/ephemio-status.desktop /defaults/gnome-autostart/; \
    fi
# Webtop's nested KDE Wayland session only runs user autostart entries.
# Include this system entry explicitly in its existing session startup loop.
RUN if test -f /defaults/startwm_wayland.sh; then \
      sed -i 's|for ENTRY in "${HOME}"/.config/autostart/\*.desktop; do|for ENTRY in /etc/xdg/autostart/ephemio-status.desktop "${HOME}"/.config/autostart/*.desktop; do|' /defaults/startwm_wayland.sh; \
    fi
# Run after Webtop initializes abc's UID/GID, before desktop services start.
COPY ephemio-identity.py /custom-cont-init.d/20-ephemio-identity
RUN chmod 755 /custom-cont-init.d/20-ephemio-identity
