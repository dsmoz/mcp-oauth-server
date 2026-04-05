from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel


class OAuthClient(BaseModel):
    client_id: str
    client_secret_hash: str
    client_name: str
    redirect_uris: List[str] = []
    grant_types: List[str] = ["authorization_code"]
    scope: str = "mcp"
    allowed_mcp_resources: List[str] = []
    created_by: Optional[str] = None
    is_active: bool = True
    credit_balance: float = 0.0
    dcr_fingerprint: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AuthorizationCode(BaseModel):
    code: str
    client_id: str
    redirect_uri: Optional[str] = None
    redirect_uri_provided_explicitly: bool = False
    scopes: List[str] = []
    code_challenge: str
    code_challenge_method: str = "S256"
    resource: Optional[str] = None
    expires_at: int
    created_at: Optional[str] = None


class AccessToken(BaseModel):
    token: str
    client_id: str
    scopes: List[str] = []
    resource: Optional[str] = None
    expires_at: Optional[int] = None
    is_revoked: bool = False
    created_at: Optional[str] = None


class RefreshToken(BaseModel):
    token: str
    client_id: str
    scopes: List[str] = []
    access_token: Optional[str] = None
    expires_at: Optional[int] = None
    is_revoked: bool = False
    created_at: Optional[str] = None


class RegistrationRequest(BaseModel):
    id: str
    company_name: str
    contact_name: str
    contact_email: str
    use_case: str
    redirect_uris_raw: str = ""
    status: str = "pending"
    created_at: Optional[str] = None
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None
