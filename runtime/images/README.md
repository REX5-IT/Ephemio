# Desktop images

The portal offers Ubuntu KDE, Ubuntu Desktop (GNOME), Fedora Workstation
(GNOME), and Linux Mint (Cinnamon). KDE uses the pinned LinuxServer Webtop image;
the other three use these custom images built on pinned LinuxServer Selkies bases.
They contain desktop environments and applications, rather than full installer
ISOs or virtual machines. All use the existing authenticated browser gateway.

Build on the desktop host from this directory:

```bash
docker build -t ephemio/ubuntu-gnome:VERSION ubuntu-gnome/
docker build -t ephemio/fedora-gnome:VERSION fedora-gnome/
docker build -t ephemio/mint-cinnamon:VERSION mint-cinnamon/
docker image inspect ephemio/ubuntu-gnome:VERSION --format '{{.Id}}'
```

Register each tested image's immutable ID in `/etc/ephemio/config.json` under
`profiles`; see [host operations](../host/README.md). Build tags are convenient
names; the worker runs immutable image IDs. Package repositories change, so
rebuilding a Dockerfile can produce a different image even with a pinned base.
Test a rebuilt image before replacing its configured ID.

Ubuntu uses Noble's GNOME 46, Ubuntu Dock and Yaru with Mozilla's native Firefox
package. Mint uses signed Zena packages on its Noble base, Cinnamon 6.6 and Mint
themes. The included Mint keyring package has SHA-256
`b71be690c543112ea7b65f43e9bbce9a3f17dd5cc784074858f0824154942a99`.

Fedora uses Fedora 44 and GNOME 50. GNOME 50's Wayland session runs through
Mutter's development viewer, displayed full screen inside the Selkies X11
capture session. PipeWire rules classify its video stream for WirePlumber.
The GTK viewer uses Cairo rendering and activates its window at startup to
avoid rendering and input-focus problems observed on this CPU-only host.
Mesa software rendering is limited to two threads to fit the guest CPU and
process limits, including when Firefox starts.
Keyboard events go directly to a private GNOME remote-desktop session through
a mode-0600 Unix socket in the guest. The pinned Selkies keyboard dispatch is
patched at build time; assertions reject an unexpected upstream implementation.
Disconnect and blur release held keys. The host waits for the initialized
`ephemio-input` process before marking this profile ready.
The Fedora s6 dependency order starts GNOME and its input socket before Selkies.
Browser acceptance checks wait for GNOME's overview to open before typing into
search; opening the overview and immediately typing can lose the first keys.
This is an experimental container adaptation; the base retains Fedora's
Container Image identity in `/etc/os-release`.
Firefox startup exceeded a 512-task cap during acceptance, so this profile uses
a bounded 768-task cap. CPU and memory limits remain 2 CPUs and 3 GiB.
Sustained application performance still needs measurement on this profile.

Desktop sessions disable screen locking and idle suspension; access is governed
by portal credentials, session-bound browser grants and the session deadline.
Audio plays through the browser from the container's virtual audio sink.

Images share layers. `docker images` sizes cannot be added to determine disk
usage. Inspect `df -h /` and `docker system df -v`. On a shared host, remove only
identified Ephemio test containers and obsolete image IDs. Do not run global
prune commands. Keep configured images and enough space for rebuilds and guest
data; admission currently requires at least 20 GiB free.

## Ubuntu KDE with Wine

Build with `docker build -t ephemio/ubuntu-kde:wine ubuntu-kde/`, then inspect
its immutable ID with `docker image inspect ephemio/ubuntu-kde:wine`.
Register the tested ID under the host's `ubuntu-kde` profile, retaining
`process: plasmashell`. For local Compose, set
`EPHEMIO_DESKTOP_IMAGE=ephemio/ubuntu-kde:wine`.

Users upload or download EXE/MSI installers and choose **Open With → Run with
Wine**. **Wine Settings** appears in the application menu. Both 32-bit and
64-bit packages are included. Compatibility varies; users can try any app.
Wine initializes on first use and stores its environment in `/config/.wine`,
which is deleted with session storage.

KDE Wallet is disabled through `/etc/xdg/kwalletrc` to avoid wallet creation
prompts in disposable desktops.

Before deployment, verify Wine Settings, EXE and MSI installation, launching an
installed app, reconnecting, and session volume removal in a fresh browser
session. Record Wine version and resource use alongside the tested image ID.

## Session companion

After building a desktop base, add the shared companion layer (repeat for each
profile, using that profile's base tag):

```bash
docker build --build-arg BASE_IMAGE=ephemio/ubuntu-gnome:VERSION \
  -t ephemio/ubuntu-gnome:VERSION-status common/
```

Register the resulting immutable image ID. `deploy-image.sh` includes this layer
for KDE. Deploy the updated host worker before launching new desktops. Existing
containers do not gain the guest socket, credentials or app until recreated.
Standalone Compose skips the app because it has no host session.

The companion starts automatically and opens a floating status window. Closing
it hides it to the tray where supported, or minimizes it when no tray exists
(including Fedora's GNOME shell). The application menu also has **Ephemio Session**.
Fedora starts the window inside its nested Wayland desktop; GNOME does not
provide tray icons by default. KDE, Cinnamon and Ubuntu's enabled AppIndicator
extension can provide a tray, depending on Qt's tray integration.

CPU (100% per core), memory, cumulative network and disk I/O, and combined
writable-layer/home disk usage refresh from the host every 10 seconds. A local
countdown updates continuously and the window reappears below five minutes.
Extend adds up to 15 minutes, capped at the host's `max_session_seconds` of
remaining time. Pause freezes the desktop and revokes browser access; the timer
continues and users resume from Ephemio. Terminate immediately removes the
desktop and its storage. These actions do not require confirmation dialogs.

The app needs a live desktop: it cannot remain visible after the host deletes
or pauses its container. Browser access lasts until the desktop deadline or explicit revocation.
Extending a desktop also extends existing browser grants. Periodic portal
renewal is no longer required; reconnect through Ephemio if the browser loses
its session cookie.

Webtop's nested KDE Wayland startup reads only user autostart files, rather
than `/etc/xdg/autostart`. The companion layer includes its entry explicitly
in that startup loop. Rebuild the companion image to apply this startup fix.
