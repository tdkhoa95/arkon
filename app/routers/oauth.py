"""
OAuth 2.1 router — Authorization Code + PKCE flow for Claude Desktop MCP.

Endpoints:
  GET  /.well-known/oauth-authorization-server  — server metadata (RFC 8414)
  POST /oauth/register                           — dynamic client registration (RFC 7591)
  GET  /oauth/authorize                          — show login form
  POST /oauth/authorize                          — submit credentials, issue code
  POST /oauth/token                              — exchange code for MCP token
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import authenticate_employee
from app.services.oauth_service import OAuthService

# Two routers: one mounts at root (for .well-known), one at /oauth
wellknown_router = APIRouter()
router = APIRouter()


# ---------------------------------------------------------------------------
# OAuth server metadata (RFC 8414)
# ---------------------------------------------------------------------------

@wellknown_router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------

@router.post("/register", status_code=201)
async def register_client(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    name = body.get("client_name", "Claude Desktop")
    redirect_uris = body.get("redirect_uris", [])

    if not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris required")

    svc = OAuthService(db)
    client = await svc.register_client(name, redirect_uris)
    await db.commit()

    base = str(request.base_url).rstrip("/")
    return {
        "client_id": client.client_id,
        "client_name": client.name,
        "redirect_uris": client.redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "registration_client_uri": f"{base}/oauth/register/{client.client_id}",
    }


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------

def _login_form(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    error: str = "",
) -> str:
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Arkon — Sign in</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #0f1117;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      color: #e2e8f0;
    }}
    .card {{
      width: 100%;
      max-width: 380px;
      background: #1a1d27;
      border: 1px solid #2d3148;
      border-radius: 12px;
      padding: 40px 36px;
    }}
    .logo {{
      font-size: 22px;
      font-weight: 700;
      color: #818cf8;
      margin-bottom: 6px;
    }}
    .subtitle {{
      font-size: 13px;
      color: #64748b;
      margin-bottom: 28px;
    }}
    label {{
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: #94a3b8;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    input[type=email], input[type=password] {{
      width: 100%;
      padding: 10px 14px;
      background: #0f1117;
      border: 1px solid #2d3148;
      border-radius: 8px;
      color: #e2e8f0;
      font-size: 14px;
      margin-bottom: 16px;
      outline: none;
      transition: border-color 0.15s;
    }}
    input:focus {{ border-color: #818cf8; }}
    .error {{
      background: #3b1219;
      border: 1px solid #7f1d1d;
      color: #fca5a5;
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 13px;
      margin-bottom: 16px;
    }}
    button {{
      width: 100%;
      padding: 11px;
      background: #818cf8;
      border: none;
      border-radius: 8px;
      color: #fff;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }}
    button:hover {{ background: #6366f1; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">Arkon</div>
    <div class="subtitle">Sign in to connect Claude Desktop</div>
    {error_html}
    <form method="post">
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <label for="email">Email</label>
      <input id="email" type="email" name="email" required autofocus>
      <label for="password">Password</label>
      <input id="password" type="password" name="password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""


@router.get("/authorize", response_class=HTMLResponse)
async def authorize_get(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    state: str = "",
    db: AsyncSession = Depends(get_db),
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported_response_type")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="unsupported_code_challenge_method")

    svc = OAuthService(db)
    client = await svc.get_client(client_id)
    if not client:
        raise HTTPException(status_code=400, detail="invalid_client")
    if redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")

    return _login_form(client_id, redirect_uri, state, code_challenge, code_challenge_method)


@router.post("/authorize", response_class=HTMLResponse)
async def authorize_post(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    svc = OAuthService(db)
    client = await svc.get_client(client_id)
    if not client or redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=400, detail="invalid_client")

    employee = await authenticate_employee(db, email, password)
    if not employee:
        return HTMLResponse(
            content=_login_form(
                client_id, redirect_uri, state, code_challenge, code_challenge_method,
                error="Invalid email or password.",
            ),
            status_code=401,
        )

    code = await svc.create_auth_code(
        client_id=client_id,
        employee_id=employee.id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    await db.commit()

    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(
        url=f"{redirect_uri}?{urlencode(params)}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@router.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    code_verifier: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    svc = OAuthService(db)
    access_token = await svc.exchange_code(
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )

    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
    })
