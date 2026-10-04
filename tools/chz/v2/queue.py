"""Synthetic issuance: durable bundle != committed, deliverable issue."""
import os
import re
import uuid

from .domain import SafetyError, canonical, digest, gtin, hit
from .store import timestamp


def flush_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Queue:
    def __init__(self, store):
        self.s = store

    def reserve(self, product, quantity, now, fault=None):
        gtin(product); timestamp(now)
        if type(quantity) is not int or quantity < 1:
            raise SafetyError('Некорректное количество')
        with self.s.lock(), self.s.tx():
            self.s.verify()
            rows = self.s.db.execute("SELECT id FROM codes WHERE gtin=? AND allocation='AVAILABLE' AND external_status='INTRODUCED' AND holder IS NULL ORDER BY position LIMIT ?", (product, quantity)).fetchall()
            if len(rows) != quantity:
                raise SafetyError('Недостаточно пригодных synthetic КИ')
            issue = uuid.uuid4().hex
            self.s.db.execute("INSERT INTO issues(id,state,created_at,item_count) VALUES(?,'RESERVED',?,?)", (issue, now, quantity))
            self.s.db.execute('INSERT INTO reservation_owners VALUES(?,NULL,?)', (issue, issue))
            for r in rows:
                self.s.db.execute('INSERT INTO reservations VALUES(?,?)', (r['id'], issue))
                self.s.db.execute('INSERT INTO issue_items VALUES(?,?)', (issue, r['id']))
            self.s.event(issue, 'QUEUE_RESERVED', now, count=quantity)
        hit(fault, 'after_reservation')
        return issue

    def _manifest(self, issue):
        if not isinstance(issue, str) or not re.fullmatch('[0-9a-f]{32}', issue):
            raise SafetyError('Некорректная identity выдачи')
        state = self.s.db.execute('SELECT * FROM issues WHERE id=?', (issue,)).fetchone()
        if not state:
            raise SafetyError('Выдача не найдена')
        rows = self.s.db.execute('SELECT code_id FROM issue_items WHERE issue_id=? ORDER BY code_id', (issue,)).fetchall()
        if len(rows) != state['item_count']:
            raise SafetyError('Нарушен состав выдачи')
        return canonical({'simulation': True, 'issue': issue, 'environment': self.s.environment,
                          'items': [r[0] for r in rows]})

    def build(self, issue, fault=None):
        with self.s.lock():
            self.s.verify()
            manifest = self._manifest(issue)
            final = self.s.root / ('bundle-' + issue)
            if final.exists():
                self._validate(issue)
                return
            stage = self.s.root / ('staging-' + issue)
            if stage.is_symlink() or final.is_symlink():
                raise SafetyError('Ссылки в bundle запрещены')
            stage.mkdir(mode=0o700, exist_ok=True)
            # This is a synthetic pack, NOT a printable production label or PDF.
            for name, data in [('items.json', manifest), ('manifest.sha256', digest(manifest).encode())]:
                fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    os.chmod(stage / name, 0o600)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                hit(fault, 'during_artifacts')
            flush_directory(stage)
            hit(fault, 'before_rename')
            os.rename(stage, final)
            flush_directory(self.s.root)
            hit(fault, 'after_rename')

    def _validate(self, issue):
        expected = self._manifest(issue)
        final = self.s.root / ('bundle-' + issue)
        if final.is_symlink() or any(p.is_symlink() for p in final.glob('*')):
            raise SafetyError('Ссылки в bundle запрещены')
        try:
            manifest = (final / 'items.json').read_bytes()
            if manifest != expected or (final / 'manifest.sha256').read_text() != digest(manifest):
                raise SafetyError('Повреждён synthetic bundle')
        except OSError:
            raise SafetyError('Bundle не завершён') from None
        fingerprint = digest(manifest)
        state = self.s.db.execute('SELECT * FROM issues WHERE id=?', (issue,)).fetchone()
        if state['state'] == 'COMMITTED':
            if state['manifest'] != fingerprint or state['committed_payload'] != manifest:
                raise SafetyError('Несовпадение immutable business commit')
            count = self.s.db.execute("SELECT COUNT(*) FROM issue_items x JOIN codes c ON c.id=x.code_id JOIN reservations r ON r.code_id=c.id WHERE x.issue_id=? AND c.holder=? AND r.owner_id=? AND c.allocation='ISSUED_TO_FF'", (issue, issue, issue)).fetchone()[0]
            if count != state['item_count']:
                raise SafetyError('Нарушена committed reservation')
        return fingerprint

    def commit(self, issue, now, fault=None):
        timestamp(now)
        with self.s.lock(), self.s.tx():
            self.s.verify()
            fingerprint = self._validate(issue)
            r = self.s.db.execute('SELECT * FROM issues WHERE id=?', (issue,)).fetchone()
            if r['state'] == 'COMMITTED':
                return
            if r['state'] != 'RESERVED':
                raise SafetyError('Недопустимое состояние выдачи')
            count = self.s.db.execute("SELECT COUNT(*) FROM issue_items x JOIN codes c ON c.id=x.code_id JOIN reservations r ON r.code_id=c.id WHERE x.issue_id=? AND c.holder=? AND r.owner_id=? AND c.allocation='RESERVED' AND c.external_status='INTRODUCED'", (issue, issue, issue)).fetchone()[0]
            if count != r['item_count']:
                raise SafetyError('Потеря reservation перед commit')
            self.s.db.execute("UPDATE issues SET state='COMMITTED',manifest=?,committed_payload=? WHERE id=?", (fingerprint, self._manifest(issue), issue))
            changed = self.s.db.execute("UPDATE codes SET allocation='ISSUED_TO_FF' WHERE id IN (SELECT code_id FROM issue_items WHERE issue_id=?) AND holder=? AND allocation='RESERVED'", (issue, issue)).rowcount
            if changed != r['item_count']:
                raise SafetyError('Несовпадение количества committed reservations')
            self.s.event(issue, 'QUEUE_COMMITTED', now, response_hash=fingerprint)
            hit(fault, 'during_issue_commit')
        hit(fault, 'after_issue_commit')

    def recovery_state(self, issue):
        if not isinstance(issue, str) or not re.fullmatch('[0-9a-f]{32}', issue):
            raise SafetyError('Некорректная identity выдачи')
        self.s.verify()
        r = self.s.db.execute('SELECT state FROM issues WHERE id=?', (issue,)).fetchone()
        if not r:
            raise SafetyError('Выдача не найдена')
        exists = (self.s.root / ('bundle-' + issue)).exists()
        if r[0] == 'COMMITTED':
            self._validate(issue)
            return 'DELIVERABLE'
        return 'NON_DELIVERABLE_PENDING_RECOVERY' if exists else 'NON_DELIVERABLE_RESERVED'

    def receipt(self, issue):
        if self.recovery_state(issue) != 'DELIVERABLE':
            raise SafetyError('Выдача не зафиксирована в SQLite')
        return {'issue_id': issue, 'state': 'COMMITTED', 'simulation': True,
                'bundle_hash': self._validate(issue), 'ff_receipt': 'UNCONFIRMED'}

    def recover_bundles(self):
        """Inventory only; filesystem discovery never commits a business issue."""
        result = {}
        with self.s.lock():
            self.s.verify()
            for path in self.s.root.glob('bundle-*'):
                issue = path.name.removeprefix('bundle-')
                if not re.fullmatch('[0-9a-f]{32}', issue) or path.is_symlink():
                    raise SafetyError('Неизвестный synthetic bundle')
                row = self.s.db.execute('SELECT 1 FROM issues WHERE id=?', (issue,)).fetchone()
                result[issue] = self.recovery_state(issue) if row else 'NON_DELIVERABLE_ORPHAN'
        return result

    def pointer(self, product):
        # Pure projection; no legacy pointer file is read or written.
        return self.s.db.execute("SELECT MIN(position) FROM codes WHERE gtin=? AND holder IS NULL AND allocation='AVAILABLE' AND external_status='INTRODUCED'", (product,)).fetchone()[0]
