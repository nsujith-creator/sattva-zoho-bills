"""Single-owner password verification and revocable sessions; standard library only."""
import hashlib
import secrets
import threading
import time
from http.cookies import SimpleCookie, CookieError

ITERATIONS = 600000
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 12 * 60 * 60


def password_hash(password):
    if not isinstance(password, str) or not 16 <= len(password) <= 256:
        raise ValueError('Owner password must contain 16 to 256 characters.')
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'


def cookie_value(header, name):
    if len(header or '') > 4096:
        return ''
    try:
        jar = SimpleCookie(header or '')
        return jar[name].value if name in jar else ''
    except CookieError:
        return ''


class OwnerSessions:
    def __init__(self, verifier, clock=time.monotonic):
        if not verifier.startswith('pbkdf2_sha256$'):
            verifier = password_hash(verifier)
        try:
            algorithm, iterations, salt, digest = verifier.split('$')
            self.iterations = int(iterations)
            self.salt, self.digest = bytes.fromhex(salt), bytes.fromhex(digest)
            assert algorithm == 'pbkdf2_sha256' and self.iterations == ITERATIONS
            assert len(self.salt) == 32 and len(self.digest) == 32
        except (ValueError, AssertionError) as exc:
            raise ValueError('Invalid owner password verifier.') from exc
        self.clock = clock
        self.lock = threading.RLock()
        self.forms, self.sessions, self.failures = {}, {}, []

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode('utf8')).hexdigest()

    def form(self, peer):
        with self.lock:
            now = self.clock()
            self.forms = {k:v for k,v in self.forms.items() if now-v['created'] < 600}
            if len(self.forms) >= 256:
                self.forms.pop(next(iter(self.forms)))
            token = secrets.token_urlsafe(32)
            self.forms[self.key(token)] = {'created':now, 'peer':peer}
            return token

    def login(self, cookie_token, form_token, password, peer):
        with self.lock:
            if not cookie_token.isascii() or not form_token.isascii():
                return None, 'form_expired'
            now = self.clock()
            self.failures = [(t,p) for t,p in self.failures if now-t < 900]
            per_peer = [t for t,p in self.failures if p == peer]
            recent_global = [t for t,p in self.failures if now-t < 60]
            if len(per_peer) >= 5 or len(recent_global) >= 20:
                return None, 'throttled'
            entry = self.forms.pop(self.key(cookie_token), None) if cookie_token else None
            if (not entry or entry['peer'] != peer or now-entry['created'] >= 600
                    or not secrets.compare_digest(cookie_token, form_token)):
                return None, 'form_expired'
            if not isinstance(password,str) or not 1 <= len(password) <= 256:
                self.failures.append((now,peer))
                return None, 'invalid'
            candidate = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), self.salt, self.iterations)
            if not secrets.compare_digest(candidate, self.digest):
                self.failures.append((now,peer))
                return None, 'invalid'
            # Count failures across successes too, so one valid login does not reset a throttle.
            if len(self.sessions) >= 8:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[self.key(token)] = {'created':now,'last_seen':now,'csrf':secrets.token_urlsafe(32)}
            return token, None

    def lookup(self, token):
        if not token or len(token) > 100:
            return None
        with self.lock:
            key = self.key(token)
            session = self.sessions.get(key)
            now = self.clock()
            if not session:
                return None
            if now-session['last_seen'] >= IDLE_SECONDS or now-session['created'] >= ABSOLUTE_SECONDS:
                self.sessions.pop(key, None)
                return None
            session['last_seen'] = now
            return dict(session)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(self.key(token), None)
