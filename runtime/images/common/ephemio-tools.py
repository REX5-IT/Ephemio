#!/usr/bin/python3
"""Local companion operations; no host credentials or external IP lookups."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

ROOT = Path('/mnt/ephemio-user')
DOCUMENTS = Path('/config/Documents')
CATALOG = {'vlc': ('VLC media player', 'vlc'),
           'gimp': ('GIMP image editor', 'gimp'),
           'libreoffice': ('LibreOffice', 'libreoffice'),
           'filezilla': ('FileZilla FTP client', 'filezilla')}
MICROSOFT_365_TRIAL_URL = 'https://www.microsoft.com/microsoft-365/try'


def microsoft_365_trial():
    """Open Microsoft's trial signup for Word and Excel in the browser."""
    executable = shutil.which('xdg-open')
    if not executable:
        raise RuntimeError('No browser opener is installed in this desktop image.')
    subprocess.run([executable, MICROSOFT_365_TRIAL_URL], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return 'Microsoft 365 trial page opened. Use Word and Excel in your browser on Linux.'


def refresh_documents():
    """Ask the desktop to navigate to the current path after replacing its symlink.

    File managers can keep displaying the resolved persistent directory after
    Documents has been disconnected. ShowFolders navigates back to the local
    Documents URI rather than merely refreshing that old resolved directory.
    """
    uri = DOCUMENTS.as_uri()
    if shutil.which('gdbus'):
        try:
            result = subprocess.run(
                ['gdbus', 'call', '--session', '--dest', 'org.freedesktop.FileManager1',
                 '--object-path', '/org/freedesktop/FileManager1',
                 '--method', 'org.freedesktop.FileManager1.ShowFolders',
                 "['" + uri + "']", ''],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    if shutil.which('xdg-open'):
        subprocess.Popen(['xdg-open', str(DOCUMENTS)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def storage(connect):
    link = DOCUMENTS
    if not ROOT.is_dir():
        raise RuntimeError('Persistent storage requires an updated managed host.')
    if link.is_symlink():
        if link.resolve() != ROOT / 'Documents':
            raise RuntimeError('/config/Documents points elsewhere; move it before connecting.')
        if not connect:
            link.unlink()
    elif link.exists() and connect:
        if not link.is_dir() or any(link.iterdir()):
            raise RuntimeError('/config/Documents contains local files; move them before connecting User storage.')
        link.rmdir()
    if connect:
        (ROOT / 'Documents').mkdir(exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(ROOT / 'Documents', target_is_directory=True)
    if not connect:
        link.mkdir(exist_ok=True)
    refresh_documents()
    return 'User storage connected at /config/Documents.' if connect else 'User storage disconnected. Saved files are retained.'


def browser():
    executable = next((shutil.which(x) for x in ('chromium', 'chromium-browser', 'google-chrome') if shutil.which(x)), None)
    if not executable:
        raise RuntimeError('Chromium is not installed in this desktop image.')
    if not ROOT.is_dir():
        raise RuntimeError('Persistent storage is unavailable.')
    profile = ROOT / 'Chromium'
    profile.mkdir(mode=0o700, exist_ok=True)
    # flock prevents simultaneous sessions from corrupting the shared profile.
    subprocess.Popen(['flock', '-n', str(ROOT / 'chromium.lock'), executable,
                      '--user-data-dir=' + str(profile), '--password-store=basic'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return 'Persistent Chromium launched (one session at a time). Close it normally to save data.'


def install(key):
    if key not in CATALOG:
        raise ValueError('Unknown catalog app')
    package = CATALOG[key][1]
    if shutil.which('apt-get'):
        commands = [['sudo', '-n', 'apt-get', 'update'],
                    ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-install-recommends', package]]
    elif shutil.which('dnf'):
        commands = [['sudo', '-n', 'dnf', 'install', '-y', package]]
    else:
        raise RuntimeError('This desktop has no supported package manager.')
    for command in commands:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
        if result.returncode:
            raise RuntimeError(result.stdout[-1200:])
    return CATALOG[key][0] + ' installed for this session.'


def connections(seen):
    groups, listeners = {}, set()
    def endpoint(value, family):
        address, port = value.split(':')
        raw = bytes.fromhex(address)
        raw = b''.join(raw[i:i+4][::-1] for i in range(0, len(raw), 4))
        return socket.inet_ntop(family, raw), int(port, 16)
    records = []
    for name, family in (('tcp', socket.AF_INET), ('tcp6', socket.AF_INET6)):
        path = Path('/proc/net') / name
        if path.exists():
            for line in path.read_text().splitlines()[1:]:
                fields = line.split()
                local, remote = endpoint(fields[1], family), endpoint(fields[2], family)
                if fields[3] == '0A':
                    listeners.add((family, local[1]))
                elif fields[3] == '01':
                    records.append((family, local, remote))
    current = {}
    reader = None
    try:
        import geoip2.database
        database = Path('/usr/share/GeoIP/GeoLite2-Country.mmdb')
        if database.exists():
            reader = geoip2.database.Reader(str(database))
    except ImportError:
        pass
    try:
        for family, local, remote in records:
            address = ipaddress.ip_address(remote[0])
            address = getattr(address, 'ipv4_mapped', None) or address
            if not address.is_global or address.is_multicast:
                continue
            remote_ip = str(address)
            key = (family, local, remote)
            first = seen.get(key, datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
            current[key] = first
            direction = 'Incoming' if (family, local[1]) in listeners else 'Outgoing (inferred)'
            if remote_ip not in groups:
                country = 'Unknown (no GeoIP data)'
                if reader:
                    try:
                        country = reader.country(remote_ip).country.name or 'Unknown'
                    except Exception:
                        country = 'Unknown'
                groups[remote_ip] = {'directions': set(), 'first': first,
                                     'ports': set(), 'count': 0, 'country': country}
            group = groups[remote_ip]
            group['directions'].add(direction)
            group['first'] = min(group['first'], first)
            group['ports'].add(remote[1])
            group['count'] += 1
    finally:
        if reader:
            reader.close()
    seen.clear()
    seen.update(current)
    return [(', '.join(sorted(group['directions'])), group['first'], address,
             ', '.join(str(port) for port in sorted(group['ports'])),
             str(group['count']), group['country'])
            for address, group in sorted(groups.items(), key=lambda item: (
                ipaddress.ip_address(item[0]).version, int(ipaddress.ip_address(item[0]))))]
