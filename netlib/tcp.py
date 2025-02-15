from __future__ import (absolute_import, print_function, division)
import os
import select
import socket
import sys
import threading
import time
import traceback

import binascii
from six.moves import range

import certifi
import six
import OpenSSL
from OpenSSL import SSL

from . import certutils, version_check

# This is a rather hackish way to make sure that
# the latest version of pyOpenSSL is actually installed.
from netlib.exceptions import InvalidCertificateException, TcpReadIncomplete, TlsException, \
    TcpTimeout, TcpDisconnect, TcpException

version_check.check_pyopenssl_version()


EINTR = 4

# To enable all SSL methods use: SSLv23
# then add options to disable certain methods
# https://bugs.launchpad.net/pyopenssl/+bug/1020632/comments/3
SSL_BASIC_OPTIONS = (
    SSL.OP_CIPHER_SERVER_PREFERENCE
)
if hasattr(SSL, "OP_NO_COMPRESSION"):
    SSL_BASIC_OPTIONS |= SSL.OP_NO_COMPRESSION

SSL_DEFAULT_METHOD = SSL.SSLv23_METHOD
SSL_DEFAULT_OPTIONS = (
    SSL.OP_NO_SSLv2 |
    SSL.OP_NO_SSLv3 |
    SSL_BASIC_OPTIONS
)
if hasattr(SSL, "OP_NO_COMPRESSION"):
    SSL_DEFAULT_OPTIONS |= SSL.OP_NO_COMPRESSION

"""
Map a reasonable SSL version specification into the format OpenSSL expects.
Don't ask...
https://bugs.launchpad.net/pyopenssl/+bug/1020632/comments/3
"""
sslversion_choices = {
    "all": (SSL.SSLv23_METHOD, SSL_BASIC_OPTIONS),
    # SSLv23_METHOD + NO_SSLv2 + NO_SSLv3 == TLS 1.0+
    # TLSv1_METHOD would be TLS 1.0 only
    "secure": (SSL.SSLv23_METHOD, (SSL.OP_NO_SSLv2 | SSL.OP_NO_SSLv3 | SSL_BASIC_OPTIONS)),
    "SSLv2": (SSL.SSLv2_METHOD, SSL_BASIC_OPTIONS),
    "SSLv3": (SSL.SSLv3_METHOD, SSL_BASIC_OPTIONS),
    "TLSv1": (SSL.TLSv1_METHOD, SSL_BASIC_OPTIONS),
    "TLSv1_1": (SSL.TLSv1_1_METHOD, SSL_BASIC_OPTIONS),
    "TLSv1_2": (SSL.TLSv1_2_METHOD, SSL_BASIC_OPTIONS),
}

class SSLKeyLogger(object):

    def __init__(self, filename):
        self.filename = filename
        self.f = None
        self.lock = threading.Lock()

    # required for functools.wraps, which pyOpenSSL uses.
    __name__ = "SSLKeyLogger"

    def __call__(self, connection, where, ret):
        if where == SSL.SSL_CB_HANDSHAKE_DONE and ret == 1:
            with self.lock:
                if not self.f:
                    d = os.path.dirname(self.filename)
                    if not os.path.isdir(d):
                        os.makedirs(d)
                    self.f = open(self.filename, "ab")
                    self.f.write(b"\r\n")
                client_random = binascii.hexlify(connection.client_random())
                masterkey = binascii.hexlify(connection.master_key())
                self.f.write(b"CLIENT_RANDOM %s %s\r\n" % (client_random, masterkey))
                self.f.flush()

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()

    @staticmethod
    def create_logfun(filename):
        if filename:
            return SSLKeyLogger(filename)
        return False

log_ssl_key = SSLKeyLogger.create_logfun(
    os.getenv("MITMPROXY_SSLKEYLOGFILE") or os.getenv("SSLKEYLOGFILE"))


class _FileLike(object):
    BLOCKSIZE = 1024 * 32

    def __init__(self, o):
        self.o = o
        self._log = None
        self.first_byte_timestamp = None

    def set_descriptor(self, o):
        self.o = o

    def __getattr__(self, attr):
        return getattr(self.o, attr)

    def start_log(self):
        """
            Starts or resets the log.

            This will store all bytes read or written.
        """
        self._log = []

    def stop_log(self):
        """
            Stops the log.
        """
        self._log = None

    def is_logging(self):
        return self._log is not None

    def get_log(self):
        """
            Returns the log as a string.
        """
        if not self.is_logging():
            raise ValueError("Not logging!")
        return b"".join(self._log)

    def add_log(self, v):
        if self.is_logging():
            self._log.append(v)

    def reset_timestamps(self):
        self.first_byte_timestamp = None


class Writer(_FileLike):

    def flush(self):
        """
            May raise TcpDisconnect
        """
        if hasattr(self.o, "flush"):
            try:
                self.o.flush()
            except (socket.error, IOError) as v:
                raise TcpDisconnect(str(v))

    def write(self, v):
        """
            May raise TcpDisconnect
        """
        if v:
            self.first_byte_timestamp = self.first_byte_timestamp or time.time()
            try:
                if hasattr(self.o, "sendall"):
                    self.add_log(v)
                    return self.o.sendall(v)
                else:
                    r = self.o.write(v)
                    self.add_log(v[:r])
                    return r
            except (SSL.Error, socket.error) as e:
                raise TcpDisconnect(str(e))


class Reader(_FileLike):

    def read(self, length):
        """
            If length is -1, we read until connection closes.
        """
        result = b''
        start = time.time()
        while length == -1 or length > 0:
            if length == -1 or length > self.BLOCKSIZE:
                rlen = self.BLOCKSIZE
            else:
                rlen = length
            try:
                data = self.o.read(rlen)
            except SSL.ZeroReturnError:
                # TLS connection was shut down cleanly
                break
            except (SSL.WantWriteError, SSL.WantReadError):
                # From the OpenSSL docs:
                # If the underlying BIO is non-blocking, SSL_read() will also return when the
                # underlying BIO could not satisfy the needs of SSL_read() to continue the
                # operation. In this case a call to SSL_get_error with the return value of
                # SSL_read() will yield SSL_ERROR_WANT_READ or SSL_ERROR_WANT_WRITE.
                if (time.time() - start) < self.o.gettimeout():
                    time.sleep(0.1)
                    continue
                else:
                    raise TcpTimeout()
            except socket.timeout:
                raise TcpTimeout()
            except socket.error as e:
                raise TcpDisconnect(str(e))
            except SSL.SysCallError as e:
                if e.args == (-1, 'Unexpected EOF'):
                    break
                raise TlsException(str(e))
            except SSL.Error as e:
                raise TlsException(str(e))
            self.first_byte_timestamp = self.first_byte_timestamp or time.time()
            if not data:
                break
            result += data
            if length != -1:
                length -= len(data)
        self.add_log(result)
        return result

    def readline(self, size=None):
        result = b''
        bytes_read = 0
        while True:
            if size is not None and bytes_read >= size:
                break
            ch = self.read(1)
            bytes_read += 1
            if not ch:
                break
            else:
                result += ch
                if ch == b'\n':
                    break
        return result

    def safe_read(self, length):
        """
            Like .read, but is guaranteed to either return length bytes, or
            raise an exception.
        """
        result = self.read(length)
        if length != -1 and len(result) != length:
            if not result:
                raise TcpDisconnect()
            else:
                raise TcpReadIncomplete(
                    "Expected %s bytes, got %s" % (length, len(result))
                )
        return result

    def peek(self, length):
        """
        Tries to peek into the underlying file object.

        Returns:
            Up to the next N bytes if peeking is successful.

        Raises:
            TcpException if there was an error with the socket
            TlsException if there was an error with pyOpenSSL.
            NotImplementedError if the underlying file object is not a (pyOpenSSL) socket
        """
        if isinstance(self.o, socket._fileobject):
            try:
                return self.o._sock.recv(length, socket.MSG_PEEK)
            except socket.error as e:
                raise TcpException(repr(e))
        elif isinstance(self.o, SSL.Connection):
            try:
                if tuple(int(x) for x in OpenSSL.__version__.split(".")[:2]) > (0, 15):
                    return self.o.recv(length, socket.MSG_PEEK)
                else:
                    # TODO: remove once a new version is released
                    # Polyfill for pyOpenSSL <= 0.15.1
                    # Taken from https://github.com/pyca/pyopenssl/commit/1d95dea7fea03c7c0df345a5ea30c12d8a0378d2
                    buf = SSL._ffi.new("char[]", length)
                    result = SSL._lib.SSL_peek(self.o._ssl, buf, length)
                    self.o._raise_ssl_error(self.o._ssl, result)
                    return SSL._ffi.buffer(buf, result)[:]
            except SSL.Error as e:
                six.reraise(TlsException, TlsException(str(e)), sys.exc_info()[2])
        else:
            raise NotImplementedError("Can only peek into (pyOpenSSL) sockets")


class Address(object):

    """
        This class wraps an IPv4/IPv6 tuple to provide named attributes and
        ipv6 information.
    """

    def __init__(self, address, use_ipv6=False):
        self.address = tuple(address)
        self.use_ipv6 = use_ipv6

    @classmethod
    def wrap(cls, t):
        if isinstance(t, cls):
            return t
        else:
            return cls(t)

    def __call__(self):
        return self.address

    @property
    def host(self):
        return self.address[0]

    @property
    def port(self):
        return self.address[1]

    @property
    def use_ipv6(self):
        return self.family == socket.AF_INET6

    @use_ipv6.setter
    def use_ipv6(self, b):
        self.family = socket.AF_INET6 if b else socket.AF_INET

    def __repr__(self):
        return "{}:{}".format(self.host, self.port)

    def __str__(self):
        return str(self.address)

    def __eq__(self, other):
        if not other:
            return False
        other = Address.wrap(other)
        return (self.address, self.family) == (other.address, other.family)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.address) ^ 42  # different hash than the tuple alone.


def ssl_read_select(rlist, timeout):
    """
    This is a wrapper around select.select() which also works for SSL.Connections
    by taking ssl_connection.pending() into account.

    Caveats:
        If .pending() > 0 for any of the connections in rlist, we avoid the select syscall
        and **will not include any other connections which may or may not be ready**.

    Args:
        rlist: wait until ready for reading

    Returns:
        subset of rlist which is ready for reading.
    """
    return [
        conn for conn in rlist
        if isinstance(conn, SSL.Connection) and conn.pending() > 0
    ] or select.select(rlist, (), (), timeout)[0]


def close_socket(sock):
    """
    Does a hard close of a socket, without emitting a RST.
    """
    try:
        # We already indicate that we close our end.
        # may raise "Transport endpoint is not connected" on Linux
        sock.shutdown(socket.SHUT_WR)

        # Section 4.2.2.13 of RFC 1122 tells us that a close() with any pending
        # readable data could lead to an immediate RST being sent (which is the
        # case on Windows).
        # http://ia600609.us.archive.org/22/items/TheUltimateSo_lingerPageOrWhyIsMyTcpNotReliable/the-ultimate-so_linger-page-or-why-is-my-tcp-not-reliable.html
        #
        # This in turn results in the following issue: If we send an error page
        # to the client and then close the socket, the RST may be received by
        # the client before the error page and the users sees a connection
        # error rather than the error page. Thus, we try to empty the read
        # buffer on Windows first. (see
        # https://github.com/mitmproxy/mitmproxy/issues/527#issuecomment-93782988)
        #

        if os.name == "nt":  # pragma: no cover
            # We cannot rely on the shutdown()-followed-by-read()-eof technique
            # proposed by the page above: Some remote machines just don't send
            # a TCP FIN, which would leave us in the unfortunate situation that
            # recv() would block infinitely. As a workaround, we set a timeout
            # here even if we are in blocking mode.
            sock.settimeout(sock.gettimeout() or 20)

            # limit at a megabyte so that we don't read infinitely
            for _ in range(1024 ** 3 // 4096):
                # may raise a timeout/disconnect exception.
                if not sock.recv(4096):
                    break

        # Now we can close the other half as well.
        sock.shutdown(socket.SHUT_RD)

    except socket.error:
        pass

    sock.close()


class _Connection(object):

    rbufsize = -1
    wbufsize = -1

    def __init__(self, connection):
        if connection:
            self.connection = connection
            self.rfile = Reader(self.connection.makefile('rb', self.rbufsize))
            self.wfile = Writer(self.connection.makefile('wb', self.wbufsize))
        else:
            self.connection = None
            self.rfile = None
            self.wfile = None

        self.ssl_established = False
        self.finished = False

    def get_current_cipher(self):
        if not self.ssl_established:
            return None

        name = self.connection.get_cipher_name()
        bits = self.connection.get_cipher_bits()
        version = self.connection.get_cipher_version()
        return name, bits, version

    def finish(self):
        self.finished = True
        # If we have an SSL connection, wfile.close == connection.close
        # (We call _FileLike.set_descriptor(conn))
        # Closing the socket is not our task, therefore we don't call close
        # then.
        if not isinstance(self.connection, SSL.Connection):
            if not getattr(self.wfile, "closed", False):
                try:
                    self.wfile.flush()
                    self.wfile.close()
                except TcpDisconnect:
                    pass

            self.rfile.close()
        else:
            try:
                self.connection.shutdown()
            except SSL.Error:
                pass

    def _create_ssl_context(self,
                            method=SSL_DEFAULT_METHOD,
                            options=SSL_DEFAULT_OPTIONS,
                            verify_options=SSL.VERIFY_NONE,
                            ca_path=None,
                            ca_pemfile=None,
                            cipher_list=None,
                            alpn_protos=None,
                            alpn_select=None,
                            alpn_select_callback=None,
                            ):
        """
        Creates an SSL Context.

        :param method: One of SSLv2_METHOD, SSLv3_METHOD, SSLv23_METHOD, TLSv1_METHOD, TLSv1_1_METHOD, or TLSv1_2_METHOD
        :param options: A bit field consisting of OpenSSL.SSL.OP_* values
        :param verify_options: A bit field consisting of OpenSSL.SSL.VERIFY_* values
        :param ca_path: Path to a directory of trusted CA certificates prepared using the c_rehash tool
        :param ca_pemfile: Path to a PEM formatted trusted CA certificate
        :param cipher_list: A textual OpenSSL cipher list, see https://www.openssl.org/docs/apps/ciphers.html
        :rtype : SSL.Context
        """
        context = SSL.Context(method)
        # Options (NO_SSLv2/3)
        if options is not None:
            context.set_options(options)

        # Verify Options (NONE/PEER and trusted CAs)
        if verify_options is not None:
            def verify_cert(conn, x509, errno, err_depth, is_cert_verified):
                if not is_cert_verified:
                    self.ssl_verification_error = dict(errno=errno,
                                                       depth=err_depth)
                return is_cert_verified

            context.set_verify(verify_options, verify_cert)
            if ca_path is None and ca_pemfile is None:
                ca_pemfile = certifi.where()
            context.load_verify_locations(ca_pemfile, ca_path)

        # Workaround for
        # https://github.com/pyca/pyopenssl/issues/190
        # https://github.com/mitmproxy/mitmproxy/issues/472
        # Options already set before are not cleared.
        context.set_mode(SSL._lib.SSL_MODE_AUTO_RETRY)

        # Cipher List
        if cipher_list:
            try:
                context.set_cipher_list(cipher_list)

                # TODO: maybe change this to with newer pyOpenSSL APIs
                context.set_tmp_ecdh(OpenSSL.crypto.get_elliptic_curve('prime256v1'))
            except SSL.Error as v:
                raise TlsException("SSL cipher specification error: %s" % str(v))

        # SSLKEYLOGFILE
        if log_ssl_key:
            context.set_info_callback(log_ssl_key)

        if OpenSSL._util.lib.Cryptography_HAS_ALPN:
            if alpn_protos is not None:
                # advertise application layer protocols
                context.set_alpn_protos(alpn_protos)
            elif alpn_select is not None and alpn_select_callback is None:
                # select application layer protocol
                def alpn_select_callback(conn_, options):
                    if alpn_select in options:
                        return bytes(alpn_select)
                    else:  # pragma no cover
                        return options[0]
                context.set_alpn_select_callback(alpn_select_callback)
            elif alpn_select_callback is not None and alpn_select is None:
                context.set_alpn_select_callback(alpn_select_callback)
            elif alpn_select_callback is not None and alpn_select is not None:
                raise TlsException("ALPN error: only define alpn_select (string) OR alpn_select_callback (method).")

        return context


class TCPClient(_Connection):

    def __init__(self, address, source_address=None):
        super(TCPClient, self).__init__(None)
        self.address = address
        self.source_address = Address.wrap(
            source_address) if source_address else None
        self.cert = None
        self.ssl_verification_error = None
        self.sni = None

    @property
    def address(self):
        return self.__address

    @address.setter
    def address(self, address):
        if self.connection:
            raise RuntimeError("Cannot change server address after establishing connection")
        if address:
            self.__address = Address.wrap(address)
        else:
            self.__address = None

    def close(self):
        # Make sure to close the real socket, not the SSL proxy.
        # OpenSSL is really good at screwing up, i.e. when trying to recv from a failed connection,
        # it tries to renegotiate...
        if isinstance(self.connection, SSL.Connection):
            close_socket(self.connection._socket)
        else:
            close_socket(self.connection)

    def create_ssl_context(self, cert=None, alpn_protos=None, **sslctx_kwargs):
        context = self._create_ssl_context(
            alpn_protos=alpn_protos,
            **sslctx_kwargs)
        # Client Certs
        if cert:
            try:
                context.use_privatekey_file(cert)
                context.use_certificate_file(cert)
            except SSL.Error as v:
                raise TlsException("SSL client certificate error: %s" % str(v))
        return context

    def convert_to_ssl(self, sni=None, alpn_protos=None, **sslctx_kwargs):
        """
            cert: Path to a file containing both client cert and private key.

            options: A bit field consisting of OpenSSL.SSL.OP_* values
            verify_options: A bit field consisting of OpenSSL.SSL.VERIFY_* values
            ca_path: Path to a directory of trusted CA certificates prepared using the c_rehash tool
            ca_pemfile: Path to a PEM formatted trusted CA certificate
        """
        context = self.create_ssl_context(
            alpn_protos=alpn_protos,
            **sslctx_kwargs)
        self.connection = SSL.Connection(context, self.connection)
        if sni:
            self.sni = sni
            self.connection.set_tlsext_host_name(sni)
        self.connection.set_connect_state()
        try:
            self.connection.do_handshake()
        except SSL.Error as v:
            if self.ssl_verification_error:
                raise InvalidCertificateException("SSL handshake error: %s" % repr(v))
            else:
                raise TlsException("SSL handshake error: %s" % repr(v))

        # Fix for pre v1.0 OpenSSL, which doesn't throw an exception on
        # certificate validation failure
        verification_mode = sslctx_kwargs.get('verify_options', None)
        if self.ssl_verification_error is not None and verification_mode == SSL.VERIFY_PEER:
            raise InvalidCertificateException("SSL handshake error: certificate verify failed")

        self.ssl_established = True
        self.cert = certutils.SSLCert(self.connection.get_peer_certificate())
        self.rfile.set_descriptor(self.connection)
        self.wfile.set_descriptor(self.connection)

    def connect(self):
        try:
            connection = socket.socket(self.address.family, socket.SOCK_STREAM)
            if self.source_address:
                connection.bind(self.source_address())
            connection.connect(self.address())
            if not self.source_address:
                self.source_address = Address(connection.getsockname())
            self.rfile = Reader(connection.makefile('rb', self.rbufsize))
            self.wfile = Writer(connection.makefile('wb', self.wbufsize))
        except (socket.error, IOError) as err:
            raise TcpException(
                'Error connecting to "%s": %s' %
                (self.address.host, err))
        self.connection = connection

    def settimeout(self, n):
        self.connection.settimeout(n)

    def gettimeout(self):
        return self.connection.gettimeout()

    def get_alpn_proto_negotiated(self):
        if OpenSSL._util.lib.Cryptography_HAS_ALPN and self.ssl_established:
            return self.connection.get_alpn_proto_negotiated()
        else:
            return b""


class BaseHandler(_Connection):

    """
        The instantiator is expected to call the handle() and finish() methods.
    """

    def __init__(self, connection, address, server):
        super(BaseHandler, self).__init__(connection)
        self.address = Address.wrap(address)
        self.server = server
        self.clientcert = None

    def create_ssl_context(self,
                           cert, key,
                           handle_sni=None,
                           request_client_cert=None,
                           chain_file=None,
                           dhparams=None,
                           **sslctx_kwargs):
        """
            cert: A certutils.SSLCert object or the path to a certificate
            chain file.

            handle_sni: SNI handler, should take a connection object. Server
            name can be retrieved like this:

                    connection.get_servername()

            And you can specify the connection keys as follows:

                    new_context = Context(TLSv1_METHOD)
                    new_context.use_privatekey(key)
                    new_context.use_certificate(cert)
                    connection.set_context(new_context)

            The request_client_cert argument requires some explanation. We're
            supposed to be able to do this with no negative effects - if the
            client has no cert to present, we're notified and proceed as usual.
            Unfortunately, Android seems to have a bug (tested on 4.2.2) - when
            an Android client is asked to present a certificate it does not
            have, it hangs up, which is frankly bogus. Some time down the track
            we may be able to make the proper behaviour the default again, but
            until then we're conservative.
        """

        context = self._create_ssl_context(**sslctx_kwargs)

        context.use_privatekey(key)
        if isinstance(cert, certutils.SSLCert):
            context.use_certificate(cert.x509)
        else:
            context.use_certificate_chain_file(cert)

        if handle_sni:
            # SNI callback happens during do_handshake()
            context.set_tlsext_servername_callback(handle_sni)

        if request_client_cert:
            def save_cert(conn_, cert, errno_, depth_, preverify_ok_):
                self.clientcert = certutils.SSLCert(cert)
                # Return true to prevent cert verification error
                return True
            context.set_verify(SSL.VERIFY_PEER, save_cert)

        # Cert Verify
        if chain_file:
            context.load_verify_locations(chain_file)

        if dhparams:
            SSL._lib.SSL_CTX_set_tmp_dh(context._context, dhparams)

        return context

    def convert_to_ssl(self, cert, key, **sslctx_kwargs):
        """
        Convert connection to SSL.
        For a list of parameters, see BaseHandler._create_ssl_context(...)
        """

        context = self.create_ssl_context(
            cert,
            key,
            **sslctx_kwargs)
        self.connection = SSL.Connection(context, self.connection)
        self.connection.set_accept_state()
        try:
            self.connection.do_handshake()
        except SSL.Error as v:
            raise TlsException("SSL handshake error: %s" % repr(v))
        self.ssl_established = True
        self.rfile.set_descriptor(self.connection)
        self.wfile.set_descriptor(self.connection)

    def handle(self):  # pragma: no cover
        raise NotImplementedError

    def settimeout(self, n):
        self.connection.settimeout(n)

    def get_alpn_proto_negotiated(self):
        if OpenSSL._util.lib.Cryptography_HAS_ALPN and self.ssl_established:
            return self.connection.get_alpn_proto_negotiated()
        else:
            return b""


class TCPServer(object):
    request_queue_size = 20

    def __init__(self, address):
        self.address = Address.wrap(address)
        self.__is_shut_down = threading.Event()
        self.__shutdown_request = False
        self.socket = socket.socket(self.address.family, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.address())
        self.address = Address.wrap(self.socket.getsockname())
        self.socket.listen(self.request_queue_size)

    def connection_thread(self, connection, client_address):
        client_address = Address(client_address)
        try:
            self.handle_client_connection(connection, client_address)
        except:
            self.handle_error(connection, client_address)
        finally:
            close_socket(connection)

    def serve_forever(self, poll_interval=0.1):
        self.__is_shut_down.clear()
        try:
            while not self.__shutdown_request:
                try:
                    r, w_, e_ = select.select(
                        [self.socket], [], [], poll_interval)
                except select.error as ex:  # pragma: no cover
                    if ex[0] == EINTR:
                        continue
                    else:
                        raise
                if self.socket in r:
                    connection, client_address = self.socket.accept()
                    t = threading.Thread(
                        target=self.connection_thread,
                        args=(connection, client_address),
                        name="ConnectionThread (%s:%s -> %s:%s)" %
                             (client_address[0], client_address[1],
                              self.address.host, self.address.port)
                    )
                    t.setDaemon(1)
                    try:
                        t.start()
                    except threading.ThreadError:
                        self.handle_error(connection, Address(client_address))
                        connection.close()
        finally:
            self.__shutdown_request = False
            self.__is_shut_down.set()

    def shutdown(self):
        self.__shutdown_request = True
        self.__is_shut_down.wait()
        self.socket.close()
        self.handle_shutdown()

    def handle_error(self, connection_, client_address, fp=sys.stderr):
        """
            Called when handle_client_connection raises an exception.
        """
        # If a thread has persisted after interpreter exit, the module might be
        # none.
        if traceback:
            exc = six.text_type(traceback.format_exc())
            print(u'-' * 40, file=fp)
            print(
                u"Error in processing of request from %s" % repr(client_address), file=fp)
            print(exc, file=fp)
            print(u'-' * 40, file=fp)

    def handle_client_connection(self, conn, client_address):  # pragma: no cover
        """
            Called after client connection.
        """
        raise NotImplementedError

    def handle_shutdown(self):
        """
            Called after server shutdown.
        """
