#!/usr/bin/python3
"""Desktop companion; host credentials are scoped to this disposable session."""
import importlib.util
from pathlib import Path
import http.client
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt5 import QtCore, QtGui, QtWidgets


class HostConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/run/ephemio-guest/api.sock')


def call(action):
    conn = HostConnection('localhost', timeout=25)
    try:
        conn.request('GET' if action == 'status' else 'POST',
                     '/guest/' + os.environ['EPHEMIO_SESSION_ID'] + '/' + action,
                     headers={'Authorization': 'Bearer ' + os.environ['EPHEMIO_GUEST_TOKEN']})
        response = conn.getresponse()
        body = response.read(65536).decode()
        if response.status != 200:
            raise RuntimeError(body or 'Host unavailable')
        return json.loads(body)
    finally:
        conn.close()


spec = importlib.util.spec_from_file_location('ephemio_tools', Path(__file__).with_name('ephemio-tools.py'))
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)

COLORS = {'cpu': '#9c8cff', 'memory': '#4cc9d9', 'disk': '#f3b86b', 'network': '#69d6a4'}


def quantity(text):
    """Docker uses both decimal and binary byte units."""
    import re
    match = re.fullmatch(r'\s*([\d.]+)\s*([kMGTPE]?i?B|B)\s*', text, re.I)
    if not match:
        raise ValueError('Unknown byte quantity: ' + text)
    unit = match[2].upper()
    exponent = 'BKMGTPE'.index(unit[0]) if unit != 'B' else 0
    return float(match[1]) * (1024 if 'I' in unit else 1000) ** exponent


def size(value):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(value) < 1000 or unit == 'TB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value:.0f} B'
        value /= 1000


def label(text, name=None):
    widget = QtWidgets.QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


class Sparkline(QtWidgets.QWidget):
    """Small rolling chart, painted in Qt without extra graph dependencies."""
    def __init__(self, color, ceiling=None):
        super().__init__()
        from collections import deque
        self.samples = deque(maxlen=60)
        self.color = QtGui.QColor(color)
        self.ceiling = ceiling
        self.setMinimumHeight(62)

    def append(self, value):
        self.samples.append(max(0, value))
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 6, -1, -5)
        painter.setPen(QtGui.QPen(QtGui.QColor('#293345'), 1))
        for fraction in (0, .5, 1):
            y = rect.top() + rect.height() * fraction
            painter.drawLine(QtCore.QPointF(rect.left(), y), QtCore.QPointF(rect.right(), y))
        if not self.samples:
            painter.setPen(QtGui.QColor('#78879d'))
            painter.drawText(rect, QtCore.Qt.AlignCenter, 'Waiting for data')
            return
        peak = max(self.ceiling or 0, max(self.samples), 1)
        points = [QtCore.QPointF(rect.right() - (len(self.samples) - 1 - i) * rect.width() / 59,
                                rect.bottom() - value / peak * rect.height())
                  for i, value in enumerate(self.samples)]
        if len(points) > 1:
            line = QtGui.QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            fill = QtGui.QPainterPath(line)
            fill.lineTo(points[-1].x(), rect.bottom())
            fill.lineTo(points[0].x(), rect.bottom())
            fill.closeSubpath()
            gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
            top = QtGui.QColor(self.color)
            top.setAlpha(70)
            bottom = QtGui.QColor(self.color)
            bottom.setAlpha(0)
            gradient.setColorAt(0, top)
            gradient.setColorAt(1, bottom)
            painter.fillPath(fill, gradient)
            painter.setPen(QtGui.QPen(self.color, 2))
            painter.drawPath(line)
        painter.setBrush(self.color)
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(points[-1], 3, 3)


class MetricCard(QtWidgets.QFrame):
    def __init__(self, title, color, ceiling=None):
        super().__init__()
        self.setObjectName('card')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 10)
        header = label(title, 'eyebrow')
        header.setStyleSheet('color: ' + color)
        layout.addWidget(header)
        self.value = label('—', 'metric')
        layout.addWidget(self.value)
        self.detail = label(' ', 'muted')
        layout.addWidget(self.detail)
        self.chart = Sparkline(color, ceiling)
        layout.addWidget(self.chart)

    def update_value(self, text, detail, sample):
        self.value.setText(text)
        self.detail.setText(detail)
        self.chart.append(sample)


STYLE = '''
QWidget { background: #101722; color: #e9edf5; font-family: "DejaVu Sans"; font-size: 11px; }
QFrame#card, QFrame#session { background: #1a2332; border: 1px solid #2a3548; border-radius: 14px; }
QFrame#card QLabel, QFrame#session QLabel { background: transparent; border: none; }
QLabel#brand { font-size: 16px; font-weight: 700; }
QLabel#muted { color: #94a3b8; font-size: 10px; }
QLabel#eyebrow { color: #aab7cc; font-size: 10px; font-weight: 700; }
QLabel#metric { font-size: 19px; font-weight: 700; }
QLabel#clock { font-size: 32px; font-weight: 700; }
QLabel#status { color: #69d6a4; background: #19352f; border-radius: 10px; padding: 5px 10px; }
QPushButton { background: #253247; border: 1px solid #36455c; border-radius: 9px; padding: 9px 14px; font-weight: 600; }
QPushButton:hover { background: #32425d; border-color: #7184a4; }
QPushButton:pressed { background: #1c283c; }
QPushButton:disabled { color: #78869c; background: #202b3b; border-color: #2d394b; }
QPushButton#primary { background: #9c8cff; color: #141125; border-color: #9c8cff; }
QPushButton#primary:hover { background: #b3a7ff; }
QPushButton#danger { color: #ffabb1; background: #38232e; border-color: #71414c; }
QPushButton#danger:hover { background: #56303c; }
QTabWidget::pane { border: none; }
QTabBar::tab { color: #94a3b8; padding: 10px 20px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #b8acff; border-bottom: 2px solid #9c8cff; }
QTabBar::tab:hover { color: #e9edf5; }
QLineEdit { background: #1a2332; border: 1px solid #36455c; border-radius: 8px; padding: 10px; }
QLineEdit:focus { border-color: #9c8cff; }
QProgressBar { background: #1a2332; border: 1px solid #36455c; border-radius: 5px; min-height: 10px; }
QProgressBar::chunk { background: #9c8cff; border-radius: 4px; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #101722; width: 8px; }
QScrollBar::handle:vertical { background: #36455c; min-height: 20px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { color: #e9edf5; background: #253247; border: 1px solid #455570; padding: 6px; }
QMenu { background: #1a2332; border: 1px solid #36455c; padding: 5px; }
QMenu::item { padding: 8px 16px; }
QMenu::item:selected { background: #32425d; }
'''


class Companion(QtWidgets.QWidget):
    # New commands belong here; the controls page and search build themselves.
    ACTIONS = (
        ('extend', 'Extend session', 'Add up to 15 minutes within the host limit.', 'extend'),
        ('pause', 'Pause desktop', 'Resume from Ephemio. Your session timer keeps running.', 'pause'),
        ('terminate', 'Terminate desktop', 'End this session immediately and delete its files.', 'stop'),
    )

    def __init__(self):
        super().__init__(flags=QtCore.Qt.Window | QtCore.Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Ephemio session')
        self.setStyleSheet(STYLE)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.local_pool = ThreadPoolExecutor(max_workers=1)
        self.local_future = None
        self.seen_connections = {}
        self.local_buttons = []
        self.future = None
        self.queued_action = None
        self.deadline = None
        self.warned = False
        self.online = False
        self.last_network = None
        self.buttons = []
        self.control_rows = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(label('ephemio', 'brand'))
        header.addStretch()
        self.status = label('Connecting', 'status')
        header.addWidget(self.status)
        layout.addLayout(header)

        session = QtWidgets.QFrame()
        session.setObjectName('session')
        box = QtWidgets.QVBoxLayout(session)
        box.setContentsMargins(18, 14, 18, 16)
        box.addWidget(label('SESSION TIME', 'eyebrow'))
        time_row = QtWidgets.QHBoxLayout()
        self.clock = label('—:—', 'clock')
        time_row.addWidget(self.clock)
        time_row.addStretch()
        self.extend = self.button('extend', 'Extend · +15 min')
        self.extend.setObjectName('primary')
        time_row.addWidget(self.extend)
        box.addLayout(time_row)
        box.addWidget(label('remaining', 'muted'))
        layout.addWidget(session)

        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)
        overview = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(overview)
        grid.setContentsMargins(0, 14, 0, 0)
        grid.setSpacing(10)
        self.cards = {}
        for i, (key, title, ceiling) in enumerate((('cpu', 'CPU', None), ('memory', 'MEMORY', 100),
                                                 ('disk', 'STORAGE', 100), ('network', 'NETWORK', None))):
            card = MetricCard(title, COLORS[key], ceiling)
            card.chart.setToolTip('Recent history · up to 10 minutes in this app')
            self.cards[key] = card
            grid.addWidget(card, i // 2, i % 2)
        self.io = label('Disk I/O  —', 'muted')
        grid.addWidget(self.io, 2, 0, 1, 2)
        self.tcp = label('TCP connections  —', 'muted')
        self.tcp.setToolTip('Established TCP connections in this desktop, including IPv4 and IPv6. Listening sockets are excluded.')
        grid.addWidget(self.tcp, 3, 0, 1, 2)
        show_tcp = QtWidgets.QPushButton('Show TCP connections…')
        show_tcp.clicked.connect(self.show_connections)
        grid.addWidget(show_tcp, 4, 0, 1, 2)
        self.tabs.addTab(overview, 'Overview')
        storage_page = QtWidgets.QWidget()
        storage_layout = QtWidgets.QVBoxLayout(storage_page)
        self.user_storage = label('▣  User storage', 'brand')
        storage_layout.addWidget(self.user_storage)
        detail = label('Your files stay on this host across sessions. Connect to show them in /config/Documents. Disconnect restores a local Documents folder; open files and the backing directory remain accessible.', 'muted')
        detail.setWordWrap(True)
        storage_layout.addWidget(detail)
        for title, callback in (
                ('⇥ Connect User storage', lambda: tools.storage(True)),
                ('⏏ Disconnect User storage', lambda: tools.storage(False))):
            storage_layout.addWidget(self.local_button(title, callback))
        consent = QtWidgets.QCheckBox('Keep Chromium history, cookies and website sign-ins')
        storage_layout.addWidget(consent)
        warning = label('Opt-in browser profile stores sensitive login data without desktop keyring encryption. Use only on a trusted host. Existing browser windows are separate. Website sign-ins may expire.', 'muted')
        warning.setWordWrap(True)
        storage_layout.addWidget(warning)
        launch = self.local_button('Launch persistent Chromium', tools.browser)
        launch.setEnabled(False)
        consent.toggled.connect(launch.setEnabled)
        self.browser_consent = consent
        self.browser_button = launch
        storage_layout.addWidget(launch)
        future_sync = label('Storage backend: local directory. Cloud / FTP sync is planned; no remote account is connected.', 'muted')
        future_sync.setWordWrap(True)
        storage_layout.addWidget(future_sync)
        storage_layout.addStretch()
        self.tabs.addTab(storage_page, 'User storage')
        catalog = QtWidgets.QWidget()
        catalog_layout = QtWidgets.QVBoxLayout(catalog)
        note = label('One-click installs from this desktop’s configured package repositories. Apps are removed when the session ends.', 'muted')
        note.setWordWrap(True)
        catalog_layout.addWidget(note)
        for key, (title, package) in tools.CATALOG.items():
            catalog_layout.addWidget(self.local_button('Install ' + title, lambda k=key: tools.install(k)))
        catalog_layout.addStretch()
        self.tabs.addTab(catalog, 'Apps')
        controls = QtWidgets.QWidget()
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 14, 0, 0)
        search = QtWidgets.QLineEdit()
        search.setPlaceholderText('Find a control…')
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self.filter_controls)
        controls_layout.addWidget(search)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        contents = QtWidgets.QWidget()
        rows = QtWidgets.QVBoxLayout(contents)
        rows.setContentsMargins(0, 6, 0, 0)
        for action, title, description, icon in self.ACTIONS:
            card = QtWidgets.QFrame()
            card.setObjectName('card')
            row = QtWidgets.QVBoxLayout(card)
            row.setContentsMargins(14, 14, 14, 14)
            top = QtWidgets.QHBoxLayout()
            top.addWidget(label(title))
            top.addStretch()
            top.addWidget(self.button(action, title.split()[0]))
            row.addLayout(top)
            detail = label(description, 'muted')
            detail.setWordWrap(True)
            row.addWidget(detail)
            rows.addWidget(card)
            self.control_rows.append((card, (title + ' ' + description).lower()))
        self.no_results = label('No matching controls', 'muted')
        self.no_results.hide()
        rows.addWidget(self.no_results)
        rows.addStretch()
        scroll.setWidget(contents)
        controls_layout.addWidget(scroll)
        self.tabs.addTab(controls, 'Controls')

        self.operation = QtWidgets.QWidget()
        operation_layout = QtWidgets.QVBoxLayout(self.operation)
        operation_layout.setContentsMargins(0, 0, 0, 0)
        self.operation_label = label('', 'muted')
        operation_layout.addWidget(self.operation_label)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName('Operation in progress')
        operation_layout.addWidget(self.progress)
        self.operation.hide()
        layout.addWidget(self.operation)
        self.message = label('', 'muted')
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(self.button('pause', 'Pause'))
        footer.addWidget(self.button('terminate', 'Terminate'))
        layout.addLayout(footer)
        self.tray = QtWidgets.QSystemTrayIcon(self)
        icon = self.make_icon('brand')
        self.setWindowIcon(icon)
        self.tray.setIcon(icon)
        menu = QtWidgets.QMenu(self)
        menu.addAction('Show session status', self.reveal)
        menu.addAction('TCP connections', self.show_connections)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.reveal())
        self.tray.show()
        self.setMinimumSize(480, 620)
        self.resize(530, 700)
        self.show()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)
        self.next_poll = 0

    def local_button(self, title, callback):
        button = QtWidgets.QPushButton(title)
        button.clicked.connect(lambda: self.run_local(callback, title))
        self.local_buttons.append(button)
        return button

    def run_local(self, callback, title='Working'):
        if self.local_future:
            return
        self.operation_label.setText(title + '…')
        self.progress.setAccessibleName(title + ' in progress')
        self.operation.show()
        self.notify('')
        for button in self.local_buttons:
            button.setEnabled(False)
        self.local_future = self.local_pool.submit(callback)

    def show_connections(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('TCP connections')
        dialog.resize(850, 380)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = label('Public TCP connections grouped by remote IP · earliest first-observed time · direction inferred from listening ports. Refresh to update.', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QtWidgets.QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(['Direction', 'First observed', 'Remote IP', 'Ports', 'Connections', 'Country'])
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        layout.addWidget(table)
        def refresh():
            try:
                rows = tools.connections(self.seen_connections)
                table.setRowCount(len(rows))
                for i, row in enumerate(rows):
                    for j, value in enumerate(row):
                        table.setItem(i, j, QtWidgets.QTableWidgetItem(value))
            except Exception as exc:
                note.setText(str(exc))
        button = QtWidgets.QPushButton('Refresh')
        button.clicked.connect(refresh)
        layout.addWidget(button)
        refresh()
        dialog.exec_()

    @staticmethod
    def make_icon(kind):
        pixmap = QtGui.QPixmap(24, 24)
        pixmap.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pixmap)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtGui.QPen(QtGui.QColor('#ffabb1' if kind == 'terminate' else '#d9d2ff'), 2,
                              QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        if kind == 'pause':
            p.drawLine(8, 6, 8, 18)
            p.drawLine(16, 6, 16, 18)
        elif kind == 'terminate':
            p.drawRoundedRect(6, 6, 12, 12, 2, 2)
        elif kind == 'extend':
            p.drawLine(12, 5, 12, 19)
            p.drawLine(5, 12, 19, 12)
        else:
            p.drawArc(4, 4, 16, 16, 40 * 16, 280 * 16)
            p.drawLine(7, 12, 18, 12)
        p.end()
        return QtGui.QIcon(pixmap)

    def button(self, action, title):
        widget = QtWidgets.QPushButton(self.make_icon(action), title)
        widget.setCursor(QtCore.Qt.PointingHandCursor)
        widget.setIconSize(QtCore.QSize(18, 18))
        widget.setToolTip(next(row[2] for row in self.ACTIONS if row[0] == action))
        widget.setAccessibleName(title)
        if action == 'terminate':
            widget.setObjectName('danger')
        widget.clicked.connect(lambda checked=False, a=action: self.act(a))
        self.buttons.append(widget)
        return widget

    def filter_controls(self, text):
        count = 0
        for row, keywords in self.control_rows:
            visible = text.strip().lower() in keywords
            row.setVisible(visible)
            count += visible
        self.no_results.setVisible(count == 0)

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
        else:
            self.showMinimized()

    def notify(self, text, error=False):
        self.message.setText(text)
        self.message.setStyleSheet('color: ' + ('#ffabb1' if error else '#b8acff'))
        self.message.setVisible(bool(text))

    def act(self, action):
        if self.future:
            if self.action == 'status' and self.queued_action is None:
                self.queued_action = action
                for button in self.buttons:
                    button.setEnabled(False)
            return
        self.start(action)

    def start(self, action):
        self.action = action
        if action != 'status':
            messages = {'extend': 'Extending your session…',
                        'pause': 'Pausing… Resume from Ephemio. Your timer keeps running.',
                        'terminate': 'Terminating… Desktop files will be deleted.'}
            self.notify(messages[action])
            for button in self.buttons:
                button.setEnabled(False)
        self.future = self.pool.submit(call, action)

    def update_usage(self, usage):
        cpu = float(usage['CPUPerc'].strip('%'))
        self.cards['cpu'].update_value(f'{cpu:.1f}%', '100% = one CPU core', cpu)
        used, limit = (quantity(part) for part in usage['MemUsage'].split('/'))
        percent = used / limit * 100 if limit else 0
        self.cards['memory'].update_value(f'{percent:.1f}%', f'{size(used)} / {size(limit)}', percent)
        used, limit = usage['DiskBytes'], usage['DiskLimitBytes']
        self.cards['disk'].update_value(size(used), f'{size(limit)} capacity', used / limit * 100 if limit else 0)
        received, sent = (quantity(part) for part in usage['NetIO'].split('/'))
        now = time.monotonic()
        if self.last_network:
            last_time, last_rx, last_tx = self.last_network
            elapsed = max(now - last_time, .01)
            rx, tx = max(0, received - last_rx) / elapsed, max(0, sent - last_tx) / elapsed
            self.cards['network'].update_value(size(rx + tx) + '/s', f'↓ {size(rx)}/s   ↑ {size(tx)}/s', rx + tx)
        else:
            self.cards['network'].value.setText('— /s')
            self.cards['network'].detail.setText('Measuring transfer rate…')
        self.last_network = (now, received, sent)
        self.cards['network'].setToolTip(f'Total received: {size(received)}\nTotal sent: {size(sent)}')
        read, written = usage['BlockIO'].split('/')
        self.io.setText(f'Disk I/O    ↓ {read.strip()} read     ↑ {written.strip()} written')
        self.tcp.setText(f"TCP connections    {usage.get('TCPConnections', '—')} established")

    def tick(self):
        connected = tools.DOCUMENTS.is_symlink() and tools.DOCUMENTS.resolve() == tools.ROOT / 'Documents'
        self.user_storage.setText('▣  User storage · ' + ('Connected' if connected else 'Disconnected'))
        if self.local_future and self.local_future.done():
            try:
                self.notify(self.local_future.result())
            except Exception as exc:
                self.notify(str(exc)[:1200], error=True)
            self.local_future = None
            self.operation.hide()
            for button in self.local_buttons:
                button.setEnabled(True)
            self.browser_button.setEnabled(self.browser_consent.isChecked())
        if self.future and self.future.done():
            action = self.action
            try:
                data = self.future.result()
                if 'session' in data:
                    self.deadline = time.monotonic() + data['session']['deadline'] - data['server_time']
                    self.update_usage(data['usage'])
                    self.online = True
                    self.notify('Session extended.' if action == 'extend' else '')
                    if action == 'extend':
                        self.warned = False
                elif data.get('state') in ('paused', 'deleted'):
                    self.online = False
                    self.status.setText('Paused' if data['state'] == 'paused' else 'Ended')
                    self.deadline = None
                    self.clock.setText('Paused' if data['state'] == 'paused' else 'Ended')
                    self.next_poll = float('inf')
            except Exception as exc:
                self.online = False
                self.status.setText('Offline')
                self.status.setStyleSheet('color: #ffabb1; background: #38232e')
                self.notify('Cannot connect: ' + str(exc)[:220], error=True)
                self.last_network = None
            self.future = None
            if self.next_poll != float('inf'):
                self.next_poll = time.monotonic() + 10
            for button in self.buttons:
                button.setEnabled(self.next_poll != float('inf'))
            if self.queued_action:
                queued, self.queued_action = self.queued_action, None
                self.start(queued)
        if self.deadline is not None:
            remaining = max(0, int(self.deadline - time.monotonic()))
            text = f'{remaining // 60:02d}:{remaining % 60:02d}'
            self.clock.setText(text)
            self.tray.setToolTip('Ephemio · ' + text + ' remaining')
            warning = remaining <= 300
            self.clock.setStyleSheet('color: ' + ('#f3b86b' if warning else '#e9edf5'))
            if self.online:
                self.status.setText('Ending soon' if warning else 'Active')
                self.status.setStyleSheet('color: #f3b86b; background: #3a3023' if warning else '')
            if warning and not self.warned:
                self.warned = True
                self.reveal()
                self.tray.showMessage('Ephemio session ending', 'Less than 5 minutes remain. Extend your session to keep working.')
        if not self.future and time.monotonic() >= self.next_poll:
            self.start('status')


if __name__ == '__main__':
    if not os.environ.get('EPHEMIO_SESSION_ID'):
        sys.exit(0)  # Standalone Compose has no managed host session.
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = Companion()
    sys.exit(app.exec_())
