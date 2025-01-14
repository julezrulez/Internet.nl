# Copyright: 2019, NLnet Labs and the Internet.nl contributors
# SPDX-License-Identifier: Apache-2.0
from binascii import hexlify
import errno
import http.client
import socket
import ssl

import time
from timeit import default_timer as timer

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from cryptography.x509 import load_pem_x509_certificate, NameOID, ExtensionOID
from cryptography.x509 import ExtensionNotFound, SignatureAlgorithmOID, DNSName
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.backends.openssl.ec import _EllipticCurvePublicKey
from cryptography.hazmat.backends.openssl.dh import _DHPublicKey
from cryptography.hazmat.backends.openssl.rsa import _RSAPublicKey
from cryptography.hazmat.backends.openssl.dsa import _DSAPublicKey
from cryptography.hazmat.primitives import hashes
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from nassl import _nassl
from nassl.ssl_client import OpenSslVersionEnum, OpenSslVerifyEnum
from nassl.legacy_ssl_client import LegacySslClient
from nassl.ssl_client import ClientCertificateRequested

from . import SetupUnboundContext, shared
from .dispatcher import check_registry, post_callback_hook
from .http_headers import HeaderCheckerContentEncoding, http_headers_check
from .http_headers import HeaderCheckerStrictTransportSecurity
from .shared import MAX_REDIRECT_DEPTH, NoIpError, resolve_dane
from .shared import results_per_domain, aggregate_subreports
from .. import scoring, categories
from .. import batch, batch_shared_task, redis_id
from ..models import DaneStatus, DomainTestTls, MailTestTls, WebTestTls
from ..models import ForcedHttpsStatus

# Workaround for https://github.com/eventlet/eventlet/issues/413 for eventlet
# while monkey patching. That way we can still catch subprocess.TimeoutExpired
# instead of just Exception which may intervene with Celery's own exceptions.
# Gevent does not have the same issue.
import eventlet
if eventlet.patcher.is_monkey_patched('subprocess'):
    subprocess = eventlet.import_patched('subprocess')
else:
    import subprocess

try:
    from ssl import OP_NO_SSLv2, OP_NO_SSLv3
except ImportError as e:
    # Support for older python versions, not for use in production
    if settings.DEBUG:
        OP_NO_SSLv2 = 16777216
        OP_NO_SSLv3 = 33554432
    else:
        raise e

SSLV2 = OpenSslVersionEnum.SSLV2
SSLV23 = OpenSslVersionEnum.SSLV23
SSLV3 = OpenSslVersionEnum.SSLV3
SSL_VERIFY_NONE = OpenSslVerifyEnum.NONE


# Maximum number of tries on failure to establish a connection.
# Useful on one-time errors on SMTP.
MAX_TRIES = 3

root_fingerprints = None
with open(settings.CA_FINGERPRINTS) as f:
    root_fingerprints = f.read().splitlines()

test_map = {
    'web': {
        'model': WebTestTls,
        'category': categories.WebTls(),
        'testset_name': 'webtestset',
        'port': 443,
    },
    'mail': {
        'model': MailTestTls,
        'category': categories.MailTls(),
        'testset_name': 'testset',
        'port': 25,
    },
}


@shared_task(bind=True)
def web_callback(self, results, domain, req_limit_id):
    """
    Save results in db.

    """
    webdomain, results = callback(results, domain, 'web')
    # Always calculate scores on saving.
    from ..probes import web_probe_tls
    web_probe_tls.rated_results_by_model(webdomain)
    post_callback_hook(req_limit_id, self.request.id)
    return results


@batch_shared_task(bind=True)
def batch_web_callback(self, results, domain):
    webdomain, results = callback(results, domain, 'web')
    # Always calculate scores on saving.
    from ..probes import batch_web_probe_tls
    batch_web_probe_tls.rated_results_by_model(webdomain)
    batch.scheduler.batch_callback_hook(webdomain, self.request.id)


@shared_task(bind=True)
def mail_callback(self, results, domain, req_limit_id):
    maildomain, results = callback(results, domain, 'mail')
    # Always calculate scores on saving.
    from ..probes import mail_probe_tls
    mail_probe_tls.rated_results_by_model(maildomain)
    post_callback_hook(req_limit_id, self.request.id)
    return results


@batch_shared_task(bind=True)
def batch_mail_callback(self, results, domain):
    maildomain, results = callback(results, domain, 'mail')
    # Always calculate scores on saving.
    from ..probes import batch_mail_probe_tls
    batch_mail_probe_tls.rated_results_by_model(maildomain)
    batch.scheduler.batch_callback_hook(maildomain, self.request.id)


@transaction.atomic
def callback(results, domain, test_type):
    results = results_per_domain(results)
    testdomain = test_map[test_type]['model'](domain=domain)
    testdomain.save()
    category = test_map[test_type]['category']
    if len(results.keys()) > 0:
        for addr, res in results.items():
            category = category.__class__()
            dttls = DomainTestTls(domain=addr)
            dttls.port = test_map[test_type]['port']
            save_results(
                dttls, res, addr, domain, test_map[test_type]['category'])
            build_report(dttls, category)
            dttls.save()
            getattr(testdomain, test_map[test_type]['testset_name']).add(dttls)
    build_summary_report(testdomain, category)
    testdomain.save()
    return testdomain, results


web_registered = check_registry("web_tls", web_callback, shared.resolve_a_aaaa)
batch_web_registered = check_registry(
    "batch_web_tls", batch_web_callback, shared.batch_resolve_a_aaaa)
mail_registered = check_registry(
    "mail_tls", mail_callback, shared.mail_get_servers)
batch_mail_registered = check_registry(
    "batch_mail_tls", batch_mail_callback, shared.batch_mail_get_servers)


@web_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def web_cert(self, addrs, url, *args, **kwargs):
    return do_web_cert(addrs, url, self, *args, **kwargs)


@batch_web_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_web_cert(self, addrs, url, *args, **kwargs):
    return do_web_cert(addrs, url, self, *args, **kwargs)


@web_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def web_conn(self, addrs, url, *args, **kwargs):
    return do_web_conn(addrs, url, *args, **kwargs)


@batch_web_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_web_conn(self, addrs, url, *args, **kwargs):
    return do_web_conn(addrs, url, *args, **kwargs)


@mail_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def mail_smtp_starttls(self, mailservers, url, *args, **kwargs):
    return do_mail_smtp_starttls(mailservers, url, self, *args, **kwargs)


@batch_mail_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_mail_smtp_starttls(self, mailservers, url, *args, **kwargs):
    return do_mail_smtp_starttls(mailservers, url, self, *args, **kwargs)


@web_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def web_http(self, addrs, url, *args, **kwargs):
    return do_web_http(addrs, url, self, *args, **kwargs)


@batch_web_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_web_http(self, addrs, url, *args, **kwargs):
    return do_web_http(addrs, url, self, args, **kwargs)


def save_results(model, results, addr, domain, category):
    """
    Save results in DB.

    """
    if isinstance(category, categories.WebTls):
        for testname, result in results:
            if testname == "tls_conn":
                model.server_reachable = result.get("server_reachable", True)
                model.tls_enabled = result.get("tls_enabled")
                model.tls_enabled_score = result.get("tls_enabled_score", 0)
                if model.server_reachable and model.tls_enabled:
                    model.dh_param = result.get("dh_param")
                    model.ecdh_param = result.get("ecdh_param")
                    model.fs_bad = result.get("fs_bad")
                    model.fs_score = result.get("fs_score")
                    model.ciphers_bad = result.get("ciphers_bad")
                    model.ciphers_score = result.get("ciphers_score")
                    model.protocols_bad = result.get("prots_bad")
                    model.protocols_score = result.get("prots_score")
                    model.compression = result.get("compression")
                    model.compression_score = result.get("compression_score")
                    model.secure_reneg = result.get("secure_reneg")
                    model.secure_reneg_score = result.get("secure_reneg_score")
                    model.client_reneg = result.get("client_reneg")
                    model.client_reneg_score = result.get("client_reneg_score")

            elif testname == "cert" and result.get("tls_cert"):
                model.cert_chain = result.get("chain")
                model.cert_trusted = result.get("trusted")
                model.cert_trusted_score = result.get("trusted_score")
                model.cert_pubkey_bad = result.get("pubkey_bad")
                model.cert_pubkey_score = result.get("pubkey_score")
                model.cert_signature_bad = result.get("sigalg_bad")
                model.cert_signature_score = result.get("sigalg_score")
                model.cert_hostmatch_score = result.get("hostmatch_score")
                model.cert_hostmatch_bad = result.get("hostmatch_bad")
                model.dane_log = result.get("dane_log")
                model.dane_score = result.get("dane_score")
                model.dane_status = result.get("dane_status")
                model.dane_records = result.get("dane_records")
                model.dane_rollover = result.get("dane_rollover")

            elif testname == "http_checks":
                model.forced_https = result.get("forced_https")
                model.forced_https_score = result.get("forced_https_score")
                model.http_compression_enabled = result.get(
                    "http_compression_enabled")
                model.http_compression_score = result.get(
                    "http_compression_score")
                model.hsts_enabled = result.get("hsts_enabled")
                model.hsts_policies = result.get("hsts_policies")
                model.hsts_score = result.get("hsts_score")

    elif isinstance(category, categories.MailTls):
        for testname, result in results:
            if testname == "smtp_starttls":
                model.server_reachable = result.get("server_reachable", True)
                model.tls_enabled = result.get("tls_enabled")
                model.tls_enabled_score = result.get("tls_enabled_score", 0)
                model.could_not_test_smtp_starttls = result.get(
                    "could_not_test_smtp_starttls", False)
                if model.could_not_test_smtp_starttls:
                    # Special case where we couldn't connect for a test.
                    # Ignore all the subtests for this server.
                    continue

                if model.server_reachable and model.tls_enabled:
                    model.dh_param = result.get("dh_param")
                    model.ecdh_param = result.get("ecdh_param")
                    model.fs_bad = result.get("fs_bad")
                    model.fs_score = result.get("fs_score")
                    model.ciphers_bad = result.get("ciphers_bad")
                    model.ciphers_score = result.get("ciphers_score")
                    model.protocols_bad = result.get("prots_bad")
                    model.protocols_score = result.get("prots_score")
                    model.compression = result.get("compression")
                    model.compression_score = result.get("compression_score")
                    model.secure_reneg = result.get("secure_reneg")
                    model.secure_reneg_score = result.get("secure_reneg_score")
                    model.client_reneg = result.get("client_reneg")
                    model.client_reneg_score = result.get("client_reneg_score")
                if result.get("tls_cert"):
                    model.cert_chain = result.get("chain")
                    model.cert_trusted = result.get("trusted")
                    model.cert_trusted_score = result.get("trusted_score")
                    model.cert_pubkey_bad = result.get("pubkey_bad")
                    model.cert_pubkey_score = result.get("pubkey_score")
                    model.cert_signature_bad = result.get("sigalg_bad")
                    model.cert_signature_score = result.get("sigalg_score")
                    model.cert_hostmatch_score = result.get("hostmatch_score")
                    model.cert_hostmatch_bad = result.get("hostmatch_bad")
                    model.dane_log = result.get("dane_log")
                    model.dane_score = result.get("dane_score")
                    model.dane_status = result.get("dane_status")
                    model.dane_records = result.get("dane_records")
                    model.dane_rollover = result.get("dane_rollover")

    model.save()


def build_report(dttls, category):
    if isinstance(category, categories.WebTls):
        if not dttls.server_reachable:
            category.subtests['https_exists'].result_unreachable()
        elif not dttls.tls_enabled:
            category.subtests['https_exists'].result_bad()
        else:
            category.subtests['https_exists'].result_good()

            if dttls.forced_https == ForcedHttpsStatus.good:
                category.subtests['https_forced'].result_good()
            elif dttls.forced_https == ForcedHttpsStatus.no_http:
                category.subtests['https_forced'].result_no_http()
            elif dttls.forced_https == ForcedHttpsStatus.bad:
                category.subtests['https_forced'].result_bad()

            if dttls.hsts_enabled:
                if dttls.hsts_score == scoring.WEB_TLS_HSTS_GOOD:
                    category.subtests['https_hsts'].result_good(
                        dttls.hsts_policies)
                else:
                    category.subtests['https_hsts'].result_bad_max_age(
                        dttls.hsts_policies)
            else:
                category.subtests['https_hsts'].result_bad()

            if dttls.http_compression_enabled:
                category.subtests['http_compression'].result_bad()
            else:
                category.subtests['http_compression'].result_good()

            if not dttls.dh_param and not dttls.ecdh_param:
                category.subtests['fs_params'].result_no_dh_params()
            else:
                if len(dttls.fs_bad) > 0:
                    category.subtests['fs_params'].result_bad(dttls.fs_bad)
                else:
                    category.subtests['fs_params'].result_good()

            if len(dttls.ciphers_bad) > 0:
                category.subtests['tls_ciphers'].result_bad(dttls.ciphers_bad)
            else:
                category.subtests['tls_ciphers'].result_good()

            if len(dttls.protocols_bad) > 0:
                category.subtests['tls_version'].result_bad(
                    dttls.protocols_bad)
            else:
                category.subtests['tls_version'].result_good()

            if dttls.compression:
                category.subtests['tls_compression'].result_bad()
            else:
                category.subtests['tls_compression'].result_good()

            if dttls.secure_reneg:
                category.subtests['renegotiation_secure'].result_good()
            else:
                category.subtests['renegotiation_secure'].result_bad()

            if dttls.client_reneg:
                category.subtests['renegotiation_client'].result_bad()
            else:
                category.subtests['renegotiation_client'].result_good()

            if dttls.cert_trusted == 0:
                category.subtests['cert_trust'].result_good()
            else:
                category.subtests['cert_trust'].result_bad(dttls.cert_chain)

            if dttls.cert_pubkey_score is None:
                pass
            elif len(dttls.cert_pubkey_bad) > 0:
                category.subtests['cert_pubkey'].result_bad(
                    dttls.cert_pubkey_bad)
            else:
                category.subtests['cert_pubkey'].result_good()

            if dttls.cert_signature_score is None:
                pass
            elif len(dttls.cert_signature_bad) > 0:
                category.subtests['cert_signature'].result_bad(
                    dttls.cert_signature_bad)
            else:
                category.subtests['cert_signature'].result_good()

            if dttls.cert_hostmatch_score is None:
                pass
            elif len(dttls.cert_hostmatch_bad) > 0:
                category.subtests['cert_hostmatch'].result_bad(
                    dttls.cert_hostmatch_bad)
            else:
                category.subtests['cert_hostmatch'].result_good()

            if dttls.dane_status == DaneStatus.none:
                category.subtests['dane_exists'].result_bad()
            elif dttls.dane_status == DaneStatus.none_bogus:
                category.subtests['dane_exists'].result_bogus()
            else:
                category.subtests['dane_exists'].result_good(
                    dttls.dane_records)

                if dttls.dane_status == DaneStatus.validated:
                    category.subtests['dane_valid'].result_good()
                elif dttls.dane_status == DaneStatus.failed:
                    category.subtests['dane_valid'].result_bad()

                # Disabled for now.
                # if dttls.dane_rollover:
                #     category.subtests['dane_rollover'].result_good()
                # else:
                #     category.subtests['dane_rollover'].result_bad()

    elif isinstance(category, categories.MailTls):
        if dttls.could_not_test_smtp_starttls:
            category.subtests['starttls_exists'].result_could_not_test()
        elif not dttls.server_reachable:
            category.subtests['starttls_exists'].result_unreachable()
        elif not dttls.tls_enabled:
            category.subtests['starttls_exists'].result_bad()
        else:
            category.subtests['starttls_exists'].result_good()

            if not dttls.dh_param and not dttls.ecdh_param:
                category.subtests['fs_params'].result_no_dh_params()
            else:
                if len(dttls.fs_bad) > 0:
                    category.subtests['fs_params'].result_bad(dttls.fs_bad)
                else:
                    category.subtests['fs_params'].result_good()

            if len(dttls.ciphers_bad) > 0:
                category.subtests['tls_ciphers'].result_bad(dttls.ciphers_bad)
            else:
                category.subtests['tls_ciphers'].result_good()

            if len(dttls.protocols_bad) > 0:
                category.subtests['tls_version'].result_bad(
                    dttls.protocols_bad)
            else:
                category.subtests['tls_version'].result_good()

            if dttls.compression:
                category.subtests['tls_compression'].result_bad()
            else:
                category.subtests['tls_compression'].result_good()

            if dttls.secure_reneg:
                category.subtests['renegotiation_secure'].result_good()
            else:
                category.subtests['renegotiation_secure'].result_bad()

            if dttls.client_reneg:
                category.subtests['renegotiation_client'].result_bad()
            else:
                category.subtests['renegotiation_client'].result_good()

            if dttls.cert_trusted == 0:
                category.subtests['cert_trust'].result_good()
            else:
                category.subtests['cert_trust'].result_bad(dttls.cert_chain)

            if dttls.cert_pubkey_score is None:
                pass
            elif len(dttls.cert_pubkey_bad) > 0:
                category.subtests['cert_pubkey'].result_bad(
                    dttls.cert_pubkey_bad)
            else:
                category.subtests['cert_pubkey'].result_good()

            if dttls.cert_signature_score is None:
                pass
            elif len(dttls.cert_signature_bad) > 0:
                category.subtests['cert_signature'].result_bad(
                    dttls.cert_signature_bad)
            else:
                category.subtests['cert_signature'].result_good()

            if dttls.cert_hostmatch_score is None:
                pass
            elif len(dttls.cert_hostmatch_bad) > 0:
                # HACK: for DANE-TA(2) and hostname mismatch!
                # Give a fail only if DANE-TA *is* present, otherwise info.
                if has_daneTA(dttls.dane_records):
                    category.subtests['cert_hostmatch'].result_has_daneTA(
                        dttls.cert_hostmatch_bad)
                else:
                    category.subtests['cert_hostmatch'].result_bad(
                        dttls.cert_hostmatch_bad)
            else:
                category.subtests['cert_hostmatch'].result_good()

            if dttls.dane_status == DaneStatus.none:
                category.subtests['dane_exists'].result_bad()
            elif dttls.dane_status == DaneStatus.none_bogus:
                category.subtests['dane_exists'].result_bogus()
            else:
                category.subtests['dane_exists'].result_good(
                    dttls.dane_records)

                if dttls.dane_status == DaneStatus.validated:
                    category.subtests['dane_valid'].result_good()
                elif dttls.dane_status == DaneStatus.failed:
                    category.subtests['dane_valid'].result_bad()

                if dttls.dane_rollover:
                    category.subtests['dane_rollover'].result_good()
                else:
                    category.subtests['dane_rollover'].result_bad()

    dttls.report = category.gen_report()


def build_summary_report(testtls, category):
    """
    Build the summary report for all the IP addresses.

    """
    category = category.__class__()
    if isinstance(category, categories.WebTls):
        category.subtests['https_exists'].result_bad()
        server_set = testtls.webtestset

    elif isinstance(category, categories.MailTls):
        category.subtests['starttls_exists'].result_no_mailservers()
        server_set = testtls.testset

    report = category.gen_report()
    subreports = {}
    for server_test in server_set.all():
        subreports[server_test.domain] = server_test.report

    aggregate_subreports(subreports, report)
    testtls.report = report


def dane(
        url, port, chain, task, dane_cb_data, score_none, score_none_bogus,
        score_failed, score_validated):
    """
    Check if there are TLSA records, if they are valid and if a DANE rollover
    scheme is currently in place.

    """
    score = score_none
    status = DaneStatus.none
    records = []
    stdout = ""
    rollover = False

    continue_testing = True

    cb_data = dane_cb_data or resolve_dane(task, port, url)

    # Check if there is a TLSA record, if TLSA records are bogus or NXDOMAIN is
    # returned for the TLSA domain (faulty signer).
    if not cb_data.get('data'):
        if cb_data.get('bogus'):
            status = DaneStatus.none_bogus
            score = score_none_bogus
        continue_testing = False
    else:
        if cb_data.get('secure'):
            # If there is a secure TLSA record check for the existence of
            # possible bogus (unsigned) NXDOMAIN in A.
            tmp_data = resolve_dane(task, port, url, check_nxdomain=True)
            if tmp_data.get('nxdomain') and tmp_data.get('bogus'):
                status = DaneStatus.none_bogus
                score = score_none_bogus
                continue_testing = False
        elif cb_data.get('bogus'):
            status = DaneStatus.failed
            score = score_failed
            continue_testing = False

    if not continue_testing:
        return dict(
            dane_score=score,
            dane_status=status,
            dane_log=stdout,
            dane_records=records,
            dane_rollover=rollover,
        )

    # Record TLSA data and also check for DANE rollover types.
    # Accepted pairs are:
    # * 3 1 x - 3 1 x
    # * 3 1 x - 2 1 x
    two_one_x = 0
    three_one_x = 0
    for cert_usage, selector, match, data in cb_data["data"]:
        records.append("{} {} {} {}".format(cert_usage, selector, match, data))
        if selector == 1:
            if cert_usage == 2:
                two_one_x += 1
            elif cert_usage == 3:
                three_one_x += 1
    if three_one_x > 1 or (three_one_x and two_one_x):
        rollover = True

    # Remove the trailing dot if any.
    hostname = url.rstrip(".")

    chain_pem = []
    for cert in chain:
        chain_pem.append(cert.as_pem())
    chain_txt = "\n".join(chain_pem)
    res = None
    with subprocess.Popen(
            [
                settings.LDNS_DANE,
                '-c', '/dev/stdin',  # Read certificate chain from stdin
                '-n',  # Do not validate hostname
                '-T',  # Exit status 2 for PKIX without (secure) TLSA records
                '-f', settings.CA_CERTIFICATES,  # CA file
                'verify', hostname, str(port),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE, universal_newlines=True) as proc:

        try:
            res = proc.communicate(input=chain_txt, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            res = proc.communicate()

    # status 0: DANE validate
    # status 1: ERROR
    # status 2: PKIX ok, no TLSA
    if res:
        stdout, stderr = res

        if ("No usable TLSA records" in stdout
                or "No usable TLSA records" in stderr):
            score = score_failed
            status = DaneStatus.failed
        elif ("No TLSA records" not in stdout
                and "No TLSA records" not in stderr):
            if proc.returncode == 0:
                score = score_validated
                status = DaneStatus.validated
            elif proc.returncode == 1:
                score = score_failed
                status = DaneStatus.failed

        # Log stderr if stdout is empty.
        if not stdout:
            stdout = stderr

    return dict(
        dane_score=score,
        dane_status=status,
        dane_log=stdout,
        dane_records=records,
        dane_rollover=rollover,
    )


def is_root_cert(cert):
    """
    Check if the certificate is a root certificate.

    """
    digest = cert.fingerprint(hashes.SHA1())
    digest = hexlify(digest).decode('ascii')
    return digest.upper() in root_fingerprints


def get_common_name(cert):
    """
    Get the commonName of the certificate.

    """
    value = "-"
    try:
        common_name = (
            cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0])
        if common_name:
            value = common_name.value
    except (IndexError, ValueError):
        pass
    return value


class DebugCertChain(object):
    """
    Class performing X509 cert checks NCSC Guidelines B3-*

    """
    def __new__(cls, chain):
        """
        In case the chain is None (ValueError from nassl) don't create an
        instance. Instead return None and it will be handled during the
        certificate checks.

        """
        if chain is None:
            return None
        return super(DebugCertChain, cls).__new__(cls)

    def __init__(self, chain):
        self.unparsed_chain = chain
        self.chain = [
            load_pem_x509_certificate(
                cert.as_pem().encode("ascii"), backend=default_backend())
            for cert in chain
        ]
        self.score_hostmatch_good = scoring.WEB_TLS_HOSTMATCH_GOOD
        self.score_hostmatch_bad = scoring.WEB_TLS_HOSTMATCH_BAD
        self.score_pubkey_good = scoring.WEB_TLS_PUBKEY_GOOD
        self.score_pubkey_bad = scoring.WEB_TLS_PUBKEY_BAD
        self.score_signature_good = scoring.WEB_TLS_SIGNATURE_GOOD
        self.score_signature_bad = scoring.WEB_TLS_SIGNATURE_BAD
        self.score_dane_none = scoring.WEB_TLS_DANE_NONE
        self.score_dane_none_bogus = scoring.WEB_TLS_DANE_NONE_BOGUS
        self.score_dane_failed = scoring.WEB_TLS_DANE_FAILED
        self.score_dane_validated = scoring.WEB_TLS_DANE_VALIDATED

    def check_hostname(self, url):
        """
        Check the hostname on the leaf certificate (commonName and SANs).

        """
        bad_hostmatch = []
        common_name = get_common_name(self.chain[0])
        try:
            sans = self.chain[0].extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            sans = sans.value.get_values_for_type(DNSName)
        except ExtensionNotFound:
            sans = []

        ssl_cert_parts = {
            'subject': (tuple([('commonName', common_name)]),),
            'subjectAltName': tuple([('DNS', name) for name in sans]),
        }
        try:
            ssl.match_hostname(ssl_cert_parts, url.rstrip("."))
            hostmatch_score = self.score_hostmatch_good
        except ssl.CertificateError:
            hostmatch_score = self.score_hostmatch_bad
            bad_hostmatch.append(common_name)
            bad_hostmatch.append(sans)
        return hostmatch_score, bad_hostmatch

    # NCSC guidelines B3-3, B3-4, B3-5
    def check_pubkey(self):
        bad_pubkey = []
        pubkey_score = self.score_pubkey_good
        for cert in self.chain:
            common_name = get_common_name(cert)
            public_key = cert.public_key()
            public_key_type = type(public_key)
            bits = public_key.key_size

            failed_key_type = ""
            curve = ""
            if public_key_type is _RSAPublicKey and bits < 2048:
                failed_key_type = "RSAPublicKey"
            elif public_key_type is _DSAPublicKey and bits < 2048:
                failed_key_type = "DSAPublicKey"
            elif public_key_type is _DHPublicKey and bits < 2048:
                failed_key_type = "DHPublicKey"
            elif public_key_type is _EllipticCurvePublicKey:
                curve = public_key.curve.name
                if (curve not in [
                        "brainpoolP512r1",
                        "brainpoolP384r1",
                        "brainpoolP256r1",
                        "secp521r1",
                        "secp384r1",
                        "secp256r1",
                        "prime256v1",
                        "secp224r1",
                        ] or bits < 224):
                    failed_key_type = "EllipticCurvePublicKey"
            if failed_key_type:
                message = "{}: {}-{} bits".format(
                    common_name, failed_key_type, bits)
                if curve:
                    message += ", curve: {}".format(curve)
                bad_pubkey.append(message)
                pubkey_score = self.score_pubkey_bad
        return pubkey_score, bad_pubkey

    def check_sigalg(self):
        """
        Check whether the certificate is signed using a good algorithm.

        Good algorithms are: sha512, sha384, sha256
        NCSC guideline B3-2

        """
        bad_sigalg = {}
        sigalg_score = self.score_signature_good
        for cert in self.chain:
            # Only validate signarture of non-root certificates
            if not is_root_cert(cert):
                sigalg = cert.signature_algorithm_oid
                # Check oids
                if sigalg not in (
                        SignatureAlgorithmOID.RSA_WITH_SHA256,
                        SignatureAlgorithmOID.RSA_WITH_SHA384,
                        SignatureAlgorithmOID.RSA_WITH_SHA512,
                        SignatureAlgorithmOID.ECDSA_WITH_SHA256,
                        SignatureAlgorithmOID.ECDSA_WITH_SHA384,
                        SignatureAlgorithmOID.ECDSA_WITH_SHA512,
                        SignatureAlgorithmOID.DSA_WITH_SHA256):
                    bad_sigalg[get_common_name(cert)] = sigalg._name
                    sigalg_score = self.score_signature_bad
        return sigalg_score, bad_sigalg

    def chain_str(self):
        """
        Return a list containing the commonNames of the certificate chain.

        """
        chain_str = []
        for cert in self.chain:
            chain_str.append(get_common_name(cert))

        return chain_str

    def check_dane(self, url, port, task, dane_cb_data=None):
        return dane(
            url, port, self.unparsed_chain, task, dane_cb_data,
            self.score_dane_none, self.score_dane_none_bogus,
            self.score_dane_failed, self.score_dane_validated)


class DebugCertChainMail(DebugCertChain):
    """
    Subclass of DebugCertChain to define the scores used for the mailtest.

    """
    def __init__(self, chain):
        super(DebugCertChainMail, self).__init__(chain)
        self.score_hostmatch_good = scoring.MAIL_TLS_HOSTMATCH_GOOD
        self.score_hostmatch_bad = scoring.MAIL_TLS_HOSTMATCH_BAD
        self.score_pubkey_good = scoring.MAIL_TLS_PUBKEY_GOOD
        self.score_pubkey_bad = scoring.MAIL_TLS_PUBKEY_BAD
        self.score_signature_good = scoring.MAIL_TLS_SIGNATURE_GOOD
        self.score_signature_bad = scoring.MAIL_TLS_SIGNATURE_BAD
        self.score_dane_none = scoring.MAIL_TLS_DANE_NONE
        self.score_dane_none_bogus = scoring.MAIL_TLS_DANE_NONE_BOGUS
        self.score_dane_failed = scoring.MAIL_TLS_DANE_FAILED
        self.score_dane_validated = scoring.MAIL_TLS_DANE_VALIDATED


class DebugConnectionHandshakeException(Exception):
    pass


class DebugConnectionSocketException(Exception):
    pass


class DebugSMTPConnectionCouldNotTestException(Exception):
    """
    Used on the SMTP STARTTLS test.

    Used when we have time outs on establishing a connection or when a mail
    server replies with an error upon connecting.

    """
    pass


class DebugConnection(LegacySslClient):
    def __init__(
            self, url, addr=None, version=SSLV23, shutdown=True,
            ciphers='ALL:COMPLEMENTOFALL', options=None, send_SNI=True):
        self.url = url
        self.version = version
        self.must_shutdown = shutdown
        self.ciphers = ciphers
        self.addr = addr
        self.options = options
        self.send_SNI = send_SNI

        self.score_compression_good = scoring.WEB_TLS_COMPRESSION_GOOD
        self.score_compression_bad = scoring.WEB_TLS_COMPRESSION_BAD
        self.score_secure_reneg_good = scoring.WEB_TLS_SECURE_RENEG_GOOD
        self.score_secure_reneg_bad = scoring.WEB_TLS_SECURE_RENEG_BAD
        self.score_client_reneg_good = scoring.WEB_TLS_CLIENT_RENEG_GOOD
        self.score_client_reneg_bad = scoring.WEB_TLS_CLIENT_RENEG_BAD
        self.score_trusted_good = scoring.WEB_TLS_TRUSTED_GOOD
        self.score_trusted_bad = scoring.WEB_TLS_TRUSTED_BAD

        self.sock_setup()
        self.connect()

    def sock_setup(self):
        self.port = 443
        self.sock_timeout = 10

        tries_left = MAX_TRIES
        while tries_left > 0:
            try:
                if self.addr:
                    self.sock = socket.socket(self.addr[0], socket.SOCK_STREAM)
                    self.sock.settimeout(self.sock_timeout)
                    self.sock.connect((self.addr[1], self.port))
                else:
                    self.sock = socket.create_connection(
                        (self.url, 443), timeout=self.sock_timeout)
                break
            except (socket.gaierror, socket.error, IOError):
                self.safe_shutdown(tls=False)
                tries_left -= 1
                if tries_left <= 0:
                    raise DebugConnectionSocketException()
                time.sleep(1)

    def connect(self):
        try:
            # TODO force ipv4 or ipv6
            super(DebugConnection, self).__init__(
                ssl_version=self.version,
                underlying_socket=self.sock,
                ssl_verify=SSL_VERIFY_NONE,
                ssl_verify_locations=settings.CA_CERTIFICATES,
                ignore_client_authentication_requests=True)
            if self.options:
                self.set_options(self.options)

            # TODO broken SNI-fallback
            if self.send_SNI and self.version != SSLV2:
                # If the url is a DNS label (mailtest) make sure to remove the
                # trailing dot.
                server_name = self.url.rstrip(".")
                self.set_tlsext_host_name(server_name)

            self.set_cipher_list(self.ciphers)
            self.do_handshake()

        except (socket.gaierror, socket.error, IOError, _nassl.OpenSSLError,
                ClientCertificateRequested):
            # Not able to connect to port 443
            raise DebugConnectionHandshakeException()
        finally:
            if self.must_shutdown:
                self.safe_shutdown()

    def get_peer_certificate_chain(self):
        """
        Wrap nassl's method in order to catch ValueError when there is an error
        getting the peer's certificate chain.

        """
        chain = None
        try:
            chain = self.get_peer_cert_chain()
        except ValueError:
            pass
        return chain

    def safe_shutdown(self, tls=True):
        """
        Shutdown TLS and socket. Ignore any exceptions.

        """
        try:
            if tls:
                self.shutdown()
            self.sock.shutdown(2)
        except (IOError, _nassl.OpenSSLError):
            pass
        finally:
            self.sock.close()

    def trusted_score(self):
        """
        Verify the certificate chain,

        """
        verify_result, _ = self.get_certificate_chain_verify_result()
        if verify_result != 0:
            return verify_result, self.score_trusted_bad
        else:
            return verify_result, self.score_trusted_good

    def check_compression(self):
        """
        Check if TLS compression is enabled.

        TLS compression should not be enabled.

        """
        compression = self.get_current_compression_method() is not None
        if compression:
            compression_score = self.score_compression_bad
        else:
            compression_score = self.score_compression_good
        return compression_score, compression

    def check_secure_reneg(self):
        """
        Check if secure renegotiation is supported.

        Secure renegotiation should be supported.

        """
        secure_reneg = self.get_secure_renegotiation_support()
        if secure_reneg:
            secure_reneg_score = self.score_secure_reneg_good
        else:
            secure_reneg_score = self.score_secure_reneg_bad
        return secure_reneg_score, secure_reneg

    def check_client_reneg(self):
        """
        Check if client renegotiation is possible.

        Client renegotiation should not be possible.

        """
        try:
            # Step 1.
            # Send reneg on open connection
            self.do_renegotiate()
            # Step 2.
            # Connection should now be closed, send 2nd reneg to verify
            self.do_renegotiate()
            # If we are still here, client reneg is supported
            client_reneg_score = self.score_client_reneg_bad
            client_reneg = True
        except (socket.error, _nassl.OpenSSLError, IOError):
            client_reneg_score = self.score_client_reneg_good
            client_reneg = False
        return client_reneg_score, client_reneg


class DebugSMTPConnection(DebugConnection):
    def __init__(
            self, url, addr=None, version=SSLV23, shutdown=True,
            ciphers='ALL:COMPLEMENTOFALL', options=None, send_SNI=True):
        super(DebugSMTPConnection, self).__init__(
            url, addr, version, shutdown, ciphers, options, send_SNI)
        self.score_compression_good = scoring.MAIL_TLS_COMPRESSION_GOOD
        self.score_compression_bad = scoring.MAIL_TLS_COMPRESSION_BAD
        self.score_secure_reneg_good = scoring.MAIL_TLS_SECURE_RENEG_GOOD
        self.score_secure_reneg_bad = scoring.MAIL_TLS_SECURE_RENEG_BAD
        self.score_client_reneg_good = scoring.MAIL_TLS_CLIENT_RENEG_GOOD
        self.score_client_reneg_bad = scoring.MAIL_TLS_CLIENT_RENEG_BAD
        self.score_trusted_good = scoring.MAIL_TLS_TRUSTED_GOOD
        self.score_trusted_bad = scoring.MAIL_TLS_TRUSTED_BAD

    @staticmethod
    def readline(fd, maximum_bytes=4096):
        line = fd.readline(maximum_bytes)
        # print(line)
        return line.decode("ascii")

    def sock_setup(self):
        """
        Setup socket for SMTP STARTTLS.

        Retries to connect when we get an error code upon connecting.

        Raises DebugSMTPConnectionCouldNotTestException when we get no reply
        from the server or when the server still replies with an error code
        upon connecting after a number of retries.

        """
        self.port = 25
        self.sock_timeout = 24

        # If we get an error code(4xx, 5xx) in the first reply upon
        # connecting, we will retry in case it was a one time error.
        tries_left = MAX_TRIES
        self.sock = None
        retry = True
        while retry and tries_left > 0:
            retry = False
            try:
                self.sock = socket.create_connection(
                    (self.url, self.port), timeout=self.sock_timeout)
                fd = self.sock.makefile("rb")
                line = self.readline(fd)

                if (line and line[3] == " " and
                        (line[0] == '4' or line[0] == '5')):
                    # The server replied with an error code.
                    # We will retry to connect in case it was an one time
                    # error.
                    self.safe_shutdown(tls=False)
                    tries_left -= 1
                    retry = True
                    if tries_left <= 0:
                        raise DebugSMTPConnectionCouldNotTestException()
                    time.sleep(1)
                    continue

                while line and line[3] != " ":
                    line = self.readline(fd)

                self.sock.sendall(b"EHLO internet.nl\r\n")

                starttls = False
                line = self.readline(fd)

                while line and line[3] != " ":
                    if "STARTTLS" in line:
                        starttls = True
                    line = self.readline(fd)

                if starttls or "STARTTLS" in line:
                    self.sock.sendall(b"STARTTLS\r\n")
                    self.readline(fd)
                    fd.close()
                else:
                    fd.close()
                    raise DebugConnectionHandshakeException()

            except (socket.error, socket.timeout, socket.gaierror):
                # We didn't get a reply back, this means our packets
                # are dropped. This happened in cases where a rate
                # limiting mechanism was in place. Skip the test.
                if self.sock:
                    self.safe_shutdown(tls=False)
                raise DebugSMTPConnectionCouldNotTestException()
            except IOError as e:
                # We can't reach the server.
                if self.sock:
                    self.safe_shutdown(tls=False)
                if e.errno in [errno.ENETUNREACH, errno.EHOSTUNREACH,
                               errno.ECONNREFUSED, errno.ENOEXEC]:
                    raise DebugSMTPConnectionCouldNotTestException()
                raise e


class StarttlsDetails:
    """
    Class used to store starttls details for the mail test.

    """
    def __init__(
            self, debug_chain=None, trusted_score=None, conn_port=None,
            dane_cb_data=None):
        self.debug_chain = debug_chain
        self.trusted_score = trusted_score
        self.conn_port = conn_port
        self.dane_cb_data = dane_cb_data


def do_web_cert(addrs, url, task, *args, **kwargs):
    """
    Check the web server's certificate.

    """
    try:
        results = {}
        for addr in addrs:
            results[addr[1]] = cert_checks(
                url, DebugConnection, task, addr, *args, **kwargs)
    except SoftTimeLimitExceeded:
        for addr in addrs:
            if not results.get(addr[1]):
                results[addr[1]] = dict(tls_cert=False)

    return ('cert', results)


def cert_checks(
        url, conn_handler, task, addr=None, starttls_details=None,
        *args, **kwargs):
    """
    Perform certificate checks.

    """
    try:
        if conn_handler is DebugConnection:
            # First try to connect to HTTPS. We don't care for
            # certificates in port 443 if there is no HTTPS there.
            shared.http_fetch(
                url, af=addr[0], path="", port=443, addr=addr[1],
                depth=MAX_REDIRECT_DEPTH, task=web_cert)
            debug_cert_chain = DebugCertChain
        else:
            debug_cert_chain = DebugCertChainMail

        if (not starttls_details or starttls_details.debug_chain is None
                or starttls_details.trusted_score is None
                or starttls_details.conn_port is None):
            # All the checks inside the smtp_starttls test are done in series.
            # If we have all the certificate related information we need from a
            # previous check, skip this connection.
            conn = conn_handler(
                url, addr=addr, version=SSLV23, shutdown=False,
                ciphers="!aNULL:ALL:COMPLEMENTOFALL")
            # check chain validity (sort of NCSC guideline B3-6)
            verify_result, verify_score = conn.trusted_score()
            debug_chain = debug_cert_chain(conn.get_peer_certificate_chain())
            conn_port = conn.port
            conn.safe_shutdown()
        else:
            verify_result, verify_score = starttls_details.trusted_score
            debug_chain = starttls_details.debug_chain
            conn_port = starttls_details.conn_port
    except (socket.error, http.client.BadStatusLine, NoIpError,
            DebugConnectionHandshakeException, DebugConnectionSocketException):
        return dict(tls_cert=False)

    if debug_chain is None:
        return dict(tls_cert=False)

    else:
        hostmatch_score, hostmatch_bad = debug_chain.check_hostname(url)
        pubkey_score, pubkey_bad = debug_chain.check_pubkey()
        sigalg_score, sigalg_bad = debug_chain.check_sigalg()
        chain_str = debug_chain.chain_str()

        if starttls_details:
            dane_results = debug_chain.check_dane(
                url, conn_port, task,
                dane_cb_data=starttls_details.dane_cb_data)
        else:
            dane_results = debug_chain.check_dane(
                url, conn_port, task)

        results = dict(
            tls_cert=True,
            chain=chain_str,
            trusted=verify_result,
            trusted_score=verify_score,
            pubkey_bad=pubkey_bad,
            pubkey_score=pubkey_score,
            sigalg_bad=sigalg_bad,
            sigalg_score=sigalg_score,
            hostmatch_bad=hostmatch_bad,
            hostmatch_score=hostmatch_score,
        )
        results.update(dane_results)

        return results


def do_web_conn(addrs, url, *args, **kwargs):
    """
    Start all the TLS related checks for the web test.

    """
    try:
        results = {}
        for addr in addrs:
            results[addr[1]] = check_web_tls(
                url, DebugConnection, addr, args, kwargs)
    except SoftTimeLimitExceeded:
        for addr in addrs:
            if not results.get(addr[1]):
                results[addr[1]] = dict(tls_enabled=False)

    return ('tls_conn', results)


def do_mail_smtp_starttls(mailservers, url, task, *args, **kwargs):
    """
    Start all the TLS related checks for the mail test.

    If we already have cached results for these mailservers from another mail
    test use those to avoid contacting well known mailservers all the time.

    """
    results = {server: False for server, _ in mailservers}
    try:
        start = timer()
        # Sleep in order for the ipv6 mail test to finish.
        # Cheap counteraction for some mailservers that allow only one
        # concurrent connection per IP.
        time.sleep(5)
        cache_ttl = redis_id.mail_starttls.ttl
        while timer() - start < cache_ttl and not all(results.values()) > 0:
            for server, dane_cb_data in mailservers:
                if results[server]:
                    continue
                # Check if we already have cached results.
                cache_id = redis_id.mail_starttls.id.format(server)
                if cache.add(cache_id, False, cache_ttl):
                    # We do not have cached results, get them and cache them.
                    results[server] = check_mail_tls(
                        server, dane_cb_data, task)
                    cache.set(cache_id, results[server], cache_ttl)
                else:
                    results[server] = cache.get(cache_id, False)
            time.sleep(1)
        for server in results:
            if results[server] is False:
                results[server] = dict(
                    tls_enabled=False, could_not_test_smtp_starttls=True)

    except SoftTimeLimitExceeded:
        for server in results:
            if results[server] is False:
                results[server] = dict(
                    tls_enabled=False, could_not_test_smtp_starttls=True)
    return ('smtp_starttls', results)


def check_mail_tls(server, dane_cb_data, task):
    """
    Perform all the TLS related checks for this mail server in series.

    """
    try:
        starttls_details = StarttlsDetails()
        starttls_details.dane_cb_data = dane_cb_data
        send_SNI = (
            dane_cb_data.get('data')
            and dane_cb_data.get('secure'))

        try:
            # First try to connect with secure ciphers.
            conn = DebugSMTPConnection(
                server, shutdown=False, send_SNI=send_SNI,
                ciphers='ALL:COMPLEMENTOFALL:'
                        '!EXP:!aNULL:!PSK:!SRP:!IDEA:!DES:!eNULL:!RC4:'
                        '!MD5')

            secure_reneg_score, secure_reneg = conn.check_secure_reneg()
            client_reneg_score, client_reneg = conn.check_client_reneg()
            compression_score, compression = conn.check_compression()

            starttls_details.trusted_score = conn.trusted_score()
            starttls_details.debug_chain = DebugCertChainMail(
                conn.get_peer_certificate_chain())
            starttls_details.conn_port = conn.port

            conn.safe_shutdown()
            connected_with_secure_ciphers = True
        except DebugConnectionSocketException:
            return dict(server_reachable=False)
        except DebugConnectionHandshakeException:
            # If we cannot connect on the TLS layer try again including
            # weak ciphers.
            connected_with_secure_ciphers = False

        ciphers_score = scoring.MAIL_TLS_SUITES_OK
        ciphers_bad = []
        ncsc_bad_ciphers = 'EXP:aNULL:PSK:SRP:IDEA:DES:eNULL:RC4:MD5'
        try:
            conn = DebugSMTPConnection(
                server, ciphers=ncsc_bad_ciphers, shutdown=False,
                send_SNI=send_SNI)
        except DebugConnectionSocketException:
            return dict(server_reachable=False)
        except DebugConnectionHandshakeException:
            # If we still cannot connect on the TLS layer, too bad.
            if not connected_with_secure_ciphers:
                return dict(tls_enabled=False)
        else:
            ciphers_score = scoring.MAIL_TLS_SUITES_BAD
            curr_cipher = conn.get_current_cipher_name()
            ciphers_bad.append(curr_cipher)

            if not connected_with_secure_ciphers:
                secure_reneg_score, secure_reneg = conn.check_secure_reneg()
                client_reneg_score, client_reneg = conn.check_client_reneg()
                compression_score, compression = conn.check_compression()

                starttls_details.trusted_score = conn.trusted_score()
                starttls_details.debug_chain = DebugCertChainMail(
                    conn.get_peer_certificate_chain())
                starttls_details.conn_port = conn.port

            conn.safe_shutdown()

        # Number of connections: 2

        # SSLv2 and SSLv3 should not be supported
        prots_bad = []
        prots_score = scoring.MAIL_TLS_PROTOCOLS_GOOD
        try:
            DebugSMTPConnection(
                server, version=SSLV2, shutdown=True, send_SNI=send_SNI)
            prots_bad.append('SSLv2')
            prots_score = scoring.MAIL_TLS_PROTOCOLS_BAD
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass
        try:
            DebugSMTPConnection(
                server, version=SSLV3, shutdown=True, send_SNI=send_SNI)
            prots_bad.append('SSLv3')
            prots_score = scoring.MAIL_TLS_PROTOCOLS_BAD
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass

        # Number of connections: 4

        # Connect using DH(E) and ECDH(E) to get FS params
        dh_param, ecdh_param = False, False
        try:
            conn = DebugSMTPConnection(
                server, ciphers="DH:DHE:!aNULL",
                shutdown=False, send_SNI=send_SNI)
            dh_param = conn._openssl_str_to_dic(conn._ssl.get_dh_param())
            dh_param = dh_param["DH_Parameters"].strip("( bit)")
            conn.safe_shutdown()
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass
        try:
            conn = DebugSMTPConnection(
                server, ciphers="ECDH:ECDHE:!aNULL",
                shutdown=False, send_SNI=send_SNI)
            ecdh_param = conn._openssl_str_to_dic(conn._ssl.get_ecdh_param())
            ecdh_param = ecdh_param["ECDSA_Parameters"].strip("( bit)")
            conn.safe_shutdown()
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass

        fs_bad = []
        if dh_param and int(dh_param) < 2048:
            fs_bad.append("DH-{}".format(dh_param))
        if ecdh_param and int(ecdh_param) < 224:
            fs_bad.append("ECDH-{}".format(ecdh_param))

        if len(fs_bad) == 0:
            fs_score = scoring.MAIL_TLS_FS_OK
        else:
            fs_score = scoring.MAIL_TLS_FS_BAD

        # Number of connections: 6

        # Check the certificates.
        cert_results = cert_checks(
            server, DebugSMTPConnection, task,
            starttls_details=starttls_details)

        # Number of connections: {6, 7}

        # HACK for DANE-TA(2) and hostname mismatch!
        # Give a good hosmatch score if DANE-TA *is not* present.
        if (not has_daneTA(cert_results['dane_records'])
                and cert_results['hostmatch_bad']):
            cert_results['hostmatch_score'] = scoring.MAIL_TLS_HOSTMATCH_GOOD

        results = dict(
            tls_enabled=True,
            prots_bad=prots_bad,
            prots_score=prots_score,

            ciphers_bad=ciphers_bad,
            ciphers_score=ciphers_score,

            compression=compression,
            compression_score=compression_score,
            secure_reneg=secure_reneg,
            secure_reneg_score=secure_reneg_score,
            client_reneg=client_reneg,
            client_reneg_score=client_reneg_score,

            dh_param=dh_param,
            ecdh_param=ecdh_param,
            fs_bad=fs_bad,
            fs_score=fs_score,
        )
        results.update(cert_results)
    except DebugSMTPConnectionCouldNotTestException:
        # If we could not test something, fail the starttls test.
        # We do not show partial results.
        return dict(
            tls_enabled=False,
            could_not_test_smtp_starttls=True,
        )

    return results


def has_daneTA(tlsa_records):
    """
    Check if any of the TLSA records is of type DANE-TA(2).

    """
    for tlsa in tlsa_records:
        if tlsa.startswith("2"):
            return True
    return False


def check_web_tls(url, conn_handler, addr=None, *args, **kwargs):
    """
    Check the webserver's TLS configuration.

    """
    try:
        # Make sure port 443 serves web content and then open the connection
        # for testing.
        shared.http_fetch(
            url, af=addr[0], path="", port=443, addr=addr[1],
            depth=MAX_REDIRECT_DEPTH, task=web_conn)
        conn = conn_handler(url, version=SSLV23, addr=addr, shutdown=False)
    except (socket.error, http.client.BadStatusLine, NoIpError,
            DebugConnectionHandshakeException,
            DebugConnectionSocketException):
        return dict(tls_enabled=False)
    else:
        secure_reneg_score, secure_reneg = conn.check_secure_reneg()
        client_reneg_score, client_reneg = conn.check_client_reneg()
        compression_score, compression = conn.check_compression()

        prots_bad = []
        prots_score = scoring.WEB_TLS_PROTOCOLS_GOOD

        ciphers_bad = []
        ciphers_score = scoring.WEB_TLS_SUITES_OK

        dh_param, ecdh_param = (False, False)
        fs_bad = []
        fs_score = scoring.WEB_TLS_FS_BAD

        conn.safe_shutdown()

        # SSLv2 and SSLv3 should not be supported
        try:
            conn_handler(url, addr=addr, version=SSLV2)
            prots_bad.append('SSLv2')
            prots_score = scoring.WEB_TLS_PROTOCOLS_BAD
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass
        try:
            conn_handler(url, addr=addr, version=SSLV3)
            prots_bad.append('SSLv3')
            prots_score = scoring.WEB_TLS_PROTOCOLS_BAD
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass

        # Connect using bad cipher
        ncsc_bad_ciphers = 'EXP:aNULL:PSK:SRP:IDEA:DES:eNULL:RC4:MD5'
        while True:
            try:
                conn = conn_handler(
                    url, addr=addr, ciphers=ncsc_bad_ciphers, shutdown=False)
            except (DebugConnectionHandshakeException,
                    DebugConnectionSocketException):
                break
            else:
                ciphers_score = scoring.WEB_TLS_SUITES_BAD
                curr_cipher = conn.get_current_cipher_name()
                ncsc_bad_ciphers = "!{}:{}".format(
                    curr_cipher, ncsc_bad_ciphers)
                ciphers_bad.append(curr_cipher)
                conn.safe_shutdown()

        # Connect using DH(E) and ECDH(E) to get FS params
        try:
            conn = conn_handler(
                url, addr=addr, ciphers="DH:DHE:!aNULL", shutdown=False)
            dh_param = conn._openssl_str_to_dic(conn._ssl.get_dh_param())
            dh_param = dh_param["DH_Parameters"].strip("( bit)")
            conn.safe_shutdown()
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass
        try:
            conn = conn_handler(
                url, addr=addr, ciphers="ECDH:ECDHE:!aNULL", shutdown=False)
            ecdh_param = conn._openssl_str_to_dic(conn._ssl.get_ecdh_param())
            ecdh_param = ecdh_param["ECDSA_Parameters"].strip("( bit)")
            conn.safe_shutdown()
        except (DebugConnectionHandshakeException,
                DebugConnectionSocketException):
            pass

        if dh_param and int(dh_param) < 2048:
            fs_bad.append("DH-{}".format(dh_param))
        if ecdh_param and int(ecdh_param) < 224:
            fs_bad.append("ECDH-{}".format(ecdh_param))

        if len(fs_bad) == 0:
            fs_score = scoring.WEB_TLS_FS_OK

        return dict(
            tls_enabled=True,
            prots_bad=prots_bad,
            prots_score=prots_score,

            ciphers_bad=ciphers_bad,
            ciphers_score=ciphers_score,

            compression=compression,
            compression_score=compression_score,
            secure_reneg=secure_reneg,
            secure_reneg_score=secure_reneg_score,
            client_reneg=client_reneg,
            client_reneg_score=client_reneg_score,

            dh_param=dh_param,
            ecdh_param=ecdh_param,
            fs_bad=fs_bad,
            fs_score=fs_score,
        )


def do_web_http(addrs, url, task, *args, **kwargs):
    """
    Start all the HTTP related checks for the web test.

    """
    try:
        results = {}
        for addr in addrs:
            results[addr[1]] = http_checks(addr, url, task)

    except SoftTimeLimitExceeded:
        for addr in addrs:
            if not results.get(addr[1]):
                results[addr[1]] = dict(
                    forced_https=False,
                    forced_https_score=scoring.WEB_TLS_FORCED_HTTPS_BAD,
                    http_compression_enabled=True,
                    http_compression_score=(
                        scoring.WEB_TLS_HTTP_COMPRESSION_BAD),
                    hsts_enabled=False,
                    hsts_policies=[],
                    hsts_score=scoring.WEB_TLS_HSTS_BAD,
                )

    return ('http_checks', results)


def http_checks(addr, url, task):
    """
    Perform the HTTP header and HTTPS redirection checks for this webserver.

    """
    forced_https, forced_https_score = forced_http_check(addr, url, task)
    header_checkers = [
        HeaderCheckerContentEncoding(),
        HeaderCheckerStrictTransportSecurity(),
    ]
    header_results = http_headers_check(addr, url, header_checkers, task)

    results = {
        'forced_https': forced_https,
        'forced_https_score': forced_https_score,
    }
    results.update(header_results)
    return results


def forced_http_check(addr, url, task):
    """
    Check if the webserver is properly configured with HTTPS redirection.

    """
    # First connect on port 80 and see if we get refused
    try:
        conn, res, headers, visited_hosts = shared.http_fetch(
            url, af=addr[0], path="", port=80, task=task, addr=addr[1])

    except (socket.error, http.client.BadStatusLine, NoIpError):
        # If we got refused on port 80 the first time
        # return the FORCED_HTTPS_NO_HTTP status and score
        return ForcedHttpsStatus.no_http, scoring.WEB_TLS_FORCED_HTTPS_NO_HTTP

    # Valid if same domain, or *higher* domain. Use case:
    # www.example.com:80 -> example.com:443. Example.com:443 can set HSTS
    # with includeSubdomains
    forced_https = ForcedHttpsStatus.bad
    forced_https_score = scoring.WEB_TLS_FORCED_HTTPS_BAD

    if 443 in visited_hosts and conn.port == 443:
        for visited_host in visited_hosts[443]:
            if visited_host in url:
                forced_https = ForcedHttpsStatus.good
                forced_https_score = scoring.WEB_TLS_FORCED_HTTPS_GOOD
                break

    return forced_https, forced_https_score
