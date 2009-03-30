from __future__ import absolute_import

import os, errno, socket, sys, traceback, signal
import select as _select
import thread as _thread

from .request import wsgi_request

# possible socket errors during accept()
_sock_non_fatal = []
for name in ('ECONNABORTED', 'ECONNRESET', 'ETIMEDOUT', 'EHOSTUNREACH',
             'ENETUNREACH', 'EPROTO', 'ENETDOWN', 'EHOSTDOWN', 'ENONET'):
    if hasattr(errno, name):
        _sock_non_fatal.append(getattr(errno, name))


class Worker(object):

    # How long select() should wait for a ready listener
    timeout = 1.0

    def __init__(self, slot, max_requests, server):
        self.slot = slot
        self.max_requests = max_requests
        self.scoreboard = server.scoreboard
        self.accepting_mutex = server.accepting_mutex
        self.listeners = server.listeners
        self.log = server.log
        self.base_environ = server.base_environ
        self.application = application

        self._ident = 0
        self._parent_ident = self._get_ident()
        self._started = False
        self._stopped = False

        # Initialize our scoreboard slot (indicate ready)
        scoreboard[slot] = '\1'
        return

    # WSGIServer method
    def get_app(self):
        return self.application

    @property
    def name(self):
        if self._ident:
            return '%s-%d' % (self.__class__.__name__, self._ident)
        return self.__class__.__name__

    @property
    def active(self):
        return self._started and not self._stopped

    @property
    def ready(self):
        return ord(self.scoreboard[self.slot])

    def __repr__(self):
        status = 'initial'
        if self._started:
            status = 'started'
        if self._stopped:
            status = 'stopped'
        return '<%s (%s)>' % (self.name, status)

    def start(self):
        if self._get_ident() != self._parent_ident:
            raise RuntimeError('only the server can start workers')
        if self._started:
            raise RuntimeError("worker '%s' already started" % self.name)
        self._start(self._bootstrap, ())
        self._started = True

    def _start(self, function, args):
        raise NotImplementedError("subclass '%s' must override" % self.__class__)

    def _bootstrap(self):
        try:
            self._started = True
            self._ident = self._get_ident()
            try:
                self.run()
            except (SystemExit, KeyboardInterrupt):
                pass
            except:
                print >> sys.stderr, "Exception in worker '%s':" % self.name
                traceback.print_exc(None, sys.stderr)
        finally:
            self._stopped = True
        self.log.info("worker '%s' stopped", self.name)
        return

    def stop(self):
        if self._get_ident() != self._parent_ident:
            raise RuntimeError('only the server can stop workers')
        if not self._stopped:
            self._stop()
        return

    def _stop(self):
        raise NotImplementedError("subclass '%s' must override" % self.__class__)

    def terminate(self):
        if self._get_ident() != self._parent_ident:
            raise RuntimeError('only the server can terminate workers')
        if not self._stopped:
            self._terminate()
        return

    def _terminate(self):
        raise NotImplementedError("subclass '%s' must override" % self.__class__)

    def kill(self):
        if os.getpid() != self._parent_ident:
            raise RuntimeError('only the server can kill workers')
        if not self._stopped:
            self._kill()
        return

    def _kill(self):
        raise NotImplementedError("subclass '%s' must override" % self.__class__)

    def run(self):
        """
        Each worker runs within this function. They wait for a job to
        become available, then handle all the requests on that connection
        until it is closed, then return to wait for more jobs.
        """
        # localize some globals
        SelectError = _select.error
        select = _select.select
        # localize some variables
        slot = self.slot
        scoreboard = self.scoreboard
        accepting_mutex = self.accepting_mutex
        listeners = self.listeners
        timeout = self.timeout
        log = self.log
        requests = self.max_requests

        SERVER_BUSY = '\0'
        SERVER_READY = '\1'

        log.debug("worker '%s' started", self.name)

        self._running = True
        while self._running and requests > 0:
            # Indicate that we are ready to handle new requests
            scoreboard[slot] = SERVER_READY

            try:
                ready, writers, errors = select(listeners, (), (), timeout)
            except SelectError, (code, error):
                # Single UNIX documents select as returning errnos
                # EBADF, EINVAL and ENOMEM... and in none of
                # those cases does it make sense to continue.
                if code != errno.EINTR:
                    log.error('during select(): [errno %d] %s', code, error)
                break
            else:
                if not ready:
                    # timed out; this allows for our owner to kill us off
                    # by setting `self._running` to `False`.
                    continue

            try:
                # Serialize the accepts between all workers
                accepting_mutex.acquire()
                try:
                    # Make sure there is still a request left to process,
                    # because of multiple workers, this is not always true.
                    try:
                        # A timeout of `0` means just poll as we already know
                        # that the list is "ready".
                        ready, writers, errors = select(ready, (), (), 0.0)
                    except SelectError:
                        break
                    else:
                        if not ready:
                            continue
                    # As soon as a connection is accepted, it no longer will
                    # be in the input pending list
                    listener = ready[0]
                    conn_sock, client_addr = listener.socket.accept()
                    server = listener.server
                finally:
                    accepting_mutex.release()
            except socket.error, (code, error):
                # Most of the errors are quite fatal. So it seems
                # best just to exit in most cases.
                if code in _sock_non_fatal:
                    # ignore common disconnect errors
                    continue
                else:
                    log.error('during socket.accept(): [errno %d] %s',
                              code, error)
                    break

            # We now have a connection, so set it up with the appropriate
            # socket options, file descriptors, and read/write buffers.
            try:
                local_addr = conn_sock.getsockname()
            except socket.error:
                log.error('getsockname')
                continue

            try:
                conn_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except socket.error:
                log.warning('setsockopt: TCP_NODELAY')

            # Signify that we are currently handling a request
            scoreboard[slot] = SERVER_BUSY

            try:
                wsgi_request(conn_sock, client_addr, self)
            except:
                exc = ''.join(traceback.format_exception(*sys.exc_info()))
                remote_ip, remote_port = client_addr
                log.error("request failed for %s:\n%s", remote_ip, exc)

            try:
                conn_sock.close()
            except socket.error:
                pass

            # Decrement our request limit counter, once this reaches zero,
            # we will exit gracefully.
            requests -= 1

        if not requests:
            log.notice("worker '%s' reached MaxRequestsPerServer", self.name)
        self._running = False
        return


class ThreadWorker(Worker):

    _get_ident = _thread.get_ident
    _start = _thread.start_new_thread

    def _stop(self):
        self._running = False
    # Python does not have a wait to terminate threads
    _terminate = _kill = _stop


class ProcessWorker(Worker):

    # select() should wait forever as signals will interrupt as needed
    timeout = None

    _get_ident = os.getpid

    def _get_stopped(self):
        pid, status = os.waitpid(self._ident, os.WNOHANG)
        return pid == self._ident
    def _set_stopped(self, value):
        return
    _stopped = property(_get_stopped, _set_stopped)
    del _get_stopped, _set_stopped

    def _start(self, function, args):
        self._ident = os.fork()
        if self._ident == 0:
            def handler(signum, frame, self=self):
                self._running = False
            signal.signal(signal.SIGHUP, handler)
            function(*args)
            os._exit(0)
        return

    def _signal(self, signal):
        # Safe-guard against rogue calls
        if os.getpid() != self._parent_ident:
            raise RuntimeError('can only signal a child process')
        try:
            os.kill(self._ident, signal)
        except OSError:
            if not self._stopped:
                raise
        return

    def _stop(self):
        self._signal(signal.SIGHUP)

    def _terminate(self):
        self._signal(signal.SIGTERM)

    def _kill(self):
        self._signal(signal.SIGKILL)