#!/bin/bash
export XDG_CURRENT_DESKTOP=ubuntu:GNOME GNOME_SHELL_SESSION_MODE=ubuntu
export XDG_SESSION_TYPE=x11 LIBGL_ALWAYS_SOFTWARE=1
unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS
exec dbus-run-session -- bash -c '
  gsettings set org.gnome.desktop.session idle-delay 0
  gsettings set org.gnome.desktop.screensaver lock-enabled false
  if [ ! -f /config/.config/ephemio-desktop-defaults ]; then
    gsettings set org.gnome.desktop.interface enable-animations false
    gsettings set org.gnome.desktop.interface gtk-theme Yaru
    gsettings set org.gnome.desktop.interface icon-theme Yaru
    gsettings set org.gnome.shell enabled-extensions "[\"ubuntu-dock@ubuntu.com\", \"ubuntu-appindicators@ubuntu.com\"]"
    gsettings set org.gnome.shell favorite-apps "[\"firefox.desktop\", \"org.gnome.Nautilus.desktop\", \"org.gnome.Terminal.desktop\", \"org.gnome.TextEditor.desktop\", \"org.gnome.Settings.desktop\"]"
    gsettings set org.gnome.shell welcome-dialog-last-shown-version "999"
    mkdir -p /config/.config
    touch /config/.config/ephemio-desktop-defaults
  fi
  exec gnome-session --session=ephemio --autostart=/defaults/gnome-autostart --disable-acceleration-check
'
