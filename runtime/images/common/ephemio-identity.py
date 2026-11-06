#!/usr/bin/env python3
"""Expose the session identity while retaining Webtop's internal abc alias."""
import os
import pwd
import re
import subprocess
from pathlib import Path


def configure():
    username = os.environ.get('EPHEMIO_LINUX_USERNAME', 'abc')
    full_name = os.environ.get('EPHEMIO_FULL_NAME', '')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username):
        raise ValueError('Invalid session username')
    if len(full_name.encode()) > 100 or re.search(r'[\x00-\x1f\x7f:]', full_name):
        raise ValueError('Invalid session full name')
    original = pwd.getpwnam('abc')
    if username != 'abc':
        try:
            existing = pwd.getpwnam(username)
        except KeyError:
            subprocess.run(['groupadd', '-o', '-g', str(original.pw_gid), username], check=True)
            subprocess.run(['useradd', '-o', '-u', str(original.pw_uid), '-g', username,
                            '-d', original.pw_dir, '-M', '-s', original.pw_shell, '-c', full_name, username], check=True)
        else:
            if (existing.pw_uid, existing.pw_gid, existing.pw_dir) != (original.pw_uid, original.pw_gid, original.pw_dir):
                raise ValueError('Session username is reserved by the image')
            subprocess.run(['usermod', '-c', full_name, username], check=True)
        # Name lookups used by Webtop still resolve abc. UID/GID lookups used by
        # terminals and desktop applications resolve the chosen name first.
        for filename in ('passwd', 'group'):
            path = Path('/etc') / filename
            lines = path.read_text().splitlines()
            selected = [line for line in lines if line.split(':')[0] == username]
            path.write_text('\n'.join(selected + [line for line in lines if line.split(':')[0] != username]) + '\n')
        import grp
        groups = [group.gr_name for group in grp.getgrall() if 'abc' in group.gr_mem and group.gr_gid != original.pw_gid]
        if groups:
            subprocess.run(['usermod', '-a', '-G', ','.join(groups), username], check=True)
    subprocess.run(['usermod', '-c', full_name, 'abc'], check=True)


if __name__ == '__main__':
    configure()
