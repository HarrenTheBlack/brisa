import asyncio
import base64
import hashlib
import ipaddress
import threading

from argon2 import PasswordHasher
import pytest

from app import auth


PASSWORD = "correct horse battery staple"


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture(scope="module")
def accepted_hash():
    return PasswordHasher(
        memory_cost=19456,
        time_cost=2,
        parallelism=1,
        salt_len=16,
        hash_len=16,
    ).hash(PASSWORD)


def write_hash(tmp_path, value, name="password.hash"):
    path = tmp_path / name
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_bytes(value)
    return path


def enabled_environment(path, **overrides):
    environ = {
        "BRISA_AUTH_ENABLED": "true",
        "BRISA_AUTH_USERNAME": "admin",
        "BRISA_PASSWORD_HASH_FILE": str(path),
    }
    environ.update(overrides)
    return environ


def proxy_settings(*networks, trust_proxy=True):
    return auth.AuthSettings(
        enabled=True,
        username="admin",
        password_hash_file=None,
        secure_cookies=True,
        session_ttl_seconds=300,
        trust_proxy=trust_proxy,
        trusted_proxy_networks=tuple(ipaddress.ip_network(item) for item in networks),
    )


def scope(peer, forwarded=None):
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded)]
    return {"type": "http", "client": (peer, 1234), "headers": headers}


def replace_parameters(encoded_hash, parameters):
    parts = encoded_hash.split("$")
    parts[3] = parameters
    return "$".join(parts)


def replace_component(encoded_hash, index, raw):
    parts = encoded_hash.split("$")
    parts[index] = base64.b64encode(raw).rstrip(b"=").decode("ascii")
    return "$".join(parts)


def test_settings_disabled_does_not_require_credentials():
    settings = auth.parse_auth_settings(
        {
            "BRISA_AUTH_ENABLED": " false ",
            "BRISA_PASSWORD_HASH_FILE": "relative/hash",
        }
    )

    assert settings.enabled is False
    assert settings.username is None
    assert settings.password_hash_file == auth.Path("relative/hash")
    assert settings.secure_cookies is True
    assert settings.session_ttl_seconds == 28800


def test_settings_ready_parses_security_and_proxy_values(tmp_path):
    path = tmp_path / "password.hash"
    settings = auth.parse_auth_settings(
        enabled_environment(
            path,
            BRISA_SECURE_COOKIES="false",
            BRISA_SESSION_TTL_SECONDS="300",
            BRISA_TRUST_PROXY="true",
            BRISA_TRUSTED_PROXY_CIDRS="10.0.0.9/8, 2001:db8::1/32",
        )
    )

    assert settings.enabled is True
    assert settings.username == "admin"
    assert settings.password_hash_file == path
    assert settings.secure_cookies is False
    assert settings.session_ttl_seconds == 300
    assert settings.trust_proxy is True
    assert settings.trusted_proxy_networks == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("2001:db8::/32"),
    )


@pytest.mark.parametrize(
    "name",
    ["BRISA_AUTH_ENABLED", "BRISA_SECURE_COOKIES", "BRISA_TRUST_PROXY"],
)
def test_settings_reject_malformed_booleans(tmp_path, name):
    environ = enabled_environment(tmp_path / "password.hash")
    environ[name] = "yes"

    with pytest.raises(auth.AuthConfigurationError, match=f"{name} must be either"):
        auth.parse_auth_settings(environ)


@pytest.mark.parametrize("username", [None, "", "contains space", "a" * 65])
def test_enabled_settings_require_valid_username(tmp_path, username):
    environ = enabled_environment(tmp_path / "password.hash")
    if username is None:
        environ.pop("BRISA_AUTH_USERNAME")
    else:
        environ["BRISA_AUTH_USERNAME"] = username

    with pytest.raises(auth.AuthConfigurationError, match="BRISA_AUTH_USERNAME"):
        auth.parse_auth_settings(environ)


def test_enabled_settings_require_hash_file_setting(tmp_path):
    environ = enabled_environment(tmp_path / "password.hash")
    environ.pop("BRISA_PASSWORD_HASH_FILE")

    with pytest.raises(auth.AuthConfigurationError, match="HASH_FILE is required"):
        auth.parse_auth_settings(environ)


def test_enabled_settings_reject_relative_hash_path():
    with pytest.raises(auth.AuthConfigurationError, match="absolute path"):
        auth.parse_auth_settings(enabled_environment(auth.Path("relative.hash")))


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_load_password_hash_accepts_utf8_record_endings(tmp_path, accepted_hash, ending):
    path = write_hash(tmp_path, accepted_hash + ending)

    assert auth._load_password_hash(path) == accepted_hash


def test_load_password_hash_rejects_non_utf8(tmp_path):
    path = write_hash(tmp_path, b"\xff\xfe")

    with pytest.raises(auth.AuthConfigurationError, match="UTF-8"):
        auth._load_password_hash(path)


def test_load_password_hash_rejects_nul(tmp_path, accepted_hash):
    path = write_hash(tmp_path, accepted_hash.encode() + b"\x00")

    with pytest.raises(auth.AuthConfigurationError, match="NUL"):
        auth._load_password_hash(path)


def test_load_password_hash_rejects_multiple_records(tmp_path, accepted_hash):
    path = write_hash(tmp_path, f"{accepted_hash}\n{accepted_hash}\n")

    with pytest.raises(auth.AuthConfigurationError, match="exactly one hash record"):
        auth._load_password_hash(path)


def test_load_password_hash_rejects_oversize_file(tmp_path):
    path = write_hash(tmp_path, b"x" * 4097)

    with pytest.raises(auth.AuthConfigurationError, match="exceeds 4096 bytes"):
        auth._load_password_hash(path)


def test_load_password_hash_rejects_malformed_record(tmp_path):
    path = write_hash(tmp_path, "$argon2id$not-a-hash")

    with pytest.raises(auth.AuthConfigurationError, match="valid Argon2 hash"):
        auth._load_password_hash(path)


def test_load_password_hash_rejects_non_argon2id(tmp_path, accepted_hash):
    path = write_hash(tmp_path, accepted_hash.replace("$argon2id$", "$argon2i$"))

    with pytest.raises(auth.AuthConfigurationError, match="must use Argon2id"):
        auth._load_password_hash(path)


def test_load_password_hash_rejects_wrong_version(tmp_path, accepted_hash):
    path = write_hash(tmp_path, accepted_hash.replace("$v=19$", "$v=16$"))

    with pytest.raises(auth.AuthConfigurationError, match="version 19"):
        auth._load_password_hash(path)


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ("m=19455,t=2,p=1", "memory cost"),
        ("m=262145,t=2,p=1", "memory cost"),
        ("m=19456,t=1,p=1", "time cost"),
        ("m=19456,t=11,p=1", "time cost"),
        ("m=19456,t=2,p=0", "parallelism"),
        ("m=19456,t=2,p=9", "parallelism"),
    ],
)
def test_load_password_hash_rejects_weak_or_excess_parameters(
    tmp_path, accepted_hash, parameters, message
):
    path = write_hash(tmp_path, replace_parameters(accepted_hash, parameters))

    with pytest.raises(auth.AuthConfigurationError, match=message):
        auth._load_password_hash(path)


@pytest.mark.parametrize(
    ("component", "size", "message"),
    [
        (4, 15, "salt length"),
        (4, 65, "salt length"),
        (5, 15, "output length"),
        (5, 65, "output length"),
    ],
)
def test_load_password_hash_rejects_salt_and_hash_length_bounds(
    tmp_path, accepted_hash, component, size, message
):
    path = write_hash(tmp_path, replace_component(accepted_hash, component, b"a" * size))

    with pytest.raises(auth.AuthConfigurationError, match=message):
        auth._load_password_hash(path)


def test_dummy_hash_is_a_syntactically_valid_argon2id_hash_with_same_parameters(
    accepted_hash,
):
    from argon2 import extract_parameters
    from argon2.low_level import Type

    dummy = auth._dummy_hash(accepted_hash)

    real_params = extract_parameters(accepted_hash)
    dummy_params = extract_parameters(dummy)

    assert dummy_params == real_params
    assert dummy_params.type is Type.ID
    assert len(dummy) == len(accepted_hash)
    # Only the final digest component may differ; algorithm, version,
    # parameters, and salt must be copied verbatim from the real hash.
    assert dummy.rsplit("$", 1)[0] == accepted_hash.rsplit("$", 1)[0]
    assert dummy != accepted_hash


def test_dummy_hash_fails_with_full_verify_mismatch_not_early_decode_error(
    accepted_hash,
):
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError

    dummy = auth._dummy_hash(accepted_hash)
    hasher = PasswordHasher()

    # A malformed/early-bail-out dummy would raise InvalidHashError instead
    # of performing the real computation and failing on digest comparison.
    with pytest.raises(VerifyMismatchError):
        hasher.verify(dummy, PASSWORD)

    try:
        hasher.verify(dummy, PASSWORD)
    except InvalidHashError:
        pytest.fail("dummy hash must not fail via malformed-encoding early exit")
    except VerifyMismatchError:
        pass


def test_dummy_hash_verification_time_matches_real_hash_verification_time(
    accepted_hash,
):
    import time

    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    dummy = auth._dummy_hash(accepted_hash)
    hasher = PasswordHasher()

    def timed(hash_value, expect_match):
        start = time.perf_counter()
        if expect_match:
            hasher.verify(hash_value, PASSWORD)
        else:
            try:
                hasher.verify(hash_value, PASSWORD)
            except VerifyMismatchError:
                pass
        return time.perf_counter() - start

    # Warm up (import/JIT/cache effects) before measuring.
    timed(accepted_hash, True)
    timed(dummy, False)

    real_times = [timed(accepted_hash, True) for _ in range(10)]
    dummy_times = [timed(dummy, False) for _ in range(10)]

    real_avg = sum(real_times) / len(real_times)
    dummy_avg = sum(dummy_times) / len(dummy_times)

    # Both must reflect a genuine full Argon2 computation at the same cost
    # parameters. Allow generous ratio bounds to absorb CI/host jitter while
    # still catching a true early-exit (which would be orders of magnitude
    # faster, not merely somewhat faster/slower).
    assert dummy_avg > real_avg * 0.4
    assert dummy_avg < real_avg * 2.5


def test_manager_initializes_disabled():
    manager = auth.AuthManager()

    manager.initialize({"BRISA_AUTH_ENABLED": "false"})

    assert manager.state is auth.AuthState.DISABLED
    assert manager.error == ""
    assert manager.verifier is None


def test_manager_disabled_logs_prominent_untrusted_network_warning(caplog):
    manager = auth.AuthManager()

    with caplog.at_level("WARNING", logger="app.auth"):
        manager.initialize({"BRISA_AUTH_ENABLED": "false"})

    assert any(
        "Authentication is disabled" in record.message
        and "untrusted networks" in record.message
        for record in caplog.records
    )


def test_manager_initializes_ready(tmp_path, accepted_hash):
    path = write_hash(tmp_path, accepted_hash)
    manager = auth.AuthManager()
    try:
        manager.initialize(enabled_environment(path))

        assert manager.state is auth.AuthState.READY
        assert manager.error == ""
        assert manager.verifier is not None
    finally:
        manager.close()


def test_manager_ready_with_insecure_cookies_logs_lan_testing_warning(
    tmp_path, accepted_hash, caplog
):
    path = write_hash(tmp_path, accepted_hash)
    manager = auth.AuthManager()
    try:
        with caplog.at_level("WARNING", logger="app.auth"):
            manager.initialize(enabled_environment(path, BRISA_SECURE_COOKIES="false"))

        assert manager.state is auth.AuthState.READY
        assert any(
            "Secure cookies are disabled" in record.message
            for record in caplog.records
        )
        # The warning must never include the password hash itself.
        assert not any(accepted_hash in record.message for record in caplog.records)
    finally:
        manager.close()


def test_manager_ready_with_secure_cookies_does_not_log_insecure_warning(
    tmp_path, accepted_hash, caplog
):
    path = write_hash(tmp_path, accepted_hash)
    manager = auth.AuthManager()
    try:
        with caplog.at_level("WARNING", logger="app.auth"):
            manager.initialize(enabled_environment(path, BRISA_SECURE_COOKIES="true"))

        assert manager.state is auth.AuthState.READY
        assert not any(
            "Secure cookies are disabled" in record.message
            for record in caplog.records
        )
    finally:
        manager.close()


def test_manager_stays_invalid_for_bad_configuration(tmp_path):
    manager = auth.AuthManager()

    manager.initialize(enabled_environment(tmp_path / "missing.hash"))

    assert manager.state is auth.AuthState.INVALID
    assert "cannot be read" in manager.error
    assert manager.verifier is None


class RecordingVerifier:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def verify(self, password, use_dummy):
        self.calls.append((password, use_dummy))
        return self.result


def ready_manager(verifier, clock=None):
    manager = auth.AuthManager(clock=clock or FakeClock())
    manager.state = auth.AuthState.READY
    manager.settings = auth.AuthSettings(True, "admin", None, True, 300, False, ())
    manager.verifier = verifier
    return manager


def test_wrong_username_uses_dummy_hash_even_with_correct_password():
    verifier = RecordingVerifier(True)
    manager = ready_manager(verifier)

    result = asyncio.run(manager.authenticate("not-admin", PASSWORD, "client"))

    assert result is False
    assert verifier.calls == [(PASSWORD, True)]


def test_wrong_password_uses_physical_hash_and_is_rejected():
    verifier = RecordingVerifier(False)
    manager = ready_manager(verifier)

    result = asyncio.run(manager.authenticate("admin", "wrong", "client"))

    assert result is False
    assert verifier.calls == [("wrong", False)]


def test_successful_password_resets_client_failures():
    verifier = RecordingVerifier(True)
    manager = ready_manager(verifier)
    manager.rate_limiter.failure("client")

    assert asyncio.run(manager.authenticate("admin", PASSWORD, "client")) is True
    assert "client" not in manager.rate_limiter._records


def test_password_verifier_runs_off_event_loop_thread(monkeypatch, accepted_hash):
    verifier = auth.PasswordVerifier(accepted_hash)
    loop_thread = threading.get_ident()
    worker_threads = []

    def verify_sync(_encoded_hash, _password):
        worker_threads.append(threading.get_ident())
        return True

    monkeypatch.setattr(verifier, "_verify_sync", verify_sync)
    try:
        assert asyncio.run(verifier.verify(PASSWORD, use_dummy=False)) is True
    finally:
        verifier.close()

    assert worker_threads and worker_threads[0] != loop_thread


def test_password_verifier_allows_one_physical_check_and_never_queues(
    monkeypatch, accepted_hash
):
    verifier = auth.PasswordVerifier(accepted_hash)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_verify(_encoded_hash, _password):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(verifier, "_verify_sync", blocking_verify)

    async def exercise():
        first = asyncio.create_task(verifier.verify(PASSWORD, use_dummy=False))
        assert await asyncio.to_thread(started.wait, 2)
        with pytest.raises(auth.VerificationUnavailable) as exc_info:
            await verifier.verify(PASSWORD, use_dummy=False)
        assert exc_info.value.retry_after == 1
        assert len(calls) == 1
        release.set()
        assert await first is True

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        verifier.close()


def test_cancelled_waiter_releases_slot_only_when_physical_check_finishes(
    monkeypatch, accepted_hash
):
    verifier = auth.PasswordVerifier(accepted_hash)
    started = threading.Event()
    release = threading.Event()

    def blocking_verify(_encoded_hash, _password):
        started.set()
        assert release.wait(2)
        return False

    monkeypatch.setattr(verifier, "_verify_sync", blocking_verify)

    async def exercise():
        task = asyncio.create_task(verifier.verify("wrong", use_dummy=False))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert verifier._busy is True
        with pytest.raises(auth.VerificationUnavailable):
            await verifier.verify("another", use_dummy=False)

        release.set()
        for _ in range(100):
            if not verifier._busy:
                break
            await asyncio.sleep(0.01)
        assert verifier._busy is False

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        verifier.close()


def test_password_verifier_enforces_global_thirty_starts_per_minute(
    monkeypatch, accepted_hash
):
    clock = FakeClock()
    verifier = auth.PasswordVerifier(accepted_hash, clock=clock)
    monkeypatch.setattr(verifier, "_verify_sync", lambda _hash, _password: True)

    async def exercise():
        for _ in range(30):
            assert await verifier.verify(PASSWORD, use_dummy=False) is True

        with pytest.raises(auth.VerificationUnavailable) as exc_info:
            await verifier.verify(PASSWORD, use_dummy=True)
        assert exc_info.value.retry_after == 60

        clock.advance(1.25)
        with pytest.raises(auth.VerificationUnavailable) as exc_info:
            await verifier.verify(PASSWORD, use_dummy=False)
        assert exc_info.value.retry_after == 59

        clock.advance(58.75)
        assert await verifier.verify(PASSWORD, use_dummy=False) is True

    try:
        asyncio.run(exercise())
    finally:
        verifier.close()


def test_session_store_uses_sha256_keys_not_raw_tokens(monkeypatch):
    values = iter(["t" * 43, "c" * 43])
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: next(values))
    store = auth.SessionStore(ttl_seconds=300)

    token, _session = store.create("admin")

    assert token == "t" * 43
    assert list(store._sessions) == [hashlib.sha256(token.encode("ascii")).hexdigest()]
    assert token not in store._sessions


def test_session_expiry_is_non_sliding():
    clock = FakeClock(10)
    store = auth.SessionStore(ttl_seconds=300, clock=clock)
    token, session = store.create("admin")

    clock.advance(200)
    assert store.get(token) is session
    assert session.expires_at_monotonic == 310
    clock.advance(100)
    assert store.get(token) is None


def test_session_create_prunes_expired_entries():
    clock = FakeClock()
    store = auth.SessionStore(ttl_seconds=10, clock=clock)
    old_token, _ = store.create("admin")
    clock.advance(10)

    new_token, _ = store.create("admin")

    assert store.get(old_token) is None
    assert store.get(new_token) is not None
    assert len(store._sessions) == 1


def test_session_delete_removes_only_named_token():
    store = auth.SessionStore(ttl_seconds=300)
    first, _ = store.create("admin")
    second, _ = store.create("admin")

    store.delete(first)

    assert store.get(first) is None
    assert store.get(second) is not None


def test_sessions_do_not_survive_new_store():
    original = auth.SessionStore(ttl_seconds=300)
    token, _ = original.create("admin")

    restarted = auth.SessionStore(ttl_seconds=300)

    assert restarted.get(token) is None


def test_session_store_caps_at_sixteen_and_evicts_oldest():
    store = auth.SessionStore(ttl_seconds=300)
    tokens = [store.create("admin")[0] for _ in range(17)]

    assert len(store._sessions) == auth.MAX_SESSIONS == 16
    assert store.get(tokens[0]) is None
    assert all(store.get(token) is not None for token in tokens[1:])


def test_fifth_failure_within_ten_minutes_starts_fifteen_minute_cooldown():
    clock = FakeClock(100)
    limiter = auth.LoginRateLimiter(clock=clock)

    for _ in range(4):
        limiter.failure("client")
    clock.advance(599)
    with pytest.raises(auth.LoginRateLimited) as exc_info:
        limiter.failure("client")

    assert exc_info.value.retry_after == 900


def test_failures_at_ten_minute_boundary_are_trimmed():
    clock = FakeClock()
    limiter = auth.LoginRateLimiter(clock=clock)
    for _ in range(4):
        limiter.failure("client")

    clock.advance(600)
    limiter.failure("client")

    assert list(limiter._records["client"].failures) == [600]
    limiter.check("client")


def test_rate_limit_retry_after_uses_ceiling_and_cooldown_expires():
    clock = FakeClock()
    limiter = auth.LoginRateLimiter(clock=clock)
    for _ in range(4):
        limiter.failure("client")
    with pytest.raises(auth.LoginRateLimited):
        limiter.failure("client")

    clock.advance(899.2)
    with pytest.raises(auth.LoginRateLimited) as exc_info:
        limiter.check("client")
    assert exc_info.value.retry_after == 1

    clock.advance(0.8)
    limiter.check("client")
    assert "client" not in limiter._records


def test_rate_limit_success_resets_failures_and_cooldown():
    limiter = auth.LoginRateLimiter(clock=FakeClock())
    for _ in range(4):
        limiter.failure("client")

    limiter.success("client")

    assert "client" not in limiter._records
    limiter.check("client")


def test_rate_limiter_has_exact_4096_client_bound():
    limiter = auth.LoginRateLimiter(clock=FakeClock())
    for index in range(auth.MAX_RATE_LIMIT_CLIENTS):
        limiter.failure(f"client-{index}")

    assert len(limiter._records) == 4096
    with pytest.raises(auth.LoginRateLimited) as exc_info:
        limiter.failure("one-too-many")
    assert exc_info.value.retry_after == 60
    assert len(limiter._records) == 4096


def test_active_cooldown_is_not_evicted_to_admit_another_client():
    clock = FakeClock()
    limiter = auth.LoginRateLimiter(clock=clock, maximum=1)
    for _ in range(4):
        limiter.failure("blocked")
    with pytest.raises(auth.LoginRateLimited):
        limiter.failure("blocked")

    with pytest.raises(auth.LoginRateLimited) as exc_info:
        limiter.failure("new-client")

    assert exc_info.value.retry_after == 60
    assert list(limiter._records) == ["blocked"]
    with pytest.raises(auth.LoginRateLimited) as exc_info:
        limiter.check("blocked")
    assert exc_info.value.retry_after == 900


def test_client_ip_ignores_forwarding_when_proxy_trust_disabled():
    settings = proxy_settings("10.0.0.0/8", trust_proxy=False)

    assert auth.effective_client_ip(scope("10.0.0.2", b"192.0.2.4"), settings) == "10.0.0.2"


def test_client_ip_ignores_forwarding_from_untrusted_peer():
    settings = proxy_settings("10.0.0.0/8")

    assert auth.effective_client_ip(scope("198.51.100.9", b"192.0.2.4"), settings) == "198.51.100.9"


def test_client_ip_walks_forwarded_chain_right_to_left_until_untrusted():
    settings = proxy_settings("10.0.0.0/8")
    request_scope = scope(
        "10.0.0.3",
        b"192.0.2.4, 198.51.100.7, 10.0.0.8",
    )

    assert auth.effective_client_ip(request_scope, settings) == "198.51.100.7"


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-forwarded-for", b"not-an-ip")],
        [(b"x-forwarded-for", b"192.0.2.1\xff")],
        [(b"x-forwarded-for", b"192.0.2.1"), (b"X-Forwarded-For", b"192.0.2.2")],
    ],
)
def test_client_ip_falls_back_to_peer_for_malformed_forwarding(headers):
    settings = proxy_settings("10.0.0.0/8")
    request_scope = {"type": "http", "client": ("10.0.0.3", 1), "headers": headers}

    assert auth.effective_client_ip(request_scope, settings) == "10.0.0.3"


def test_client_ip_falls_back_for_oversize_forwarded_header():
    settings = proxy_settings("10.0.0.0/8")
    forwarded = b"1" * (auth.MAX_FORWARDED_HEADER_LENGTH + 1)

    assert auth.effective_client_ip(scope("10.0.0.3", forwarded), settings) == "10.0.0.3"


def test_client_ip_falls_back_for_too_many_forwarded_hops():
    settings = proxy_settings("10.0.0.0/8")
    forwarded = b", ".join([b"10.0.0.1"] * (auth.MAX_FORWARDED_HOPS + 1))

    assert auth.effective_client_ip(scope("10.0.0.3", forwarded), settings) == "10.0.0.3"


def test_client_ip_normalizes_ipv4_mapped_addresses_for_trust_and_result():
    settings = proxy_settings("10.0.0.0/8")

    assert (
        auth.effective_client_ip(
            scope("::ffff:10.0.0.3", b"::ffff:192.0.2.4"), settings
        )
        == "192.0.2.4"
    )


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-real-ip", b"192.0.2.7")],
        [(b"forwarded", b"for=192.0.2.7;proto=https")],
        [
            (b"x-real-ip", b"192.0.2.7"),
            (b"forwarded", b"for=192.0.2.8"),
        ],
    ],
)
def test_client_ip_never_trusts_non_x_forwarded_for_headers(headers):
    """Only the explicitly documented X-Forwarded-For format is accepted.
    Supporting another convention implicitly would give a proxy client a
    second, potentially unstripped way to choose a rate-limit identity."""
    settings = proxy_settings("10.0.0.0/8")
    request_scope = {"type": "http", "client": ("10.0.0.3", 1), "headers": headers}

    assert auth.effective_client_ip(request_scope, settings) == "10.0.0.3"


def test_client_ip_walks_ipv6_proxy_chain_right_to_left():
    settings = proxy_settings("2001:db8:10::/48")
    request_scope = scope(
        "2001:db8:10::3",
        b"2001:db8:ffff::99, 2001:db8:10::8",
    )

    assert auth.effective_client_ip(request_scope, settings) == "2001:db8:ffff::99"


def test_client_ip_ignores_attacker_controlled_left_hand_hops_after_client():
    settings = proxy_settings("10.0.0.0/8")
    # A client can control arbitrary values left of its own address. The
    # algorithm walks from the trusted proxy end, so the first untrusted hop
    # is the client itself, not an attacker-selected farther-left value.
    request_scope = scope(
        "10.0.0.3",
        b"203.0.113.66, 198.51.100.17, 10.0.0.8",
    )

    assert auth.effective_client_ip(request_scope, settings) == "198.51.100.17"


def test_client_ip_bounds_malformed_peer_identity():
    malformed = "not-an-ip-" * 30

    assert auth.effective_client_ip(scope(malformed), proxy_settings()) == malformed[:128]
