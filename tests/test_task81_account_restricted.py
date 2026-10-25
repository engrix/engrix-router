"""Contract for the vendor-restricted account (codebuddy code 11140).

Live evidence this locks in (2026-10-25, probed against the real backend):

  * account 354 answers 403 {"code":11140,"msg":"request illegal",
    "displayMsg":{"en":"Your account has been restricted due to unusual activity"}}
    with BOTH the router header set and the shipped WorkBuddy 5.8.4 header set;
  * three never-tested accounts (355/356/357) answer 200 with the router header set;
  * the JWT for 354 stays valid for another year -- the token is not dead.

So 11140 means "the vendor blocked this ACCOUNT", not "the credential expired".
The old mapping (credential_dead -> needs_reauth) was wrong twice over: it told
the operator to log in again (useless) and needs_reauth has no self-heal, so the
account stayed dead in the router even after the vendor lifted the block.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engrix_router.core import errors                                    # noqa: E402

RESTRICTED_BODY = (
    '{"code":11140,"msg":"request illegal","requestId":"4a25b948790a4c259677dea3e467f1bb",'
    '"displayMsg":{"en":"Your account has been restricted due to unusual activity",'
    '"zh":"\\u8d26\\u53f7\\u5b58\\u5728\\u5f02\\u5e38\\u884c\\u4e3a\\uff0c\\u5df2\\u88ab\\u9650\\u5236\\u4f7f\\u7528"},'
    '"displayTips":{"en":"We detected unusual activity on this account. Submit feedback to appeal this restriction"},'
    '"actions":["EDIT_INPUT","SUBMIT_FEEDBACK"]}'
)

ZCODE_ANTI_ABUSE_BODY = '{"code":3012,"msg":"unusual activity detected"}'


@pytest.fixture()
def seeded_account():
    """Insert one account row into the throwaway test DB and hand back its id."""
    from engrix_router.accounts import health
    from engrix_router.storage import sqlite as db

    now = health.now_ms()
    cid = "T-RESTRICTED"
    db.execute(
        "INSERT OR REPLACE INTO connections (id, provider, auth_type, name, priority,"
        " is_active, cred_json, psd_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, "codebuddy", "oauth", cid, 9998, 0, "{}", "{}", now, now),
    )
    return cid, now


def test_vendor_block_gets_its_own_class():
    classified = errors.classify(status=403, text=RESTRICTED_BODY)
    assert classified.error_class == errors.CLASS_ACCOUNT_RESTRICTED
    assert classified.vendor_code == "11140"


def test_vendor_block_locks_the_account_for_an_hour():
    classified = errors.classify(status=403, text=RESTRICTED_BODY)
    assert classified.policy.lock == "restricted"
    assert classified.policy.retry_internally is True
    assert classified.policy.retry_after_s == 3600


def test_vendor_block_still_fails_over_to_the_next_account():
    # 11140 is about ONE account: the request must move on, not die here.
    assert errors.policy_of(errors.CLASS_ACCOUNT_RESTRICTED).retry_internally is True


def test_zcode_anti_abuse_is_not_swallowed_by_the_restriction_rule():
    # ZCode 3012 is a body-shape rejection (HTTP 405), a completely different cause.
    classified = errors.classify(status=405, text=ZCODE_ANTI_ABUSE_BODY)
    assert classified.error_class == errors.CLASS_ANTI_ABUSE


def test_a_genuinely_dead_token_is_still_credential_dead():
    classified = errors.classify(status=401, text='{"code":401,"msg":"invalid token"}')
    assert classified.error_class == errors.CLASS_CREDENTIAL_DEAD


def test_restricted_account_heals_out_of_needs_reauth(seeded_account):
    from engrix_router.accounts import health
    from engrix_router.storage import sqlite as db

    cid, now = seeded_account
    db.execute(
        "INSERT OR REPLACE INTO connection_health (connection_id, test_status, error_class,"
        " error_code, last_error, last_error_at, updated_at, rate_limited_until, backoff_level)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, health.STATUS_NEEDS_REAUTH, errors.CLASS_ACCOUNT_RESTRICTED, "11140",
         "request illegal", now, now, None, 0),
    )

    assert health.is_available(cid, "deepseek-v4.1-flash") is True
    assert health.get(cid)["test_status"] == health.STATUS_COOLING


def test_dead_credential_does_not_heal_itself(seeded_account):
    from engrix_router.accounts import health
    from engrix_router.storage import sqlite as db

    cid, now = seeded_account
    db.execute(
        "INSERT OR REPLACE INTO connection_health (connection_id, test_status, error_class,"
        " error_code, last_error, last_error_at, updated_at, rate_limited_until, backoff_level)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, health.STATUS_NEEDS_REAUTH, errors.CLASS_CREDENTIAL_DEAD, "401",
         "invalid token", now, now, None, 0),
    )

    # A revoked token really does need a re-login: it must stay blocked.
    assert health.is_available(cid, "deepseek-v4.1-flash") is False
    assert health.get(cid)["test_status"] == health.STATUS_NEEDS_REAUTH
