#!/usr/bin/python3
"""Local companion operations; no host credentials or external IP lookups."""
import datetime
import ipaddress
from pathlib import Path
import shutil
import socket
import subprocess

CATALOG = {'vlc': ('VLC media player', 'vlc'),
           'gimp': ('GIMP image editor', 'gimp'),
           'libreoffice': ('LibreOffice', 'libreoffice'),
           'filezilla': ('FileZilla FTP client', 'filezilla')}


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
