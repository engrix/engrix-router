# Regression TASK-46: klasifikasi error vendor.
# 1) Qoder HTTP 429 {"code":"provider_error","message":"All backends failed"} harus
#    jadi CLASS_UPSTREAM_UNAVAILABLE (transient, tidak eskalasi backoff akun)
#    -- dulu jatuh ke CLASS_RATE_LIMIT (status==429) dan ngunci akun sehat
#    64->128->300 detik padahal masalahnya di sisi vendor.
# 2) Rate limit asli (429 tanpa provider_error) tetap CLASS_RATE_LIMIT.
# 3) Katalog zcode: GLM-5.3 vision=False, GLM-5.3-Flash vision=True (dari
#    zcode-builtin.json modelRules, bukan tebakan).
# 4) Katalog qoder: supports_tools tidak lagi hardcode True -- vendor tidak
#    mengirim flag tools di wire /model/list.

from engrix_router.core import errors


def test_qoder_provider_error_429_bukan_rate_limit():
    body = ('{"body": "{\\"code\\":\\"provider_error\\",\\"message\\":\\"All backends failed\\"",'
            ' "statusCodeValue": 429}')
    result = errors.classify(status=429, text=body)
    assert result.error_class == errors.CLASS_UPSTREAM_UNAVAILABLE, (
        f"expected upstream_unavailable, got {result.error_class}")
    # transient = tidak backoff eksponensial, tidak mark connection sebagai rate limit
    assert result.policy.lock == "transient"


def test_plain_429_tetap_rate_limited():
    result = errors.classify(status=429, text="Too Many Requests")
    assert result.error_class == errors.CLASS_RATE_LIMIT
    assert result.policy.lock == "backoff"


def test_provider_error_dengan_kata_rate_tetap_rate_limit():
    # guard: kalau body mengandung "rate" DAN provider_error, rate limit menang
    # (jangan sampai regex provider_error menelan rate limit asli).
    body = '{"code":"provider_error","message":"rate limit exceeded"}'
    result = errors.classify(status=429, text=body)
    assert result.error_class == errors.CLASS_RATE_LIMIT
