#!/bin/bash
export XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=wayland
export LIBGL_ALWAYS_SOFTWARE=1 GSK_RENDERER=cairo GTK_A11Y=none GDK_BACKEND=x11 GDK_DISABLE=vulkan
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS WAYLAND_DISPLAY WLR_BACKENDS
chmod 1777 /tmp/.X11-unix
exec dbus-run-session -- bash -c '
  gsettings set org.gnome.desktop.session idle-delay 0
  gsettings set org.gnome.desktop.screensaver lock-enabled false
  gsettings set org.gnome.desktop.interface enable-animations false
  gsettings set org.gnome.shell welcome-dialog-last-shown-version "999"
  gsettings set org.gnome.desktop.search-providers disable-external true
  gsettings set org.gnome.shell favorite-apps "[\"org.mozilla.firefox.desktop\", \"org.gnome.Nautilus.desktop\", \"org.gnome.Terminal.desktop\", \"org.gnome.TextEditor.desktop\"]"
  pipewire &
  while [ ! -S "$XDG_RUNTIME_DIR/pipewire-0" ]; do sleep 0.2; done
  wireplumber &
  /usr/bin/python3 /defaults/gnome-input.py &
  openbox --config-file /defaults/fedora-openbox.xml &
  timeout 60 xdotool search --sync --onlyvisible --name "^Mutter Development Kit$" windowactivate --sync &
  # The devkit shell has no gnome-session to run XDG autostart entries.
  if [ -f /usr/local/lib/ephemio-status.py ]; then
    (
      while [ ! -S "$XDG_RUNTIME_DIR/wayland-gnome" ]; do sleep 0.5; done
      QT_QPA_PLATFORM=wayland WAYLAND_DISPLAY=wayland-gnome /usr/bin/python3 /usr/local/lib/ephemio-status.py
    ) &
  fi
  exec gnome-shell --devkit --wayland --wayland-display=wayland-gnome
'
