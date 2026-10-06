"""Agent downloads (section 5.4): no auth, tailnet only, like /dl. 503 while fleetagent/ is absent."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from host.api.deps import get_config
from host.config import Config
from host.workloads import agent_bundle

router = APIRouter(tags=["dl"])
PLACEHOLDER = "__FLEET_HOST_URL__"
MISSING = "the fleetagent package is not available on this host"


@router.get("/dl/agent/version", response_model=None)
def agent_version() -> Any:
    """Identity of the agent tarball."""
    bundle = agent_bundle.get_bundle()
    if bundle is None:
        return JSONResponse({"detail": MISSING}, status_code=503)
    return {"agent_version": bundle.code_version, "sha256": bundle.sha256}


@router.get("/dl/agent.tar.gz", response_model=None)
def agent_tarball() -> Response:
    """The in-memory fleetagent tarball."""
    bundle = agent_bundle.get_bundle()
    if bundle is None:
        return JSONResponse({"detail": MISSING}, status_code=503)
    return Response(
        content=bundle.data,
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="agent.tar.gz"',
            "X-Agent-Version": bundle.code_version,
            "X-Content-SHA256": bundle.sha256,
        },
    )


@router.get("/install-agent.sh", response_model=None)
def install_agent_script(config: Config = Depends(get_config)) -> Response:
    """deploy/install_agent.sh with the host URL substituted."""
    path = config.deploy_dir / "install_agent.sh"
    if not path.is_file():
        return PlainTextResponse(
            f"install_agent.sh is not available on this host (expected at {path})\n", status_code=503
        )
    text = path.read_text(encoding="utf-8").replace(PLACEHOLDER, config.public_url)
    return PlainTextResponse(text, media_type="text/x-shellscript")
