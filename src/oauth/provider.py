from __future__ import annotations

import hashlib
import base64
import json
from typing import Optional

from src.config import get_settings
from src.crypto import generate_token, hash_token, now_unix
from src.db import get_db
from src.models import OAuthClient, AuthorizationCode, AccessToken, RefreshToken

# Named constants for expiry magic numbers
SESSION_EXPIRY_SECONDS = 300   # 5 minutes for pending consent sessions
CODE_EXPIRY_SECONDS = 600      # 10 minutes for issued authorization codes


class SupabaseOAuthProvider:
    def __init__(self):
        self.settings = get_settings()
        self.db = get_db()

    def _single(self, table: str, **filters) -> dict | None:
        try:
            q = self.db.table(table).select("*")
            for k, v in filters.items():
                q = q.eq(k, v)
            result = q.limit(1).execute()
            return result.data[0] if result.data else None
        except Exception:
            return None

    # ── Clients ──────────────────────────────────────────────────────────────

    def get_client(self, client_id: str) -> Optional[OAuthClient]:
        row = self._single("oauth_clients", client_id=client_id)
        if row is None:
            return None
        return OAuthClient(**row)

    # ── Authorization ─────────────────────────────────────────────────────────

    def authorize(
        self,
        client: OAuthClient,
        code_challenge: str,
        code_challenge_method: str,
        redirect_uri: Optional[str],
        scopes: list[str],
        state: Optional[str],
        resource: Optional[str],
    ) -> str:
        """
        Phase 1: store a pending session in oauth_authorization_codes using
        a session_id as the `code` temporarily.  Expires in 5 minutes.
        Returns the consent URL with ?session=<session_id>.
        """
        try:
            session_id = generate_token(24)
            expires_at = now_unix() + SESSION_EXPIRY_SECONDS

            # Store all pending session params as JSON in the resource column.
            # This avoids the fragile "state|||resource" delimiter approach.
            session_data = json.dumps({
                "client_id": client.client_id,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "redirect_uri": redirect_uri,
                "scopes": scopes,
                "state": state,
                "resource": resource,
            })

            row = {
                "code": session_id,
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "redirect_uri_provided_explicitly": redirect_uri is not None,
                "scopes": scopes,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "resource": session_data,
                "expires_at": expires_at,
            }

            self.db.table("oauth_authorization_codes").insert(row).execute()

            issuer = self.settings.OAUTH_ISSUER_URL
            return f"{issuer}/authorize/consent?session={session_id}"
        except Exception as exc:
            raise ValueError("Authorization session could not be created") from exc

    def get_pending_session(self, session_id: str) -> Optional[dict]:
        """Fetch a pending authorization session (phase 1 record)."""
        row = self._single("oauth_authorization_codes", code=session_id)
        if row is None:
            return None
        # Check not expired
        if row["expires_at"] < now_unix():
            return None
        # Parse JSON session data from the resource column
        try:
            session_data = json.loads(row.get("resource") or "{}")
        except (json.JSONDecodeError, TypeError):
            session_data = {}
        row["_state"] = session_data.get("state")
        row["_resource"] = session_data.get("resource")
        return row

    def complete_authorization(
        self, session_id: str, client_id: str
    ) -> tuple[str, Optional[str]]:
        """
        Phase 2: replace the session_id code with a real authorization code.
        Returns (code, redirect_uri).
        """
        try:
            pending = self.get_pending_session(session_id)
            if pending is None:
                raise ValueError("Session not found or expired")
            if pending["client_id"] != client_id:
                raise ValueError("Client mismatch")

            real_code = generate_token(32)
            real_expires = now_unix() + CODE_EXPIRY_SECONDS

            # Extract resource from parsed JSON session data (already decoded in get_pending_session)
            resource = pending.get("_resource")

            self.db.table("oauth_authorization_codes").update(
                {
                    "code": real_code,
                    "expires_at": real_expires,
                    "resource": resource,
                }
            ).eq("code", session_id).execute()

            return real_code, pending.get("redirect_uri")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Authorization could not be completed") from exc

    # ── Token Exchange ────────────────────────────────────────────────────────

    def load_authorization_code(self, code: str) -> Optional[AuthorizationCode]:
        row = self._single("oauth_authorization_codes", code=code)
        if row is None:
            return None
        return AuthorizationCode(**row)

    def _validate_pkce(self, code_verifier: str, code_challenge: str) -> None:
        digest = hashlib.sha256(code_verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != code_challenge:
            raise ValueError("PKCE verification failed")

    def exchange_authorization_code(
        self, code: str, client_id: str, code_verifier: str
    ) -> tuple[str, str, int]:
        """
        Validate code + PKCE, issue access + refresh tokens.
        Returns (access_token, refresh_token, expires_in).
        """
        try:
            auth_code = self.load_authorization_code(code)
            if auth_code is None:
                raise ValueError("Authorization code not found")
            if auth_code.client_id != client_id:
                raise ValueError("Client ID mismatch")
            if auth_code.expires_at < now_unix():
                raise ValueError("Authorization code expired")

            self._validate_pkce(code_verifier, auth_code.code_challenge)

            # Delete the code atomically and verify it was actually deleted
            delete_result = self.db.table("oauth_authorization_codes").delete().eq("code", code).execute()
            if not delete_result.data:
                # Code was already used (concurrent request beat us)
                raise ValueError("Authorization code already used or expired")

            # Issue tokens — if inserts fail after deletion, caller gets a clear error
            access_token = generate_token(32)
            refresh_token = generate_token(32)
            ttl = self.settings.ACCESS_TOKEN_TTL
            refresh_ttl = self.settings.REFRESH_TOKEN_TTL
            at_expires = now_unix() + ttl
            rt_expires = now_unix() + refresh_ttl

            try:
                self.db.table("oauth_access_tokens").insert(
                    {
                        "token": hash_token(access_token),
                        "client_id": client_id,
                        "scopes": auth_code.scopes,
                        "resource": auth_code.resource,
                        "expires_at": at_expires,
                        "is_revoked": False,
                    }
                ).execute()

                self.db.table("oauth_refresh_tokens").insert(
                    {
                        "token": hash_token(refresh_token),
                        "client_id": client_id,
                        "scopes": auth_code.scopes,
                        "access_token": hash_token(access_token),
                        "expires_at": rt_expires,
                        "is_revoked": False,
                    }
                ).execute()
            except Exception as exc:
                raise ValueError("Token issuance failed — please re-authorize") from exc

            return access_token, refresh_token, ttl
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Authorization code exchange failed") from exc

    def load_access_token(self, token: str) -> Optional[AccessToken]:
        row = self._single("oauth_access_tokens", token=hash_token(token))
        if row is None:
            return None
        at = AccessToken(**row)
        # Return the raw presented token to the caller, not the hash
        at.token = token
        return at

    def load_refresh_token(self, token: str) -> Optional[RefreshToken]:
        row = self._single("oauth_refresh_tokens", token=hash_token(token))
        if row is None:
            return None
        rt = RefreshToken(**row)
        # Return the raw presented token to the caller, not the hash
        rt.token = token
        return rt

    def exchange_refresh_token(
        self, refresh_token_str: str, client_id: str
    ) -> tuple[str, str, int]:
        """
        Rotate refresh token: revoke old, issue new access + refresh tokens.
        Returns (access_token, refresh_token, expires_in).
        """
        try:
            rt = self.load_refresh_token(refresh_token_str)
            if rt is None:
                raise ValueError("Refresh token not found")
            if rt.client_id != client_id:
                raise ValueError("Client ID mismatch")
            if rt.is_revoked:
                raise ValueError("Refresh token revoked")
            if rt.expires_at and rt.expires_at < now_unix():
                raise ValueError("Refresh token expired")

            hashed_rt = hash_token(refresh_token_str)

            # Revoke old tokens (use hashes for DB lookup)
            self.db.table("oauth_refresh_tokens").update({"is_revoked": True}).eq(
                "token", hashed_rt
            ).execute()
            if rt.access_token:
                # rt.access_token stored in DB is already hashed; use it directly
                self.db.table("oauth_access_tokens").update({"is_revoked": True}).eq(
                    "token", rt.access_token
                ).execute()

            # Issue new tokens
            new_access = generate_token(32)
            new_refresh = generate_token(32)
            ttl = self.settings.ACCESS_TOKEN_TTL
            refresh_ttl = self.settings.REFRESH_TOKEN_TTL
            at_expires = now_unix() + ttl
            rt_expires = now_unix() + refresh_ttl

            self.db.table("oauth_access_tokens").insert(
                {
                    "token": hash_token(new_access),
                    "client_id": client_id,
                    "scopes": rt.scopes,
                    "resource": None,
                    "expires_at": at_expires,
                    "is_revoked": False,
                }
            ).execute()

            self.db.table("oauth_refresh_tokens").insert(
                {
                    "token": hash_token(new_refresh),
                    "client_id": client_id,
                    "scopes": rt.scopes,
                    "access_token": hash_token(new_access),
                    "expires_at": rt_expires,
                    "is_revoked": False,
                }
            ).execute()

            return new_access, new_refresh, ttl
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Refresh token exchange failed") from exc

    def revoke_token(self, token: str) -> None:
        """Revoke an access token and its linked refresh token, or a refresh token directly (RFC 7009)."""
        try:
            from src.crypto import hash_token as _hash_token
            hashed = _hash_token(token)

            # Try to revoke as access token first
            at_result = self.db.table("oauth_access_tokens").update({"is_revoked": True}).eq(
                "token", hashed
            ).execute()

            if at_result.data:
                # Also revoke any linked refresh token
                self.db.table("oauth_refresh_tokens").update({"is_revoked": True}).eq(
                    "access_token", hashed
                ).execute()
            else:
                # Try as refresh token directly (RFC 7009 allows clients to revoke refresh tokens)
                self.db.table("oauth_refresh_tokens").update({"is_revoked": True}).eq(
                    "token", hashed
                ).execute()
        except Exception as exc:
            raise ValueError("Token revocation failed") from exc

    def revoke_client_tokens(self, client_id: str) -> None:
        """Revoke ALL tokens for a client."""
        try:
            self.db.table("oauth_access_tokens").update({"is_revoked": True}).eq(
                "client_id", client_id
            ).execute()
            self.db.table("oauth_refresh_tokens").update({"is_revoked": True}).eq(
                "client_id", client_id
            ).execute()
        except Exception as exc:
            raise ValueError("Client token revocation failed") from exc
