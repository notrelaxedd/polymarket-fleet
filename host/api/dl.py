"""Download routes: worker tarball, its version, and the install script."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse

from host.api.deps import get_config
from host.config import Config

router = APIRouter(tags=["dl"])
PLACEHOLDER = "__FLEET_HOST_URL__"


@router.get("/dl/version")
def version(request: Request) -> dict[str, Any]:
    """Identity of the tarball built at app start."""
    bundle = request.app.state.bundle
    return {"code_version": bundle.code_version, "sha256": bundle.sha256}


@router.get("/dl/worker.tar.gz")
def tarball(request: Request) -> Response:
    """The in-memory worker tarball."""
    bundle = request.app.state.bundle
    return Response(
        content=bundle.data,
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="worker.tar.gz"',
            "X-Code-Version": bundle.code_version,
            "X-Content-SHA256": bundle.sha256,
        },
    )


@router.get("/install.sh")
def install_script(config: Config = Depends(get_config)) -> Response:
    """deploy/install_worker.sh with the host URL substituted."""
    path = config.deploy_dir / "install_worker.sh"
    if not path.is_file():
        return PlainTextResponse(
            f"install_worker.sh is not available on this host (expected at {path})\n", status_code=503
        )
    text = path.read_text(encoding="utf-8").replace(PLACEHOLDER, config.public_url)
    return PlainTextResponse(text, media_type="text/x-shellscript")
